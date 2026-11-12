"""Local, dependency-free demonstration of the pip -> uv migration precheck.

The script never creates a real virtual environment and never installs a package. Both installers are replaced
by in-process fakes that reuse the exact command builders from :mod:`build.env`, so the recorded command lines are
the ones a real run would receive. Run it with::

    .venv/bin/python tasks/migration_precheck_demo.py
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import textwrap
from collections.abc import Collection, Mapping
from pathlib import Path

from build import env as build_env


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PACKAGE = REPO_ROOT / 'tests' / 'packages' / 'test-flit'
# PEP 517 requirements declared by the local sample package's pyproject.toml.
SAMPLE_REQUIREMENTS: tuple[str, ...] = ('flit_core >=2,<4',)


@dataclasses.dataclass
class RecordedCall:
    """One interaction with a fake installer."""

    phase: str
    command: list[str] | None = None


@dataclasses.dataclass
class FakeBackend:
    """Stand-in for a real installer backend: records phases instead of spawning processes."""

    name: str
    executable: str
    calls: list[RecordedCall]

    def create(self, path: str) -> None:
        self.calls.append(RecordedCall(phase='create', command=[self.name, 'venv', path]))

    def install_dependencies(
        self,
        requirements: Collection[str],
        constraints_txt_path: str | None,
        *,
        requirement_file: str | None,
        verbosity: int = 0,
    ) -> None:
        if self.name == 'pip':
            assert requirement_file is not None
            command = build_env._pip_install_command(
                self.executable,
                constraints_txt_path,
                requirement_file,
                use_outer_pip=False,
                fresh=True,
                verbosity=verbosity,
            )
        else:
            command = build_env._uv_install_command(
                'uv',
                self.executable,
                requirements,
                constraints_txt_path,
                verbosity=verbosity,
            )
        self.calls.append(RecordedCall(phase='install', command=command))


@dataclasses.dataclass
class DemoResult:
    """Outcome of one simulated build for one installer."""

    installer: str
    report: build_env.InstallerPrecheckReport
    calls: list[RecordedCall]
    failure_phase: str | None
    error: str | None


def simulate_build(
    installer: build_env.Installer,
    environ: Mapping[str, str],
    constraints_txt_path: str | None,
    requirements: Collection[str] = SAMPLE_REQUIREMENTS,
) -> DemoResult:
    """
    Simulate the isolated-environment half of a build against a fake installer.

    The phases mirror :meth:`build.env.DefaultIsolatedEnv.__enter__` and its ``install`` calls:
    precheck -> environment creation -> dependency installation.
    """
    calls: list[RecordedCall] = []
    report = build_env.installer_precheck(
        installer, environ=environ, constraints_txt_path=constraints_txt_path
    )
    calls.append(RecordedCall(phase='precheck'))

    failure_phase: str | None = None
    error: str | None = None
    requirement_file: str | None = None
    try:
        report.raise_if_unsupported()

        backend = FakeBackend(installer, executable=f'/fake-{installer}-venv/bin/python', calls=calls)
        backend.create('/fake-build-env')

        if installer == 'pip':
            # Mirror _PipBackend: pip receives requirements through a temporary requirements file.
            with tempfile.NamedTemporaryFile('w', prefix='demo-requirements-', suffix='.txt', delete=False) as handle:
                handle.write('\n'.join(requirements))
                requirement_file = handle.name
        backend.install_dependencies(
            requirements,
            constraints_txt_path,
            requirement_file=requirement_file,
        )
    except build_env.InstallerPrecheckError as exc:
        failure_phase = 'precheck'
        error = str(exc)
    finally:
        if requirement_file is not None:
            Path(requirement_file).unlink(missing_ok=True)

    return DemoResult(installer, report, calls, failure_phase, error)


def _write_constraints(directory: Path) -> str:
    path = directory / 'constraints.txt'
    path.write_text('flit_core<4\n', encoding='utf-8')
    return str(path)


def _print_result(result: DemoResult) -> None:
    print(f'  installer: {result.installer}')
    for line in result.report.format().splitlines():
        print(f'    {line}')
    for call in result.calls:
        if call.command is None:
            print(f'    phase[{call.phase}]')
        else:
            print(f'    phase[{call.phase}] $ {" ".join(call.command)}')
    if result.error is not None:
        print(f'    FAILED at phase: {result.failure_phase}')
        for line in textwrap.indent(result.error, '      ').splitlines():
            print(line)
    else:
        print('    result: all phases completed (fake install, nothing downloaded)')


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix='build-migration-demo-'))
    try:
        constraints_path = _write_constraints(workdir)
        print(f'using local sample package: {SAMPLE_PACKAGE}')
        print(f'using local constraints file: {constraints_path}')
        print(f'requirements: {list(SAMPLE_REQUIREMENTS)}')

        print('\n=== Scenario 1: standard, unrestricted build ===')
        clean_environ: dict[str, str] = {'PATH': '/usr/bin'}
        for installer in ('pip', 'uv'):
            _print_result(simulate_build(installer, clean_environ, constraints_path))

        print('\n=== Scenario 2: restricted corporate environment (pip-era settings) ===')
        restricted_environ = {
            'PATH': '/usr/bin',
            'PIP_INDEX_URL': 'https://ci-user:top-secret@pypi.company.com/simple',
            'PIP_EXTRA_INDEX_URL': 'https://pypi.org/simple',
            'PIP_TRUSTED_HOST': 'pypi.company.com',
            'PIP_CERT': '/etc/ssl/company-ca.pem',
            'PIP_CONSTRAINT': '/etc/build/pip-constraints.txt',
            'HTTPS_PROXY': 'http://proxy.company.com:8080',
        }
        for installer in ('pip', 'uv'):
            _print_result(simulate_build(installer, restricted_environ, constraints_path))

        print('\n=== Scenario 3: same restrictions after migrating to UV_* variables ===')
        migrated_environ = {
            'PATH': '/usr/bin',
            'UV_INDEX_URL': 'https://ci-user:top-secret@pypi.company.com/simple',
            'UV_EXTRA_INDEX_URL': 'https://pypi.org/simple',
            'UV_CONSTRAINT': '/etc/build/uv-constraints.txt',
            'UV_NATIVE_TLS': '1',
            'HTTPS_PROXY': 'http://proxy.company.com:8080',
        }
        _print_result(simulate_build('uv', migrated_environ, constraints_path))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    main()
