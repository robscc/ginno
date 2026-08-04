"""Kept for entry-point compatibility; intentionally imports nothing at startup.

History: this module used to eagerly ``import pandas / openpyxl / docx / pptx /
pypdf`` so PyInstaller's static analysis would walk into them and bundle their
transitive native deps. The cost was several seconds of frozen startup (pandas
alone is heavy), paid on *every* launch even when the user never parses a doc.

The docs extras are now bundled via the explicit ``--collect-all <pkg>`` flags in
the Makefile (``make runtime``), and ``files/extractors.py`` imports each library
lazily inside its parse function. So nothing needs importing at startup anymore.
This module stays as a no-op so existing entry points that import it keep working.
"""

from __future__ import annotations

# Intentionally empty. Do not add heavy imports here — they run at every startup.
