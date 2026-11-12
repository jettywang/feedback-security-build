from __future__ import annotations


__lazy_modules__ = {
    'contextlib',
    'importlib.metadata',
    'importlib.util',
    'packaging.requirements',
    'packaging.utils',
    'platform',
    'shutil',
    'subprocess',
    'sysconfig',
    'tempfile',
    'warnings',
    f'{__spec__.parent}',
    f'{__spec__.parent}._ctx',
    f'{__spec__.parent}._exceptions',
    f'{__spec__.parent}._util',
}

import abc
import contextlib
import dataclasses
import enum
import functools
import importlib.metadata
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import typing
import warnings

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import _ctx
from ._ctx import run_subprocess
from ._exceptions import BuildException, FailedProcessError, InstallerPrecheckError
from ._util import check_dependency


TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from typing import Self

    from . import _types


Installer = typing.Literal['pip', 'uv']

INSTALLERS: tuple[Installer, ...] = typing.get_args(Installer)

# Match the ``venv`` CLI default. Symlinked interpreters on Windows can fail, see #1175.
_USE_SYMLINKS = os.name != 'nt'


class SettingStatus(enum.Enum):
    """How the selected installer treats an active environment setting."""

    HONORED = 'honored'
    UNHONORED = 'unhonored'
    UNSUPPORTED = 'unsupported'


@dataclasses.dataclass(frozen=True)
class _SettingSpec:
    """
    How the uv installer treats a pip-era environment variable.

    :param uv_equivalent: the ``UV_*`` variable carrying the same meaning, when a one-to-one mapping exists
    :param reason: explanation shown when the setting cannot be carried over as-is
    """

    uv_equivalent: str | None = None
    reason: str = ''


@dataclasses.dataclass(frozen=True)
class SettingCheck:
    """The result of evaluating one active environment variable against the selected installer."""

    name: str
    value: str
    status: SettingStatus
    detail: str

    @property
    def blocks(self) -> bool:
        """Whether the setting stops the migration until it is reconfigured."""
        return self.status is not SettingStatus.HONORED


@dataclasses.dataclass(frozen=True)
class InstallerPrecheckReport:
    """
    The outcome of :func:`installer_precheck` for the selected installer.

    :param installer: the installer that was evaluated
    :param checks: one entry per environment variable that was active and is relevant to dependency installation
    :param constraints_txt_path: the constraints file that will be passed through, if any
    """

    installer: Installer
    checks: tuple[SettingCheck, ...]
    constraints_txt_path: str | None

    @property
    def blocking(self) -> tuple[SettingCheck, ...]:
        """Settings the installer cannot honour, in the order they were discovered."""
        return tuple(check for check in self.checks if check.blocks)

    @property
    def ok(self) -> bool:
        """Whether every active setting is honoured by the selected installer."""
        return not self.blocking

    def raise_if_unsupported(self) -> None:
        """Raise :class:`InstallerPrecheckError` when an active setting cannot be honoured."""
        if self.blocking:
            lines = '\n'.join(f'- {check.name}: {check.detail}' for check in self.blocking)
            installer = self.installer
            msg = (
                f'{len(self.blocking)} active pip-era setting(s) are not honoured by the {installer} installer; '
                f'reconfigure the environment before switching installers:\n{lines}'
            )
            raise InstallerPrecheckError(installer, msg)

    def format(self) -> str:
        """Render a human-readable summary, one setting per line."""
        if not self.checks:
            body = '- no active installer-specific environment settings detected'
        else:
            body = '\n'.join(
                f'- [{check.status.value}] {check.name}={check.value}: {check.detail}' for check in self.checks
            )
        constraints = self.constraints_txt_path or '(none)'
        return f'installer precheck for {self.installer!r} (constraints: {constraints})\n{body}'


# pip-specific settings carried by environment variables, and how uv treats them.
# Only settings that influence index access, network/TLS policy or dependency resolution are listed.
_UV_SETTING_SPECS: dict[str, _SettingSpec] = {
    'PIP_INDEX_URL': _SettingSpec(
        uv_equivalent='UV_INDEX_URL',
        reason='uv does not read PIP_INDEX_URL; set UV_INDEX_URL to the same private index',
    ),
    'PIP_EXTRA_INDEX_URL': _SettingSpec(
        uv_equivalent='UV_EXTRA_INDEX_URL',
        reason='uv does not read PIP_EXTRA_INDEX_URL; set UV_EXTRA_INDEX_URL instead',
    ),
    'PIP_CERT': _SettingSpec(
        reason='uv has no PIP_CERT equivalent; install the CA certificate into the native store with UV_NATIVE_TLS=1 '
        'or rely on SSL_CERT_FILE',
    ),
    'PIP_TRUSTED_HOST': _SettingSpec(
        uv_equivalent='UV_INSECURE_HOST',
        reason='uv does not read PIP_TRUSTED_HOST; allow the host with UV_INSECURE_HOST (prefer a trusted CA instead)',
    ),
    'PIP_CONSTRAINT': _SettingSpec(
        uv_equivalent='UV_CONSTRAINT',
        reason='uv does not read PIP_CONSTRAINT; set UV_CONSTRAINT or pass --dependency-constraints-txt',
    ),
    'PIP_TIMEOUT': _SettingSpec(
        uv_equivalent='UV_HTTP_TIMEOUT',
        reason='uv does not read PIP_TIMEOUT; set UV_HTTP_TIMEOUT (seconds) instead',
    ),
    'PIP_NO_CACHE_DIR': _SettingSpec(
        uv_equivalent='UV_NO_CACHE',
        reason='uv does not read PIP_NO_CACHE_DIR; set UV_NO_CACHE=1 to disable caching',
    ),
    'PIP_RETRIES': _SettingSpec(
        reason='uv does not expose a pip-style retry count; remove the override and rely on uv defaults',
    ),
    'PIP_REQUIRE_VIRTUALENV': _SettingSpec(
        reason='uv targets the build environment explicitly via --python; the require-virtualenv guard has no '
        'equivalent and must be unset',
    ),
    'PIP_CONFIG_FILE': _SettingSpec(
        reason='uv does not read pip configuration files; migrate index/cert settings to UV_* variables or uv.toml',
    ),
}

# Generic, installer-agnostic variables read by both pip and uv (and the TLS libraries underneath them).
_HONORED_ENV_VARS: frozenset[str] = frozenset(
    {
        'HTTP_PROXY',
        'HTTPS_PROXY',
        'NO_PROXY',
        'ALL_PROXY',
        'SSL_CERT_FILE',
        'SSL_CERT_DIR',
        'REQUESTS_CA_BUNDLE',
    }
)


def _redact_url(value: str) -> str:
    """Mask embedded credentials in a URL so they never appear in precheck output."""
    if '://' not in value or '@' not in value.split('://', 1)[1]:
        return value
    scheme, rest = value.split('://', 1)
    _credentials, _, host = rest.partition('@')
    return f'{scheme}://***:***@{host}'


def _evaluate_uv_setting(name: str, value: str, environ: Mapping[str, str]) -> SettingCheck:
    upper_name = name.upper()
    redacted = _redact_url(value)

    if upper_name in _HONORED_ENV_VARS:
        return SettingCheck(
            name=upper_name,
            value=redacted,
            status=SettingStatus.HONORED,
            detail='generic network/TLS variable, read natively by uv',
        )

    spec = _UV_SETTING_SPECS[upper_name]
    active_upper = {key.upper(): key for key in environ if environ[key]}

    if spec.uv_equivalent is not None and spec.uv_equivalent in active_upper:
        return SettingCheck(
            name=upper_name,
            value=redacted,
            status=SettingStatus.HONORED,
            detail=f'uv equivalent {spec.uv_equivalent} is already set; the PIP_* value is otherwise ignored',
        )

    return SettingCheck(
        name=upper_name,
        value=redacted,
        status=SettingStatus.UNSUPPORTED if spec.uv_equivalent is None else SettingStatus.UNHONORED,
        detail=spec.reason,
    )


def installer_precheck(
    installer: Installer,
    *,
    environ: Mapping[str, str] | None = None,
    constraints_txt_path: _types.StrPath | None = None,
) -> InstallerPrecheckReport:
    """
    Check whether the selected installer supports the active build configuration.

    Inspects the environment for pip-era index access restrictions and build dependency constraints that the
    requested installer must be able to honour. With ``installer='pip'`` every relevant setting is honoured as-is.
    With ``installer='uv'``, pip-specific variables are only accepted when their uv equivalent is already
    configured; otherwise the returned report marks them as blocking.

    :param installer: the installer that will create the build environment
    :param environ: environment mapping to inspect (defaults to :data:`os.environ`)
    :param constraints_txt_path: constraints file that will be forwarded to the installer
    :return: a report describing each relevant active setting and whether it is honoured
    """
    env = os.environ if environ is None else environ
    recognized = frozenset(_UV_SETTING_SPECS) | _HONORED_ENV_VARS

    checks: list[SettingCheck] = []
    for name, value in env.items():
        if not value or name.upper() not in recognized:
            continue
        if installer == 'pip':
            checks.append(
                SettingCheck(
                    name=name.upper(),
                    value=_redact_url(value),
                    status=SettingStatus.HONORED,
                    detail='native pip setting',
                )
            )
        else:
            checks.append(_evaluate_uv_setting(name, value, env))

    return InstallerPrecheckReport(
        installer=installer,
        checks=tuple(checks),
        constraints_txt_path=os.fspath(constraints_txt_path) if constraints_txt_path is not None else None,
    )


class IsolatedEnv(typing.Protocol):
    """Isolated build environment ABC."""

    @property
    @abc.abstractmethod
    def python_executable(self) -> str:
        """The Python executable of the isolated environment."""

    @abc.abstractmethod
    def make_extra_environ(self) -> Mapping[str, str] | None:
        """Generate additional env vars specific to the isolated environment."""


def _has_dependency(
    name: str, minimum_version_str: str | None = None, /, *, path: list[str] | None = None
) -> importlib.metadata.Distribution | None:
    """
    Given a distribution name, see if it is present and return the distribution
    if the version is sufficient for build, None if the package is missing or
    too old.
    """
    from packaging.version import Version

    try:
        distribution = next(iter(importlib.metadata.distributions(name=name, path=sys.path if path is None else path)))
    except StopIteration:
        return None

    if minimum_version_str is None:
        return distribution

    if Version(distribution.version) < Version(minimum_version_str):
        return None

    return distribution


class DefaultIsolatedEnv(IsolatedEnv):
    """
    Isolated environment which supports several different underlying implementations.
    """

    _env_backend: _EnvBackend
    _path: str

    def __init__(
        self,
        *,
        installer: Installer = 'pip',
        path: str | None = None,
    ) -> None:
        self.installer: Installer = installer
        self._requested_path = path

    def __enter__(self) -> Self:
        if self._requested_path is None:
            path = tempfile.mkdtemp(prefix='build-env-')
        else:
            path = self._requested_path
            if os.path.exists(path) and not os.path.isdir(path):
                msg = f'Build environment location is not a directory: {path}'
                raise BuildException(msg)
            if os.path.isdir(path):
                with os.scandir(path) as entries:
                    if next(entries, None) is not None:
                        msg = f'Build environment location is not empty: {path}'
                        raise BuildException(msg)
            os.makedirs(path, exist_ok=True)

        # Set early so ``__exit__`` can clean up even if the calls below fail.
        self._path = path
        try:
            # Call ``realpath`` to prevent spurious warning from being emitted
            # that the venv location has changed on Windows for the venv impl.
            # The username is DOS-encoded in the output of tempfile - the location is the same
            # but the representation of it is different, which confuses venv.
            # Ref: https://bugs.python.org/issue46171
            path = os.path.realpath(path)
            self._path = path

            # uv is opt-in only.
            if self.installer == 'uv':
                # Fail before creating anything when the migration cannot honour active pip-era settings.
                report = installer_precheck('uv')
                _ctx.log(report.format(), kind=('subprocess', 'cmd'))
                report.raise_if_unsupported()
                self._env_backend = _UvBackend()
            else:
                self._env_backend = _PipBackend()

            _ctx.log(f'Creating isolated environment: {self._env_backend.display_name}...', kind=('step',))
            self._env_backend.create(self._path)

        except BaseException:  # cleanup folder if creation fails or is interrupted
            self.__exit__(*sys.exc_info())
            raise

        return self

    def __exit__(self, *args: object) -> None:
        # A temporary location is always cleaned up. A caller-specified location is kept when the build fails so
        # its environment can be inspected, but removed on success so the same location can be reused.
        if self._requested_path is None or args[0] is None:
            shutil.rmtree(self._path, ignore_errors=True)

    @property
    def path(self) -> str:
        """The location of the isolated build environment."""
        return self._path

    @property
    def python_executable(self) -> str:
        """The python executable of the isolated build environment."""
        return self._env_backend.python_executable

    def make_extra_environ(self) -> dict[str, str]:
        path = os.environ.get('PATH')
        return {
            'PATH': os.pathsep.join([self._env_backend.scripts_dir, path])
            if path is not None
            else self._env_backend.scripts_dir,
            # Set PYTHONPATH to empty to override any host value. An empty
            # PYTHONPATH is treated as unset by CPython's path initialization
            # (the ``if pythonpath_env:`` check makes it a no-op).
            'PYTHONPATH': '',
        }

    def installed_versions(self, requirements: Collection[str]) -> dict[str, str]:
        """
        Map the canonical name of each requirement to the version installed in the environment.

        Requirements that resolved to nothing installed are omitted. Reads the distribution metadata
        directly from the environment's ``purelib`` so no subprocess is spawned in the isolated environment.

        :param requirements: PEP 508 requirement specifications to look up; non-PEP 508 entries (such as local
            paths or URLs accepted by :meth:`install`) are skipped since their installed name cannot be derived
        """
        wanted = {name for requirement in requirements if (name := _canonical_requirement_name(requirement)) is not None}
        distributions = importlib.metadata.distributions(path=[self._env_backend.purelib])
        return {
            name: distribution.version
            for distribution in distributions
            if (name := canonicalize_name(distribution.name)) in wanted
        }

    def install(
        self,
        requirements: Collection[str],
        constraints_txt_path: _types.StrPath | None = None,
        *,
        _fresh: bool = False,  # Used internally by CLI to support preset PYTHONPATH
    ) -> None:
        """
        Install packages from PEP 508 requirements in the isolated build environment.

        :param requirements: PEP 508 requirement specification to install

        :note: Passing non-PEP 508 strings will result in undefined behavior, you *should not* rely on it. It is
               merely an implementation detail, it may change any time without warning.
        """
        if not requirements:
            return

        _ctx.log(
            'Installing packages in isolated environment:\n' + '\n'.join(f'- {r}' for r in sorted(requirements)),
            kind=('step',),
        )
        self._env_backend.install_dependencies(requirements, constraints_txt_path, _fresh=_fresh)


def _canonical_requirement_name(requirement: str) -> str | None:
    try:
        return canonicalize_name(Requirement(requirement).name)
    except InvalidRequirement:
        return None


class _EnvBackend(typing.Protocol):  # pragma: no cover
    python_executable: str
    scripts_dir: str
    purelib: str

    def create(self, path: str) -> None: ...

    def install_dependencies(
        self,
        requirements: Collection[str],
        constraints_txt_path: _types.StrPath | None,
        *,
        _fresh: bool = False,
    ) -> None: ...

    @property
    def display_name(self) -> str: ...


@functools.cache
def _has_keyring_cli() -> bool:
    return shutil.which('keyring') is not None


def _pip_env() -> dict[str, str] | None:
    if 'PIP_KEYRING_PROVIDER' not in os.environ and _has_keyring_cli():
        return {**os.environ, 'PIP_KEYRING_PROVIDER': 'subprocess'}
    return None


def _pip_install_command(
    python_executable: str,
    constraints_txt_path: _types.StrPath | None,
    requirement_file: _types.StrPath,
    *,
    use_outer_pip: bool,
    fresh: bool,
    verbosity: int,
) -> list[str]:
    """Build the exact ``pip install`` command line the pip backend runs."""
    cmd = (
        [sys.executable, '-m', 'pip', '--python', python_executable]
        if use_outer_pip
        else [python_executable, '-Im', 'pip']
    )

    if verbosity > 1:
        cmd += [f'-{"v" * (verbosity - 1)}']

    cmd += ['install']
    if fresh:
        cmd += ['--ignore-installed']
    cmd += ['--use-pep517', '--no-warn-script-location', '--no-compile', '--no-input', '-r', os.fspath(requirement_file)]

    if constraints_txt_path:
        cmd += ['-c', str(constraints_txt_path)]

    return cmd


def _uv_install_command(
    uv_bin: str,
    python_executable: str,
    requirements: Collection[str],
    constraints_txt_path: _types.StrPath | None,
    *,
    verbosity: int,
) -> list[str]:
    """Build the exact ``uv pip install`` command line the uv backend runs."""
    cmd = [uv_bin, 'pip']

    if verbosity > 1:
        cmd += [f'-{"v" * min(2, verbosity - 1)}']

    cmd += ['install', *requirements, '--python', python_executable]

    if constraints_txt_path:
        cmd += ['-c', str(constraints_txt_path)]

    return cmd


class _PipBackend(_EnvBackend):
    def __init__(self) -> None:
        self._create_with_virtualenv = not self._has_valid_outer_pip and self._has_virtualenv

    @functools.cached_property
    def _has_valid_outer_pip(self) -> bool | None:
        """
        This checks for a valid global pip. Returns None if pip is missing, False
        if pip is too old or debundled, and True if it can be used.
        """

        # Version to have added the `--python` option.
        # `pip install --python` is nonfunctional on Gentoo debundled pip.
        if dist := _has_dependency('pip', '22.3'):  # pragma: no cover
            files = dist.files
            if files:
                return any(str(f).startswith('pip/_vendor') for f in files)
            # The distribution package manager deleted the RECORD file,
            # generally to force pip to be unable to uninstall itself
            # Only try this on 3.12+ since it can have side effects before 3.12
            # due to _distutils_hack and pip interacting.
            if sys.version_info >= (3, 12):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    if importlib.util.find_spec('pip._vendor') is not None:
                        return True
            return False

        return None  # pragma: no cover

    @functools.cached_property
    def _has_virtualenv(self) -> bool:
        """
        virtualenv might be incompatible if it was installed separately
        from build. This verifies that virtualenv and all of its
        dependencies are installed as required by build.
        """
        name = 'virtualenv'

        return importlib.util.find_spec(name) is not None and not any(
            Requirement(d[1]).name == name for d in check_dependency(f'build[{name}]') if len(d) > 1
        )

    @staticmethod
    def _get_minimum_pip_version_str() -> str:
        if platform.system() == 'Darwin':
            release, _, machine = platform.mac_ver()
            major = release.split('.', 1)[0]
            if major and int(major) >= 11:
                # macOS 11+ name scheme change requires 20.3. Intel macOS 11.0 can be
                # told to report 10.16 for backwards compatibility; but that also fixes
                # earlier versions of pip so this is only needed for 11+.
                is_apple_silicon_python = machine != 'x86_64'
                return '21.0.1' if is_apple_silicon_python else '20.3.0'

        # PEP-517 and manylinux1 was first implemented in 19.1
        return '19.1.0'

    def create(self, path: str) -> None:
        if self._create_with_virtualenv:
            import packaging.version
            import virtualenv

            virtualenv_ver = packaging.version.Version(importlib.metadata.version('virtualenv'))

            opts = [
                path,
                '--activators',
                '',
                '--no-setuptools',
                '--no-periodic-update',
            ]

            if virtualenv_ver < packaging.version.Version('20.31.0'):
                opts.append('--no-wheel')

            result = virtualenv.cli_run(opts, setup_logging=False)

            # The creator attributes are `pathlib.Path`s.
            self.python_executable = str(result.creator.exe)
            self.scripts_dir = str(result.creator.script_dir)
            self.purelib = str(result.creator.purelib)

        else:
            import venv

            with_pip = not self._has_valid_outer_pip

            try:
                venv.EnvBuilder(symlinks=_USE_SYMLINKS, with_pip=with_pip).create(path)
            except subprocess.CalledProcessError as exc:
                _ctx.log_subprocess_error(exc)
                raise FailedProcessError(exc, 'Failed to create venv. Maybe try installing virtualenv.') from None

            self.python_executable, self.scripts_dir, purelib = _find_executable_and_scripts(path)
            self.purelib = purelib

            if with_pip:
                minimum_pip_version_str = self._get_minimum_pip_version_str()
                if not _has_dependency(
                    'pip',
                    minimum_pip_version_str,
                    path=[purelib],
                ):
                    run_subprocess(
                        [self.python_executable, '-Im', 'pip', 'install', '--no-input', f'pip>={minimum_pip_version_str}'],
                        env=_pip_env(),
                    )

                # Uninstall setuptools from the build env to prevent depending on it implicitly.
                # Pythons 3.12 and up do not install setuptools, check if it exists first.
                if _has_dependency(
                    'setuptools',
                    path=[purelib],
                ):
                    run_subprocess(
                        [self.python_executable, '-Im', 'pip', 'uninstall', '--no-input', '-y', 'setuptools'],
                        env=_pip_env(),
                    )

    def install_dependencies(
        self,
        requirements: Collection[str],
        constraints_txt_path: _types.StrPath | None,
        *,
        _fresh: bool = False,
    ) -> None:
        with contextlib.ExitStack() as exit_stack:
            # pip does not honour environment markers in command line arguments
            # but it does from requirement files.
            with tempfile.NamedTemporaryFile(
                'w', prefix='build-requirements-', suffix='.txt', delete=False, encoding='utf-8'
            ) as requirement_file:
                requirement_file.write('\n'.join(requirements))
            exit_stack.callback(functools.partial(os.unlink, requirement_file.name))

            cmd = _pip_install_command(
                self.python_executable,
                constraints_txt_path,
                os.path.abspath(requirement_file.name),
                use_outer_pip=bool(self._has_valid_outer_pip),
                fresh=_fresh,
                verbosity=_ctx.verbosity,
            )

            run_subprocess(cmd, env=_pip_env())

    @property
    def display_name(self) -> str:
        return 'virtualenv+pip' if self._create_with_virtualenv else 'venv+pip'


class _UvBackend(_EnvBackend):
    _env_path: str
    _uv_bin: str

    def create(self, path: str) -> None:
        import venv

        self._env_path = path

        try:
            import uv

            self._uv_bin = uv.find_uv_bin()
        except (ModuleNotFoundError, FileNotFoundError):
            uv_bin = shutil.which(os.environ.get('UV') or 'uv')
            if uv_bin is None:
                msg = 'uv executable not found'
                raise RuntimeError(msg) from None

            _ctx.log(f'Using external uv from {uv_bin}')
            self._uv_bin = uv_bin

        venv.EnvBuilder(symlinks=_USE_SYMLINKS, with_pip=False).create(self._env_path)
        self.python_executable, self.scripts_dir, self.purelib = _find_executable_and_scripts(self._env_path)

    def install_dependencies(  # pragma: no cover -- uv tests are skipped on PyPy, covered on CPython
        self,
        requirements: Collection[str],
        constraints_txt_path: _types.StrPath | None,
        *,
        _fresh: bool = False,
    ) -> None:
        cmd = _uv_install_command(
            self._uv_bin,
            self.python_executable,
            requirements,
            constraints_txt_path,
            verbosity=_ctx.verbosity,
        )

        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['VIRTUAL_ENV'] = self._env_path
        if 'UV_KEYRING_PROVIDER' not in os.environ and _has_keyring_cli():
            env['UV_KEYRING_PROVIDER'] = 'subprocess'
        run_subprocess(cmd, env=env)

    @property
    def display_name(self) -> str:
        return 'venv+uv'


def _find_executable_and_scripts(path: str) -> tuple[str, str, str]:
    """
    Detect the Python executable and script folder of a virtual environment.

    :param path: The location of the virtual environment
    :return: The Python executable, script folder, and purelib folder
    """
    config_vars = sysconfig.get_config_vars().copy()  # globally cached, copy before altering it
    config_vars['base'] = path
    scheme_names = sysconfig.get_scheme_names()
    if 'venv' in scheme_names:
        # Python distributors with custom default installation scheme can set a
        # scheme that can't be used to expand the paths in a venv.
        # This can happen if build itself is not installed in a venv.
        # The distributors are encouraged to set a "venv" scheme to be used for this.
        # See https://bugs.python.org/issue45413
        # and https://github.com/pypa/virtualenv/issues/2208
        paths = sysconfig.get_paths(scheme='venv', vars=config_vars)  # pragma: no cover
    elif 'posix_local' in scheme_names:
        # The Python that ships on Debian/Ubuntu varies the default scheme to
        # install to /usr/local
        # But it does not (yet) set the "venv" scheme.
        # If we're the Debian "posix_local" scheme is available, but "venv"
        # is not, we use "posix_prefix" instead which is venv-compatible there.
        paths = sysconfig.get_paths(scheme='posix_prefix', vars=config_vars)
    elif 'osx_framework_library' in scheme_names:
        # The Python that ships with the macOS developer tools varies the
        # default scheme depending on whether the ``sys.prefix`` is part of a framework.
        # But it does not (yet) set the "venv" scheme.
        # If the Apple-custom "osx_framework_library" scheme is available but "venv"
        # is not, we use "posix_prefix" instead which is venv-compatible there.
        paths = sysconfig.get_paths(scheme='posix_prefix', vars=config_vars)
    else:
        paths = sysconfig.get_paths(vars=config_vars)

    executable = os.path.join(paths['scripts'], 'python.exe' if os.name == 'nt' else 'python')
    if not os.path.exists(executable):
        msg = f'Virtual environment creation failed, executable {executable} missing'
        raise RuntimeError(msg)

    return executable, paths['scripts'], paths['purelib']


__all__ = [
    'DefaultIsolatedEnv',
    'InstallerPrecheckError',
    'InstallerPrecheckReport',
    'IsolatedEnv',
    'SettingCheck',
    'SettingStatus',
    'installer_precheck',
]
