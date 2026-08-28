"""Billing fetch entries: deterministic bill pull + normalization.

Both entries shell out to the user's billing skill CLIs (same mechanism the
agents use — login shell so credential exports from ~/.zshrc are present;
JSON on stdout, progress on stderr). No cloud SDKs in the sidecar venv.

Normalized record (canonical shape, see :mod:`compare`)::

    {"provider", "cycle", "date", "sku", "billing_item",
     "usage_tokens", "usage_raw", "original", "discount", "payable",
     "discount_rate"}
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

_CLI_TIMEOUT_S = 120

# Where the billing skill scripts may live (ginno home first, then the user's
# Claude Code skills — volcengine-bill is commonly only installed there).
_SKILL_ROOTS = [
    Path(os.environ.get("GINNO_HOME", Path.home() / ".ginno")) / "skills",
    Path.home() / ".claude" / "skills",
]


def _find_skill_script(skill: str, script: str) -> Path | None:
    for root in _SKILL_ROOTS:
        p = root / skill / script
        if p.exists():
            return p
    return None


def _run_cli(script: Path, argv: list[str], timeout: int) -> dict:
    """Run the skill CLI under the user's login shell (credential exports)."""
    user_shell = os.environ.get("SHELL") or "/bin/sh"
    cmd = " ".join([shlex.quote("python3"), shlex.quote(str(script)), *argv])
    r = subprocess.run(
        [user_shell, "-lc", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{script.name} {' '.join(argv[:2])} failed: {r.stderr.strip()[:300]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{script.name} returned non-JSON: {r.stdout.strip()[:200]}"
        ) from None


_NUM = r"([0-9]+(?:\.[0-9]+)?)"
_UNIT_MULT = [
    (re.compile(r"百万\s*tokens|mtokens|m\s*tokens", re.I), 1_000_000),
    (re.compile(r"千\s*tokens|ktokens|k\s*tokens", re.I), 1_000),
    (re.compile(r"tokens?", re.I), 1),
]


def _usage_to_tokens(usage: str | None, unit: str | None) -> float | None:
    """Parse bill usage strings like ``123.4 千tokens`` into raw token counts."""
    raw = f"{usage or ''} {unit or ''}".strip()
    m = re.search(_NUM, raw)
    if not m:
        return None
    val = float(m.group(1))
    for rx, mult in _UNIT_MULT:
        if rx.search(raw):
            return val * mult
    return None


def _f(*vals) -> float:
    for v in vals:
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _months_between(start: str, end: str) -> list[str]:
    """YYYY-MM-DD (or YYYY-MM) bounds → inclusive YYYY-MM list."""
    def ym(s: str) -> tuple[int, int]:
        p = (s or "").split("-")
        return int(p[0]), int(p[1])
    y0, m0 = ym(start)
    y1, m1 = ym(end or start)
    out: list[str] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1) and len(out) < 24:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _norm_record(provider: str, cycle: str, date: str, sku: str, item_name: str,
                 usage_tokens: float | None, usage_raw: str,
                 original: float, discount: float, payable: float) -> dict:
    return {
        "provider": provider,
        "cycle": cycle,
        "date": date or "",
        "sku": sku or "",
        "billing_item": item_name or "",
        "usage_tokens": usage_tokens,
        "usage_raw": usage_raw or "",
        "original": round(original, 6),
        "discount": round(discount, 6),
        "payable": round(payable, 6),
        "discount_rate": round(discount / original, 4) if original else None,
    }


def fetch_aliyun_bills(args: dict) -> dict:
    """Pull Aliyun instance bills per cycle in [start_date, end_date] and keep
    the line items related to ``model_name`` (product/spec/name match)."""
    script = _find_skill_script("aliyun-bill", "aliyun_bill.py")
    if script is None:
        raise RuntimeError(
            "aliyun-bill skill script not found "
            "(looked in ~/.ginno/skills and ~/.claude/skills)"
        )
    model = (args.get("model_name") or "").strip().lower()
    timeout = int(args.get("cli_timeout") or _CLI_TIMEOUT_S)
    records: list[dict] = []
    for cycle in _months_between(args.get("start_date") or "", args.get("end_date") or ""):
        data = _run_cli(script, ["instance", cycle, "--json"], timeout)
        if not data.get("Success"):
            raise RuntimeError(f"aliyun instance {cycle}: {data.get('Message', 'query failed')}")
        for i in (data.get("Data", {}) or {}).get("Items", []) or []:
            hay = " ".join(
                str(i.get(k) or "")
                for k in ("ProductName", "ProductCode", "InstanceSpec", "NickName")
            ).lower()
            if model and model not in hay:
                continue
            original = _f(i.get("PretaxGrossAmount"), i.get("PretaxAmount"))
            discount = _f(i.get("InvoiceDiscount"), i.get("DiscountAmount"))
            payable = _f(i.get("PaymentAmount"), i.get("PretaxAmount"))
            usage_raw = f"{i.get('Usage') or ''} {i.get('UsageUnit') or ''}".strip()
            records.append(
                _norm_record(
                    "aliyun", cycle, str(i.get("BillingDate") or ""),
                    str(i.get("InstanceSpec") or i.get("InstanceID") or ""),
                    str(i.get("BillingItem") or i.get("ProductName") or ""),
                    _usage_to_tokens(str(i.get("Usage") or ""), str(i.get("UsageUnit") or "")),
                    usage_raw, original, discount, payable,
                )
            )
    return {"aliyun_records": records}


def fetch_volc_bills(args: dict) -> dict:
    """Pull Volcano Engine bills for ``volc_billing_period``.

    Prefers a local export when ``local_export`` points at an existing JSON
    file ({"list": [...]} or a bare list); otherwise shells out to the
    volcengine-bill skill's ``daily`` (amortized) query per cycle.
    """
    model = (args.get("model_name") or "").strip().lower()
    timeout = int(args.get("cli_timeout") or _CLI_TIMEOUT_S)
    items_by_cycle: dict[str, list[dict]] = {}

    local = args.get("local_export") or ""
    if local and Path(local).exists():
        data = json.loads(Path(local).read_text(encoding="utf-8"))
        items = data.get("list") if isinstance(data, dict) else data
        cycle = (args.get("volc_billing_period") or "local")
        items_by_cycle[cycle] = items or []
    else:
        script = _find_skill_script("volcengine-bill", "volcengine_bill.py")
        if script is None:
            raise RuntimeError(
                "volcengine-bill skill script not found and no local_export given "
                "(looked in ~/.ginno/skills and ~/.claude/skills)"
            )
        periods = (
            [args["volc_billing_period"]]
            if args.get("volc_billing_period")
            else _months_between(args.get("start_date") or "", args.get("end_date") or "")
        )
        for cycle in periods:
            data = _run_cli(
                script,
                ["daily", cycle, "--product", args.get("model_name") or "", "--json"],
                timeout,
            )
            items_by_cycle[cycle] = data.get("list", []) or []

    records: list[dict] = []
    for cycle, items in items_by_cycle.items():
        for i in items:
            hay = " ".join(
                str(i.get(k) or "") for k in ("product_zh", "product", "spec", "instance_name")
            ).lower()
            if model and model not in hay:
                continue
            original = _f(i.get("original_bill_amount"))
            discount = _f(i.get("discount_bill_amount"))
            payable = _f(i.get("payable_amount"))
            usage = i.get("usage") or i.get("usage_amount") or ""
            usage_raw = f"{usage} {i.get('usage_unit') or ''}".strip()
            records.append(
                _norm_record(
                    "volc", cycle, str(i.get("amortized_day") or i.get("expense_date") or ""),
                    str(i.get("spec") or i.get("instance_name") or ""),
                    str(i.get("billing_item") or i.get("product_zh") or ""),
                    _usage_to_tokens(
                        str(i.get("usage") or i.get("usage_amount") or ""),
                        str(i.get("usage_unit") or ""),
                    ),
                    usage_raw, original, discount, payable,
                )
            )
    return {"volc_records": records}
