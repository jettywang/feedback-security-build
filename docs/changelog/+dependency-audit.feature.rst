Add ``--audit-dependencies PATH`` and ``ProjectBuilder.audit_dependencies``: a machine-readable JSON report of a
distribution's build dependencies, split into the project-declared stage (``build-system.requires``) and the
backend-added stage (which requires running the backend's ``get_requires_for_build_<distribution>`` hook). The audit
never installs anything; invalid requirement strings and duplicate requests are reported instead of raising.
