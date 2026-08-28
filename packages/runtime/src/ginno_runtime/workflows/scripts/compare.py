"""Pure-compute entries: normalize provider records and compare real prices.

Canonical record shape (produced by :mod:`billing` fetch entries)::

    {"provider", "cycle", "date", "sku", "billing_item",
     "usage_tokens" (float | None), "usage_raw" (str),
     "original", "discount", "payable", "discount_rate" (float | None)}

All amounts are CNY. ``normalize_and_compare`` is 100% deterministic — the
ideal ``python`` node: no I/O, trivially unit-testable.
"""

from __future__ import annotations

from typing import Any


def _sum(records: list[dict], key: str) -> float:
    return sum(float(r.get(key) or 0) for r in records)


def _provider_stats(records: list[dict], list_prices: dict, provider: str) -> dict:
    """Aggregate one provider's records into comparable stats.

    Effective unit price (per 1K tokens) prefers bill-reported usage tokens;
    when the bill carries no token usage (common), fall back to estimating
    tokens from the list price table (payable / list unit price) — noted in
    ``caveats`` so the report writer can phrase it honestly.
    """
    payable = _sum(records, "payable")
    original = _sum(records, "original")
    discount = _sum(records, "discount")
    tokens = _sum(records, "usage_tokens")
    caveats: list[str] = []
    estimated_tokens = False
    if not tokens:
        unit = (list_prices.get(provider) or {}).get("per_1k_tokens") or {}
        blended = float(unit.get("blended") or 0)
        if blended > 0 and payable > 0:
            tokens = payable / blended * 1000
            estimated_tokens = True
            caveats.append(f"{provider}: 账单无 token 用量，按刊例混合单价估算 tokens")
    price_per_1k = (payable / tokens * 1000) if tokens else None
    discount_rate = (discount / original) if original else None
    return {
        "provider": provider,
        "records": len(records),
        "payable": round(payable, 4),
        "original": round(original, 4),
        "discount": round(discount, 4),
        "discount_rate": round(discount_rate, 4) if discount_rate is not None else None,
        "usage_tokens": round(tokens, 2) if tokens else None,
        "usage_estimated": estimated_tokens,
        "effective_per_1k_tokens": round(price_per_1k, 6) if price_per_1k else None,
        "caveats": caveats,
    }


def normalize_and_compare(args: dict) -> dict:
    """Combine both providers' records + list prices into a comparison summary.

    Args (rendered from workflow context): ``aliyun_records``, ``volc_records``,
    ``list_prices`` (``{aliyun: {per_1k_tokens: {blended, input, output, ...}},
    volc: {...}}``), ``model_name``.
    """
    model_name = str(args.get("model_name") or "")
    aliyun_records = list(args.get("aliyun_records") or [])
    volc_records = list(args.get("volc_records") or [])
    list_prices = dict(args.get("list_prices") or {})
    if not aliyun_records and not volc_records:
        raise ValueError("normalize_and_compare: both aliyun_records and volc_records are empty")

    stats = {
        "aliyun": _provider_stats(aliyun_records, list_prices, "aliyun"),
        "volc": _provider_stats(volc_records, list_prices, "volc"),
    }
    caveats: list[str] = []
    for s in stats.values():
        caveats.extend(s["caveats"])

    priced = [s for s in stats.values() if s["effective_per_1k_tokens"]]
    winner = None
    if len(priced) == 2:
        winner = min(priced, key=lambda s: s["effective_per_1k_tokens"])["provider"]
    elif len(priced) == 1:
        winner = priced[0]["provider"]
        caveats.append("仅一方有有效单价，胜负按可用数据判定")
    else:
        # No token basis at all — compare total payable as a last resort.
        winner = min(stats.values(), key=lambda s: s["payable"])["provider"]
        caveats.append("双方均无 token 用量/刊例，退化为比较应付总额")

    # Same-workload cost: what each provider would charge for the COMBINED
    # observed token volume (only meaningful where a unit price exists).
    total_tokens = (stats["aliyun"]["usage_tokens"] or 0) + (stats["volc"]["usage_tokens"] or 0)
    same_workload: dict[str, Any] = {}
    for s in stats.values():
        if s["effective_per_1k_tokens"] and total_tokens:
            same_workload[s["provider"]] = round(
                total_tokens * s["effective_per_1k_tokens"] / 1000, 4
            )

    usage_mix = {}
    if total_tokens:
        usage_mix = {
            p: round((s["usage_tokens"] or 0) / total_tokens, 4) for p, s in stats.items()
        }

    summary = {
        "model_name": model_name,
        "providers": stats,
        "total_tokens": round(total_tokens, 2) if total_tokens else None,
        "usage_mix": usage_mix,
        "same_workload_cost": same_workload,
        "cheaper": winner,
        "caveats": sorted(set(caveats)),
    }
    return {"comparison_summary": summary}
