/* Public ABI for libginno_cef.dylib.
 *
 * Rust dlopens this. The dylib itself dlopens Chromium Embedded
 * Framework.framework — cargo never links CEF. No Space / ownership
 * lives here; the sidecar talks CDP to remote_debugging_port.
 */
#ifndef GINNO_CEF_H_
#define GINNO_CEF_H_

#ifdef __cplusplus
extern "C" {
#endif

/* Load the framework and cef_initialize. parent_view may be NULL; pass
 * the hole NSView when you have it so the first Alloy browser is a
 * child of the atrium wrapper (created asynchronously after
 * OnContextInitialized — never create_browser_sync from launch).
 * Returns 1 on success. Idempotent. */
int ginno_cef_init(const char* framework_dir, const char* helper_exe,
                   const char* main_bundle, const char* cache_path,
                   int debug_port, void* parent_nsview);

/* Create (or resize) the Alloy browser as a child of parent_nsview. */
int ginno_cef_attach(void* parent_nsview, int width, int height);

void ginno_cef_set_hidden(int hidden);

/* WKWebView sits on top of the hole. When passthrough is on, hitTest in
 * the wrapper rect is forwarded to the CEF child so the user can click
 * the real page. Agent-owned tiles leave this off so React can take over. */
void ginno_cef_install_hittest(void* webview, void* wrapper);
void ginno_cef_set_passthrough(int enabled);

int ginno_cef_ready(void);
int ginno_cef_debug_port(void);
const char* ginno_cef_last_error(void);

#ifdef __cplusplus
}
#endif

#endif /* GINNO_CEF_H_ */
