# Security Policy

## Supported versions

Ginno is pre-1.0 and iterates fast. Only the **latest `main`** and the newest
tagged release receive security fixes.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's **private vulnerability reporting** for this repository:
*Security* tab → *Report a vulnerability* (or contact the maintainer directly
if you cannot use it). Include: affected version/commit, reproduction steps,
and impact assessment. You will get an acknowledgement within **5 business
days** and a fix timeline within **90 days**. Credit is given by default
(opt out in your report).

## Scope — what matters in Ginno's threat model

Ginno is a **local-first** desktop app: no database, no account, no cloud. All
state lives under `~/.ginno/` as plain files. It is a *client* — it never takes
custody of your model API keys or your data beyond that local folder. Areas we
consider security-sensitive:

| Surface | Risk |
|---|---|
| API keys in `~/.ginno/settings.json` | plaintext at rest, like any local CLI config — file permissions and disk encryption are the user's layer; we must never transmit keys anywhere except the configured model endpoints |
| Tools / MCP servers / hooks / skills | arbitrary local code execution **by design** (that is the product) — permission prompts and allowlists are the guardrails; bypassing them without user consent is a vulnerability |
| Embedded browser & web fetch | untrusted web content must not escalate into session/agent control (prompt-injection-driven tool calls are tracked as hardening issues, not just bugs) |
| Connectors / channels (DingTalk, WeCom, …) | message-in → tool-call trust boundary; sender allowlists and token handling |
| PyInstaller sidecar | spawns a local FastAPI on `127.0.0.1:8787` — anything listening on localhost can reach it; cross-origin access to it from web pages is in scope |

**Out of scope**: the user's own machine being compromised (threat model
assumes the local user account is trusted), model provider-side issues,
and volumetric DoS of the local server.

## Known design limitations (not vulnerabilities, but you should know)

- Settings (including API keys) are **plaintext JSON** on disk.
- The sidecar API binds to loopback only, but has no auth token by default —
  any local process can call it. Sandboxing it is on the roadmap.
- The distributed dmg is currently **ad-hoc / self-signed, not notarized**
  (Gatekeeper requires right-click → Open on first launch). Verify downloads
  against the SHA-256 in release notes when it matters.
