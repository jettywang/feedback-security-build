# SPDX-License-Identifier: MIT

from __future__ import annotations

import os.path
import tarfile
import zipfile


NAME = 'test_partial_wheel-1.0.0'
WHEEL = f'{NAME}-py3-none-any.whl'


def build_sdist(sdist_directory, config_settings=None):
    file = f'{NAME}.tar.gz'
    with tarfile.open(os.path.join(sdist_directory, file), 'w:gz') as archive:
        for member in ('pyproject.toml', 'PKG-INFO', 'backend.py'):
            archive.add(member, f'{NAME}/{member}')
    return file


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    # Always leave a file in the output directory first, so a failing hook can be
    # shown to drop partial output instead of having it linger as an artifact.
    partial = os.path.join(wheel_directory, f'{NAME}.whl.partial')
    with open(partial, 'wb') as handle:
        handle.write(b'truncated wheel content')

    if config_settings and config_settings.get('fail-wheel'):
        msg = 'wheel hook failed on purpose'
        raise RuntimeError(msg)

    os.replace(partial, os.path.join(wheel_directory, WHEEL))
    with zipfile.ZipFile(os.path.join(wheel_directory, WHEEL), 'a'):
        pass
    return WHEEL
