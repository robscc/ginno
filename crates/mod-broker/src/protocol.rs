//! Wire protocol shared by every link of the mods bus (Python↔broker and
//! broker↔runner): newline-delimited JSON frames, semantically identical on
//! both links so a single codec serves both (design §6.1).
//!
//! Frame kinds:
//! - `call`       request-response (`$` op, or a meta call such as `hello` /
//!                `hooks-registered` routed by `ns`)
//! - `result`     response to a call: `{ok:true,value}` or `{ok:false,code,message}`
//! - `event`      hook dispatch (broker→runner) and engine event raise (Python→broker);
//!                both carry `invocation`/`deadlineMs` and expect a `result`
//! - `next`       runner→broker: hand the input to the chain beneath one hook
//! - `catch-call` broker→runner: run a failed hook's `.catch` handler
//! - `notify`     one-way (bands/toasts/status/reports/ping)
//!
//! The handshake frames of design §6.2 are served as calls: Python sends
//! `hello{role:"runtime",token,config}` (accepted either as `kind:"hello"` or
//! `call{ns:"broker",method:"hello"}`) and the broker answers with the
//! `ready{mods:[...]}` payload; a runner reports
//! `call{ns:"runner",method:"hooks-registered"}` and gets `start` back.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fmt;

/// Protocol version. P0 pins semantics to Claude Code 2.1.289 (`v` is echoed
/// on every frame; a mismatch is a hard error at handshake time).
pub const PROTOCOL_VERSION: u64 = 1;

/// Error codes carried on failed `result` frames.
pub const CODE_DENIED: &str = "denied";
pub const CODE_NO_IMPLEMENTATION: &str = "no-implementation";
pub const CODE_NOT_FOUND: &str = "not-found";
pub const CODE_HOOK_FAILED: &str = "hook-failed";
pub const CODE_RUNNER_DEAD: &str = "runner-dead";
pub const CODE_ERROR: &str = "error";

/// One wire frame. Field names match the design verbatim (`mod` is a raw
/// identifier in Rust, hence `mod_name` with a serde rename).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum Frame {
    /// A call awaiting one `result` frame with the same `id`.
    Call {
        v: u64,
        id: u64,
        ns: String,
        method: String,
        #[serde(default)]
        args: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        session: Option<String>,
        #[serde(rename = "mod", default, skip_serializing_if = "Option::is_none")]
        mod_name: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        invocation: Option<String>,
        /// Extra link-specific fields (`raisedBy` on `$.tool.call` forwarding).
        #[serde(flatten, default, skip_serializing_if = "Value::is_null")]
        extra: Value,
    },
    /// Response to a call or event frame, matched by `id`.
    Result {
        v: u64,
        id: u64,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        value: Option<Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        code: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        message: Option<String>,
    },
    /// An event: Python→broker raises an engine event; broker→runner dispatches
    /// one hook. `payload` is the event input; `deadlineMs` is the per-hook
    /// running-time budget the broker enforces.
    Event {
        v: u64,
        id: u64,
        event: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        session: Option<String>,
        invocation: String,
        #[serde(default)]
        payload: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        deadline_ms: Option<u64>,
        /// Registry entry the dispatch belongs to, so a runner with several
        /// hooks on one event can tell them apart. Runners may ignore it.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hook: Option<u64>,
    },
    /// Runner→broker: the hook passed its (possibly rewritten) input beneath.
    /// `phase:"catch"` marks a call from inside a `.catch` handler.
    Next {
        v: u64,
        id: u64,
        invocation: String,
        #[serde(default)]
        e: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        phase: Option<String>,
    },
    /// Broker→runner: run the failed hook's `.catch` handler for `invocation`.
    CatchCall {
        v: u64,
        id: u64,
        invocation: String,
        event: String,
        #[serde(default)]
        payload: Value,
        failure: HookFailure,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        deadline_ms: Option<u64>,
    },
    /// One-way notification: `bands.update`, `ui.toast`, `mod.status`,
    /// `report` (diagnostics), `ping` (heartbeat).
    Notify {
        v: u64,
        method: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        session: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        mod_name: Option<String>,
        #[serde(default, skip_serializing_if = "Value::is_null")]
        args: Value,
    },
    /// Python→broker handshake (see module docs). The broker replies with a
    /// `Result` frame whose `value` is the `ready{mods:[...]}` payload.
    Hello {
        v: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        id: Option<u64>,
        role: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
        #[serde(default)]
        config: Value,
    },
}

/// How a hook failed: `throw` (any non-timeout failure) or `timeout`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HookFailure {
    pub kind: String,
    pub message: String,
}

impl HookFailure {
    pub fn throw(message: impl Into<String>) -> Self {
        Self { kind: "throw".into(), message: message.into() }
    }
    pub fn timeout(limit_ms: u64) -> Self {
        Self { kind: "timeout".into(), message: format!("ran past its {limit_ms} ms limit") }
    }
}

/// A failed call's payload, as the caller of a round-trip sees it.
#[derive(Debug, Clone)]
pub struct RpcError {
    pub code: String,
    pub message: String,
}

impl RpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self { code: code.into(), message: message.into() }
    }
    pub fn no_implementation(op: &str) -> Self {
        Self::new(CODE_NO_IMPLEMENTATION, format!("no implementation for {op}"))
    }
}

impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl Frame {
    pub fn version(&self) -> u64 {
        match self {
            Frame::Call { v, .. }
            | Frame::Result { v, .. }
            | Frame::Event { v, .. }
            | Frame::Next { v, .. }
            | Frame::CatchCall { v, .. }
            | Frame::Notify { v, .. }
            | Frame::Hello { v, .. } => *v,
        }
    }

    /// The frame's request id, when it expects or carries a `result`.
    pub fn id(&self) -> Option<u64> {
        match self {
            Frame::Call { id, .. }
            | Frame::Result { id, .. }
            | Frame::Event { id, .. }
            | Frame::Next { id, .. }
            | Frame::CatchCall { id, .. } => Some(*id),
            Frame::Hello { id, .. } => *id,
            Frame::Notify { .. } => None,
        }
    }

    pub fn encode_into(&self, out: &mut String) {
        let _ = serde_json::to_writer(WriteAdapter(out), self);
        out.push('\n');
    }
}

/// `std::fmt::Write` adapter so `serde_json::to_writer` can append to a `String`.
struct WriteAdapter<'a>(&'a mut String);

impl std::io::Write for WriteAdapter<'_> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.push_str(&String::from_utf8_lossy(buf));
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Decode one frame from a line (without the trailing newline).
pub fn decode_line(line: &str) -> Result<Frame, String> {
    let frame: Frame = serde_json::from_str(line)
        .map_err(|e| format!("malformed frame: {e}; line: {}", truncate(line, 300)))?;
    if frame.version() != PROTOCOL_VERSION {
        return Err(format!(
            "unsupported protocol version {} (this broker speaks v{PROTOCOL_VERSION})",
            frame.version()
        ));
    }
    Ok(frame)
}

fn truncate(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

/// Build a successful `result` frame.
pub fn result_ok(id: u64, value: Value) -> Frame {
    Frame::Result { v: PROTOCOL_VERSION, id, ok: true, value: Some(value), code: None, message: None }
}

/// Build a failed `result` frame.
pub fn result_err(id: u64, code: &str, message: impl Into<String>) -> Frame {
    Frame::Result {
        v: PROTOCOL_VERSION,
        id,
        ok: false,
        value: None,
        code: Some(code.to_string()),
        message: Some(message.into()),
    }
}

/// Line-oriented frame writer shared by every connection sink.
#[derive(Clone)]
pub struct FrameWriter {
    tx: tokio::sync::mpsc::UnboundedSender<String>,
}

impl FrameWriter {
    /// Spawn a writer task draining frames into `sink`.
    pub fn new<S>(mut sink: S) -> Self
    where
        S: tokio::io::AsyncWrite + Unpin + Send + 'static,
    {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<String>();
        tokio::spawn(async move {
            while let Some(line) = rx.recv().await {
                if tokio::io::AsyncWriteExt::write_all(&mut sink, line.as_bytes())
                    .await
                    .is_err()
                {
                    break;
                }
            }
        });
        Self { tx }
    }

    pub fn send(&self, frame: &Frame) {
        let mut line = String::new();
        frame.encode_into(&mut line);
        let _ = self.tx.send(line);
    }

    pub fn send_result(&self, id: u64, res: &Result<Value, RpcError>) {
        let frame = match res {
            Ok(value) => result_ok(id, value.clone()),
            Err(e) => result_err(id, &e.code, e.message.clone()),
        };
        self.send(&frame);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn round_trips_every_frame_kind() {
        let frames = vec![
            json!({"v":1,"kind":"call","id":42,"ns":"fs","method":"read","args":{"path":"./x.py"},"session":"s1","mod":"token-weather"}),
            json!({"v":1,"kind":"result","id":42,"ok":true,"value":"<content>"}),
            json!({"v":1,"kind":"result","id":42,"ok":false,"code":"denied","message":"no"}),
            json!({"v":1,"kind":"event","id":7,"event":"tool.call","session":"s1","invocation":"i-17","payload":{"tool":"Bash"},"deadlineMs":10000}),
            json!({"v":1,"kind":"next","id":8,"invocation":"i-17","e":{"tool":"Bash"}}),
            json!({"v":1,"kind":"catch-call","id":9,"invocation":"i-17","event":"tool.call","payload":{},"failure":{"kind":"throw","message":"Error: boom"}}),
            json!({"v":1,"kind":"notify","method":"bands.update","session":"s1","args":{"generation":3,"tree":[]}}),
            json!({"v":1,"kind":"hello","id":1,"role":"runtime","token":"t","config":{}}),
        ];
        for expected in frames {
            let mut line = String::new();
            Frame::Event { v: 1, id: 0, event: String::new(), session: None, invocation: String::new(), payload: Value::Null, deadline_ms: None, hook: None }.encode_into(&mut line); // warm path check only
            let text = serde_json::to_string(&expected).unwrap();
            let frame = decode_line(&text).unwrap_or_else(|e| panic!("{e}"));
            let mut out = String::new();
            frame.encode_into(&mut out);
            let round: Value = serde_json::from_str(out.trim()).unwrap();
            assert_eq!(round, expected, "frame did not round-trip");
        }
    }

    #[test]
    fn decodes_design_examples_verbatim() {
        // The exact frames printed in design §6.1 must decode.
        assert!(decode_line(r#"{"v":1,"id":42,"kind":"call","ns":"fs","method":"read","args":{"path":"./x.py"},"session":"s1","mod":"token-weather"}"#).is_ok());
        assert!(decode_line(r#"{"v":1,"id":42,"kind":"result","ok":true,"value":"<content>"}"#).is_ok());
        assert!(decode_line(r#"{"v":1,"kind":"event","event":"tool.call","session":"s1","invocation":"i-17","payload":{},"deadlineMs":10000,"id":1}"#).is_ok());
        assert!(decode_line(r#"{"v":1,"kind":"next","invocation":"i-17","e":{},"id":2}"#).is_ok());
        assert!(decode_line(r#"{"v":1,"kind":"catch-call","invocation":"i-17","id":3,"event":"e","payload":{},"failure":{"kind":"throw","message":"m"}}"#).is_ok());
        assert!(decode_line(r#"{"v":1,"kind":"notify","method":"bands.update","session":"s1","args":{},"mod":"m"}"#).is_ok());
    }

    #[test]
    fn rejects_wrong_version() {
        assert!(decode_line(r#"{"v":2,"kind":"notify","method":"x"}"#).is_err());
    }
}
