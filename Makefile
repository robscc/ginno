# Ginno — desktop app build
#
# Reproduces the full packaged-app pipeline documented in
# docs/p3-packaging-notes.md:
#
#     web (Next static export)  →  runtime (PyInstaller --onedir bundle with
#     web_out + all deps)  →  staged as a Tauri resource  →  Tauri app + dmg.
#
# The runtime is a PyInstaller *onedir* bundle (executable + _internal/), not
# --onefile: onefile re-extracts ~3000 files into a fresh temp dir on every
# launch, and macOS endpoint-security scanning of each freshly-written library
# made every start take 15-25s. With onedir the files live at a stable, signed
# path inside Ginno.app, so they are scanned once and cached — subsequent
# starts drop to ~1-2s.
#
# Usage:
#     make app        # full rebuild → apps/desktop/target/release/bundle/...
#     make runtime    # just the PyInstaller bundle (dist/ginno-runtime/)
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
WEB_OUT := $(ROOT)/apps/web/out
RUNTIME := $(ROOT)/packages/runtime
# Where the onedir bundle is staged; tauri.conf.json bundles it into
# Contents/Resources/resources/runtime/ and lib.rs launches the executable.
RUNTIME_RES := $(ROOT)/apps/desktop/resources/runtime

.PHONY: all app sidecar runtime web clean help e2e-ui

all: app

## app: full rebuild — web + runtime bundle + Tauri desktop app (+ dmg)
app: sidecar
	@# Unlock the dedicated codesign keychain (locked after sleep/reboot). It
	@# holds the self-signed "Ginno Local Code Signing" identity that keeps a
	@# stable designated requirement across rebuilds, so macOS TCC grants
	@# (Desktop/Documents access prompts) persist instead of resetting.
	@security unlock-keychain -p ginno $(HOME)/Library/Keychains/ginno-codesign.keychain-db 2>/dev/null || true
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

## sidecar: stage the runtime onedir bundle as a Tauri resource
sidecar: runtime
	rm -rf $(RUNTIME_RES)
	mkdir -p $(RUNTIME_RES)
	cp -R $(RUNTIME)/dist/ginno-runtime/ $(RUNTIME_RES)/
	@find $(RUNTIME_RES) -name .DS_Store -delete
	@echo "✅ Runtime bundle → $(RUNTIME_RES)"

## runtime: PyInstaller onedir bundle with web_out + all deps (incl. docs extra)
# `--extra docs` installs the file-parsing deps. files/extractors.py imports
# them lazily (keeps startup light); the --collect-all flags bundle them
# regardless, so no eager import at startup is needed (see _frozen_imports.py).
runtime: web
	cd $(RUNTIME) && uv run --extra docs pyinstaller --noconfirm --onedir --paths src --name ginno-runtime \
	  --collect-all langchain_openai --collect-all langchain_anthropic \
	  --collect-all langgraph --collect-all mcp --collect-all pydantic \
	  --collect-all pandas --collect-all python_calamine --collect-all openpyxl \
	  --collect-all docx --collect-all pptx --collect-all pypdf \
	  --add-data "$(WEB_OUT):web_out" \
	  bin/ginno-runtime.py
	@echo "✅ Runtime → $(RUNTIME)/dist/ginno-runtime/"

## web: build the Next.js static export (bundled into the runtime as web_out/)
web:
	cd $(ROOT) && pnpm --filter @ginno/web build

## e2e-ui: packaged-UI Playwright e2e — 真浏览器验证列表/添加session（缺 chromium 自动安装）
# Sync with --extra docs too: a bare `--group test` sync would UNINSTALL the
# docs extras (pandas/openpyxl/…), silently breaking the files-preview unit
# tests afterward.
e2e-ui:
	cd $(RUNTIME) && uv sync --group test --extra docs && uv run --group test --extra docs pytest tests/e2e/test_packaged_ui_playwright.py -q

## clean: remove build artifacts (web export, PyInstaller output, staged bundle)
clean:
	rm -rf $(WEB_OUT)
	rm -rf $(RUNTIME)/dist $(RUNTIME)/build $(RUNTIME)/ginno-runtime.spec
	rm -rf $(RUNTIME_RES) $(ROOT)/apps/desktop/binaries

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE) | sed 's/## /  /'
