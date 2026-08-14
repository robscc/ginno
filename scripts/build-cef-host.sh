#!/usr/bin/env bash
# Compile Ginno's CEF helper + libginno_cef.dylib and stage them next to
# Chromium Embedded Framework.framework.
#
# cmake is not required. Headers come from Spotify's *standard* tarball
# (the *minimal* tarball we ship has no include/). The dylib dlopens the
# framework at runtime — cargo never links CEF.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ROOT}/apps/desktop/Frameworks"
SRC="${ROOT}/apps/desktop/cef"
CACHE="${GINNO_CEF_CACHE:-${HOME}/.cache/ginno/cef}"
CDN="https://cef-builds.spotifycdn.com"

CEF_VERSION="${GINNO_CEF_VERSION:-151.3.17+gf059e67+chromium-151.0.7922.138}"
arch="$(uname -m)"
case "${arch}" in
  arm64)  CEF_ARCH="macosarm64"; STD_SHA="${GINNO_CEF_STD_SHA:-da0d745ac91cabc252eaa53c3c60c2aa60c73991}" ;;
  x86_64) CEF_ARCH="macosx64";   STD_SHA="${GINNO_CEF_STD_SHA:-}" ;;
  *) echo "unsupported arch: ${arch}" >&2; exit 1 ;;
esac

ENC_VERSION="${CEF_VERSION//+/%2B}"
STD_TARBALL="cef_binary_${CEF_VERSION}_${CEF_ARCH}.tar.bz2"
STD_URL="${CDN}/cef_binary_${ENC_VERSION}_${CEF_ARCH}.tar.bz2"
INCLUDE_ROOT="${GINNO_CEF_INCLUDE:-${CACHE}/std-${CEF_VERSION}}"

CLANG="${CLANG:-/usr/bin/clang}"
MACOSX_MIN="${MACOSX_MIN:-11.0}"

mkdir -p "${DEST}" "${CACHE}" "${SRC}"

ensure_headers() {
  if [[ -f "${INCLUDE_ROOT}/include/capi/cef_app_capi.h" ]]; then
    return 0
  fi
  local tarball="${CACHE}/${STD_TARBALL}"
  local need_dl=1
  if [[ -f "${tarball}" ]]; then
    if [[ -n "${STD_SHA}" ]]; then
      local got
      got="$(shasum -a 1 "${tarball}" | awk '{print $1}')"
      if [[ "${got}" == "${STD_SHA}" ]]; then
        need_dl=0
      else
        echo "standard tarball sha1 ${got} != ${STD_SHA}; re-downloading"
      fi
    else
      need_dl=0
    fi
  fi
  if [[ "${need_dl}" == "1" ]]; then
    echo "↓ ${STD_URL}"
    local tmp="${tarball}.partial"
    curl -fL --retry 3 --retry-delay 2 -C - -o "${tmp}" "${STD_URL}"
    if [[ -n "${STD_SHA}" ]]; then
      local got
      got="$(shasum -a 1 "${tmp}" | awk '{print $1}')"
      if [[ "${got}" != "${STD_SHA}" ]]; then
        echo "sha1 mismatch: got ${got} want ${STD_SHA}" >&2
        rm -f "${tmp}"
        exit 1
      fi
    fi
    mv "${tmp}" "${tarball}"
  fi
  local extract="${CACHE}/extract-std-${CEF_VERSION}-${CEF_ARCH}"
  rm -rf "${extract}"
  mkdir -p "${extract}"
  echo "unpack headers from ${tarball}"
  tar -xjf "${tarball}" -C "${extract}"
  local found
  found="$(find "${extract}" -type d -name include -print -quit || true)"
  if [[ -z "${found}" ]]; then
    echo "could not find include/ in ${tarball}" >&2
    exit 1
  fi
  mkdir -p "${INCLUDE_ROOT}"
  rm -rf "${INCLUDE_ROOT}/include"
  cp -R "${found}" "${INCLUDE_ROOT}/include"
  echo "✅ CEF headers → ${INCLUDE_ROOT}/include"
}

write_helper_plist() {
  local name="$1"
  local ident="$2"
  local dest="$3"
  cat > "${dest}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>${name}</string>
  <key>CFBundleIdentifier</key>
  <string>${ident}</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>${name}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>0.1.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key>
  <true/>
</dict>
</plist>
EOF
}

stamp_helper() {
  local name="$1"
  local ident="$2"
  local bin="$3"
  local app="${DEST}/${name}.app"
  rm -rf "${app}"
  mkdir -p "${app}/Contents/MacOS"
  cp "${bin}" "${app}/Contents/MacOS/${name}"
  chmod +x "${app}/Contents/MacOS/${name}"
  write_helper_plist "${name}" "${ident}" "${app}/Contents/Info.plist"
  printf 'APPL????' > "${app}/Contents/PkgInfo"
  xattr -cr "${app}" 2>/dev/null || true
  echo "✅ ${app}"
}

ensure_headers

HELPER_BIN="${CACHE}/ginno-helper-${CEF_VERSION}"
need_helper=1
if [[ -f "${HELPER_BIN}" && "${SRC}/helper_main.c" -ot "${HELPER_BIN}" ]]; then
  need_helper=0
fi
if [[ "${need_helper}" == "1" ]]; then
  echo "cc helper_main.c"
  "${CLANG}" -std=c11 -O2 -mmacosx-version-min="${MACOSX_MIN}" \
    -DCEF_API_VERSION=15101 \
    -I "${INCLUDE_ROOT}" \
    -o "${HELPER_BIN}" "${SRC}/helper_main.c"
fi

stamp_helper "Ginno Helper"            "io.ginno.desktop.helper"          "${HELPER_BIN}"
stamp_helper "Ginno Helper (GPU)"      "io.ginno.desktop.helper.gpu"      "${HELPER_BIN}"
stamp_helper "Ginno Helper (Plugin)"   "io.ginno.desktop.helper.plugin"   "${HELPER_BIN}"
stamp_helper "Ginno Helper (Renderer)" "io.ginno.desktop.helper.renderer" "${HELPER_BIN}"

DYLIB="${DEST}/libginno_cef.dylib"
need_dylib=1
if [[ -f "${DYLIB}" && "${SRC}/ginno_cef.m" -ot "${DYLIB}" && "${SRC}/ginno_cef.h" -ot "${DYLIB}" ]]; then
  need_dylib=0
fi
if [[ "${need_dylib}" == "1" ]]; then
  echo "cc ginno_cef.m → libginno_cef.dylib"
  "${CLANG}" -std=c11 -fobjc-arc -O2 -dynamiclib \
    -mmacosx-version-min="${MACOSX_MIN}" \
    -DCEF_API_VERSION=15101 \
    -I "${INCLUDE_ROOT}" \
    -I "${SRC}" \
    -framework AppKit -framework CoreFoundation -framework WebKit \
    -Wl,-undefined,dynamic_lookup \
    -install_name @executable_path/../Frameworks/libginno_cef.dylib \
    -o "${DYLIB}" \
    "${SRC}/ginno_cef.m"
  xattr -cr "${DYLIB}" 2>/dev/null || true
fi
echo "✅ ${DYLIB}"
