"""Deterministic entry functions for ``python`` workflow nodes.

A ``python`` node's DSL ``entry`` must name a key of :data:`ENTRY_REGISTRY`
(whitelist — no arbitrary code). Entries are plain sync callables
``fn(args: dict) -> dict`` returning the node's ``writes`` payload; the node
runs them in a worker thread, so entries may block (subprocess, CPU) but must
be deterministic (no LLM).

The billing entries shell out to the user's billing skill CLIs — the same
mechanism the agents use — so the sidecar venv needs no cloud SDKs.
Normalized records share one canonical shape (see :mod:`billing`).
"""

from __future__ import annotations

from .billing import fetch_aliyun_bills, fetch_volc_bills
from .compare import normalize_and_compare

ENTRY_REGISTRY: dict = {
    "fetch_aliyun_bills": fetch_aliyun_bills,
    "fetch_volc_bills": fetch_volc_bills,
    "normalize_and_compare": normalize_and_compare,
}

__all__ = ["ENTRY_REGISTRY", "fetch_aliyun_bills", "fetch_volc_bills", "normalize_and_compare"]
