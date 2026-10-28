/**
 * Client for the Python sidecar (FastAPI on 127.0.0.1:8787).
 * Dev: fixed port. Release: Tauri-managed sidecar (same port for now).
 *
 * This file is a thin barrel: the implementation lives in ./api/, one module
 * per domain. Consumers keep importing from "@/lib/runtime" unchanged.
 *
 * `core` is re-exported by name (not `export *`) so its internal helpers
 * (json / H / wsBase / OVERRIDE_PORT) stay private to the ./api modules.
 */

export { BASE, health } from "./api/core";
export * from "./api/sessions";
export * from "./api/models";
export * from "./api/workspace";
export * from "./api/chat";
export * from "./api/workflows";
export * from "./api/code";
export * from "./api/knowledge";
export * from "./api/connectors";
export * from "./api/schedule";
export * from "./api/tasks";
export * from "./api/misc";
