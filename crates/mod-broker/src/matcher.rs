//! Event-name patterns and field matchers: which registered hooks an event
//! selects (design §3.2.1). Matchers arrive serialized from the runner
//! (`string` → equality, `array` → membership, `{"regexp": source, "flags"}`
//! → regex); the broker evaluates all of them, so the JS/Rust regex dialect
//! gap lives in one place and is a documented known limitation.

use regex::Regex;
use serde_json::Value;

/// The engine events Claude Code's mods spec knows. Only used for the
/// "registered, but this host never raises that event" unserved report — the
/// broker routes whatever is registered regardless.
pub const ENGINE_EVENTS: &[&str] = &[
    "tool.call", "tool.check", "tool.describe",
    "prompt.submit", "prompt.fill", "prompt.suggest", "prompt.edit", "prompt.compose", "prompt.section",
    "prompt.context", "prompt.attachment", "skill.prompt", "attribution.text",
    "command.run", "command.describe", "config.set", "config.describe",
    "turn.start", "turn.step", "turn.complete",
    "session.start", "session.end", "session.compact", "session.receive", "session.send", "session.append",
    "session.attach", "session.detach", "session.measure",
    "agent.offer", "agent.spawn",
    "ui.render", "ui.resolve", "ui.press", "ui.input", "ui.select", "ui.focus", "ui.scroll", "ui.close", "ui.message",
    "plugin.register", "engine.create",
    "telemetry.log", "telemetry.mark",
];

/// The engine events this broker actually raises (design §5.3, P0/P1 rows).
pub const SERVED_EVENTS: &[&str] = &[
    "session.start", "session.end", "turn.start", "turn.complete", "prompt.submit",
    "tool.call", "tool.check", "command.run", "agent.spawn", "session.compact",
    "ui.render", "ui.press", "ui.input", "ui.ask",
];

/// Whether a name passed to `on` is an exact event name, `*`, or a `<ns>.*`
/// glob over known events. Mirrors DSH `isEventPattern` for the runner side;
/// the broker uses it only for diagnostics.
pub fn is_event_pattern(pattern: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if let Some(namespace) = pattern.strip_suffix(".*") {
        return !namespace.is_empty()
            && ENGINE_EVENTS.iter().chain(KNOWN_OP_EVENTS).any(|event| event.starts_with(&format!("{namespace}.")));
    }
    ENGINE_EVENTS.contains(&pattern) || KNOWN_OP_EVENTS.contains(&pattern)
}

/// The `<ns>.<method>` op names every `$` call raises (DSH KNOWN_EVENTS tail).
pub const KNOWN_OP_EVENTS: &[&str] = &[
    "ui.log", "ui.toast", "ui.status", "ui.notice", "ui.invalidate", "ui.open", "ui.panes", "ui.blit", "ui.ask", "ui.copy",
    "command.register", "command.list",
    "tool.register", "tool.list",
    "agent.register", "agent.list",
    "model.complete", "model.fork", "model.classify",
    "prompt.read",
    "turn.abort",
    "session.messages", "session.cwd", "session.root", "session.model", "session.turns", "session.id", "session.repo",
    "session.surface", "session.surfaces", "session.usage", "session.version", "session.authorize",
    "config.list",
    "settings.read",
    "env.get", "env.set",
    "fs.read", "fs.write", "fs.list", "fs.exists", "fs.stat", "fs.ancestors",
    "store.get", "store.set", "store.delete", "store.keys",
    "state.get", "state.set",
    "clock.now", "clock.sleep", "clock.after", "clock.every", "clock.cancel",
    "http.fetch",
    "process.run", "process.spawn",
    "mcp.call", "mcp.connect",
    "audio.play", "audio.speak",
];

/// Whether a registered pattern selects an event. `*` selects every event
/// except the telemetry ones, which a mod hooks by name or as `telemetry.*`.
pub fn event_matches(pattern: &str, event: &str) -> bool {
    if pattern == event {
        return true;
    }
    if pattern == "*" {
        return !event.starts_with("telemetry.");
    }
    pattern.strip_suffix('*').is_some_and(|prefix| prefix.ends_with('.') && event.starts_with(prefix))
}

/// One matcher field's expected value, as serialized across the wire.
#[derive(Debug, Clone)]
pub enum MatcherValue {
    /// Equality against a scalar (string, number, bool, null).
    Scalar(Value),
    /// Membership: any element matches.
    Array(Vec<MatcherValue>),
    /// Rust-side regex test against a string (or number rendered as text).
    Regex(Regex),
}

impl MatcherValue {
    /// Parse one matcher value from its wire form. `None` for shapes this
    /// broker cannot evaluate (the hook is then never selected).
    pub fn parse(value: &Value) -> Option<MatcherValue> {
        match value {
            Value::String(s) => Some(MatcherValue::Scalar(Value::String(s.clone()))),
            Value::Number(_) | Value::Bool(_) | Value::Null => Some(MatcherValue::Scalar(value.clone())),
            Value::Array(items) => {
                let mut parsed = Vec::with_capacity(items.len());
                for item in items {
                    parsed.push(MatcherValue::parse(item)?);
                }
                Some(MatcherValue::Array(parsed))
            }
            Value::Object(fields) => {
                let source = fields.get("regexp")?.as_str()?;
                let flags = fields.get("flags").and_then(Value::as_str).unwrap_or("");
                MatcherValue::regex(source, flags)
            }
        }
    }

    /// Build a Rust regex from a JS source and flags. JS flags with no Rust
    /// equivalent (`g`, `y`, `u`, `d`) are dropped; `s` maps to `(?s)`.
    fn regex(source: &str, flags: &str) -> Option<MatcherValue> {
        let mut rust_flags = String::new();
        for flag in flags.chars() {
            match flag {
                'i' | 'm' | 'U' | 'x' => rust_flags.push(flag),
                's' => rust_flags.push('s'),
                // global / sticky / unicode / has-indices: no-op or default here.
                'g' | 'y' | 'u' | 'd' => {}
                _ => return None,
            }
        }
        let pattern = if rust_flags.is_empty() { source.to_string() } else { format!("(?{rust_flags}){source}") };
        Regex::new(&pattern).ok().map(MatcherValue::Regex)
    }

    fn matches(&self, actual: Option<&Value>) -> bool {
        match self {
            MatcherValue::Scalar(expected) => match (expected, actual) {
                // A string expectation also matches a number field, as DSH's
                // `===` does not — keep strict equality instead.
                (_, Some(actual)) => expected == actual,
                (_, None) => false,
            },
            MatcherValue::Array(candidates) => candidates.iter().any(|candidate| candidate.matches(actual)),
            MatcherValue::Regex(regex) => match actual {
                Some(Value::String(text)) => regex.is_match(text),
                Some(Value::Number(number)) => regex.is_match(&number.to_string()),
                _ => false,
            },
        }
    }
}

/// Whether every matcher field accepts the event's top-level field of the same
/// name: a scalar by equality, an array by membership, a regex by test.
pub fn matcher_matches(matcher: &[(String, MatcherValue)], input: &Value) -> bool {
    let Value::Object(fields) = input else { return false };
    matcher.iter().all(|(field, expected)| expected.matches(fields.get(field)))
}

/// Parse a whole matcher object into ordered (field, value) pairs.
pub fn parse_matcher(value: &Value) -> Option<Vec<(String, MatcherValue)>> {
    let Value::Object(fields) = value else { return None };
    let mut out = Vec::with_capacity(fields.len());
    for (field, value) in fields {
        out.push((field.clone(), MatcherValue::parse(value)?));
    }
    Some(out)
}

/// Render a matcher the way `claude plugin validate` prints it: array
/// members bare and pipe-joined, regexes as `/source/flags`.
pub fn describe_matcher(matcher: Option<&Value>) -> String {
    let Some(matcher) = matcher else { return String::new() };
    let Value::Object(fields) = matcher else { return String::new() };
    let rendered = fields
        .iter()
        .map(|(field, value)| format!("{field}={}", describe_value(value)))
        .collect::<Vec<_>>()
        .join(",");
    format!("{{{rendered}}}")
}

fn describe_value(value: &Value) -> String {
    match value {
        Value::Array(items) => items.iter().map(describe_value).collect::<Vec<_>>().join("|"),
        Value::String(text) => text.clone(),
        Value::Object(fields) => match (fields.get("regexp"), fields.get("flags")) {
            (Some(source), flags) => {
                let source = source.as_str().unwrap_or_default();
                let flags = flags.and_then(Value::as_str).unwrap_or_default();
                format!("/{source}/{flags}")
            }
            _ => value.to_string(),
        },
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_exact_names_namespace_globs_and_star_without_telemetry() {
        assert!(event_matches("tool.call", "tool.call"));
        assert!(!event_matches("tool.call", "tool.check"));
        assert!(event_matches("tool.*", "tool.check"));
        assert!(!event_matches("tool.*", "turn.start"));
        assert!(event_matches("*", "turn.start"));
        assert!(!event_matches("*", "telemetry.log"));
        assert!(event_matches("telemetry.*", "telemetry.log"));
    }

    #[test]
    fn knows_claude_code_event_names_and_globs() {
        assert!(is_event_pattern("tool.call"));
        assert!(!is_event_pattern("tool.calls"));
        assert!(is_event_pattern("*"));
        assert!(!is_event_pattern("classic.*"));
        assert!(is_event_pattern("telemetry.*"));
        assert!(!is_event_pattern(".*"));
        assert!(is_event_pattern("state.get"));
    }

    #[test]
    fn compares_scalars_by_equality_arrays_by_membership_and_regexes_by_test() {
        let parse = |v: &Value| parse_matcher(v).unwrap();
        let input = json!({"tool": "mcp__github__issues", "count": 3, "flag": true});
        assert!(matcher_matches(&parse(&json!({"tool": "mcp__github__issues"})), &input));
        assert!(!matcher_matches(&parse(&json!({"tool": "Bash"})), &input));
        assert!(matcher_matches(&parse(&json!({"tool": ["Edit", "mcp__github__issues"]})), &input));
        assert!(matcher_matches(&parse(&json!({"tool": {"regexp": "^mcp__github__"}})), &input));
        assert!(matcher_matches(&parse(&json!({"count": {"regexp": "^3$"}})), &input));
        assert!(!matcher_matches(&parse(&json!({"flag": {"regexp": "true"}})), &input));
        assert!(!matcher_matches(&parse(&json!({"tool": "x", "count": 3})), &input));
        assert!(!matcher_matches(&parse(&json!({"tool": "x"})), &json!("not an object")));
        assert!(matcher_matches(&parse(&json!({})), &input));
    }

    #[test]
    fn describes_matchers_like_claude_plugin_validate() {
        assert_eq!(describe_matcher(None), "");
        assert_eq!(describe_matcher(Some(&json!({"component": "Spinner"}))), "{component=Spinner}");
        assert_eq!(
            describe_matcher(Some(&json!({"tool": ["Edit", "Write"], "id": {"regexp": "^T-", "flags": ""}}))),
            "{tool=Edit|Write,id=/^T-/}"
        );
    }
}
