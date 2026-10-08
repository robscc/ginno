//! The `ginno-mod-broker` CLI: `--socket <path>` serves the bus standalone
//! (dev/web mode — Python spawns it), `validate <dir>` loads one mod in a
//! temporary runner and prints the validate report (registered events ×
//! matchers, `$` calls, unserved warnings).

use std::path::PathBuf;
use ginno_mod_broker::Broker;

fn main() {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--socket") => {
            let socket = args.next().unwrap_or_else(|| die("usage: ginno-mod-broker --socket <path>"));
            serve(socket);
        }
        Some("validate") => {
            let dir = args.next().unwrap_or_else(|| die("usage: ginno-mod-broker validate <dir>"));
            std::process::exit(validate(&dir));
        }
        _ => die("usage: ginno-mod-broker (--socket <path> | validate <dir>)"),
    }
}

fn die(message: &str) -> ! {
    eprintln!("ginno-mod-broker: {message}");
    std::process::exit(2);
}

fn serve(socket: String) {
    let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("tokio runtime");
    runtime.block_on(async move {
        let broker = Broker::new(None);
        if let Err(e) = ginno_mod_broker::runtime::serve(broker, PathBuf::from(&socket)).await {
            eprintln!("ginno-mod-broker: {e}");
            std::process::exit(1);
        }
    });
}

/// `validate <dir>`: spawn the runner against the mod with a mock bus, print
/// what it registers. The node binary and runner bundle come from the
/// environment (`NODE_BIN`, `GINNO_MOD_RUNNER`); without them the command
/// explains what is missing instead of pretending to validate.
fn validate(dir: &str) -> i32 {
    let manifest = std::path::Path::new(dir).join(".claude-plugin").join("plugin.json");
    let name = std::fs::read_to_string(&manifest)
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|value| value.get("name").and_then(|name| name.as_str()).map(str::to_string))
        .unwrap_or_else(|| {
            std::path::Path::new(dir)
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_else(|| "mod".to_string())
        });

    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("tokio runtime");
    let report = runtime.block_on(validate_mod(dir, &name));
    print!("{report}");
    if report.contains("error:") {
        1
    } else {
        0
    }
}

async fn validate_mod(dir: &str, name: &str) -> String {
    let node = std::env::var("NODE_BIN").unwrap_or_else(|_| "node".to_string());
    let runner = match std::env::var("GINNO_MOD_RUNNER") {
        Ok(path) => PathBuf::from(path),
        Err(_) => {
            return format!(
                "error: GINNO_MOD_RUNNER is not set; point it at packages/mod-runner/dist/mod-runner.mjs to validate {name}\n"
            );
        }
    };
    let mut command = tokio::process::Command::new(&node);
    command.arg(&runner).arg("--mod").arg(dir);
    command.stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped());
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(e) => return format!("error: cannot spawn node for {name}: {e}\n"),
    };
    let mut stdout = child.stdout.take().expect("runner stdout");
    // Load the mod, take the hooks-registered report, and shut it down. The
    // broker answers with `start` so the runner proceeds (and idles quietly).
    let mut report = format!("{name} (tier user)\n");
    let mut buffer = vec![0u8; 65_536];
    let deadline = tokio::time::sleep(std::time::Duration::from_secs(15));
    tokio::pin!(deadline);
    let collected = tokio::select! {
        n = tokio::io::AsyncReadExt::read(&mut stdout, &mut buffer) => n.ok().map(|n| buffer[..n].to_vec()),
        _ = &mut deadline => None,
    };
    match collected {
        Some(bytes) => {
            for line in String::from_utf8_lossy(&bytes).lines() {
                if line.trim().is_empty() {
                    continue;
                }
                match ginno_mod_broker::protocol::decode_line(line) {
                    Ok(frame) => {
                        if let ginno_mod_broker::WireFrame::Call { id, ns, method, args, .. } = frame {
                            if ns == "runner" && method == "hooks-registered" {
                                for hook in args.get("hooks").and_then(|hooks| hooks.as_array()).into_iter().flatten() {
                                    let event = hook.get("event").and_then(|e| e.as_str()).unwrap_or("?");
                                    let matcher = ginno_mod_broker::matcher::describe_matcher(hook.get("matcher"));
                                    report.push_str(&format!("  {event}{matcher}\n"));
                                    if !ginno_mod_broker::matcher::SERVED_EVENTS.contains(&event)
                                        && !event.ends_with('*')
                                        && event != "*"
                                        && !ginno_mod_broker::matcher::KNOWN_OP_EVENTS.contains(&event)
                                    {
                                        report.push_str(&format!("  warning: \"{event}\" is not an event this host knows\n"));
                                    }
                                }
                                let _ = id;
                            }
                        }
                    }
                    Err(e) => report.push_str(&format!("  error: bad frame from runner: {e}\n")),
                }
            }
        }
        None => report.push_str("  error: runner did not report its hooks in time\n"),
    }
    let _ = child.start_kill();
    let _ = child.wait().await;
    report
}
