# Ginno — desktop app build
#
# Reproduces the full packaged-app pipeline documented in
# docs/p3-packaging-notes.md:
#
#     web (Next static export)  →  runtime (PyInstaller sidecar bundling
#     web_out + all deps)  →  sidecar (copied with target-triple suffix)
#     →  Tauri app + dmg.
#
# Usage:
#     make app        # full rebuild → apps/desktop/target/release/bundle/...
#     make runtime    # just the PyInstaller sidecar (dist/ginno-runtime)
#     make web        # just the web static export
#     make clean      # remove build artifacts
#
# NOTE: `make app` overwrites apps/desktop/target/release/bundle/macos/Ginno.app
# — quit the running Ginno.app first, or the bundle step can hit a file lock.

SHELL   := /bin/bash
# Make runs a non-interactive shell (no rc files), so ensure the rustup
# shims are visible even when invoked outside a login shell; otherwise
# TRIPLE resolves to empty and the tauri build can't find its sidecar.
export PATH := $(HOME)/.cargo/bin:$(PATH)
ROOT    := $(CURDIR)
# macOS ships GNU make 3.81, where `export PATH` above does not reach
# parse-time $(shell) calls — so put the cargo shims on PATH inline here.
TRIPLE  := $(shell PATH="$(HOME)/.cargo/bin:$$PATH" rustc -vV 2>/dev/null | grep host | awk '{print $$2}')
ifeq ($(TRIPLE),)
$(error rustc not found — install the Rust toolchain via rustup; shims expected in ~/.cargo/bin)
endif
WEB_OUT := $(ROOT)/apps/web/out
RUNTIME := $(ROOT)/packages/runtime
SIDECAR := $(ROOT)/apps/desktop/binaries/ginno-runtime-$(TRIPLE)

.PHONY: all app sidecar runtime web clean help e2e-ui

all: app

## app: full rebuild — web + runtime sidecar + Tauri desktop app (+ dmg)
app: sidecar
	cd $(ROOT)/apps/desktop && pnpm tauri build
	@# Regression guard: an only-linker-signed .app makes WKWebView's
	@# Networking helper reject every request -> the webview white-screens
	@# while the sidecar looks perfectly healthy. bundle.macOS.signingIdentity
	@# in tauri.conf.json must produce a real (ad-hoc or better) signature.
	@app="$(ROOT)/apps/desktop/target/release/bundle/macos/Ginno.app"; \
	if codesign -dvvv "$$app" 2>&1 | grep -q linker-signed; then \
	  echo "❌ $$app is only linker-signed — the webview will white-screen."; \
	  echo "   Set bundle.macOS.signingIdentity in apps/desktop/tauri.conf.json."; \
	  exit 1; \
	fi
	@echo "✅ Code signature OK (not linker-signed)"
	@echo ""
	@echo "✅ Built:"
	@echo "   $(ROOT)/apps/desktop/target/release/bundle/macos/Ginno.app"
	@echo "   $(ROOT)/apps/desktop/target/release/bundle/dmg/"

## sidecar: build the runtime and place it as the Tauri sidecar (triple suffix)
sidecar: runtime
	cp $(RUNTIME)/dist/ginno-runtime $(SIDECAR)
	@echo "✅ Sidecar → $(SIDECAR)"

## runtime: PyInstaller sidecar bundling web_out + all deps (incl. docs extra)
# `--extra docs` installs the file-parsing deps; files/extractors.py imports
# them lazily, so _frozen_imports.py (imported by the entry script) re-imports
# them for PyInstaller, and the --collect-all flags grab their data files.
runtime: web
	cd $(RUNTIME) && uv run --extra docs pyinstaller --onefile --paths src --name ginno-runtime \
	  --collect-all langchain_openai --collect-all langchain_anthropic \
	  --collect-all langgraph --collect-all mcp --collect-all pydantic \
	  --collect-all pandas --collect-all python_calamine --collect-all openpyxl \
	  --collect-all docx --collect-all pptx --collect-all pypdf \
	  --add-data "$(WEB_OUT):web_out" \
	  bin/ginno-runtime.py
	@echo "✅ Runtime → $(RUNTIME)/dist/ginno-runtime"

## web: build the Next.js static export (bundled into the sidecar as web_out/)
web:
	cd $(ROOT) && pnpm --filter @ginno/web build

## e2e-ui: packaged-UI Playwright e2e — 真浏览器验证列表/添加session（缺 chromium 自动安装）
e2e-ui:
	cd $(RUNTIME) && uv sync --group test && uv run --group test pytest tests/e2e/test_packaged_ui_playwright.py -q

## clean: remove build artifacts (web export + PyInstaller output)
clean:
	rm -rf $(WEB_OUT)
	rm -rf $(RUNTIME)/dist $(RUNTIME)/build $(RUNTIME)/ginno-runtime.spec

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
