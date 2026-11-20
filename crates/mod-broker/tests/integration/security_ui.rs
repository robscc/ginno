//! Security & UI surfaces domain: fs workspace jail, grants enforcement,
//! rewritten-args re-authorization, band redraws/presses, panes, `$.ui.ask`
//! round-trips, and the real mod-runner Token Weather example.

use super::common::{default_budgets, node_bin, Env};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[tokio::test(flavor = "multi_thread")]
async fn fs_is_root_limited_to_the_workspace() {
    needs_node!();
    let env = Env::new("fs").await;
    env.write_workspace("notes.txt", "hello");
    env.scenario(
        "m",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["fs", "read", { "path": "notes.txt" }],
                ["fs", "read", { "path": "../../etc/passwd" }],
                ["fs", "read", { "path": "/etc/passwd" }],
                ["fs", "write", { "path": "note.txt", "text": "written" }],
            ] },
        ] }),
    );
    let items = json!({ "m": env.spec("m", json!({ "fs.write": true })) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({}))
        .await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["value"], json!("hello"));
    assert_eq!(results[1]["ok"], false);
    assert!(results[1]["message"]
        .as_str()
        .unwrap()
        .contains("outside the workspace"));
    assert_eq!(results[2]["ok"], false);
    assert!(results[2]["message"]
        .as_str()
        .unwrap()
        .contains("outside the workspace"));
    assert_eq!(results[3]["ok"], true);
    assert_eq!(
        std::fs::read_to_string(env.dir.join("workspace/note.txt")).unwrap(),
        "written"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn grants_deny_unauthorized_calls() {
    needs_node!();
    let env = Env::new("grants").await;
    env.scenario(
        "gated",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["process", "run", { "argv": ["git", "status"] }],
                ["env", "get", { "name": "AWS_SECRET" }],
                ["http", "fetch", { "url": "https://example.com" }],
                ["env", "get", { "name": "PATH" }],
            ] },
        ] }),
    );
    let items = json!({ "gated": env.spec("gated", json!({ "env.get": ["PATH"] })) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({}))
        .await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["code"], "denied", "process.run without a grant");
    assert_eq!(results[1]["code"], "denied", "env.get outside the grant");
    assert_eq!(results[2]["code"], "denied", "http.fetch without a grant");
    assert_eq!(
        results[3]["ok"], true,
        "PATH is in the env grant: {}",
        results[3]
    );
    // Every denial points at the fix: grants on the Mods settings page (§8).
    assert!(
        results[0]["message"]
            .as_str()
            .unwrap()
            .contains("Mods settings page"),
        "{}",
        results[0]["message"]
    );
    assert!(
        results[2]["message"]
            .as_str()
            .unwrap()
            .contains("Mods settings page"),
        "{}",
        results[2]["message"]
    );
}

// Design §3.4: renders subscribe to the state slots they read; a write
// redraws the band (one generation per tree change).
#[tokio::test(flavor = "multi_thread")]
async fn state_writes_redraw_subscribed_bands() {
    needs_node!();
    let env = Env::new("subscribe").await;
    env.scenario("a", json!({ "hooks": [{ "event": "ui.render", "behavior": "render_state", "stateRef": { "plugin": "a", "key": "v" } }] }));
    env.scenario(
        "b",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "calls_collect", "calls": [
            ["state", "set", { "plugin": "a", "key": "v", "value": 2 }],
            ["state", "set", { "plugin": "b", "key": "unrelated", "value": 9 }],
        ] }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // session.start triggers the first drawing.
    let _ = env
        .runtime
        .raise("session.start", Some("s1"), json!({}))
        .await;
    let first = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    assert_eq!(first["args"]["generation"], 1);
    assert!(
        first["args"]["tree"].to_string().contains("count null"),
        "got {}",
        first
    );

    // One turn writes a subscribed slot AND an unrelated one: exactly one
    // redraw, with the new value (burst coalesced, unrelated write ignored).
    let _ = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({}))
        .await;
    let second = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    assert_eq!(second["args"]["generation"], 2);
    assert!(
        second["args"]["tree"].to_string().contains("count 2"),
        "got {}",
        second
    );
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(2) {
        assert!(
            env.runtime
                .take(|frame| frame.get("method").and_then(Value::as_str) == Some("bands.update"))
                .is_none(),
            "an unrelated state write must not redraw"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn press_routes_to_the_runner_and_stale_generations_are_ignored() {
    needs_node!();
    let env = Env::new("press").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _ = env
        .runtime
        .raise("session.start", Some("s1"), json!({}))
        .await;
    let drawn = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(
        drawn["args"]["tree"].to_string().contains("a:a0"),
        "got {}",
        drawn
    );

    // A current-generation press reaches the runner.
    env.runtime.notify(
        "ui.press",
        json!({ "generation": 1, "actionId": "a:a0" }),
        Some("s1"),
    );
    env.wait_trace("a", "a:press:a:a0").await;

    // A pane Input/Select submission rides the press frame as `value`; the
    // runner call carries it through (observable in the mock's trace).
    env.runtime.notify(
        "ui.press",
        json!({ "generation": 1, "actionId": "a:a0", "value": "cm" }),
        Some("s1"),
    );
    env.wait_trace("a", r#"a:press:a:a0:"cm""#).await;

    // A stale-generation press is ignored with a report.
    env.runtime.notify(
        "ui.press",
        json!({ "generation": 0, "actionId": "a:a0" }),
        Some("s1"),
    );
    let line = env
        .runtime
        .wait_report_containing("band press ignored", Duration::from_secs(10))
        .await;
    assert!(line.contains("generation 0"), "unexpected report: {line}");
}

// Design §7.1 per-mod bands: each mod with a `ui.render` hook draws its own
// segment — one mod's tree never blanks another's, presses validate against
// the owning mod's generation, and the payload keeps the first non-empty
// tree as the legacy `tree` field.
#[tokio::test(flavor = "multi_thread")]
async fn each_mod_draws_its_own_band_segment() {
    needs_node!();
    let env = Env::new("per-mod").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }),
    );
    env.scenario(
        "b",
        json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _ = env
        .runtime
        .raise("session.start", Some("s1"), json!({}))
        .await;
    let drawn = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    let mods = drawn["args"]["mods"]
        .as_array()
        .expect("a per-mod mods array");
    assert_eq!(mods.len(), 2, "both mods draw: {drawn}");
    assert_eq!(mods[0]["mod"], "a");
    assert_eq!(mods[1]["mod"], "b");
    assert_eq!(mods[0]["generation"], 1);
    assert_eq!(mods[1]["generation"], 1);
    assert!(mods[0]["tree"].to_string().contains("a:a0"), "got {drawn}");
    assert!(mods[1]["tree"].to_string().contains("b:a0"), "got {drawn}");
    // Legacy fields mirror the first non-empty mod (a, in load order).
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(
        drawn["args"]["tree"].to_string().contains("a:a0"),
        "got {drawn}"
    );

    // A press names its mod by the action id prefix; b's own generation 1 is
    // valid even though a's tree came first.
    env.runtime.notify(
        "ui.press",
        json!({ "generation": 1, "actionId": "b:a0" }),
        Some("s1"),
    );
    env.wait_trace("b", "b:press:b:a0").await;

    // A generation from the other mod's drawing does not validate for a.
    env.runtime.notify(
        "ui.press",
        json!({ "generation": 99, "actionId": "a:a0" }),
        Some("s1"),
    );
    let line = env
        .runtime
        .wait_report_containing("band press ignored", Duration::from_secs(10))
        .await;
    assert!(line.contains("a:a0"), "unexpected report: {line}");
}

// Design §7.4 panes, Rust side: `$.ui.open` records the pane and reports
// `isPlaced`, the next render pass draws it through the owner's chain and
// pushes `mod.pane.update`, `$.ui.close`/`session.end` push `mod.pane.close`
// and clear it, and the 4-panes-per-session cap holds.
#[tokio::test(flavor = "multi_thread")]
async fn panes_open_render_close_and_clear_on_session_end() {
    needs_node!();
    let env = Env::new("panes").await;
    env.scenario(
        "p",
        json!({ "hooks": [
            { "event": "ui.render", "behavior": "render_button" },
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["ui", "open", { "id": "w1", "title": "Weather" }],
                ["ui", "open", { "id": "w2", "title": "Two" }],
                ["ui", "open", { "id": "w3", "title": "Three" }],
                ["ui", "open", { "id": "w4", "title": "Four" }],
                ["ui", "open", { "id": "w5", "title": "Five" }],
            ] },
            { "event": "session.end", "behavior": "calls_collect", "calls": [
                ["ui", "close", { "id": "w1" }],
            ] },
        ] }),
    );
    let items = json!({ "p": env.spec("p", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // Four panes open; the fifth is refused by the cap. Each open reports
    // isPlaced and reaches Python as `mod.pane.open`.
    let settled = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({ "turnId": 1 }))
        .await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["value"]["isPlaced"], true, "{settled}");
    assert_eq!(results[0]["value"]["id"], "w1");
    assert_eq!(
        results[4]["ok"], false,
        "the fifth pane is over the cap: {settled}"
    );
    assert!(results[4]["message"]
        .as_str()
        .unwrap()
        .contains("at most 4 panes"));
    for id in ["w1", "w2", "w3", "w4"] {
        let opened = env
            .runtime
            .wait_notify("mod.pane.open", Duration::from_secs(30))
            .await;
        assert_eq!(opened["args"]["id"], id, "got {opened}");
        assert_eq!(opened["mod_name"], "p");
    }

    // The render pass draws each pane through the owner's ui.render chain.
    for id in ["w1", "w2", "w3", "w4"] {
        let updated = env
            .runtime
            .wait_notify("mod.pane.update", Duration::from_secs(30))
            .await;
        assert_eq!(updated["args"]["id"], id, "got {updated}");
        assert_eq!(updated["args"]["generation"], 1);
        assert!(
            updated["args"]["tree"].to_string().contains("p:a0"),
            "got {updated}"
        );
        assert_eq!(updated["mod_name"], "p");
    }

    // ui.panes lists them, oldest first.
    // (Checked from the broker side below via session.end's close order.)
    let _ = env
        .runtime
        .raise("session.end", Some("s1"), json!({}))
        .await;
    // session.end first runs the mod's own close of w1, then clears the rest.
    // The session.end chain settles, then forget_session closes every pane.
    let mut closed = Vec::new();
    for _ in 0..4 {
        let frame = env
            .runtime
            .wait_notify("mod.pane.close", Duration::from_secs(30))
            .await;
        closed.push(frame["args"]["id"].as_str().unwrap().to_string());
    }
    closed.sort();
    assert_eq!(
        closed,
        vec!["w1", "w2", "w3", "w4"],
        "every open pane closes"
    );

    // The band table is gone too: a further draw would re-open, so a fresh
    // session's pane cap starts clean.
    let panes = env.broker.surfaces_panes("s1");
    assert!(panes.is_empty(), "session.end cleared the pane table");
}

// ---------------------------------------------------------------------------
// $.ui.ask (design §7.3): mod → broker pending → runtime mod.ask → broker.answer
// ---------------------------------------------------------------------------

// The pending-ask table is process-global and the disconnect test's
// `fail_all_asks` sweeps every ask in it, so the two tests serialize.
static ASK_TESTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[tokio::test(flavor = "multi_thread")]
async fn ui_ask_round_trips_through_the_runtime() {
    needs_node!();
    let _guard = ASK_TESTS.lock().await;
    let env = Env::new("ui-ask").await;
    env.scenario("asking", json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["ui", "ask", { "message": "Proceed?", "choices": ["yes", "no"] }] }] }));
    let items = json!({ "asking": env.spec("asking", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // The event only settles once the test (as the runtime) answers.
    let event_id = env
        .runtime
        .send_event("turn.complete", Some("s1"), json!({ "turnId": 1 }));
    let ask = env
        .runtime
        .wait_notify("mod.ask", Duration::from_secs(30))
        .await;
    assert_eq!(ask["args"]["message"], "Proceed?");
    assert_eq!(ask["args"]["choices"], json!(["yes", "no"]));
    assert_eq!(ask["mod_name"], "asking");
    assert_eq!(ask["session"], "s1");
    let ask_id = ask["args"]["id"].as_str().expect("an ask id").to_string();

    // The runtime's answer resolves the pending ask; the runner's `$` call
    // settles with the op's `{value}` shape.
    let answer_id = env
        .runtime
        .call_broker("answer", json!({ "id": ask_id, "value": "yes" }));
    let ack = env
        .runtime
        .wait_result(answer_id, "broker.answer", Duration::from_secs(10))
        .await
        .expect("answer ack");
    assert_eq!(ack, json!({ "resolved": true }));

    let settled = env
        .runtime
        .wait_result(event_id, "turn.complete", Duration::from_secs(30))
        .await
        .expect("event settles");
    assert_eq!(settled, json!({ "ok": true, "value": { "value": "yes" } }));
    env.wait_trace(
        "asking",
        r#"asking:call:ui.ask:{"ok":true,"value":{"value":"yes"}}"#,
    )
    .await;

    // Answering an id that names no ask is a not-found, not a silent ok.
    let bogus = env
        .runtime
        .call_broker("answer", json!({ "id": "ask-gone", "value": "x" }));
    let err = env
        .runtime
        .wait_result(bogus, "bogus answer", Duration::from_secs(10))
        .await
        .unwrap_err();
    assert!(err.contains("no pending ask"), "{err}");
}

#[tokio::test(flavor = "multi_thread")]
async fn ui_ask_fails_when_the_runtime_disconnects() {
    needs_node!();
    let _guard = ASK_TESTS.lock().await;
    let env = Env::new("ui-ask-disconnect").await;
    env.scenario("asking", json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["ui", "ask", { "message": "Still there?" }] }] }));
    let items = json!({ "asking": env.spec("asking", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _event_id = env
        .runtime
        .send_event("turn.complete", Some("s1"), json!({}));
    let ask = env
        .runtime
        .wait_notify("mod.ask", Duration::from_secs(30))
        .await;
    assert_eq!(
        ask["args"]["choices"],
        Value::Null,
        "no choices field when the mod sent none"
    );

    // The runtime goes away mid-question: the ask aborts, the runner's call
    // errors, and its trace keeps the proof (the event result itself has no
    // runtime left to reach).
    env.runtime.disconnect().await;
    env.wait_trace("asking", r#"asking:call:ui.ask:{"ok":false"#)
        .await;
    let trace = env.trace("asking");
    assert!(trace.contains("runtime disconnected"), "{trace}");
}

// ---------------------------------------------------------------------------
// the real runner: the vendored Token Weather example over the actual
// packages/mod-runner source (dev mode: node type stripping)
// ---------------------------------------------------------------------------

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}

#[tokio::test(flavor = "multi_thread")]
async fn real_runner_draws_the_token_weather_band() {
    needs_node!();
    let runner_src = repo_root().join("packages/mod-runner/src/index.ts");
    let mod_dir = repo_root().join("packages/mod-runner/examples/token-weather");
    if !runner_src.exists() || !mod_dir.exists() {
        eprintln!("skipping: packages/mod-runner not present");
        return;
    }

    let env = Env::new("real-token-weather").await;
    // The runner asks for the pushed config before registering; the spec is
    // what `runner.loaded` answers with.
    let spec = json!({
        "enabled": true,
        "dir": mod_dir,
        "grants": {},
        "config": {},
    });
    let config = json!({
        "nodePath": node_bin(),
        "runnerPath": runner_src,
        "workspace": env.dir.join("workspace"),
        "storePath": env.dir.join("store.json"),
        "logsDir": env.dir.join("logs"),
        "mods": { "items": { "token-weather": spec } },
        "budgets": {},
    });

    // Answer forwarded ops like the runtime would: session.usage feeds the
    // mod's readings; anything else is refused.
    let session_usage =
        json!({ "context": { "tokens": 32_000, "window": 200_000, "percent": 16 } });
    env.runtime.push_responder(move |ns, method| {
        if ns == "session" && method == "usage" {
            Some(session_usage.clone())
        } else {
            None
        }
    });

    let ready = env.runtime.hello(&config).await;
    let first = &ready["mods"][0];
    assert_eq!(first["name"], "token-weather", "{ready}");
    assert_eq!(first["status"], "loaded", "{ready}");
    assert!(
        first["hooks"].as_str().unwrap().contains("session.start"),
        "{ready}"
    );

    // session.start: the hook takes a reading through the runtime, then the
    // band draws with one data point.
    let _ = env
        .runtime
        .raise("session.start", Some("s1"), json!({}))
        .await;
    let drawn = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(
        drawn["args"]["tree"].to_string().contains("of context"),
        "band should show the forecast: {}",
        drawn
    );

    // turn.complete: a second reading lands, the band redraws with a trend.
    let _ = env
        .runtime
        .raise("turn.complete", Some("s1"), json!({ "turnId": 1 }))
        .await;
    let redrawn = env
        .runtime
        .wait_notify("bands.update", Duration::from_secs(30))
        .await;
    assert_eq!(redrawn["args"]["generation"], 2);
    assert!(
        redrawn["args"]["tree"].to_string().contains("last turns"),
        "band with two readings shows the trend: {}",
        redrawn
    );
}

// Issue #18 (2026-11-05): the op entry grant check ran on the ORIGINAL args
// only — a middleware `next` could rewrite them in flight. The broker now
// re-runs the authorization decision on the FINAL args at the chain core.
// Contract: (a) unchanged args still pass, (b) a rewrite that stays inside
// the grant passes, (c) a rewrite that widens the scope is denied, with the
// denial naming the final (rewritten) value.
#[tokio::test(flavor = "multi_thread")]
async fn rewritten_args_are_rechecked_against_grants() {
    needs_node!();

    async fn run(name: &str, patch: Value, grant: Value, asked: String) -> Value {
        let env = Env::new(name).await;
        env.scenario(
            "mid",
            json!({ "hooks": [{ "event": "env.get", "behavior": "next", "ePatch": patch }] }),
        );
        env.scenario(
            "worker",
            json!({ "hooks": [{ "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["env", "get", { "name": asked }],
            ] }] }),
        );
        let items = json!({
            "mid": env.spec("mid", json!({})),
            "worker": env.spec("worker", grant),
        });
        let _ready = env.hello_with(items, default_budgets()).await;
        let settled = env
            .runtime
            .raise("turn.complete", Some("s1"), json!({}))
            .await;
        let results = settled.as_array().expect("collect results");
        results[0].clone()
    }

    // (a) middleware passes the args through unchanged: normal call.
    let result = run(
        "recheck-plain",
        json!({}),
        json!({ "env.get": ["PATH"] }),
        "PATH".to_string(),
    )
    .await;
    assert_eq!(result["ok"], true, "unchanged args: {result}");

    // (b) a legal rewrite — the new args are still inside the grant.
    let result = run(
        "recheck-legal",
        json!({ "name": "HOME" }),
        json!({ "env.get": ["PATH", "HOME"] }),
        "PATH".to_string(),
    )
    .await;
    assert_eq!(result["ok"], true, "legal rewrite: {result}");
    assert_eq!(
        result["value"],
        json!(std::env::var("HOME").unwrap_or_default()),
        "legal rewrite: {result}"
    );

    // (c) a scope-widening rewrite — denied at the core, with the FINAL
    // value in the reason (the entry check had passed on the original).
    let result = run(
        "recheck-widen",
        json!({ "name": "AWS_SECRET" }),
        json!({ "env.get": ["PATH"] }),
        "PATH".to_string(),
    )
    .await;
    assert_eq!(result["code"], "denied", "widening rewrite: {result}");
    let message = result["message"].as_str().unwrap();
    assert!(
        message.contains("AWS_SECRET"),
        "the denial must audit the final args: {message}"
    );
    assert!(message.contains("Mods settings page"), "{message}");
}
