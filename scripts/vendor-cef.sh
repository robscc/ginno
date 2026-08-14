#!/usr/bin/env bash
# Download Spotify's official CEF "minimal" binary and stage
# Chromium Embedded Framework.framework for the Tauri bundle.
#
# The framework is NOT committed. make app / this script fetch it into
# ~/.cache/ginno/cef and copy it to apps/desktop/Frameworks/.
#
# Usage:
#   scripts/vendor-cef.sh            # pin + extract if missing
#   scripts/vendor-cef.sh --force    # re-download
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ROOT}/apps/desktop/Frameworks"
CACHE="${GINNO_CEF_CACHE:-${HOME}/.cache/ginno/cef}"
CDN="https://cef-builds.spotifycdn.com"

# Pin to a known-good 151.x Spotify build. Bump both version + sha together.
# Source: https://cef-builds.spotifycdn.com/index.html (macosarm64 / macosx64)
CEF_VERSION="${GINNO_CEF_VERSION:-151.3.17+gf059e67+chromium-151.0.7922.138}"

arch="$(uname -m)"
case "${arch}" in
  arm64)  CEF_ARCH="macosarm64"; CEF_SHA="${GINNO_CEF_SHA:-b4ebe97348e8feac0cda40656475f7652e326a50}" ;;
  x86_64) CEF_ARCH="macosx64";   CEF_SHA="${GINNO_CEF_SHA:-}" ;;
  *) echo "unsupported arch: ${arch}" >&2; exit 1 ;;
esac

# Filename uses the version as-is (plus signs stay).
TARBALL="cef_binary_${CEF_VERSION}_${CEF_ARCH}_minimal.tar.bz2"
# Spotify URL-encodes '+' as '%2B'.
ENC_VERSION="${CEF_VERSION//+/%2B}"
URL="${CDN}/cef_binary_${ENC_VERSION}_${CEF_ARCH}_minimal.tar.bz2"

FW_NAME="Chromium Embedded Framework.framework"
STAGED="${DEST}/${FW_NAME}"
MARKER="${DEST}/.cef-version"

if [[ "${1:-}" != "--force" && -d "${STAGED}" && -f "${MARKER}" && "$(cat "${MARKER}")" == "${CEF_VERSION}" ]]; then
  echo "✅ CEF ${CEF_VERSION} already staged at ${STAGED}"
  exit 0
fi

mkdir -p "${CACHE}" "${DEST}"
tarball_path="${CACHE}/${TARBALL}"

need_dl=1
if [[ -f "${tarball_path}" ]]; then
  if [[ -n "${CEF_SHA}" ]]; then
    got="$(shasum -a 1 "${tarball_path}" | awk '{print $1}')"
    if [[ "${got}" == "${CEF_SHA}" ]]; then
      need_dl=0
    else
      echo "cached tarball sha1 ${got} != ${CEF_SHA}; re-downloading"
    fi
  else
    need_dl=0
  fi
fi

if [[ "${need_dl}" == "1" ]]; then
  echo "↓ ${URL}"
  tmp="${tarball_path}.partial"
  curl -fL --retry 3 --retry-delay 2 -o "${tmp}" "${URL}"
  if [[ -n "${CEF_SHA}" ]]; then
    got="$(shasum -a 1 "${tmp}" | awk '{print $1}')"
    if [[ "${got}" != "${CEF_SHA}" ]]; then
      echo "sha1 mismatch: got ${got} want ${CEF_SHA}" >&2
      rm -f "${tmp}"
      exit 1
    fi
  fi
  mv "${tmp}" "${tarball_path}"
fi

extract="${CACHE}/extract-${CEF_VERSION}-${CEF_ARCH}"
rm -rf "${extract}"
mkdir -p "${extract}"
echo "unpack ${tarball_path}"
tar -xjf "${tarball_path}" -C "${extract}"

# Layout: cef_binary_<ver>_<arch>_minimal/Release/Chromium Embedded Framework.framework
found="$(find "${extract}" -type d -name "${FW_NAME}" -print -quit || true)"
if [[ -z "${found}" ]]; then
  echo "could not find ${FW_NAME} inside ${tarball_path}" >&2
  find "${extract}" -maxdepth 4 -type d | head -40 >&2
  exit 1
fi

rm -rf "${STAGED}"
mkdir -p "${DEST}"
cp -R "${found}" "${STAGED}"
# Drop quarantine so codesign / Gatekeeper don't trip on a freshly fetched binary.
xattr -cr "${STAGED}" 2>/dev/null || true
printf '%s\n' "${CEF_VERSION}" > "${MARKER}"
echo "✅ staged ${STAGED} (${CEF_VERSION})"
