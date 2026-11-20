//! Tool & op relay domain: tool.call reroute/args-rewrite/result-takeover
//! contract, tool.check decisions, tool name aliases, `$` op calls through
//! earlier mods, and the store op roundtrip/cap.

use super::common::{default_budgets, Env};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

// Design §15.5 (P0 rule, still true in P1): a tool.call reroute — swapping
// the tool itself — is refused; the chain continues with the original input.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_reroutes_are_refused() {
    needs_node!();
    let env = Env::new("reroute").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "next", "ePatch": { "tool": "Edit" } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise(
            "tool.call",
            Some("s1"),
            json!({ "tool": "bash", "args": { "command": "ls" } }),
        )
        .await;
    assert_eq!(
        settled,
        json!({ "tool": "bash", "args": { "command": "ls" } }),
        "the logged call runs as logged (host spelling)"
    );
    let line = env
        .runtime
        .wait_report_containing(
            "rerouted the call from Bash to Edit",
            Duration::from_secs(10),
        )
        .await;
    assert!(
        line.contains("a: tool.call hook skipped"),
        "unexpected report: {line}"
    );
}

// Design §15.5 (P1): an args rewrite now passes through — the rewritten args
// run beneath unchanged, and the event settles with them so the engine
// re-walks the permission policy.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_args_rewrites_pass_through() {
    needs_node!();
    let env = Env::new("args-rewrite").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "next", "ePatch": { "args": { "command": "ls -la" } } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise(
            "tool.call",
            Some("s1"),
            json!({ "tool": "bash", "args": { "command": "ls" } }),
        )
        .await;
    assert_eq!(
        settled,
        json!({ "tool": "bash", "args": { "command": "ls -la" } })
    );
    assert!(env.trace("a").contains("a:tool.call:begin"));
    // No refusal report: the rewrite is served, not skipped.
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(500) {
        assert!(
            env.runtime
                .take(|frame| frame.get("method").and_then(Value::as_str) == Some("mod.report"))
                .is_none(),
            "an args rewrite must not report a skip"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

// Design §15.5 (P1): a `{result}` answer takes the tool.call event over — the
// tool does not execute and the object rides back to the runtime as-is.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_result_takeover_reaches_the_runtime() {
    needs_node!();
    let env = Env::new("takeover").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": "blocked by policy", "isError": true } } }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise(
            "tool.call",
            Some("s1"),
            json!({ "tool": "bash", "args": {} }),
        )
        .await;
    assert_eq!(
        settled,
        json!({ "result": { "value": "blocked by policy", "isError": true } })
    );
}

// Design §15.5 (P1): an invalid `{result}` shape is a chain error — the hook
// skips with a report and the chain continues from its input; a non-object
// `{args}` rewrite fails the same way.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_invalid_answer_shapes_skip_the_hook() {
    needs_node!();
    let env = Env::new("bad-shapes").await;
    env.scenario("badresult", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": "text, not an object" } }] }));
    env.scenario("nullvalue", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": null } } }] }));
    env.scenario("badiserror", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": "x", "isError": "yes" } } }] }));
    env.scenario("badargs", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "args": "not an object" } }] }));
    let items = json!({
        "badresult": env.spec("badresult", json!({})),
        "nullvalue": env.spec("nullvalue", json!({})),
        "badiserror": env.spec("badiserror", json!({})),
        "badargs": env.spec("badargs", json!({})),
    });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise(
            "tool.call",
            Some("s1"),
            json!({ "tool": "bash", "args": { "command": "ls" } }),
        )
        .await;
    // Every hook skipped: the event settles with the original input.
    assert_eq!(
        settled,
        json!({ "tool": "bash", "args": { "command": "ls" } })
    );
    env.runtime
        .wait_report_containing(
            "badresult: tool.call hook skipped: answered with an invalid result takeover",
            Duration::from_secs(10),
        )
        .await;
    env.runtime
        .wait_report_containing(
            "nullvalue: tool.call hook skipped: answered with an invalid result takeover",
            Duration::from_secs(10),
        )
        .await;
    env.runtime
        .wait_report_containing(
            "badiserror: tool.call hook skipped: answered with an invalid result takeover",
            Duration::from_secs(10),
        )
        .await;
    env.runtime
        .wait_report_containing(
            "badargs: tool.call hook skipped: answered with an invalid args rewrite",
            Duration::from_secs(10),
        )
        .await;
}

// Design §5.3: tool.check passes its `{decision}` three-value answers through
// to the runtime; a decision outside the three values is a chain error.
#[tokio::test(flavor = "multi_thread")]
async fn tool_check_decisions_pass_through() {
    needs_node!();
    let env = Env::new("tool-check").await;
    env.scenario("gate", json!({ "hooks": [{ "event": "tool.check", "behavior": "answer", "answer": { "decision": "ask", "reason": "confirm?" } }] }));
    let items = json!({ "gate": env.spec("gate", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("tool.check", Some("s1"), json!({ "tool": "bash" }))
        .await;
    assert_eq!(settled, json!({ "decision": "ask", "reason": "confirm?" }));

    let env2 = Env::new("tool-check-bad").await;
    env2.scenario("gate", json!({ "hooks": [{ "event": "tool.check", "behavior": "answer", "answer": { "decision": "maybe" } }] }));
    let items = json!({ "gate": env2.spec("gate", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    let settled = env2
        .runtime
        .raise("tool.check", Some("s1"), json!({ "tool": "bash" }))
        .await;
    assert_eq!(
        settled,
        json!({ "tool": "bash" }),
        "an invalid decision skips the hook"
    );
    let line = env2
        .runtime
        .wait_report_containing("answered with an invalid decision", Duration::from_secs(10))
        .await;
    assert!(
        line.contains("gate: tool.check hook skipped"),
        "unexpected report: {line}"
    );
}

// Design §15.8: the broker maps Claude Code tool names both ways.
#[tokio::test(flavor = "multi_thread")]
async fn tool_name_aliases_apply_on_tool_call() {
    needs_node!();
    let env = Env::new("aliases").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "matcher": { "tool": "Read" }, "behavior": "answer", "answer": { "seen": "Read" } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // Ginno's name reaches the chain as the mod-visible alias.
    let settled = env
        .runtime
        .raise(
            "tool.call",
            Some("s1"),
            json!({ "tool": "read_file", "args": {} }),
        )
        .await;
    assert_eq!(settled, json!({ "seen": "Read" }));
}

// Design §3.2: a `$` call is an event — mods loaded before the caller see it.
#[tokio::test(flavor = "multi_thread")]
async fn op_calls_run_through_earlier_mods() {
    needs_node!();
    let env = Env::new("op-chain").await;
    env.scenario(
        "early",
        json!({ "hooks": [{ "event": "state.get", "behavior": "next" }] }),
    );
    env.scenario(
        "late",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["state", "get", { "plugin": "early", "key": "k" }] }] }),
    );
    let items =
        json!({ "early": env.spec("early", json!({})), "late": env.spec("late", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({ "turnId": 1 }))
        .await;
    assert_eq!(settled["ok"], true, "the call succeeded: {settled}");
    // DSH opCore shape: `{ value: null }` for a missing slot (the runner's
    // shim turns the inner null into undefined for the mod).
    assert_eq!(
        settled["value"],
        json!({ "value": null }),
        "the state slot is empty"
    );
    env.wait_trace("early", "early:state.get:begin").await;

    // `tool.call` as an op never re-enters the chain (§15.2).
    let env2 = Env::new("op-toolcall").await;
    env2.scenario("watcher", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "denied": true } }] }));
    env2.scenario(
        "caller",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["tool", "call", { "tool": "read_file", "path": "x" }] }] }),
    );
    let items = json!({ "watcher": env2.spec("watcher", json!({})), "caller": env2.spec("caller", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    // The op is forwarded to the runtime (which refuses here) — but crucially
    // the watcher mod was NOT consulted: tool.call never re-enters the chain.
    let settled = env2
        .runtime
        .raise("turn.complete", Some("s1"), json!({}))
        .await;
    assert_eq!(settled["ok"], false, "{settled}");
    assert!(
        env2.trace("watcher").is_empty(),
        "tool.call op must not re-enter the chain"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn store_roundtrip_and_machine_wide_cap() {
    needs_node!();
    let env = Env::new("store").await;
    env.scenario(
        "m",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "single_call", "call": ["store", "set", { "key": "x", "value": 42 }] },
            { "event": "session.start", "behavior": "single_call", "call": ["store", "get", { "key": "x" }] },
            { "event": "tool.call", "behavior": "store_limit" },
        ] }),
    );
    let items = json!({ "m": env.spec("m", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({}))
        .await;
    assert_eq!(settled["ok"], true, "{settled}");
    let settled = env
        .runtime
        .raise("session.start", Some("s1"), json!({}))
        .await;
    assert_eq!(settled["value"], json!(42), "{settled}");

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled["ok"], false, "{settled}");
    assert_eq!(settled["code"], "error", "{settled}");
}
