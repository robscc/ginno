"""Model pricing table and cost estimation (V2 P1-1).

Ginno records tokens but has never shown money. Rates are user-configurable
(prices change monthly and differ per gateway), with a ``default`` entry used
for any model without its own row. Lookup order:

1. ``provider/model`` exact key
2. ``model`` exact key
3. ``default``

Units are currency per **1M tokens**. An empty table yields 0 cost — the UI
then shows token counts only, never a fabricated number.

Peak / off-peak (economy): a row may carry discounted rates for a daily
window, which is how several gateways price overnight usage:

```json
"pricing": {
  "deepseek-flash": {
    "input": 2.0, "output": 8.0, "cache_read": 0.2,
    "off_peak": {"input": 1.0, "output": 4.0, "cache_read": 0.1},
    "off_peak_windows": [["00:30", "08:30"]],
    "timezone": "Asia/Shanghai"
  }
}
```

Windows are local wall-clock ``HH:MM`` pairs in ``timezone`` (default: the
machine's zone) and may wrap midnight. The rate is chosen per usage record
from its own timestamp, so a day that spans the window is priced correctly.
"""

from __future__ import annotations

import logging
import time

from ginno_runtime import jsonx as json

from . import paths

_log = logging.getLogger("ginno.pricing")

_CACHE: dict = {"at": 0.0, "table": {}}
_TTL_S = 30.0
_FIELDS = ("input", "output", "cache_read", "cache_write")


def _read_table() -> dict:
    try:
        data = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    table = data.get("pricing")
    return table if isinstance(table, dict) else {}


def _rates(row: dict) -> dict[str, float]:
    clean = {}
    for f in _FIELDS:
        try:
            clean[f] = float(row.get(f) or 0)
        except (TypeError, ValueError):
            clean[f] = 0.0
    return clean


def _normalized(table: dict) -> dict:
    out: dict[str, dict] = {}
    for key, row in (table or {}).items():
        if not isinstance(row, dict):
            continue
        clean = _rates(row)
        off = row.get("off_peak")
        windows = row.get("off_peak_windows")
        if (
            (any(clean.values()) or isinstance(off, dict))
            and isinstance(off, dict)
            and isinstance(windows, list)
        ):
            clean["off_peak"] = _rates(off)  # type: ignore[assignment]
            clean["off_peak_windows"] = [  # type: ignore[assignment]
                w
                for w in windows
                if isinstance(w, (list, tuple)) and len(w) == 2
            ]
            clean["timezone"] = str(row.get("timezone") or "")  # type: ignore[assignment]
        if any(clean.get(f) for f in _FIELDS) or clean.get("off_peak"):
            out[str(key)] = clean
    return out


def table() -> dict:
    now = time.time()
    if now - _CACHE["at"] >= _TTL_S:
        _CACHE.update({"at": now, "table": _normalized(_read_table())})
    return _CACHE["table"]


def _row_for(provider: str | None, model: str | None) -> dict | None:
    t = table()
    if not t:
        return None
    pm = f"{provider or '?'}/{model or '?'}"
    for key in (pm, str(model or ""), "default"):
        if key and key in t:
            return t[key]
    return None


def _in_window(ts: float, windows: list, tz_name: str) -> bool:
    """True when ``ts`` falls in any [start, end) local window (may wrap)."""
    import datetime as _dt

    try:
        if tz_name:
            from zoneinfo import ZoneInfo

            dt = _dt.datetime.fromtimestamp(ts, ZoneInfo(tz_name))
        else:
            dt = _dt.datetime.fromtimestamp(ts)
    except Exception:  # noqa: BLE001 — bad tz: fall back to local time
        dt = _dt.datetime.fromtimestamp(ts)
    minutes = dt.hour * 60 + dt.minute
    for pair in windows or []:
        try:
            sh, sm = str(pair[0]).split(":")
            eh, em = str(pair[1]).split(":")
            start = int(sh) * 60 + int(sm)
            end = int(eh) * 60 + int(em)
        except (ValueError, TypeError):
            continue
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # wraps midnight
            return True
    return False


def rates_for(
    provider: str | None, model: str | None, at: float | None = None
) -> dict[str, float] | None:
    """Rates for this call — off-peak discount applied when ``at`` is inside
    a configured window."""
    row = _row_for(provider, model)
    if not row:
        return None
    base = {f: float(row.get(f) or 0) for f in _FIELDS}
    off = row.get("off_peak")
    if isinstance(off, dict) and at and _in_window(
        float(at), row.get("off_peak_windows") or [], str(row.get("timezone") or "")
    ):
        return {f: float(off.get(f) or base[f]) for f in _FIELDS}
    return base


def cost_of(entry: dict) -> tuple[float, float]:
    """(estimated cost, cache saving) for one usage record.

    Cache saving = what the cached input would have cost at the full input
    rate minus what it did cost at the cache-read rate.
    """
    r = rates_for(entry.get("provider"), entry.get("model"), entry.get("ts"))
    if not r:
        return 0.0, 0.0
    inp = int(entry.get("input_tokens") or 0)
    out = int(entry.get("output_tokens") or 0)
    # input_tokens is the GROSS prompt (cache hit included) in this store.
    cached = int(entry.get("cache_read_tokens") or 0)
    net_in = max(0, inp - cached)
    cost = (net_in * r["input"] + out * r["output"] + cached * r["cache_read"]) / 1_000_000
    saving = max(0.0, (cached * (r["input"] - r["cache_read"]))) / 1_000_000
    return round(cost, 6), round(saving, 6)


def configured() -> bool:
    return bool(table())
