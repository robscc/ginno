# ROADMAP

Ginno is a local-first personal AI agent for your own machine. This file is
the honest, non-binding map of where the project is heading — priorities move
with dogfooding feedback. Feature requests welcome via Issues.

> 中文版要点见 README.zh-CN.md；子系统级设计文档在 [`docs/`](docs/)。

## Now — quality & release engineering

- [x] Full-stack CI (runtime pytest · web lint/typecheck/vitest/build · Rust fmt/check)
- [x] Runtime test coverage reported to Codecov
- [x] Release automation: `v*` tag → dmg on GitHub Releases
- [x] CHANGELOG + versioning runbook
- [ ] Frontend test coverage beyond the first pure-logic modules (stores,
      workflow DSL editor, citation rendering)
- [ ] **Code signing + notarization** for macOS builds (drops the
      right-click → Open dance)

## Next — user-visible reliability

- [ ] **Upgrade path**: in-app "check for updates" + guided self-update
      (design in [`docs/self-update-design.md`](docs/self-update-design.md);
      relauncher + swap + session-restore, no new Rust deps planned)
- [ ] Windows & Linux builds — Tauri is cross-platform; the Python sidecar
      needs per-OS PyInstaller validation. Evaluation in progress; no promise
      before it survives dogfooding.
- [ ] Sandboxing the local sidecar API (auth token for `127.0.0.1:8787`)
- [ ] FAQ/troubleshooting coverage expanding from real user reports

## Later — directions under exploration

- [ ] Update feed service (version check against GitHub Releases)
- [ ] Deeper workflow reliability work (see
      [`docs/workflow-reliability-discussion.md`](docs/workflow-reliability-discussion.md))
- [ ] More channels/connectors following the
      [connector module design](docs/connector-module-design.md)
- [ ] External-contributor ownership map (which subsystems accept PRs freely)

## Not planned (for now)

- Cloud sync / accounts — the design rule is *your data stays on your machine*.
- A hosted version of the runtime.

---

Dates and scope are best-effort; this file is updated as items land. See
[CHANGELOG.md](CHANGELOG.md) for what actually shipped.
