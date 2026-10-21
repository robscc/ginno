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

.PHONY: all app app-force app-impl sidecar runtime web mod-broker mod-runner clean help e2e-ui

all: app

# 构建缓存（内容戳）：阶段未变则跳过。改一行 web 不再重跑 3-5 分钟的
# PyInstaller；clean 会一并清掉。戳 = 输入文件列表的内容哈希。
CACHE := $(ROOT)/.build-cache

## app: full rebuild — i18n check + web + runtime bundle + Tauri desktop app (+ dmg)
# The running-app guard first: rebuilding the runtime bundle in place while a
# live Ginno still maps it is the known zlib-extract corruption (CLAUDE.md
# 已知故障 #1). FORCE=1 skips the guard.
# 内层 $(MAKE) -j app-impl：mod-broker / mod-runner / sidecar(web→runtime)
# 三条独立链并行（guard 先串行通过，保持 fail-fast）。
app: check guard-app-not-running
	@$(MAKE) -j app-impl
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

## app-impl: app 的并行构建段（mod-broker / mod-runner / sidecar 三链并发）
app-impl: mod-broker mod-runner sidecar

## pnpm-deps: 单次串行的 workspace 安装。mod-runner 与 web 两条链在 -j 下
## 并行，各自 `pnpm install` 会同时改根 node_modules 的 .pnpm hoist——
## 2026-10-21 打包失败即此竞态（两个 headless install 抢 unlink
## .pnpm/node_modules/autoprefixer → ENOENT → Error 254，间歇复现）。
## 两目标改为依赖本目标：安装只跑一次、且先于并行段开始。
pnpm-deps:
	cd $(ROOT) && pnpm install --prefer-offline

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

## runtime-wheel: pure-python wheel of the runtime (benchmark harness delivery).
# workbuddy-bench freezes this into a PyInstaller onefile inside manylinux_2_28
# (configs/harnesses/ginno/docker/Dockerfile in the bench fork). Pure python →
# the wheel built on macOS installs fine on Linux.
runtime-wheel:
	cd $(RUNTIME) && uv build --out-dir dist
	@echo "✅ Wheel → $(RUNTIME)/dist/ginno_runtime-*.whl"
	@echo "   stage into the bench: scripts/ginno/stage-wheel.sh <wheel>"

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
# 内容戳：源未变且 dist 在 → 跳过。
mod-runner: pnpm-deps
	@mkdir -p $(CACHE); \
	h=$$( (find $(ROOT)/packages/mod-runner/src $(ROOT)/packages/mod-runner/package.json -type f 2>/dev/null | sort | xargs cat 2>/dev/null) | shasum | cut -d' ' -f1); \
	if [ -f $(CACHE)/mod-runner.stamp ] && [ "$$(cat $(CACHE)/mod-runner.stamp)" = "$$h" ] && [ -f $(ROOT)/packages/mod-runner/dist/mod-runner.mjs ]; then \
	  echo "⏭️  mod-runner 未变，跳过"; \
	else \
	  cd $(ROOT) && pnpm --filter @ginno/mod-runner build && echo "$$h" > $(CACHE)/mod-runner.stamp; \
	fi
	@echo "✅ Runner bundle → $(ROOT)/packages/mod-runner/dist/mod-runner.mjs"

## runtime: PyInstaller onedir bundle with web_out + all deps (incl. docs extra)
# `--extra docs` installs the file-parsing deps. files/extractors.py imports
# them lazily (keeps startup light); the --collect-all flags bundle them
# regardless, so no eager import at startup is needed (see _frozen_imports.py).
# 内容戳：runtime 源/web_out/extension 未变且 bundle 在 → 跳过（改一行 web
# 不再重跑几分钟的 PyInstaller）。FORCE_RUNTIME=1 强制重打。
runtime: web
	@mkdir -p $(CACHE); \
	h=$$( (find $(RUNTIME)/src $(RUNTIME)/bin $(ROOT)/packages/extension/src $(ROOT)/packages/extension/native-host -type f 2>/dev/null | sort | xargs cat 2>/dev/null; \
	       find $(WEB_OUT) -type f 2>/dev/null | sort | xargs shasum 2>/dev/null; \
	       cat $(RUNTIME)/pyproject.toml) | shasum | cut -d' ' -f1); \
	if [ "$(FORCE_RUNTIME)" != "1" ] && [ -f $(CACHE)/runtime.stamp ] && [ "$$(cat $(CACHE)/runtime.stamp)" = "$$h" ] && [ -x $(RUNTIME)/dist/ginno-runtime/ginno-runtime ]; then \
	  echo "⏭️  runtime 输入未变，跳过 PyInstaller"; \
	else \
	  cd $(RUNTIME) && uv run --locked --extra docs pyinstaller --noconfirm --onedir --paths src --name ginno-runtime \
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
	  bin/ginno-runtime.py && echo "$$h" > $(CACHE)/runtime.stamp; \
	fi
	@echo "✅ Runtime → $(RUNTIME)/dist/ginno-runtime/"

## web: build the Next.js static export (bundled into the runtime as web_out/)
# pnpm install first: a fresh pull may add deps (lockfile-synced, ~1s no-op
# when up to date) — without it `next build` fails on missing modules.
# 内容戳：src/messages/配置/lockfile 未变且 out/ 在 → 跳过。
web: pnpm-deps
	@mkdir -p $(CACHE); \
	h=$$( (find $(ROOT)/apps/web/src $(ROOT)/apps/web/messages -type f 2>/dev/null | sort | xargs cat 2>/dev/null; \
	       cat $(ROOT)/apps/web/package.json $(ROOT)/apps/web/next.config.* $(ROOT)/apps/web/tailwind.config.* $(ROOT)/pnpm-lock.yaml 2>/dev/null) | shasum | cut -d' ' -f1); \
	if [ -f $(CACHE)/web.stamp ] && [ "$$(cat $(CACHE)/web.stamp)" = "$$h" ] && [ -d $(WEB_OUT) ]; then \
	  echo "⏭️  web 未变，跳过 next build"; \
	else \
	  cd $(ROOT) && pnpm --filter @ginno/web build && echo "$$h" > $(CACHE)/web.stamp; \
	fi; \
	# 防呆（2026-10-14 icon 事故）：next build 曾静默漏拷整个 public/ 到 out/
	# （app 内 /icon.png 被 sidecar catch-all 兜底成 index.html → icon 消失，
	# 构建全程无报错；且 stamp 已写，后续构建跳过重建、沿用坏产物）。校验
	# 因此必须独立于 skip/重建两条路径、每次都跑：缺了就补拷自愈，补不上
	# 则构建失败——宁可 loudly fail，不可静默带病出包。
	for f in $(ROOT)/apps/web/public/*; do \
	  [ -e "$$f" ] || continue; \
	  b=$$(basename "$$f"); \
	  if [ ! -e "$(WEB_OUT)/$$b" ]; then \
	    echo "⚠️  导出缺 public 资源 $$b，已补拷"; \
	    cp -R "$$f" "$(WEB_OUT)/$$b" || { echo "❌ 补拷失败：$$b"; exit 1; }; \
	  fi; \
	done

.PHONY: check
## check: i18n catalog 一致性检查（i18n-design.md §10.2；app 的前置依赖，构建即拦截）
check:
	python3 $(ROOT)/scripts/check_i18n.py

## e2e-ui: packaged-UI Playwright e2e — 真浏览器验证列表/添加session（缺 chromium 自动安装）
# Sync with --extra docs too: a bare `--group test` sync would UNINSTALL the
# docs extras (pandas/openpyxl/…), silently breaking the files-preview unit
# tests afterward.
e2e-ui:
	cd $(RUNTIME) && uv sync --locked --group test --extra docs && uv run --locked --group test --extra docs pytest tests/e2e/test_packaged_ui_playwright.py -q

## clean: remove build artifacts (web export, PyInstaller output, staged bundle)
clean:
	rm -rf $(WEB_OUT)
	rm -rf $(RUNTIME)/dist $(RUNTIME)/build $(RUNTIME)/ginno-runtime.spec
	rm -rf $(RUNTIME_RES) $(RUNNER_RES) $(ROOT)/apps/desktop/binaries
	rm -rf $(ROOT)/target $(ROOT)/crates/mod-broker/target
	rm -rf $(CACHE)

.PHONY: loc
## loc: 统计代码行数（按树分组，排除构建产物/依赖；零依赖 find+wc，无需 cloc）
loc:
	@ex='( -name node_modules -o -name .venv -o -name __pycache__ -o -name dist \
	      -o -name build -o -name out -o -name target -o -name .next \
	      -o -name coverage -o -name .turbo -o -name .git ) -prune -o -type f'; \
	count() { \
	  n=$$(find "$$1" $$ex \( $$2 \) -print 2>/dev/null | wc -l | tr -d ' '); \
	  l=$$(find "$$1" $$ex \( $$2 \) -print0 2>/dev/null | xargs -0 cat 2>/dev/null | wc -l | tr -d ' '); \
	  printf '%-26s %8s %12s\n' "$$3" "$$n" "$$l"; \
	  tn=$$((tn + n)); tl=$$((tl + l)); \
	}; \
	tn=0; tl=0; \
	printf '%-26s %8s %12s\n' 'tree' 'files' 'lines'; \
	count $(RUNTIME)/src       '-name *.py'          'runtime src (py)'; \
	count $(RUNTIME)/tests     '-name *.py'          'runtime tests (py)'; \
	count $(ROOT)/apps/web/src '-name *.ts -o -name *.tsx' 'web src (ts/tsx)'; \
	count $(ROOT)/apps/desktop/src '-name *.rs'      'desktop (rs)'; \
	count $(ROOT)/crates/mod-broker/src '-name *.rs' 'mod-broker (rs)'; \
	count $(ROOT)/packages/mod-runner '-name *.mjs -o -name *.ts' 'mod-runner'; \
	count $(ROOT)/packages/extension '-name *.js -o -name *.py'   'extension'; \
	count $(ROOT)/scripts     '-name *.py -o -name *.sh' 'scripts'; \
	printf '%-26s %8s %12s\n' '──────' '──────' '────────'; \
	printf '%-26s %8s %12s\n' 'total' "$$tn" "$$tl"

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
