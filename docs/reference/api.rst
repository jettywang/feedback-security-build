###################
 API Documentation
###################

build provides a Python API for programmatic use in build tools, CI systems, and automation scripts.

.. note::

    When not using an isolated environment, the build backend must be installed in the current environment. When using
    :class:`DefaultIsolatedEnv <build.env.DefaultIsolatedEnv>`, you must explicitly install build dependencies into the
    isolated environment via :meth:`~build.env.DefaultIsolatedEnv.install` before calling
    :meth:`~build.ProjectBuilder.build` — the environment is created empty.

*************
 Basic Usage
*************

Building a package programmatically:

.. code-block:: python

    from build import ProjectBuilder

    builder = ProjectBuilder("path/to/project")
    builder.build("wheel", "dist/")

Building both sdist and wheel:

.. code-block:: python

    from build import ProjectBuilder

    builder = ProjectBuilder(".")
    builder.build("sdist", "dist/")
    builder.build("wheel", "dist/")

Using an isolated environment:

``DefaultIsolatedEnv`` creates a clean virtual environment, but it is initially empty. You must install the project's
build dependencies before invoking the backend:

.. code-block:: python

    from build import ProjectBuilder
    from build.env import DefaultIsolatedEnv

    with DefaultIsolatedEnv() as env:
        builder = ProjectBuilder.from_isolated_env(env, ".")
        # Install build-system.requires (e.g., flit-core, setuptools, ...)
        env.install(builder.build_system_requires)
        # Install additional backend dependencies for the target distribution
        env.install(builder.get_requires_for_build("wheel"))
        builder.build("wheel", "dist/")

Pass ``path`` to ``DefaultIsolatedEnv`` to use a fixed location. The directory must be empty; it is removed when the
``with`` block exits cleanly and kept when the ``with`` block raises.

Controlling the hook environment:

By default, build hooks inherit the full environment of the calling process. Callers that hold variables the backend
must not see (for example, scheduler credentials in a build worker) can pass ``inherit_env`` to opt into a minimal
environment instead. Hooks then receive only the variables required to launch the interpreter and operate the
isolated environment, the caller's variables named in ``inherit_env``, and any explicit values given via
``extra_environ``:

.. code-block:: python

    with DefaultIsolatedEnv(inherit_env=["APP_CHANNEL"], extra_environ={"APP_MODE": "release"}) as env:
        builder = ProjectBuilder.from_isolated_env(env, ".")
        ...

``ProjectBuilder`` on its own uses the current environment. The ``runner`` argument customizes subprocess execution.

Getting package metadata:

:meth:`~build.ProjectBuilder.metadata_path` may build a wheel if the backend has no metadata hook. See
:ref:`read-wheel-metadata` for the fallback behavior and the equivalent CLI commands.

.. code-block:: python

    from build import ProjectBuilder
    import tempfile

    builder = ProjectBuilder(".")
    with tempfile.TemporaryDirectory() as tmpdir:
        metadata_dir = builder.metadata_path(tmpdir)
        # Read METADATA file from metadata_dir to extract package info

Accessing build dependencies:

.. code-block:: python

    from build import ProjectBuilder

    builder = ProjectBuilder(".")
    requires = builder.build_system_requires
    print(f"Build requires: {requires}")

    # Get additional dependencies for building a wheel
    wheel_requires = builder.get_requires_for_build("wheel")
    print(f"Wheel build requires: {wheel_requires}")

Handling errors:

.. code-block:: python

    from build import ProjectBuilder
    from build import BuildException

    try:
        builder = ProjectBuilder(".")
        builder.build("wheel", "dist/")
    except BuildException as e:
        print(f"Build failed: {e}")

***********
 Reference
***********

******************
 ``build`` module
******************

.. automodule:: build
    :members:
    :undoc-members:
    :show-inheritance:

**********************
 ``build.env`` module
**********************

.. automodule:: build.env
    :members:
    :undoc-members:
    :show-inheritance:

***********************
 ``build.util`` module
***********************

.. automodule:: build.util
    :members:
    :undoc-members:
    :show-inheritance:

*******
 Types
*******

.. autoclass:: build._types.SubprocessRunner()
    :special-members: __call__
