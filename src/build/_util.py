from __future__ import annotations


__lazy_modules__ = {'dataclasses', 'importlib.metadata', 'packaging.requirements', 'packaging.utils'}

import dataclasses
import importlib.metadata
import sys

import packaging.requirements
import packaging.utils


TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence
    from collections.abc import Set as AbstractSet


@dataclasses.dataclass(frozen=True)
class UnmetDependency:
    """
    A requirement that could not be satisfied, together with every dependency
    chain that reaches it.

    :param requirement: The normalized requirement string of the unsatisfied leaf
    :param chains: Distinct source chains, each ending in ``requirement``
    """

    requirement: str
    chains: tuple[tuple[str, ...], ...]


def _canonical_requirement_string(req_string: str) -> str:
    """Return the requirement string with a PEP 503 canonicalized package name."""
    req = packaging.requirements.Requirement(req_string)
    req.name = packaging.utils.canonicalize_name(req.name)
    return str(req)


def _requirement_identity(req_string: str) -> tuple[str, ...]:
    """
    A case-insensitive identity for a requirement string.

    Package names are compared by their PEP 503 normalized form so that
    differently cased spellings of the same package (``Foo``, ``foo``,
    ``FOO``) collapse, while different specifiers, extras or markers stay
    distinct requirements.
    """
    req = packaging.requirements.Requirement(req_string)
    return (
        packaging.utils.canonicalize_name(req.name),
        tuple(sorted(packaging.utils.canonicalize_name(extra) for extra in req.extras)),
        str(req.specifier),
        str(req.marker) if req.marker is not None else '',
    )


def check_dependency(
    req_string: str, ancestral_req_strings: tuple[str, ...] = (), parent_extras: AbstractSet[str] = frozenset()
) -> Iterator[tuple[str, ...]]:
    """
    Verify that a dependency and all of its dependencies are met.

    :param req_string: Requirement string
    :param parent_extras: Extras (eg. "test" in myproject[test])
    :yields: Unmet dependency chains, each a tuple of requirement strings from
        the checked requirement down to the unsatisfied requirement
    """
    yield from _check_dependency(req_string, ancestral_req_strings, (), frozenset(parent_extras), set())


def _check_dependency(
    req_string: str,
    ancestral_req_strings: tuple[str, ...],
    ancestral_identities: tuple[tuple[str, ...], ...],
    parent_extras: AbstractSet[str],
    seen: set[tuple[str, ...]],
) -> Generator[tuple[str, ...], None, bool]:
    req = packaging.requirements.Requirement(req_string)
    normalised_req_string = str(req)
    identity = _requirement_identity(normalised_req_string)

    # ``seen`` holds requirements already verified as fully satisfied (nothing
    # yielded), so shared subtrees of a diamond dependency graph are walked once.
    # Identity is case-insensitive, so differently cased spellings of the same
    # package share a memo entry.
    if identity in seen:
        return True

    # ``Requirement`` doesn't implement ``__eq__`` so we cannot compare reqs for
    # equality directly; identities compare the normalized package name instead.
    if identity in ancestral_identities:
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
            yield from _check_dependency(
                other_req_string,
                (*ancestral_req_strings, normalised_req_string),
                (*ancestral_identities, identity),
                req.extras,
                seen,
            )
        ):
            satisfied = False

    # unmet requirements are not memoised: they must be reported once per
    # dependency chain that reaches them.
    if satisfied:
        seen.add(identity)
    return satisfied


def collect_unmet_dependencies(chains: Sequence[tuple[str, ...]]) -> tuple[UnmetDependency, ...]:
    """
    Merge raw unmet-dependency chains into one record per unsatisfied requirement.

    The same requirement can be reached through several parent dependencies, and
    package names may be spelled with different casing along those paths. Chains
    ending at the same requirement are grouped, and duplicate source chains are
    removed. Aggregation is kept separate from formatting so that callers can
    consume the structured records without rendering them for humans.

    :param chains: Unmet dependency chains as produced by :func:`check_dependency`
    :returns: Records sorted by their unsatisfied requirement string
    """
    grouped: dict[tuple[str, ...], UnmetDependency] = {}
    for chain in chains:
        requirement = chain[-1]
        key = _requirement_identity(requirement)
        record = grouped.get(key)
        if record is None:
            grouped[key] = UnmetDependency(_canonical_requirement_string(requirement), (chain,))
        else:
            unique_chains = tuple(dict.fromkeys((*record.chains, chain)))
            # The leaf keeps a single canonical spelling even when its name was
            # declared with different casing in different places; the per-chain
            # entries below preserve each original declaration.
            grouped[key] = dataclasses.replace(record, chains=unique_chains)
    return tuple(sorted(grouped.values(), key=lambda record: record.requirement))


def format_unmet_dependencies(unmet: Sequence[UnmetDependency]) -> str:
    body = ''.join(_format_missing_dependency(record) for record in unmet)
    return f'Unmet dependencies (checked against {sys.executable}):{body}'


def _format_missing_dependency(unmet: UnmetDependency) -> str:
    requirement = packaging.requirements.Requirement(unmet.requirement)
    try:
        found = importlib.metadata.distribution(requirement.name).version
    except importlib.metadata.PackageNotFoundError:
        found = 'not installed'
    wanted = str(requirement.specifier) if requirement.specifier else 'any'

    if len(unmet.chains) == 1:
        chain = _format_dep_chain(unmet.chains[0])
    else:
        # The leaf is shown once; every source chain that pulls it in is listed
        # in full so the original (possibly differently cased) declarations are
        # traceable back to the root requirements.
        sources = '\n'.join(f'\t\trequired via: {_format_dep_chain(chain)}' for chain in unmet.chains)
        chain = f'{_format_dep_chain((unmet.requirement,))}\n{sources}'
    return f'\n\t{chain}\n\t\twanted: {wanted}\n\t\tfound: {found}'


def _format_dep_chain(dep_chain: Sequence[str]) -> str:
    return ' -> '.join(dep.partition(';')[0].strip() for dep in dep_chain)
