from __future__ import annotations


__lazy_modules__ = {'os', 'subprocess'}

import contextvars
import logging
import os
import subprocess
import typing


TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from ._types import StrPath


class Logger(typing.Protocol):  # pragma: no cover
    def __call__(self, message: str, *, kind: tuple[str, ...] | None = None) -> None: ...


_default_logger = logging.getLogger(typing.cast('str', __spec__.parent))


def _log_default(message: str, *, kind: tuple[str, ...] | None = None) -> None:  # noqa: ARG001
    # the log function that works in tests, real log function is set in __main__
    _default_logger.log(logging.INFO, message, stacklevel=2)


LOGGER = contextvars.ContextVar[Logger]('LOGGER', default=_log_default)
VERBOSITY = contextvars.ContextVar[int]('VERBOSITY', default=0)

REDACTED_PLACEHOLDER = '***'


class Redactor:
    """
    Declares sensitive values to scrub from subprocess diagnostics.

    Only the declared values are masked; stage messages, field names, and
    command flags stay visible so the failing stage and configuration item
    can still be located from the logs.
    """

    def __init__(self, *, fields: Collection[str] = (), args: Collection[str] = ()) -> None:
        """
        :param fields: environment variable names whose values are sensitive, such as
            installer configuration carrying temporary credentials
        :param args: command argument values that are sensitive
        """
        self._fields = frozenset(fields)
        self._args = frozenset(args)

    def secrets(self, env: Mapping[str, str] | None = None) -> frozenset[str]:
        """
        Resolve the declared fields and arguments to the plaintext values to mask.

        :param env: the environment the subprocess runs with; ``os.environ`` is used
            when the subprocess inherits it (``env=None``)
        """
        source = os.environ if env is None else env
        secrets = {source[field] for field in self._fields if source.get(field)}
        secrets.update(self._args)
        secrets.discard('')
        return frozenset(secrets)

    def redact(self, text: str, env: Mapping[str, str] | None = None) -> str:
        """
        Replace every declared sensitive value in ``text`` with ``***``.
        """
        # Mask longer values first so overlapping secrets cannot leave fragments behind.
        for secret in sorted(self.secrets(env), key=len, reverse=True):
            text = text.replace(secret, REDACTED_PLACEHOLDER)
        return text


REDACTOR = contextvars.ContextVar[Redactor | None]('REDACTOR', default=None)


def _redact(text: str, redactor: Redactor | None, env: Mapping[str, str] | None) -> str:
    if redactor is None:
        redactor = REDACTOR.get()
    if redactor is None:
        return text
    return redactor.redact(text, env)


def log_subprocess_error(
    error: subprocess.CalledProcessError,
    *,
    env: Mapping[str, str] | None = None,
    redactor: Redactor | None = None,
) -> None:
    log = LOGGER.get()

    log(_redact(subprocess.list2cmdline(error.cmd), redactor, env), kind=('subprocess', 'cmd'))

    for stream_name in ('stdout', 'stderr'):
        stream = getattr(error, stream_name)
        if stream:
            log(
                _redact(
                    stream.decode(errors='backslashreplace') if isinstance(stream, bytes) else stream,
                    redactor,
                    env,
                ),
                kind=('subprocess', stream_name),
            )


def run_subprocess(
    cmd: Sequence[StrPath],
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    redactor: Redactor | None = None,
) -> None:
    verbosity = VERBOSITY.get()

    if verbosity > 0:
        log = LOGGER.get()

        with subprocess.Popen(  # noqa: S603
            cmd,
            cwd=cwd,
            encoding='utf-8',
            errors='backslashreplace',
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            log(_redact(subprocess.list2cmdline(cmd), redactor, env), kind=('subprocess', 'cmd'))

            assert process.stdout
            for line in process.stdout:
                log(_redact(line, redactor, env), kind=('subprocess', 'stdout'))

            code = process.wait()
            if code:  # pragma: no cover
                raise subprocess.CalledProcessError(code, process.args)

    else:
        try:
            subprocess.run(cmd, capture_output=True, check=True, cwd=cwd, env=env)  # noqa: S603
        except subprocess.CalledProcessError as error:
            log_subprocess_error(error, env=env, redactor=redactor)
            raise


if TYPE_CHECKING:
    log: Logger
    verbosity: int

else:

    def __getattr__(name: str) -> Logger | int:
        if name == 'log':
            return LOGGER.get()
        if name == 'verbosity':
            return VERBOSITY.get()
        raise AttributeError(name)  # pragma: no cover


__all__ = [
    'LOGGER',
    'REDACTOR',
    'VERBOSITY',
    'Redactor',
    'log',
    'log_subprocess_error',
    'run_subprocess',
    'verbosity',
]
