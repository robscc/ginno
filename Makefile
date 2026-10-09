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
#     make mod-broker # ginno-mod-broker CLI → target/release/ (Python 发现梯子)
#     make mod-runner # mod-runner.mjs bundle (packages/mod-runner/dist/)
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
# mod-runner.mjs staged beside it; tauri.conf.json bundles both under
# Contents/Resources/resources/ and lib.rs points GINNO_MOD_RUNNER_PATH at it.
RUNNER_RES := $(ROOT)/apps/desktop/resources/mod-runner.mjs

.PHONY: all app app-force sidecar runtime web mod-broker mod-runner clean help e2e-ui

all: app

## app: full rebuild — i18n check + web + runtime bundle + Tauri desktop app (+ dmg)
# The running-app guard first: rebuilding the runtime bundle in place while a
# live Ginno still maps it is the known zlib-extract corruption (CLAUDE.md
# 已知故障 #1). FORCE=1 skips the guard.
app: check guard-app-not-running mod-broker mod-runner sidecar
	@# Unlock the dedicated codesign keychain (locked after sleep/reboot). It
	@# holds the self-signed "Ginno Local Code Signing" identity that keeps a
	@# stable designated requirement across rebuilds, so macOS TCC grants
	@# (Desktop/Documents access prompts) persist instead of resetting.
	@security unlock-keychain -p ginno $(HOME)/Library/Keychains/ginno-codesign.keychain-db 2>/dev/null || true
	cd $(ROOT)/apps/desktop && node node_modules/@tauri-apps/cli/tauri.js build
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

## app-force: `app` without the running-app guard (FORCE=1) — convenience alias
# Exists because typing FORCE=1 in front of `make app` is easy to forget. It is
# a THIN alias: the guard's own warning and its reasoning are unchanged, and the
# post-build reminder below is printed unconditionally. If you want the safe
# path, quit Ginno first and use plain `make app`.
app-force:
	@$(MAKE) app FORCE=1
	@echo ""
	@echo "⚠️  Built while Ginno may have been running (guard skipped)."
	@echo "   FULLY QUIT AND RELAUNCH Ginno before verifying anything —"
	@echo "   otherwise lazy imports fail with 'zlib error: incorrect header"
	@echo "   check' and only a restart recovers (CLAUDE.md 已知故障 #1)."

## sidecar: stage the runtime onedir bundle as a Tauri resource
sidecar: runtime
	@# Never rm -rf the staging dir: Finder may recreate .DS_Store inside it
	@# mid-delete and rmdir would fail the build. rsync overwrites + prunes
	@# without rmdir-ing the watched dir, so the race cannot fail the build
	@# (a .DS_Store sneaking in mid-transfer just stays until find cleans it).
	mkdir -p $(RUNTIME_RES)
	rsync -a --delete $(RUNTIME)/dist/ginno-runtime/ $(RUNTIME_RES)/
	@find $(RUNTIME_RES) -name .DS_Store -delete
	@# mod-runner rides along as its own bundled resource (plain text, no
	@# signing concerns — design §11); mod-runner ran before us via app's
	@# prerequisite order, so the dist file exists.
	cp $(ROOT)/packages/mod-runner/dist/mod-runner.mjs $(RUNNER_RES)
	@echo "✅ Runtime bundle → $(RUNTIME_RES)"

## guard-app-not-running: refuse to build while a live Ginno maps the bundle
# Rebuilding the PyInstaller bundle in place under a running Ginno is the
# known zlib-extract failure (CLAUDE.md 已知故障 #1: PyInstaller re-opens the
# archive by path on every lazy extract; swapped bytes → garbage reads → only
# a restart recovers). Same for the broker/tauri app itself (file locks).
# FORCE=1 skips the guard for scripted rebuilds that already quit the app.
guard-app-not-running:
	@if [ "$(FORCE)" = "1" ]; then \
	  echo "⚠️  FORCE=1 — running-app guard skipped"; exit 0; \
	fi; \
	if pgrep -f "Ginno\.app/Contents/MacOS/Ginno" >/dev/null 2>&1; then \
	  echo "❌ Ginno.app is running. Replacing its bundle under a live process"; \
	  echo "   corrupts lazy PyInstaller extracts (zlib errors until restart)."; \
	  echo "   先完全退出 Ginno 再构建 (⌘Q, not just closing the window)."; \
	  echo "   (FORCE=1 to override)"; exit 1; \
	fi; \
	pids=$$(lsof -t -iTCP:8787 -sTCP:LISTEN 2>/dev/null); \
	if [ -n "$$pids" ]; then \
	  busy=""; \
	  for pid in $$pids; do \
	    if ps -p $$pid -o args= | grep -qE "ginno-runtime|ginno_runtime\.server|Ginno\.app"; then \
	      busy="$$busy $$pid"; \
	    fi; \
	  done; \
	  if [ -n "$$busy" ]; then \
	    echo "❌ Port 8787 is held by a Ginno sidecar ($$busy)."; \
	    echo "   退出 Ginno（或停掉 pnpm dev:runtime）再构建。(FORCE=1 to override)"; \
	    exit 1; \
	  fi; \
	fi; \
	echo "✅ No live Ginno / sidecar — safe to rebuild"

## mod-broker: the ginno-mod-broker CLI (dev/web mode — Python spawns it)
# --target-dir $(ROOT)/target puts the binary exactly on the runtime's
# discovery ladder (bridge_utils.py: <repo>/target/{debug,release}/). The
# desktop app does NOT use this binary — it links the crate in-process via a
# path dependency (design §2: one crate, two hosts).
mod-broker:
	cargo build --release --manifest-path $(ROOT)/crates/mod-broker/Cargo.toml --target-dir $(ROOT)/target
	@echo "✅ Broker CLI → $(ROOT)/target/release/ginno-mod-broker"

## mod-runner: the mod-runner.mjs single-file bundle (esbuild)
# Packaged form: sidecar stages it as a Tauri resource; dev/web form: the
# runtime's resolve_runner() finds it at packages/mod-runner/dist/.
mod-runner:
	cd $(ROOT) && pnpm install --prefer-offline && pnpm --filter @ginno/mod-runner build
	@echo "✅ Runner bundle → $(ROOT)/packages/mod-runner/dist/mod-runner.mjs"

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
	  --add-data "$(ROOT)/packages/extension/src:extension_src" \
	  --add-data "$(ROOT)/packages/extension/native-host:extension_src_native_host" \
	  --add-data "$(RUNTIME)/src/ginno_runtime/i18n:ginno_runtime/i18n" \
	  --add-data "$(RUNTIME)/src/ginno_runtime/skills/builtin:ginno_runtime/skills/builtin" \
	  --add-data "$(ROOT)/apps/web/messages:web_messages" \
	  bin/ginno-runtime.py
	@echo "✅ Runtime → $(RUNTIME)/dist/ginno-runtime/"

## web: build the Next.js static export (bundled into the runtime as web_out/)
# pnpm install first: a fresh pull may add deps (lockfile-synced, ~1s no-op
# when up to date) — without it `next build` fails on missing modules.
web:
	cd $(ROOT) && pnpm install --prefer-offline && pnpm --filter @ginno/web build

.PHONY: check
## check: i18n catalog 一致性检查（i18n-design.md §10.2；app 的前置依赖，构建即拦截）
check:
	python3 $(ROOT)/scripts/check_i18n.py

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
	rm -rf $(RUNTIME_RES) $(RUNNER_RES) $(ROOT)/apps/desktop/binaries
	rm -rf $(ROOT)/target $(ROOT)/crates/mod-broker/target

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
