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
ROOT    := $(CURDIR)
TRIPLE  := $(shell rustc -vV | grep host | awk '{print $$2}')
WEB_OUT := $(ROOT)/apps/web/out
RUNTIME := $(ROOT)/packages/runtime
SIDECAR := $(ROOT)/apps/desktop/binaries/ginno-runtime-$(TRIPLE)

.PHONY: all app sidecar runtime web clean help

all: app

## app: full rebuild — web + runtime sidecar + Tauri desktop app (+ dmg)
app: sidecar
	cd $(ROOT)/apps/desktop && pnpm tauri build
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

## clean: remove build artifacts (web export + PyInstaller output)
clean:
	rm -rf $(WEB_OUT)
	rm -rf $(RUNTIME)/dist $(RUNTIME)/build $(RUNTIME)/ginno-runtime.spec

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
