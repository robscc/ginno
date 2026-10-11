// ---------------------------------------------------------------------------
// Code panel file operations — docs/code-panel-design.md §4.4, brief §3.3.
//
// These are the shell's first non-pin commands. Each takes an ABSOLUTE `root`
// and an ABSOLUTE `path` (the webview composes them from `/api/code/roots`),
// resolves the path itself, and refuses anything that lands outside the root.
// ---------------------------------------------------------------------------

use std::path::PathBuf;

/// Error contract for the code_* commands: `{code, detail}` — the shell holds
/// no user-facing copy (i18n-design.md §7), so `code` is a stable identifier
/// the web panel maps to `code.notice.err.<code>` in the message catalog, and
/// `detail` carries the raw OS/path context shown as-is. Older shells / non-
/// object errors keep the pre-existing generic-fallback path in the panel.
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CodeCmdError {
    code: &'static str,
    detail: String,
}

impl CodeCmdError {
    fn new(code: &'static str, detail: impl std::fmt::Display) -> Self {
        Self {
            code,
            detail: detail.to_string(),
        }
    }
}

/// Second, independent containment check — neither substitute nor courtesy.
///
/// The sidecar already refused a path outside its root, but everything arriving
/// over IPC from the webview is untrusted input: a bug (or a compromised page)
/// could invoke these commands with any two strings, so the shell re-derives the
/// boundary on its own. ``canonicalize`` dereferences symlinks, so a
/// lexically-contained link pointing out of the root is refused here exactly as
/// S1's fence gate 3 refuses it in Python (design §4.6).
///
/// Returns ``(root_real, path_real)``. Both must exist — every command below
/// acts on real bytes on disk (reveal / open / trash), so a path that cannot be
/// canonicalized is an error rather than something to guess around.
fn code_resolve_in_root(root: &str, path: &str) -> Result<(PathBuf, PathBuf), CodeCmdError> {
    let root_real = std::fs::canonicalize(root)
        .map_err(|e| CodeCmdError::new("resolve-root", format!("{root} ({e})")))?;
    let path_real = std::fs::canonicalize(path)
        .map_err(|e| CodeCmdError::new("resolve-path", format!("{path} ({e})")))?;
    if path_real != root_real && !path_real.starts_with(&root_real) {
        return Err(CodeCmdError::new("outside-root", path));
    }
    Ok((root_real, path_real))
}

/// Reveal a workspace entry in Finder / Explorer (design §4.4).
#[tauri::command]
pub(crate) fn code_reveal(root: String, path: String) -> Result<(), CodeCmdError> {
    let (_root_real, target) = code_resolve_in_root(&root, &path)?;
    tauri_plugin_opener::reveal_item_in_dir(&target)
        .map_err(|e| CodeCmdError::new("reveal-failed", e))
}

/// Open a workspace file with the OS default application (design §4.4).
///
/// Files only: "open a directory in its default app" is a Finder action, and
/// the panel already has `code_reveal` for that.
#[tauri::command]
pub(crate) fn code_open_external(root: String, path: String) -> Result<(), CodeCmdError> {
    let (_root_real, target) = code_resolve_in_root(&root, &path)?;
    if !target.is_file() {
        return Err(CodeCmdError::new("not-file", target.display().to_string()));
    }
    tauri_plugin_opener::open_path(&target, None::<&str>)
        .map_err(|e| CodeCmdError::new("open-failed", e))
}

/// Delete = move to the OS trash (design D6) — never a permanent unlink.
///
/// This is the real delete path in the app; the server endpoint's `.trash-*`
/// fallback (api/code_fsops.py) only runs in the browser / dev build.
#[tauri::command]
pub(crate) fn code_trash(root: String, path: String) -> Result<(), CodeCmdError> {
    let (root_real, target) = code_resolve_in_root(&root, &path)?;
    // A root is never deletable (design §4.6), same rule the sidecar enforces.
    if target == root_real {
        return Err(CodeCmdError::new("root-not-deletable", ""));
    }
    trash::delete(&target).map_err(|e| CodeCmdError::new("trash-failed", e))
}
