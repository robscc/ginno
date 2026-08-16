"""Batch-replay saved synthesis cases against the CURRENT prompt (P4, stability plan).

Reads each case's stored trace from ~/.ginno/synthesis, re-runs the synthesis
loop with today's ``_SYNTHESIZE_PROMPT`` (real provider calls!), and prints a
side-by-side comparison with the stored result: ok-rate, attempts, fail_stage
distribution, doctor-error counts and latency. Run it before AND after a prompt
change to A/B against real historical traces (quality-plan §3.4, "replay as the
minimal eval engine").

Usage (from packages/runtime):
    uv run python scripts/synth_replay_batch.py [--limit 20] [--provider anthropic]
"""

from __future__ import annotations

import argparse
import asyncio


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--provider", default=None, help="provider id (default: configured default)")
    args = ap.parse_args()

    from ginno_runtime import providers as prov_mod
    from ginno_runtime.api import workflows as wf_api
    from ginno_runtime.models import build_model
    from ginno_runtime.workflows import synthesis as wf_synth

    provider = args.provider or prov_mod.get_default_provider()
    model = build_model(provider)

    rows: list[dict] = []
    for c in wf_synth.list_cases(limit=args.limit):
        case = wf_synth.load_case(c["synthesis_id"])
        trace = ((case or {}).get("input") or {}).get("trace") or ""
        if not trace:
            continue
        stored = (case or {}).get("output") or {}
        res = await wf_api._run_synthesis(trace, model, None)
        rows.append({
            "id": c["synthesis_id"],
            "stored_ok": stored.get("status") == "ok",
            "stored_stage": stored.get("fail_stage"),
            "stored_version": ((case or {}).get("input") or {}).get("prompt_version"),
            "replay_ok": res["ok"],
            "replay_stage": res["fail_stage"],
            "replay_attempts": res["attempts_used"],
            "replay_ms": res["total_ms"],
        })
        r = rows[-1]
        stored_label = "ok" if r["stored_ok"] else r["stored_stage"]
        replay_label = "ok" if res["ok"] else res["fail_stage"]
        print(
            f"{r['id']}  stored={stored_label}  replay={replay_label}"
            f"  attempts={res['attempts_used']}  {res['total_ms']}ms"
        )

    n = len(rows)
    if not n:
        print("no replayable cases found")
        return
    stored_l1 = sum(1 for r in rows if r["stored_ok"])
    replay_l1 = sum(1 for r in rows if r["replay_ok"])
    stages: dict[str, int] = {}
    for r in rows:
        if not r["replay_ok"] and r["replay_stage"]:
            stages[r["replay_stage"]] = stages.get(r["replay_stage"], 0) + 1
    print("-" * 60)
    print(f"cases={n}  stored L1={stored_l1}/{n}  replay L1={replay_l1}/{n} "
          f"(prompt={wf_api.SYNTH_PROMPT_VERSION})")
    print("replay fail stages:", stages or "none")


if __name__ == "__main__":
    asyncio.run(main())
