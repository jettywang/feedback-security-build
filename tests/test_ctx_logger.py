from __future__ import annotations

import logging
import subprocess
import sys

import pytest
import pytest_mock

import build._ctx


pytestmark = pytest.mark.contextvars


def test_default_ctx_logger(caplog: pytest.LogCaptureFixture) -> None:
    build._ctx.log('foo')

    [record] = caplog.records
    assert record.name == 'build'
    assert record.levelno == logging.INFO
    assert record.message == 'foo'


def test_ctx_custom_logger(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')

    build._ctx.LOGGER.set(log_stub)
    build._ctx.log('foo')

    log_stub.assert_called_once_with('foo')


def test_ctx_custom_logger_with_custom_verbosity(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')

    def log(message: str, **_kwargs: object) -> None:
        if build._ctx.verbosity >= 9000:
            log_stub(message)

    build._ctx.LOGGER.set(log)
    build._ctx.log('foo')
    build._ctx.VERBOSITY.set(9000)
    build._ctx.log('bar')

    log_stub.assert_called_once_with('bar')


@pytest.mark.parametrize(
    ('verbosity', 'kwarg_origins'),
    [
        (0, []),
        (1, [('subprocess', 'cmd'), ('subprocess', 'stdout')]),
    ],
)
def test_custom_subprocess_runner_ctx_logging(
    mocker: pytest_mock.MockerFixture, verbosity: int, kwarg_origins: list[tuple[str, ...]]
) -> None:
    log_stub = mocker.stub('custom_logger')

    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(verbosity)

    build._ctx.run_subprocess([sys.executable, '-m', 'build', '-V'])

    assert log_stub.call_count == len(kwarg_origins)
    assert [c.kwargs['kind'] for c in log_stub.call_args_list] == kwarg_origins


@pytest.mark.parametrize('verbosity', [0, 1])
def test_subprocess_invalid_utf8_preserves_failure(mocker: pytest_mock.MockerFixture, verbosity: int) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(verbosity)
    command = [sys.executable, '-c', "import os; os.write(2, b'failed: \\xff\\n'); raise SystemExit(7)"]

    with pytest.raises(subprocess.CalledProcessError) as caught:
        build._ctx.run_subprocess(command)

    assert caught.value.returncode == 7
    assert any(r'failed: \xff' in call.args[0] for call in log_stub.call_args_list)


def test_verbose_subprocess_invalid_utf8_success(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(1)
    build._ctx.run_subprocess([sys.executable, '-c', "import os; os.write(1, b'output: \\xff\\n')"])
    assert any(r'output: \xff' in call.args[0] for call in log_stub.call_args_list)


SECRET = 't0p-secret-token'
SECRET_URL = f'https://user:{SECRET}@example.com/simple'
SECRET_ENV = {'PIP_INDEX_URL': SECRET_URL}
SECRET_CMD = ['pip', 'install', '--index-url', SECRET_URL, '--no-input', 'foo']


def _redactor() -> build._ctx.Redactor:
    return build._ctx.Redactor(fields=['PIP_INDEX_URL'], args=[SECRET])


def _logged_messages(log_stub: pytest_mock.MockerFixture) -> list[str]:
    return [call.args[0] for call in log_stub.call_args_list]


def test_redactor_resolves_declared_secrets() -> None:
    redactor = _redactor()

    assert redactor.secrets(SECRET_ENV) == {SECRET_URL, SECRET}
    # fields missing from the environment resolve to nothing
    assert redactor.secrets({}) == {SECRET}
    # empty declarations never mask anything
    assert build._ctx.Redactor().redact(SECRET_URL, SECRET_ENV) == SECRET_URL


def test_verbose_subprocess_redacts_declared_secrets(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(1)

    process = mocker.MagicMock()
    process.stdout = iter([f'Looking in indexes: {SECRET_URL}\n', 'Collecting foo\n'])
    process.wait.return_value = 0
    popen = mocker.patch.object(build._ctx.subprocess, 'Popen')
    popen.return_value.__enter__.return_value = process

    build._ctx.run_subprocess(SECRET_CMD, env=SECRET_ENV, redactor=_redactor())

    messages = _logged_messages(log_stub)
    assert [call.kwargs['kind'] for call in log_stub.call_args_list] == [
        ('subprocess', 'cmd'),
        ('subprocess', 'stdout'),
        ('subprocess', 'stdout'),
    ]
    # declared sensitive plaintext reached no log outlet
    assert all(SECRET not in message for message in messages)
    assert all(SECRET_URL not in message for message in messages)
    # the failing configuration item and stage context stay locatable
    assert '--index-url ***' in messages[0]
    assert 'install' in messages[0]
    assert messages[1] == 'Looking in indexes: ***\n'
    assert messages[2] == 'Collecting foo\n'


def test_quiet_subprocess_failure_redacts_declared_secrets(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(0)

    error = subprocess.CalledProcessError(
        2,
        SECRET_CMD,
        output=f'ERROR: Could not find a version (from {SECRET_URL})',
        stderr=f'WARNING: 401 for {SECRET_URL}',
    )
    mocker.patch.object(build._ctx.subprocess, 'run', side_effect=error)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        build._ctx.run_subprocess(SECRET_CMD, env=SECRET_ENV, redactor=_redactor())

    assert caught.value.returncode == 2
    messages = _logged_messages(log_stub)
    assert [call.kwargs['kind'] for call in log_stub.call_args_list] == [
        ('subprocess', 'cmd'),
        ('subprocess', 'stdout'),
        ('subprocess', 'stderr'),
    ]
    assert all(SECRET not in message for message in messages)
    assert all(SECRET_URL not in message for message in messages)
    assert '--index-url ***' in messages[0]
    assert messages[1] == 'ERROR: Could not find a version (from ***)'
    assert messages[2] == 'WARNING: 401 for ***'


def test_quiet_subprocess_decode_failure_still_redacts(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(0)

    error = subprocess.CalledProcessError(
        7,
        SECRET_CMD,
        output=b'failed: \xff from ' + SECRET_URL.encode() + b'\n',
    )
    mocker.patch.object(build._ctx.subprocess, 'run', side_effect=error)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        build._ctx.run_subprocess(SECRET_CMD, env=SECRET_ENV, redactor=_redactor())

    assert caught.value.returncode == 7
    messages = _logged_messages(log_stub)
    assert all(SECRET not in message for message in messages)
    assert all(SECRET_URL not in message for message in messages)
    # undecodable bytes still surface via backslash replacement
    assert any(r'\xff' in message for message in messages)


def test_subprocess_redactor_from_context(mocker: pytest_mock.MockerFixture) -> None:
    log_stub = mocker.stub('custom_logger')
    build._ctx.LOGGER.set(log_stub)
    build._ctx.VERBOSITY.set(0)
    build._ctx.REDACTOR.set(_redactor())

    error = subprocess.CalledProcessError(1, SECRET_CMD, output=f'from {SECRET_URL}')
    mocker.patch.object(build._ctx.subprocess, 'run', side_effect=error)

    with pytest.raises(subprocess.CalledProcessError):
        build._ctx.run_subprocess(SECRET_CMD, env=SECRET_ENV)

    messages = _logged_messages(log_stub)
    assert all(SECRET not in message for message in messages)
    assert all(SECRET_URL not in message for message in messages)
