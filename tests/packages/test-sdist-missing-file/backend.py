# SPDX-License-Identifier: MIT

import io
import os.path
import tarfile
import zipfile


NAME = 'test_sdist_missing_file-1.0.0'


def build_sdist(sdist_directory, config_settings=None):
    # The sdist is valid but incomplete: needed-for-wheel.txt is left out,
    # so the wheel stage fails when built from the extracted sdist.
    file = f'{NAME}.tar.gz'
    pkg_info = b'Metadata-Version: 2.1\nName: test-sdist-missing-file\nVersion: 1.0.0\n'
    with tarfile.open(os.path.join(sdist_directory, file), 'w') as archive:
        archive.add('pyproject.toml', f'{NAME}/pyproject.toml')
        archive.add('backend.py', f'{NAME}/backend.py')
        archive.addfile(tarfile.TarInfo(f'{NAME}/PKG-INFO'), io.BytesIO(pkg_info))
    return file


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    if not os.path.isfile('needed-for-wheel.txt'):
        msg = 'needed-for-wheel.txt is missing!'
        raise FileNotFoundError(msg)
    file = f'{NAME}-py3-none-any.whl'
    zipfile.ZipFile(os.path.join(wheel_directory, file), 'w').close()
    return file
