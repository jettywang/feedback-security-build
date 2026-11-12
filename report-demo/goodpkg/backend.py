"""Minimal in-tree build backend for the report demo."""

import io
import os.path
import tarfile
import zipfile


NAME = 'report_demo-1.0.0'


def build_sdist(sdist_directory, config_settings=None):
    file = f'{NAME}.tar.gz'
    pkg_info = b'Metadata-Version: 2.1\nName: report-demo\nVersion: 1.0.0\n'
    with tarfile.open(os.path.join(sdist_directory, file), 'w') as archive:
        archive.add('pyproject.toml', f'{NAME}/pyproject.toml')
        archive.add('backend.py', f'{NAME}/backend.py')
        archive.add('needed-for-wheel.txt', f'{NAME}/needed-for-wheel.txt')
        archive.addfile(tarfile.TarInfo(f'{NAME}/PKG-INFO'), io.BytesIO(pkg_info))
    return file


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    if not os.path.isfile('needed-for-wheel.txt'):
        msg = 'needed-for-wheel.txt is missing!'
        raise FileNotFoundError(msg)
    file = f'{NAME}-py3-none-any.whl'
    zipfile.ZipFile(os.path.join(wheel_directory, file), 'w').close()
    return file
