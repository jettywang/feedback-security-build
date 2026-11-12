# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import pytest_mock

import build.env


MIGRATABLE_PIP_VARS = {
    'PIP_INDEX_URL': 'UV_INDEX_URL',
    'PIP_EXTRA_INDEX_URL': 'UV_EXTRA_INDEX_URL',
    'PIP_CONSTRAINT': 'UV_CONSTRAINT',
    'PIP_TIMEOUT': 'UV_HTTP_TIMEOUT',
    'PIP_NO_CACHE_DIR': 'UV_NO_CACHE',
    'PIP_TRUSTED_HOST': 'UV_INSECURE_HOST',
}
HONORED_GENERIC_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
UNSUPPORTED_PIP_VARS = ('PIP_CERT', 'PIP_RETRIES', 'PIP_REQUIRE_VIRTUALENV', 'PIP_CONFIG_FILE')

DEMO_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'tasks' / 'migration_precheck_demo.py'


def test_clean_environment_passes_for_both_installers() -> None:
    for installer in ('pip', 'uv'):
        report = build.env.installer_precheck(installer, environ={'PATH': '/usr/bin'})
        assert report.ok
        assert report.checks == ()
        assert report.blocking == ()
        report.raise_if_unsupported()


@pytest.mark.parametrize('var_name', sorted(MIGRATABLE_PIP_VARS))
def test_pip_installer_honours_all_pip_settings(var_name: str) -> None:
    report = build.env.installer_precheck('pip', environ={var_name: 'value'})
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.HONORED
    assert report.ok


@pytest.mark.parametrize(('var_name', 'uv_equivalent'), sorted(MIGRATABLE_PIP_VARS.items()))
def test_uv_blocks_unmigrated_pip_setting(var_name: str, uv_equivalent: str) -> None:
    report = build.env.installer_precheck('uv', environ={var_name: 'value'})
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.UNHONORED
    assert check.blocks
    assert uv_equivalent in check.detail

    with pytest.raises(build.env.InstallerPrecheckError, match=var_name):
        report.raise_if_unsupported()


@pytest.mark.parametrize(('var_name', 'uv_equivalent'), sorted(MIGRATABLE_PIP_VARS.items()))
def test_uv_accepts_pip_setting_when_equivalent_is_configured(var_name: str, uv_equivalent: str) -> None:
    report = build.env.installer_precheck('uv', environ={var_name: 'value', uv_equivalent: 'uv-value'})
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.HONORED
    assert report.ok
    report.raise_if_unsupported()


@pytest.mark.parametrize('var_name', UNSUPPORTED_PIP_VARS)
def test_uv_marks_settings_without_equivalent_as_unsupported(var_name: str) -> None:
    report = build.env.installer_precheck('uv', environ={var_name: 'value'})
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.UNSUPPORTED
    assert check.blocks
    with pytest.raises(build.env.InstallerPrecheckError):
        report.raise_if_unsupported()


@pytest.mark.parametrize('var_name', HONORED_GENERIC_VARS)
def test_uv_honours_generic_network_and_tls_vars(var_name: str) -> None:
    report = build.env.installer_precheck('uv', environ={var_name: 'value'})
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.HONORED
    assert report.ok


def test_lowercase_proxy_and_pip_names_are_recognised() -> None:
    report = build.env.installer_precheck('uv', environ={'https_proxy': 'http://proxy:3128', 'pip_index_url': 'u'})
    names = [check.name for check in report.checks]
    assert names == ['HTTPS_PROXY', 'PIP_INDEX_URL']


def test_lowercase_uv_equivalent_still_satisfies_precheck() -> None:
    report = build.env.installer_precheck(
        'uv',
        environ={'PIP_INDEX_URL': 'https://pypi.corp/simple', 'uv_index_url': 'https://pypi.corp/simple'},
    )
    (check,) = report.checks
    assert check.status is build.env.SettingStatus.HONORED
    assert report.ok


def test_empty_values_are_ignored() -> None:
    report = build.env.installer_precheck('uv', environ={'PIP_INDEX_URL': '', 'UV_INDEX_URL': ''})
    assert report.checks == ()


def test_unrelated_variables_are_ignored() -> None:
    report = build.env.installer_precheck('uv', environ={'PATH': '/bin', 'HOME': '/root', 'PYTHONPATH': '/x'})
    assert report.checks == ()


def test_credentials_in_urls_are_redacted() -> None:
    report = build.env.installer_precheck(
        'uv',
        environ={'PIP_INDEX_URL': 'https://user:secret@pypi.corp/simple'},
    )
    (check,) = report.checks
    assert 'secret' not in check.value
    assert check.value == 'https://***:***@pypi.corp/simple'


def test_uv_only_environment_needs_no_changes() -> None:
    report = build.env.installer_precheck(
        'uv',
        environ={'UV_INDEX_URL': 'https://pypi.corp/simple', 'UV_NO_CACHE': '1'},
    )
    assert report.ok
    assert report.checks == ()


def test_report_aggregates_every_blocking_setting() -> None:
    environ = {
        'PIP_INDEX_URL': 'https://pypi.corp/simple',
        'PIP_TRUSTED_HOST': 'pypi.corp',
        'PIP_CERT': '/ca.pem',
        'HTTPS_PROXY': 'http://proxy:8080',
    }
    report = build.env.installer_precheck('uv', environ=environ)
    assert [check.name for check in report.blocking] == ['PIP_INDEX_URL', 'PIP_TRUSTED_HOST', 'PIP_CERT']
    assert not report.ok

    with pytest.raises(build.env.InstallerPrecheckError) as exc_info:
        report.raise_if_unsupported()
    message = str(exc_info.value)
    assert '3 active pip-era setting(s)' in message
    assert 'PIP_INDEX_URL' in message and 'PIP_TRUSTED_HOST' in message and 'PIP_CERT' in message
    assert exc_info.value.installer == 'uv'


def test_constraints_path_is_recorded_in_report(tmp_path: pathlib.Path) -> None:
    constraints = tmp_path / 'constraints.txt'
    constraints.write_text('flit_core<4\n', encoding='utf-8')
    report = build.env.installer_precheck('uv', environ={}, constraints_txt_path=constraints)
    assert report.constraints_txt_path == str(constraints)
    assert str(constraints) in report.format()


def test_precheck_runs_before_environment_creation(mocker: pytest_mock.MockerFixture) -> None:
    create = mocker.patch('build.env._UvBackend.create')
    mocker.patch.dict(
        'os.environ',
        {'PIP_INDEX_URL': 'https://pypi.corp/simple'},
        clear=False,
    )

    with pytest.raises(build.env.InstallerPrecheckError):
        build.env.DefaultIsolatedEnv(installer='uv').__enter__()

    create.assert_not_called()


def test_clean_uv_environment_passes_enter(mocker: pytest_mock.MockerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*build.env._UV_SETTING_SPECS, *build.env._HONORED_ENV_VARS):
        monkeypatch.delenv(var, raising=False)
    mocker.patch('build.env._UvBackend.create')
    mocker.patch('build.env._find_executable_and_scripts', return_value=('/x/python', '/x/scripts', '/x/purelib'))

    env = build.env.DefaultIsolatedEnv(installer='uv')
    with env:
        assert env.installer == 'uv'


def test_pip_enter_does_not_run_uv_precheck(mocker: pytest_mock.MockerFixture) -> None:
    precheck = mocker.patch('build.env.installer_precheck')
    mocker.patch('build.env._PipBackend.create')

    with build.env.DefaultIsolatedEnv(installer='pip'):
        pass

    precheck.assert_not_called()


def test_pip_command_builder_matches_backend_shape() -> None:
    cmd = build.env._pip_install_command(
        '/venv/bin/python',
        '/cfg/constraints.txt',
        '/tmp/requirements.txt',
        use_outer_pip=False,
        fresh=True,
        verbosity=2,
    )
    assert cmd == [
        '/venv/bin/python',
        '-Im',
        'pip',
        '-v',
        'install',
        '--ignore-installed',
        '--use-pep517',
        '--no-warn-script-location',
        '--no-compile',
        '--no-input',
        '-r',
        '/tmp/requirements.txt',
        '-c',
        '/cfg/constraints.txt',
    ]


def test_uv_command_builder_matches_backend_shape() -> None:
    cmd = build.env._uv_install_command(
        '/bin/uv',
        '/venv/bin/python',
        ['flit_core', 'wheel'],
        '/cfg/constraints.txt',
        verbosity=3,
    )
    assert cmd == [
        '/bin/uv',
        'pip',
        '-vv',
        'install',
        'flit_core',
        'wheel',
        '--python',
        '/venv/bin/python',
        '-c',
        '/cfg/constraints.txt',
    ]


def test_command_builders_omit_constraints_when_absent() -> None:
    pip_cmd = build.env._pip_install_command(
        '/venv/bin/python', None, '/tmp/req.txt', use_outer_pip=True, fresh=False, verbosity=0
    )
    assert '-c' not in pip_cmd
    uv_cmd = build.env._uv_install_command('/bin/uv', '/venv/bin/python', ['x'], None, verbosity=0)
    assert '-c' not in uv_cmd


def _load_demo_module() -> object:
    spec = importlib.util.spec_from_file_location('migration_precheck_demo', DEMO_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def test_demo_script_runs_three_scenarios() -> None:
    module = _load_demo_module()

    clean = module.simulate_build('uv', {'PATH': '/usr/bin'}, constraints_txt_path=None)
    assert clean.failure_phase is None
    assert [call.phase for call in clean.calls] == ['precheck', 'create', 'install']
    install_call = next(call for call in clean.calls if call.phase == 'install')
    assert install_call.command is not None
    assert install_call.command[:2] == ['uv', 'pip']

    pip_restricted = module.simulate_build(
        'pip',
        {'PIP_INDEX_URL': 'https://pypi.corp/simple'},
        constraints_txt_path=None,
    )
    assert pip_restricted.failure_phase is None
    pip_install = next(call for call in pip_restricted.calls if call.phase == 'install')
    assert pip_install.command is not None and 'pip' in pip_install.command

    uv_restricted = module.simulate_build(
        'uv',
        {
            'PIP_INDEX_URL': 'https://u:p@pypi.corp/simple',
            'PIP_TRUSTED_HOST': 'pypi.corp',
            'HTTPS_PROXY': 'http://proxy:8080',
        },
        constraints_txt_path=None,
    )
    assert uv_restricted.failure_phase == 'precheck'
    assert [call.phase for call in uv_restricted.calls] == ['precheck']
    assert 'secret' not in uv_restricted.error if uv_restricted.error else True


def test_demo_main_outputs_all_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_demo_module()

    module.main()
    output = capsys.readouterr().out
    assert 'Scenario 1: standard, unrestricted build' in output
    assert 'Scenario 2: restricted corporate environment' in output
    assert 'Scenario 3: same restrictions after migrating' in output
    assert 'FAILED at phase: precheck' in output
    assert '***:***@' in output
