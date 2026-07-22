"""Optional semantic retrieval (P4): local embeddings + cosine similarity.

This is **best-effort and fully degradable**. The `rag` optional extra provides
``sentence-transformers`` (local CPU embeddings — no network embedding endpoint
needed) and ``lancedb`` (an on-disk vector store under ``~/.ginno/vectorstore``).

Degradation ladder (nothing here ever raises into the caller):

* ``sentence-transformers`` missing  → semantic retrieval is OFF (``get_semantic_index``
  returns ``None``); the lexical retriever works unchanged.
* ``lancedb`` missing                → vectors live in memory only; they are
  re-encoded on the next build/reindex after a sidecar restart.
* model download / encode failure    → the index is simply not ``ready`` and the
  retriever falls back to lexical scoring for that query.

LanceDB is used as a *persistent cache* of the (expensive) embeddings — written
on build and read back via ``to_arrow()`` on a cold start so a restart does not
re-run the encoder. The actual similarity search is an in-memory dot product
over L2-normalised vectors (== cosine), which keeps scoring unambiguous and
independent of any ANN distance-metric quirks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import paths

if TYPE_CHECKING:
    from .types import KnowledgeConfig, WikiEntry

log = logging.getLogger(__name__)

# Multilingual + small; covers the Chinese-first use case and English alike.
DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ---- optional deps, probed once at import time ----
_ST: Any = None
_SEMANTIC_REASON = ""
try:
    from sentence_transformers import SentenceTransformer as _ST  # type: ignore
except Exception as e:  # noqa: BLE001
    _SEMANTIC_REASON = f"sentence-transformers 未安装（uv sync --extra rag 以启用语义检索）: {type(e).__name__}"

try:
    import lancedb as _lancedb  # type: ignore
except Exception:  # noqa: BLE001
    _lancedb = None


def semantic_available() -> tuple[bool, str]:
    """Whether embeddings can run at all (i.e. sentence-transformers present)."""
    return (_ST is not None, _SEMANTIC_REASON)


def _text_for(e: "WikiEntry") -> str:
    return " ".join([e.title or "", e.summary or "", " ".join(e.tags or [])]).strip()


def _table_name(vault_key: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", vault_key).strip("_") or "vault"
    return f"wiki_{safe}"[:80]


class SemanticIndex:
    """In-memory cosine index, optionally backed by a LanceDB table on disk."""

    def __init__(self, cfg: "KnowledgeConfig", embed_model: str = "") -> None:
        self._model_name = embed_model or DEFAULT_EMBED_MODEL
        self._model: Any = None
        self._vecs: dict[str, list[float]] = {}  # relative_path -> unit vector
        self._store_dir = paths.home() / "vectorstore"
        self._table_name = _table_name(str(Path(cfg.vault_path).expanduser().resolve()))
        self.ready = False

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if _ST is None:
            return False
        try:
            self._model = _ST(self._model_name)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("semantic: failed to load embedding model %r: %s", self._model_name, e)
            self._model = None
            return False

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not self._ensure_model() or not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return [v.tolist() for v in vecs]

    # ---- lance persistence (optional) ----
    def _open_table(self, create: bool = False) -> Any:
        if _lancedb is None:
            return None
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            db = _lancedb.connect(str(self._store_dir))
            names = getattr(db, "table_names", lambda: [])()
            if self._table_name in names:
                return db.open_table(self._table_name)
            return None if not create else None
        except Exception as e:  # noqa: BLE001
            log.debug("semantic: lance open failed: %s", e)
            return None

    def _load_from_lance(self, entries: list["WikiEntry"]) -> dict[str, list[float]]:
        """Restore vectors whose checksum still matches the current vault."""
        tbl = self._open_table()
        if tbl is None:
            return {}
        try:
            cols = tbl.to_arrow().to_pydict()
        except Exception as e:  # noqa: BLE001
            log.debug("semantic: lance read failed: %s", e)
            return {}
        by_path = {e.relative_path: e.checksum for e in entries}
        out: dict[str, list[float]] = {}
        for rel, cksum, vec in zip(
            cols.get("relative_path", []), cols.get("checksum", []), cols.get("vector", [])
        ):
            if rel in by_path and by_path[rel] == cksum and vec:
                out[rel] = list(vec)
        return out

    def _write_lance(self, entries: list["WikiEntry"]) -> None:
        if _lancedb is None:
            return
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            db = _lancedb.connect(str(self._store_dir))
            rows = [
                {"relative_path": e.relative_path, "checksum": e.checksum, "vector": self._vecs[e.relative_path]}
                for e in entries
                if e.relative_path in self._vecs
            ]
            if not rows:
                return
            db.create_table(self._table_name, rows, mode="overwrite")
        except Exception as e:  # noqa: BLE001
            log.debug("semantic: lance write failed: %s", e)

    # ---- build / restore ----
    def build_or_restore(self, entries: list["WikiEntry"]) -> None:
        if not self._ensure_model():
            self.ready = False
            return
        vecs = self._load_from_lance(entries)
        missing = [e for e in entries if e.relative_path not in vecs]
        if missing:
            new = self._encode([_text_for(e) for e in missing])
            for e, v in zip(missing, new):
                if v:
                    vecs[e.relative_path] = v
        # keep only current entries
        keep = {e.relative_path for e in entries}
        self._vecs = {k: v for k, v in vecs.items() if k in keep}
        self._write_lance(entries)
        self.ready = bool(self._vecs)

    # ---- query ----
    def scores(self, query: str) -> dict[str, float]:
        """Cosine similarity per relative_path; {} when not ready."""
        if not self.ready or not self._ensure_model():
            return {}
        qv = self._encode([query])
        if not qv:
            return {}
        q = qv[0]
        out: dict[str, float] = {}
        for rel, v in self._vecs.items():
            # vectors are L2-normalised → dot == cosine; clip negatives to 0
            s = sum(a * b for a, b in zip(q, v))
            if s > 0:
                out[rel] = s
        return out


_CACHE: dict[str, SemanticIndex] = {}


def _key(cfg: "KnowledgeConfig") -> str:
    return str(Path(cfg.vault_path).expanduser().resolve())


def get_semantic_index(
    cfg: "KnowledgeConfig", entries: list["WikiEntry"], *, build: bool = False
) -> SemanticIndex | None:
    """Return a ready semantic index, or None when semantic retrieval is off/unavailable.

    ``build=True`` (reindex/build endpoints) encodes any missing pages; the
    injection / search paths pass ``build=False`` so they never block on a full
    re-encode — if no cached index exists yet, they simply get None and the
    lexical retriever carries the query alone.
    """
    if not getattr(cfg, "use_semantic", False):
        return None
    if _ST is None:
        return None
    key = _key(cfg)
    si = _CACHE.get(key)
    if si is None:
        si = SemanticIndex(cfg, getattr(cfg, "embedding_model", ""))
        _CACHE[key] = si
    if build or not si.ready:
        try:
            si.build_or_restore(entries)
        except Exception as e:  # noqa: BLE001
            log.warning("semantic: build failed, falling back to lexical: %s", e)
    return si if si.ready else None


def reset_semantic() -> None:
    """Drop cached indices (e.g. after the vault path changes)."""
    _CACHE.clear()
