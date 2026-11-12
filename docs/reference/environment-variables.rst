#######################
 Environment Variables
#######################

This reference documents environment variables that affect build's behavior. You can use these to configure private
package indexes, proxies, SSL certificates, and other settings.

************************
 Variables Set by build
************************

``PATH`` and ``PYTHONPATH``
===========================

For isolated backend subprocesses, build prepends the environment's scripts directory to ``PATH`` and sets
``PYTHONPATH`` to an empty string so host import paths do not leak into the build.

``VIRTUAL_ENV``
===============

When using the uv installer, build sets ``VIRTUAL_ENV`` for the dependency-installation subprocess to identify the
target environment. It does not set this variable for backend hooks, so backends should not rely on it to identify
build's isolated environment.

*****************************
 Package Index Configuration
*****************************

By default, build uses `pip <https://pip.pypa.io/>`_ to install build dependencies in isolated environments. Configure
pip using these environment variables to work with private package indexes or mirrors. For detailed examples, see
:doc:`../how-to/corporate-environments`.

.. note::

    These ``PIP_*`` variables only apply when using the default pip installer. When using ``--installer=uv``, configure
    uv through its own `environment variables <https://docs.astral.sh/uv/configuration/environment/>`_ instead (e.g.,
    ``UV_INDEX_URL``, ``UV_EXTRA_INDEX_URL``).

.. _installer-migration-precheck:

Installer migration precheck
============================

Switching installers with ``--installer=uv`` while pip-era variables are still exported can silently drop index access
restrictions or build dependency constraints. Before creating the build environment, build runs a precheck that
evaluates every active installer-relevant variable against the selected installer:

- generic network and TLS variables such as ``HTTPS_PROXY``, ``NO_PROXY`` and ``SSL_CERT_FILE`` are read natively by
  both installers and always pass;
- pip-specific variables with a uv equivalent (for example ``PIP_INDEX_URL`` -> ``UV_INDEX_URL`` and
  ``PIP_CONSTRAINT`` -> ``UV_CONSTRAINT``) pass once the equivalent ``UV_*`` variable is set;
- settings without a uv equivalent (for example ``PIP_TRUSTED_HOST`` and ``PIP_CONFIG_FILE``) are always reported as
  unsupported.

When one or more active settings cannot be honoured, build raises an :class:`build.env.InstallerPrecheckError`
*before* creating the virtual environment, listing each blocking variable and the required remediation. Credentials
embedded in index URLs are redacted from the report. The same check is available programmatically through
:func:`build.env.installer_precheck`, which returns an :class:`build.env.InstallerPrecheckReport` without raising.

``PIP_INDEX_URL``
=================

Sets the primary `package index <https://packaging.python.org/en/latest/glossary/#term-Package-Index>`_ URL where pip
looks for packages. Use this to point to private package repositories or PyPI mirrors. Defaults to
``https://pypi.org/simple``.

See :doc:`../how-to/corporate-environments` for private-index setup.

``PIP_EXTRA_INDEX_URL``
=======================

Adds additional package indexes to search beyond the primary index. This allows using both `PyPI <https://pypi.org/>`_
and a private index simultaneously.

.. warning::

    Combining indexes risks dependency confusion: a package on one index can shadow a package on another. Prefer a
    single trusted index with ``PIP_INDEX_URL``. See :doc:`../how-to/corporate-environments` for private-index setup.

``PIP_CERT``
============

Path to a custom CA certificate bundle for SSL/TLS verification. Use this when your organization uses self-signed
certificates or a custom certificate authority. Defaults to system certificates.

See :ref:`corporate-certificates` for setup instructions.

``PIP_TRUSTED_HOST``
====================

Disables SSL verification for the specified host. Only use this in secure development environments where SSL
certificates aren't available.

Prefer configuring a CA bundle as described in :ref:`corporate-certificates`.

***********************
 Network Configuration
***********************

``PIP_TIMEOUT``
===============

Sets the timeout in seconds for network operations. Increase this on slow or unreliable network connections. Defaults to
15 seconds.

.. code-block:: console

    $ export PIP_TIMEOUT=60
    $ python -m build

``PIP_RETRIES``
===============

Number of times pip retries failed downloads. Increase this on unstable network connections. Defaults to 5 retries.

.. code-block:: console

    $ export PIP_RETRIES=10
    $ python -m build

``PIP_NO_CACHE_DIR``
====================

Disables pip's package cache directory, forcing fresh downloads every time. Useful in CI environments to ensure
completely clean builds, though this makes builds slower.

.. code-block:: console

    $ export PIP_NO_CACHE_DIR=1
    $ python -m build

*********************
 Proxy Configuration
*********************

If you're behind a corporate proxy, configure these variables to route pip's network requests through your proxy server.
Both uppercase and lowercase versions are recognized.

See :ref:`corporate-proxies` for examples, including authentication and bypassing the proxy for internal hosts.

``HTTP_PROXY`` / ``http_proxy``
===============================

Proxy server for HTTP requests. Specify the proxy URL including protocol and port.

``HTTPS_PROXY`` / ``https_proxy``
=================================

Proxy server for HTTPS requests. Even though the proxy URL uses ``http://``, this handles HTTPS traffic.

``NO_PROXY`` / ``no_proxy``
===========================

Comma-separated list of hosts that should bypass the proxy. Use this for internal servers that don't require proxy
access.

***********************************
 SSL/TLS Certificate Configuration
***********************************

These variables configure SSL/TLS certificates for secure connections. Use them when your organization uses self-signed
certificates or custom certificate authorities.

``REQUESTS_CA_BUNDLE``
======================

Path to custom CA certificate bundle. The `requests <https://requests.readthedocs.io/>`_ library (which `pip
<https://pip.pypa.io/>`_ depends on) uses this variable. Alternative to ``PIP_CERT``.

See :ref:`corporate-certificates` for an example.

``SSL_CERT_FILE``
=================

Another variable for specifying a custom CA certificate bundle. Recognized by various Python libraries.

.. code-block:: console

    $ export SSL_CERT_FILE=/path/to/company-ca-bundle.crt
    $ python -m build

******************************
 Python Runtime Configuration
******************************

.. _python-warnings:

``PYTHONWARNINGS``
==================

Controls `Python's warning filters <https://docs.python.org/3/library/warnings.html>`_. Use this in CI to catch
deprecated features or potential issues in your build configuration by turning warnings into errors.

Catch deprecation warnings:

.. code-block:: console

    $ PYTHONWARNINGS=error::DeprecationWarning python -m build

Catch setuptools-specific warnings:

.. code-block:: console

    $ PYTHONWARNINGS=error:::setuptools.config.setupcfg python -m build

``PYTHONUTF8``
==============

Enables `UTF-8 mode <https://docs.python.org/3/library/os.html#utf-8-mode>`_ on Windows. Use this when building packages
with non-ASCII characters in filenames or file content on Windows systems.

.. code-block:: console

    $ set PYTHONUTF8=1
    $ python -m build

.. _temporary-directories:

``TMPDIR`` / ``TEMP`` / ``TMP``
===============================

These variables select the parent directory for temporary build environments and extracted sdists. Use them to place
temporary files on a filesystem with enough space or write permissions.

On Unix-like systems, use ``TMPDIR``:

.. code-block:: console

    $ TMPDIR=/path/to/custom/tmp python -m build

On Windows, use ``TEMP``:

.. code-block:: console

    $ set TEMP=C:\path\to\custom\tmp
    $ python -m build

build still creates a new subdirectory for each temporary environment or sdist extraction and removes it afterwards. For
retaining a failed build environment, see :ref:`inspect-build-environment`.

**Common use cases**:

- **Disk space**: Use a different filesystem with more available space
- **Performance**: Use faster storage (e.g., tmpfs on Linux)
- **Restricted filesystems**: Point at the writable part of a read-only container

For stable paths that work with compiler caches, see :ref:`compiler-caching`.

****************************
 Backend-Specific Variables
****************************

Each backend may define its own environment variables. See :ref:`backend-documentation` for links to backend guides.

*********************
 Non-Isolated Builds
*********************

When using ``--no-isolation``, build skips creating an `isolated environment
<https://packaging.python.org/en/latest/glossary/#term-Isolated-Build>`_. All environment variables from your current
shell pass directly to the build backend, including:

- ``PYTHONPATH`` - Affects Python module discovery
- ``PATH`` - Determines which executables are found
- Any custom variables your build backend uses

build does not set ``VIRTUAL_ENV`` in this mode. You're responsible for installing build dependencies yourself. See
:doc:`../how-to/basic-usage` for appropriate use cases.

**********************************
 Alternative: Configuration Files
**********************************

For persistent installer settings, see :ref:`pip-configuration`.

**********
 See also
**********

- :doc:`../how-to/corporate-environments` for proxy and SSL configuration
- :doc:`../how-to/troubleshooting` for common issues
- :doc:`cli` for command-line options
- `pip documentation <https://pip.pypa.io/en/stable/topics/configuration/>`_ for pip configuration
