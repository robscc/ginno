#!/usr/bin/env bash
# Copy Ginno Helper.app variants into a packaged Ginno.app and re-sign.
# Tauri's bundle.macOS.frameworks only copies .framework / .dylib; the
# four Helper.app bundles have to be stamped in after `tauri build`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-}"
if [[ -z "${APP}" || ! -d "${APP}" ]]; then
  echo "usage: $0 /path/to/Ginno.app" >&2
  exit 1
fi

SRC="${ROOT}/apps/desktop/Frameworks"
DEST="${APP}/Contents/Frameworks"
IDENT="${APPLE_SIGNING_IDENTITY:-Ginno Local Code Signing}"
ENT="${ROOT}/apps/desktop/entitlements.plist"

if [[ ! -d "${SRC}/Ginno Helper.app" ]]; then
  echo "❌ ${SRC}/Ginno Helper.app not staged — run scripts/build-cef-host.sh" >&2
  exit 1
fi

mkdir -p "${DEST}"
for name in "Ginno Helper" "Ginno Helper (GPU)" "Ginno Helper (Plugin)" "Ginno Helper (Renderer)"; do
  rm -rf "${DEST}/${name}.app"
  cp -R "${SRC}/${name}.app" "${DEST}/${name}.app"
  xattr -cr "${DEST}/${name}.app" 2>/dev/null || true
done
if [[ -f "${SRC}/libginno_cef.dylib" ]]; then
  cp "${SRC}/libginno_cef.dylib" "${DEST}/libginno_cef.dylib"
  xattr -cr "${DEST}/libginno_cef.dylib" 2>/dev/null || true
fi

# Keychain may already be unlocked by `make app`; best-effort here too.
security unlock-keychain -p ginno "${HOME}/Library/Keychains/ginno-codesign.keychain-db" 2>/dev/null || true

sign() {
  local target="$1"
  codesign --force --sign "${IDENT}" --entitlements "${ENT}" --options runtime "${target}"
}

for name in "Ginno Helper" "Ginno Helper (GPU)" "Ginno Helper (Plugin)" "Ginno Helper (Renderer)"; do
  sign "${DEST}/${name}.app/Contents/MacOS/${name}"
  sign "${DEST}/${name}.app"
done
if [[ -f "${DEST}/libginno_cef.dylib" ]]; then
  codesign --force --sign "${IDENT}" --options runtime "${DEST}/libginno_cef.dylib"
fi
# Re-seal the .app after adding Helper.app. Do not --deep (Tauri already
# signed the rest; we only need a valid outer signature).
sign "${APP}"
echo "✅ staged + signed CEF helpers in ${DEST}"
