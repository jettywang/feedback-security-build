from __future__ import annotations


__lazy_modules__ = {'importlib.metadata', 'packaging.requirements', 'packaging.utils'}

import importlib.metadata
import sys

import packaging.requirements
import packaging.utils


TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from typing import TypedDict

    class RequirementInfo(TypedDict):
        """Machine-readable description of a single requirement string."""

        requirement: str
        name: str | None
        valid: bool
        error: str | None

    class DuplicateRequirements(TypedDict):
        """Requirements for the same package requested more than once across audit stages."""

        name: str
        occurrences: dict[str, list[str]]


def check_dependency(
    req_string: str, ancestral_req_strings: tuple[str, ...] = (), parent_extras: AbstractSet[str] = frozenset()
) -> Iterator[tuple[str, ...]]:
    """
    Verify that a dependency and all of its dependencies are met.

    :param req_string: Requirement string
    :param parent_extras: Extras (eg. "test" in myproject[test])
    :yields: Unmet dependencies
    """
    yield from _check_dependency(req_string, ancestral_req_strings, parent_extras, set())


def _check_dependency(
    req_string: str, ancestral_req_strings: tuple[str, ...], parent_extras: AbstractSet[str], seen: set[str]
) -> Generator[tuple[str, ...], None, bool]:
    req = packaging.requirements.Requirement(req_string)
    normalised_req_string = str(req)

    # ``seen`` holds requirements already verified as fully satisfied (nothing
    # yielded), so shared subtrees of a diamond dependency graph are walked once.
    if normalised_req_string in seen:
        return True

    # ``Requirement`` doesn't implement ``__eq__`` so we cannot compare reqs for
    # equality directly but the string representation is stable.
    if normalised_req_string in ancestral_req_strings:
        # A cycle is skipped, but its ancestors may still have unmet dependencies.
        return False

    if req.marker:
        extras = frozenset(('',)).union(parent_extras)
        # a requirement can have multiple extras but ``evaluate`` can
        # only check one at a time.
        if all(not req.marker.evaluate(environment={'extra': e}) for e in extras):
            # if the marker conditions are not met, we pretend that the
            # dependency is satisfied.
            return True

    try:
        dist = importlib.metadata.distribution(req.name)
    except importlib.metadata.PackageNotFoundError:
        # dependency is not installed in the environment.
        yield (*ancestral_req_strings, normalised_req_string)
        return False

    if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
        # the installed version is incompatible.
        yield (*ancestral_req_strings, normalised_req_string)
        return False

    satisfied = True
    for other_req_string in dist.requires or ():
        # yields transitive dependencies that are not satisfied.
        if not (
            yield from _check_dependency(other_req_string, (*ancestral_req_strings, normalised_req_string), req.extras, seen)
        ):
            satisfied = False

    # unmet requirements are not memoised: they must be reported once per
    # dependency chain that reaches them.
    if satisfied:
        seen.add(normalised_req_string)
    return satisfied


def inspect_requirement(req_string: str) -> RequirementInfo:
    """
    Describe a requirement string for auditing purposes, capturing parse
    failures instead of raising them.

    :param req_string: Requirement string
    :returns: Mapping with the original string, the canonicalized package name
        (``None`` when the string cannot be parsed), a validity flag and the
        parse error message (``None`` when valid)
    """
    try:
        req = packaging.requirements.Requirement(req_string)
    except packaging.requirements.InvalidRequirement as e:
        return {'requirement': req_string, 'name': None, 'valid': False, 'error': str(e)}
    return {
        'requirement': req_string,
        'name': str(packaging.utils.canonicalize_name(req.name)),
        'valid': True,
        'error': None,
    }


def find_duplicate_requirements(stage_requirements: Mapping[str, Sequence[RequirementInfo]]) -> list[DuplicateRequirements]:
    """
    Find packages whose requirements appear more than once across audit stages.

    :param stage_requirements: Mapping of stage name to the stage's inspected requirements
    :returns: One entry per package requested more than once in total, listing the
        requirement strings per stage; unparseable requirements are ignored
    """
    by_name: dict[str, dict[str, list[str]]] = {}
    for stage, entries in stage_requirements.items():
        for entry in entries:
            if entry['name'] is not None:
                by_name.setdefault(entry['name'], {}).setdefault(stage, []).append(entry['requirement'])
    return [
        {'name': name, 'occurrences': occurrences}
        for name, occurrences in sorted(by_name.items())
        if sum(len(requirements) for requirements in occurrences.values()) > 1
    ]


def format_unmet_dependencies(unmet: AbstractSet[tuple[str, ...]]) -> str:
    body = ''.join(_format_missing_dependency(chain) for chain in sorted(unmet))
    return f'Unmet dependencies (checked against {sys.executable}):{body}'


def _format_missing_dependency(dep_chain: tuple[str, ...]) -> str:
    requirement = packaging.requirements.Requirement(dep_chain[-1])
    try:
        found = importlib.metadata.distribution(requirement.name).version
    except importlib.metadata.PackageNotFoundError:
        found = 'not installed'
    wanted = str(requirement.specifier) if requirement.specifier else 'any'
    return f'\n\t{_format_dep_chain(dep_chain)}\n\t\twanted: {wanted}\n\t\tfound: {found}'


def _format_dep_chain(dep_chain: tuple[str, ...]) -> str:
    return ' -> '.join(dep.partition(';')[0].strip() for dep in dep_chain)
