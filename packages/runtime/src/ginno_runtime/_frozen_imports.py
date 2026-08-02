"""Force-bundle the optional ``docs`` dependencies for frozen (PyInstaller) builds.

``files/extractors.py`` imports pandas / python-docx / python-pptx / pypdf
*lazily* (inside functions) so dev startup stays light and the runtime degrades
gracefully without the ``docs`` extra. The downside: PyInstaller's static
analysis never sees those imports, so a frozen binary would ship without them
and every parse would raise ``ExtractorUnavailable``.

This module imports them eagerly at the PyInstaller entry point so the analysis
walks into each library and collects its transitive native deps (pandas._libs,
lxml, Pillow, python_calamine's Rust extension, …). Guarded by try/except so a
build env without the extra still imports cleanly (the frozen build always has
them installed via ``--extra docs``).
"""

from __future__ import annotations

try:  # spreadsheets / tables
    import pandas  # noqa: F401
    import openpyxl  # noqa: F401
    import python_calamine  # noqa: F401
except ImportError:  # pragma: no cover - only when extra not installed
    pass

try:  # word
    import docx  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:  # powerpoint (pulls lxml + Pillow)
    import pptx  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:  # pdf
    import pypdf  # noqa: F401
except ImportError:  # pragma: no cover
    pass
