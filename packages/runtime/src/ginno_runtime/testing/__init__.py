"""Test-only helpers for the Ginno runtime.

This subpackage is a leaf: it imports only ``langchain_core`` and the standard
library, so it can be imported from ``models.py`` (for the ``GINNO_FAKE_LLM``
seam) and from the test suite without creating an import cycle.
"""
