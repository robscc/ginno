"""Claude Code Mods compatibility layer (claude-code-mods-design.md §5).

Python side of the mods bus: ModChannel (Unix-socket client for the Rust
ginno-mod-broker), the five P0 event taps, the runtime-backed ``$.session``
op implementations, the FastAPI management routes, and the node/broker
binary discovery for dev/web mode. Config ownership stays here
(settings.json ``mods.*``); the broker owns runtime state.
"""
