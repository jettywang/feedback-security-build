# SPDX-License-Identifier: MIT

"""
Lifecycle tests for the isolated build environment.

These tests exercise the real :class:`build.env.DefaultIsolatedEnv` context
manager and the real CLI entry point with a fake environment backend, and
assert from on-disk state (plus the surfaced errors) that:

- temporary environments are always removed, even when creation, dependency
  installation, or the build itself fails;
- a caller-requested environment (``--env-dir``) is kept on failure so the
  on-call engineer can inspect it, and removed on success so the location
  can be reused.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import typing

import pytest
import pytest_mock

import build.__main__
import build.env

from build._exceptions import BuildBackendException, FailedProcessError


if typing.TYPE_CHECKING:
    from collections.abc import Sequence


pytestmark = pytest.mark.contextvars

MARKER_FILE = 'env-marker.txt'
MARKER_CONTENTS = 'partial environment, kept for inspection'


class FakeEnvBackend:
    """
    Stand-in for the real environment backends with injectable failures.

    ``create`` leaves a marker file behind so tests can tell a kept
    environment apart from a removed (or never populated) one. ``failures``
    selects which stages raise: ``'create'`` and/or ``'install'``.
    """

    def __init__(self, created_paths: list[str], failures: set[str]) -> None:
        self._created_paths = created_paths
        self._failures = failures

    def create(self, path: str) -> None:
        self._created_paths.append(path)
        self.python_executable = os.path.join(path, 'bin', 'python')
        self.scripts_dir = os.path.join(path, 'bin')
        self.purelib = os.path.join(path, 'lib', 'site-packages')
        # A real backend leaves a partially created environment on disk when
        # it fails; the marker doubles as the diagnostic content to inspect.
        pathlib.Path(path, MARKER_FILE).write_text(MARKER_CONTENTS, encoding='utf-8')
        if 'create' in self._failures:
            raise FailedProcessError(
                subprocess.CalledProcessError(2, ['python', '-m', 'venv', path]),
                'Failed to create venv',
            )

    def install_dependencies(
        self,
        requirements: typing.Collection[str],
        constraints_txt_path: os.PathLike[str] | str | None,
        *,
        _fresh: bool = False,
    ) -> None:
        if 'install' in self._failures:
            raise FailedProcessError(
                subprocess.CalledProcessError(1, ['pip', 'install', *sorted(requirements)]),
                'Failed to install packages in isolated environment',
            )

    @property
    def display_name(self) -> str:
        return 'fake'


class FakeEnvControl(typing.NamedTuple):
    """Handle returned by the ``fake_env_backend`` fixture."""

    created_paths: list[str]  # environment paths the backend was asked to create, in order
    failures: set[str]  # stages that should fail: 'create' and/or 'install'


@pytest.fixture
def fake_env_backend(monkeypatch: pytest.MonkeyPatch) -> FakeEnvControl:
    """Replace both environment backends with :class:`FakeEnvBackend`."""
    control = FakeEnvControl([], set())

    def factory() -> FakeEnvBackend:
        return FakeEnvBackend(control.created_paths, control.failures)

    monkeypatch.setattr(build.env, '_PipBackend', factory)
    monkeypatch.setattr(build.env, '_UvBackend', factory)
    return control


def assert_env_removed(path: str) -> None:
    assert not os.path.exists(path), f'environment should have been cleaned up: {path}'


def assert_env_kept(path: str) -> None:
    assert os.path.isdir(path), f'environment should have been kept for inspection: {path}'
    marker = pathlib.Path(path, MARKER_FILE)
    assert marker.read_text(encoding='utf-8') == MARKER_CONTENTS


@pytest.mark.parametrize('requested', [False, True], ids=['temporary', 'requested'])
def test_context_manager_creation_failure(
    fake_env_backend: FakeEnvControl,
    tmp_path: pathlib.Path,
    requested: bool,
) -> None:
    env_dir = str(tmp_path / 'env') if requested else None
    fake_env_backend.failures.add('create')

    with pytest.raises(FailedProcessError, match='Failed to create venv'), build.env.DefaultIsolatedEnv(path=env_dir):
        raise AssertionError('unreachable')

    (assert_env_kept if requested else assert_env_removed)(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('requested', [False, True], ids=['temporary', 'requested'])
def test_context_manager_install_failure(
    fake_env_backend: FakeEnvControl,
    tmp_path: pathlib.Path,
    requested: bool,
) -> None:
    env_dir = str(tmp_path / 'env') if requested else None
    fake_env_backend.failures.add('install')

    with (
        pytest.raises(FailedProcessError, match='Failed to install'),
        build.env.DefaultIsolatedEnv(path=env_dir) as env,
    ):
        env.install({'flit_core >=2,<4'})

    (assert_env_kept if requested else assert_env_removed)(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('requested', [False, True], ids=['temporary', 'requested'])
def test_context_manager_build_failure(
    fake_env_backend: FakeEnvControl,
    tmp_path: pathlib.Path,
    requested: bool,
) -> None:
    env_dir = str(tmp_path / 'env') if requested else None
    backend_error = BuildBackendException(RuntimeError('boom'))

    with pytest.raises(BuildBackendException), build.env.DefaultIsolatedEnv(path=env_dir) as env:
        env.install({'flit_core >=2,<4'})
        raise backend_error

    (assert_env_kept if requested else assert_env_removed)(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('requested', [False, True], ids=['temporary', 'requested'])
def test_context_manager_success(
    fake_env_backend: FakeEnvControl,
    tmp_path: pathlib.Path,
    requested: bool,
) -> None:
    env_dir = str(tmp_path / 'env') if requested else None

    with build.env.DefaultIsolatedEnv(path=env_dir) as env:
        env.install({'flit_core >=2,<4'})
        assert os.path.isdir(env.path)

    # Both locations are removed on success: a temporary one is worthless
    # afterwards, and a requested one must be reusable by the next build.
    assert_env_removed(fake_env_backend.created_paths[0])


def run_cli(
    mocker: pytest_mock.MockerFixture,
    srcdir: str,
    outdir: str,
    extra_args: Sequence[str],
    *,
    build_error: Exception | None = None,
) -> None:
    mocker.patch('build.ProjectBuilder.get_requires_for_build', return_value=set())
    artifact = os.path.join(outdir, 'test_flit-1.0.0-py3-none-any.whl')
    build_mock = mocker.patch('build.ProjectBuilder.build', return_value=artifact)
    if build_error is not None:
        build_mock.side_effect = build_error
    build.__main__.main([srcdir, '--wheel', '--outdir', outdir, *extra_args])


@pytest.mark.parametrize('use_env_dir', [False, True], ids=['temporary', 'env-dir'])
def test_cli_creation_failure(
    fake_env_backend: FakeEnvControl,
    mocker: pytest_mock.MockerFixture,
    package_test_flit: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    use_env_dir: bool,
) -> None:
    env_dir = tmp_path / 'kept-env'
    fake_env_backend.failures.add('create')

    with pytest.raises(SystemExit) as exc_info:
        run_cli(mocker, package_test_flit, str(tmp_path / 'dist'), ['--env-dir', str(env_dir)] if use_env_dir else [])

    assert exc_info.value.code == 1
    assert 'Failed to create venv' in capsys.readouterr().err
    (assert_env_kept if use_env_dir else assert_env_removed)(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('use_env_dir', [False, True], ids=['temporary', 'env-dir'])
def test_cli_install_failure(
    fake_env_backend: FakeEnvControl,
    mocker: pytest_mock.MockerFixture,
    package_test_flit: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    use_env_dir: bool,
) -> None:
    env_dir = tmp_path / 'kept-env'
    fake_env_backend.failures.add('install')

    with pytest.raises(SystemExit) as exc_info:
        run_cli(mocker, package_test_flit, str(tmp_path / 'dist'), ['--env-dir', str(env_dir)] if use_env_dir else [])

    assert exc_info.value.code == 1
    assert 'Failed to install packages in isolated environment' in capsys.readouterr().err
    (assert_env_kept if use_env_dir else assert_env_removed)(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('use_env_dir', [False, True], ids=['temporary', 'env-dir'])
def test_cli_build_failure(
    fake_env_backend: FakeEnvControl,
    mocker: pytest_mock.MockerFixture,
    package_test_flit: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    use_env_dir: bool,
) -> None:
    env_dir = tmp_path / 'kept-env'
    error = BuildBackendException(RuntimeError('boom'), "Backend 'flit_core.buildapi' exploded")

    with pytest.raises(SystemExit) as exc_info:
        run_cli(
            mocker,
            package_test_flit,
            str(tmp_path / 'dist'),
            ['--env-dir', str(env_dir)] if use_env_dir else [],
            build_error=error,
        )

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "Backend 'flit_core.buildapi' exploded" in stderr
    if use_env_dir:
        # The hint must point the on-call engineer at the preserved directory.
        assert f'inspect the build environment at {env_dir}' in stderr
        assert_env_kept(fake_env_backend.created_paths[0])
    else:
        assert 'pass --env-dir' in stderr
        assert_env_removed(fake_env_backend.created_paths[0])


@pytest.mark.parametrize('use_env_dir', [False, True], ids=['temporary', 'env-dir'])
def test_cli_success(
    fake_env_backend: FakeEnvControl,
    mocker: pytest_mock.MockerFixture,
    package_test_flit: str,
    tmp_path: pathlib.Path,
    use_env_dir: bool,
) -> None:
    env_dir = tmp_path / 'kept-env'

    run_cli(mocker, package_test_flit, str(tmp_path / 'dist'), ['--env-dir', str(env_dir)] if use_env_dir else [])

    assert_env_removed(fake_env_backend.created_paths[0])
