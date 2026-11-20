//! Protocol-level integration tests: a real broker on a real Unix socket,
//! mock Node runners (`tests/fixtures/mock-runner.mjs`) driven by per-fixture
//! `scenario.json` files, and the test itself playing the Python runtime —
//! raising engine events, receiving bands/toasts/reports. These are the port
//! of DSH's chain.spec / matcher.spec semantics to the distributed broker
//! (design §12 P0 test row); matcher and BudgetClock unit cases live beside
//! those modules.
//!
//! Cases are split by domain into sibling modules under `tests/integration/`:
//! [`mod_lifecycle`] (load/reload + chain execution), [`tool_relay`]
//! (tool.* bridging + ops) and [`security_ui`] (grants/fs jail + UI);
//! the shared harness lives in [`common`].

#[macro_use]
mod common;

mod mod_lifecycle;
mod security_ui;
mod tool_relay;
