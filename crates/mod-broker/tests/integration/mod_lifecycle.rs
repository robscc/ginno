//! Mod lifecycle & chain-execution domain: handshake/ready schema surface,
//! chain order + matcher re-evaluation, next/skip/catch/timeout rules,
//! runner crash isolation, config reload & apply-config acks.

use super::common::{default_budgets, Env};
use serde_json::json;
use std::time::{Duration, Instant};

#[tokio::test(flavor = "multi_thread")]
async fn handshake_reports_loaded_mods_and_hooks() {
    needs_node!();
    let env = Env::new("handshake").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call" }, { "event": "turn.complete", "matcher": { "tool": "Write" } }] }));
    let ready = env
        .hello_with(json!({ "a": env.spec("a", json!({})) }), default_budgets())
        .await;
    let first = &ready["mods"][0];
    assert_eq!(first["name"], "a");
    assert_eq!(first["status"], "loaded");
    assert!(first["hooks"].as_str().unwrap().contains("tool.call"));
    assert!(first["hooks"]
        .as_str()
        .unwrap()
        .contains("turn.complete{tool=Write}"));
    // A runner that reports no defaults still yields an object, and no schema.
    assert_eq!(first["userConfig"], json!({}));
    assert!(first.get("configSchema").is_none());
}

// `runner.loaded` carries the mod's defaults + optional configSchema; both
// must reach the ready payload and `broker.status` (the settings/detail API).
#[tokio::test(flavor = "multi_thread")]
async fn ready_and_status_carry_user_config_and_schema() {
    needs_node!();
    let env = Env::new("config-schema").await;
    env.scenario(
        "a",
        json!({
            "userConfig": { "units": "metric", "limit": 3 },
            "configSchema": {
                "type": "object",
                "properties": {
                    "units": { "type": "string", "enum": ["metric", "imperial"], "default": "metric" },
                    "apiKey": { "type": "string", "default": "", "x-ginno-secret": true },
                },
            },
            "hooks": [{ "event": "tool.call" }],
        }),
    );
    let ready = env
        .hello_with(json!({ "a": env.spec("a", json!({})) }), default_budgets())
        .await;
    let first = &ready["mods"][0];
    assert_eq!(
        first["userConfig"],
        json!({ "units": "metric", "limit": 3 })
    );
    assert_eq!(
        first["configSchema"]["properties"]["units"]["enum"],
        json!(["metric", "imperial"])
    );
    assert_eq!(
        first["configSchema"]["properties"]["apiKey"]["x-ginno-secret"],
        true
    );

    let status = env.broker.status_payload().await;
    let entry = &status["mods"][0];
    assert_eq!(
        entry["userConfig"],
        json!({ "units": "metric", "limit": 3 })
    );
    assert_eq!(entry["configSchema"]["type"], "object");
}

// DSH chain.spec: "runs hooks outermost first and the core last, with
// matchers evaluated against the input each hook receives".
#[tokio::test(flavor = "multi_thread")]
async fn chain_order_and_matchers_reevaluate_per_hook() {
    needs_node!();
    let env = Env::new("chain-order").await;
    // DSH's "rewrites" spec case lives on prompt.submit: matchers re-evaluate
    // against the input each hook receives. (On tool.call this rewrite would
    // be refused — see tool_call_reroutes_are_refused.)
    env.scenario("a", json!({ "hooks": [{ "event": "prompt.submit", "behavior": "next", "ePatch": { "text": "trimmed" } }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "prompt.submit", "matcher": { "text": "  raw  " }, "behavior": "answer", "answer": { "seen": "b" } }] }));
    env.scenario("c", json!({ "hooks": [{ "event": "prompt.submit", "matcher": { "text": ["trimmed", "x"] }, "behavior": "answer", "answer": { "seen": "c" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})), "c": env.spec("c", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("prompt.submit", Some("s1"), json!({ "text": "  raw  " }))
        .await;
    // a rewrote to "trimmed"; b's matcher no longer matches the input it
    // would receive; c's does. The chain settles with c's answer.
    assert_eq!(settled, json!({ "seen": "c" }));
    env.wait_trace("a", "a:prompt.submit:begin").await;
    assert!(env.trace("c").contains("c:prompt.submit:begin"));
    assert!(
        !env.trace("b").contains("prompt.submit"),
        "b must be skipped once the input changed"
    );
}

// DSH chain.spec: "hands a second next call the first run" + eager answer.
#[tokio::test(flavor = "multi_thread")]
async fn next_runs_once_and_eager_answer_still_waits_for_beneath() {
    needs_node!();
    let env = Env::new("next-once").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "next_twice" }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "same": true, "beneath": { "ran": "b" } }));
    env.wait_trace("a", "same=true").await;
    // The beneath hook ran exactly once.
    assert_eq!(env.trace_count("b", "b:tool.call:begin"), 1);
}

// DSH chain.spec: skip rules — throw before next, fail after next, no result.
#[tokio::test(flavor = "multi_thread")]
async fn skip_rules_throw_after_next_and_no_result() {
    needs_node!();
    let env = Env::new("skip-rules").await;
    env.scenario(
        "throws",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "throw", "message": "boom" }] }),
    );
    env.scenario("late", json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after" }] }));
    env.scenario(
        "void",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "no_result" }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));

    // throw before next: the next hook runs in its place.
    let items = json!({ "throws": env.spec("throws", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env.runtime
        .wait_report_containing(
            "throws: tool.call hook skipped: threw boom",
            Duration::from_secs(10),
        )
        .await;

    // Fail after next resolved: the result from beneath is kept, nothing reruns.
    let env2 = Env::new("skip-rules-late").await;
    env2.scenario("late", json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after" }] }));
    env2.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "late": env2.spec("late", json!({})), "b": env2.spec("b", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    let settled = env2.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env2.runtime
        .wait_report_containing(
            "late: tool.call hook skipped: threw after",
            Duration::from_secs(10),
        )
        .await;
    assert_eq!(env2.trace_count("b", "b:tool.call:begin"), 1);

    // A hook settling with no result is skipped the same way.
    let env3 = Env::new("skip-rules-noresult").await;
    env3.scenario(
        "void",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "no_result" }] }),
    );
    env3.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "void": env3.spec("void", json!({})), "b": env3.spec("b", json!({})) });
    let _ready = env3.hello_with(items, default_budgets()).await;
    let settled = env3.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env3.runtime
        .wait_report_containing("returned no result", Duration::from_secs(10))
        .await;
}

// DSH chain.spec: "skips a hook whose own time runs past the budget".
#[tokio::test(flavor = "multi_thread")]
async fn timeout_skips_a_busy_hook() {
    needs_node!();
    let env = Env::new("timeout").await;
    env.scenario(
        "slow",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "sleep_next", "sleepMs": 900 }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "slow": env.spec("slow", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, json!({ "hookMs": 80 })).await;

    let start = Instant::now();
    let settled = env
        .runtime
        .raise("tool.call", Some("s1"), json!({ "tool": "Bash" }))
        .await;
    assert_eq!(settled, json!({ "ran": "b" }));
    // The chain skipped the slow hook at ~80 ms instead of waiting 900 ms.
    assert!(
        start.elapsed() < Duration::from_secs(3),
        "chain took {:?}",
        start.elapsed()
    );
    let line = env
        .runtime
        .wait_report_containing("ran past its 80 ms limit", Duration::from_secs(10))
        .await;
    assert!(
        line.contains("slow: tool.call hook skipped"),
        "unexpected report: {line}"
    );
}

// Design §15.1: `prompt.edit` hooks get the 50 ms budget; sleep counts as
// the hook's own time.
#[tokio::test(flavor = "multi_thread")]
async fn prompt_edit_budget_is_short_and_sleep_counts_as_busy() {
    needs_node!();
    let env = Env::new("prompt-edit").await;
    env.scenario(
        "slow",
        json!({ "hooks": [{ "event": "prompt.edit", "behavior": "sleep_next", "sleepMs": 900 }] }),
    );
    let items = json!({ "slow": env.spec("slow", json!({})) });
    let _ready = env
        .hello_with(items, json!({ "hookMs": 60_000, "promptEditMs": 60 }))
        .await;

    let start = Instant::now();
    let settled = env
        .runtime
        .raise("prompt.edit", Some("s1"), json!({ "text": "hi" }))
        .await;
    // The hook timed out; the event settles with the original input.
    assert_eq!(settled, json!({ "text": "hi" }));
    assert!(
        start.elapsed() < Duration::from_secs(3),
        "prompt.edit took {:?}",
        start.elapsed()
    );
    env.runtime
        .wait_report_containing("ran past its 60 ms limit", Duration::from_secs(10))
        .await;
}

// DSH chain.spec: .catch answers in the failed hook's place.
#[tokio::test(flavor = "multi_thread")]
async fn catch_handler_answers_in_place() {
    needs_node!();
    let env = Env::new("catch").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "throw", "message": "boom", "catch": "answer", "catchAnswer": { "deny": "failed closed" } }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "deny": "failed closed" }));
}

// DSH chain.spec: the handler shares the hook's beneath state — nothing
// beneath runs twice.
#[tokio::test(flavor = "multi_thread")]
async fn catch_handler_shares_the_beneath_run() {
    needs_node!();
    let env = Env::new("catch-beneath").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after", "catch": "next", "catchAnswer": { "patched": true } }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    // The handler got the already-resolved result from beneath and patched it.
    assert_eq!(settled, json!({ "patched": true }));
    assert_eq!(
        env.trace_count("b", "b:tool.call:begin"),
        1,
        "beneath must run exactly once"
    );
}

// Design §15.2 v2 addition: a runner crash fails its own hook; the chain
// continues.
#[tokio::test(flavor = "multi_thread")]
async fn runner_crash_fails_its_hook_and_the_chain_continues() {
    needs_node!();
    let env = Env::new("crash").await;
    env.scenario(
        "doomed",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "crash" }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "doomed": env.spec("doomed", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    let line = env
        .runtime
        .wait_report_containing("hook skipped", Duration::from_secs(10))
        .await;
    assert!(line.contains("doomed"), "unexpected report: {line}");
}

#[tokio::test(flavor = "multi_thread")]
async fn config_reload_unloads_disabled_mods() {
    needs_node!();
    let env = Env::new("reload").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "from": "a" } }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "from": "b" } }] }));
    let _ready = env
        .hello_with(
            json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) }),
            default_budgets(),
        )
        .await;

    // b goes away: its hooks stop, a's keep working. The apply is awaitable:
    // the ack reports the post-reload status of every desired mod.
    let ack = env
        .runtime
        .apply_config(&env.config(json!({ "a": env.spec("a", json!({})) }), default_budgets()))
        .await;
    assert_eq!(ack["ok"], true, "ack: {ack}");
    assert_eq!(ack["reloaded"], true, "ack: {ack}");
    assert_eq!(ack["mods"].as_array().unwrap().len(), 1);
    assert_eq!(ack["mods"][0]["name"], "a");
    assert_eq!(ack["mods"][0]["status"], "loaded");
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "from": "a" }));
    let status = env.broker.status_payload().await;
    let mods = status["mods"].as_array().unwrap();
    assert_eq!(mods.len(), 1, "b should be gone: {status}");
    assert_eq!(mods[0]["name"], "a");
}

#[tokio::test(flavor = "multi_thread")]
async fn apply_config_ack_reports_failures_and_noop_reloads() {
    needs_node!();
    let env = Env::new("apply-ack").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call" }] }));
    let config = env.config(json!({ "a": env.spec("a", json!({})) }), default_budgets());
    let _ready = env
        .hello_with(json!({ "a": env.spec("a", json!({})) }), default_budgets())
        .await;

    // An identical push reloads nothing but still reports the loaded status.
    let ack = env.runtime.apply_config(&config).await;
    assert_eq!(ack["ok"], true, "ack: {ack}");
    assert_eq!(ack["reloaded"], false, "ack: {ack}");
    assert_eq!(ack["mods"][0]["status"], "loaded");

    // Python's push_config wraps the config as {role, config}; the same ack
    // must come back from the envelope shape.
    let id = env.runtime.call_broker(
        "apply-config",
        json!({ "role": "runtime", "config": config }),
    );
    let envelope_ack = env
        .runtime
        .wait_result(id, "envelope apply-config", Duration::from_secs(120))
        .await
        .expect("envelope apply-config reply");
    assert_eq!(envelope_ack["ok"], true, "ack: {envelope_ack}");
    assert_eq!(envelope_ack["reloaded"], false, "ack: {envelope_ack}");

    // A new mod that cannot be spawned fails fast: ok=false with its error.
    // (`a` is untouched — nodePath is not part of its ModSpec — so it stays up.)
    let broken = env.dir.join("mods").join("broken");
    std::fs::create_dir_all(&broken).unwrap();
    let mut config = env.config(
        json!({
            "a": env.spec("a", json!({})),
            "broken": { "enabled": true, "dir": broken, "grants": {}, "config": {} },
        }),
        default_budgets(),
    );
    config["nodePath"] = json!("/definitely/not/a/node");
    let ack = env.runtime.apply_config(&config).await;
    assert_eq!(ack["ok"], false, "ack: {ack}");
    assert_eq!(ack["reloaded"], true, "ack: {ack}");
    let broken_entry = ack["mods"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["name"] == "broken")
        .expect("broken mod in ack");
    assert_eq!(broken_entry["status"], "error");
    assert!(
        broken_entry["error"]
            .as_str()
            .is_some_and(|e| e.contains("cannot spawn node")),
        "ack: {ack}"
    );
}
