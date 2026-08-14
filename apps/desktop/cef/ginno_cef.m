/* CEF host for the atrium hole.
 *
 * Compiled as a dylib and dlopened by the Tauri shell. Links nothing from
 * CEF at cargo-time: we dlopen the framework, then call the C API with
 * -undefined dynamic_lookup.
 *
 * Main-thread only (cef_initialize / create_browser / the pump).
 *
 * CEF 151 defaults to Chrome runtime style. A child NSView must be Alloy,
 * and the first browser must not be created with create_browser_sync from
 * inside didFinishLaunching — that CHECKs (SIGTRAP) before the external
 * pump has run OnContextInitialized.
 */

#import <AppKit/AppKit.h>
#import <CoreFoundation/CoreFoundation.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>

#include <dispatch/dispatch.h>
#include <dlfcn.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

#include "ginno_cef.h"

#include "include/cef_api_hash.h"
#include "include/capi/cef_app_capi.h"
#include "include/capi/cef_browser_capi.h"
#include "include/capi/cef_browser_process_handler_capi.h"
#include "include/capi/cef_client_capi.h"
#include "include/capi/cef_life_span_handler_capi.h"
#include "include/capi/cef_request_handler_capi.h"

#define GINNO_CEF_ERR_LEN 512

static char g_err[GINNO_CEF_ERR_LEN];
static int g_inited;
static int g_ready;
static int g_ctx_ready;
static int g_creating;
static int g_want_browser;
static int g_port;
static int g_pending_w;
static int g_pending_h;
static void* g_fw;
static cef_browser_t* g_browser;
static CFRunLoopTimerRef g_pump;
static CFRunLoopTimerRef g_pump_once;
static void* g_parent;

/* Timestamped breadcrumb trail to /tmp/ginno-cef-bc.log — correlates the
 * last host action with a Chromium-side crash (stripped .ips symbols are
 * useless on their own). */
static void bc(const char* fmt, ...) {
  FILE* f = fopen("/tmp/ginno-cef-bc.log", "a");
  if (f == NULL) {
    return;
  }
  struct timeval tv;
  gettimeofday(&tv, NULL);
  struct tm tmv;
  localtime_r(&tv.tv_sec, &tmv);
  fprintf(f, "%02d:%02d:%02d.%03d ", tmv.tm_hour, tmv.tm_min, tmv.tm_sec,
          (int)(tv.tv_usec / 1000));
  va_list ap;
  va_start(ap, fmt);
  vfprintf(f, fmt, ap);
  va_end(ap);
  fputc('\n', f);
  fclose(f);
}

typedef struct {
  cef_client_t client;
  atomic_int refs;
} ginno_client_t;

typedef struct {
  cef_app_t app;
  atomic_int refs;
} ginno_app_t;

typedef struct {
  cef_browser_process_handler_t handler;
  atomic_int refs;
} ginno_bph_t;

typedef struct {
  cef_life_span_handler_t handler;
  atomic_int refs;
} ginno_life_t;

typedef struct {
  cef_request_handler_t handler;
  atomic_int refs;
} ginno_req_t;

static ginno_client_t g_client;
static ginno_app_t g_app;
static ginno_bph_t g_bph;
static ginno_life_t g_life;
static ginno_req_t g_req;

static void set_err(const char* msg) {
  snprintf(g_err, sizeof(g_err), "%s", msg ? msg : "");
}

#define GINNO_RC_FUNCS(prefix, T)                                              \
  static void prefix##_add_ref(cef_base_ref_counted_t* self) {                 \
    T* c = (T*)self;                                                           \
    atomic_fetch_add(&c->refs, 1);                                             \
  }                                                                            \
  static int prefix##_release(cef_base_ref_counted_t* self) {                  \
    T* c = (T*)self;                                                           \
    int prev = atomic_fetch_sub(&c->refs, 1);                                  \
    return prev == 1;                                                          \
  }                                                                            \
  static int prefix##_has_one_ref(cef_base_ref_counted_t* self) {              \
    T* c = (T*)self;                                                           \
    return atomic_load(&c->refs) == 1;                                         \
  }                                                                            \
  static int prefix##_has_at_least_one_ref(cef_base_ref_counted_t* self) {     \
    T* c = (T*)self;                                                           \
    return atomic_load(&c->refs) >= 1;                                         \
  }

GINNO_RC_FUNCS(client, ginno_client_t)
GINNO_RC_FUNCS(app, ginno_app_t)
GINNO_RC_FUNCS(bph, ginno_bph_t)
GINNO_RC_FUNCS(life, ginno_life_t)
GINNO_RC_FUNCS(req, ginno_req_t)

static int create_browser(void* parent_nsview, int width, int height);

static void set_cef_str(cef_string_t* dst, const char* utf8) {
  if (dst == NULL) {
    return;
  }
  cef_string_clear(dst);
  if (utf8 == NULL || utf8[0] == '\0') {
    return;
  }
  cef_string_utf8_to_utf16(utf8, strlen(utf8), dst);
}

static void pump_cb(CFRunLoopTimerRef timer, void* info) {
  (void)timer;
  (void)info;
  if (g_inited) {
    cef_do_message_loop_work();
  }
}

static void start_pump(void) {
  if (g_pump != NULL) {
    return;
  }
  CFRunLoopTimerContext ctx = {0, NULL, NULL, NULL, NULL};
  g_pump = CFRunLoopTimerCreate(kCFAllocatorDefault,
                                CFAbsoluteTimeGetCurrent() + 0.01, 0.01, 0, 0,
                                pump_cb, &ctx);
  if (g_pump != NULL) {
    CFRunLoopAddTimer(CFRunLoopGetMain(), g_pump, kCFRunLoopCommonModes);
  }
}

static void bph_on_schedule(cef_browser_process_handler_t* self,
                            int64_t delay_ms) {
  (void)self;
  if (g_pump_once != NULL) {
    CFRunLoopTimerInvalidate(g_pump_once);
    CFRelease(g_pump_once);
    g_pump_once = NULL;
  }
  double wait = delay_ms <= 0 ? 0.001 : ((double)delay_ms / 1000.0);
  CFRunLoopTimerContext ctx = {0, NULL, NULL, NULL, NULL};
  g_pump_once = CFRunLoopTimerCreate(kCFAllocatorDefault,
                                     CFAbsoluteTimeGetCurrent() + wait, 0, 0, 0,
                                     pump_cb, &ctx);
  if (g_pump_once != NULL) {
    CFRunLoopAddTimer(CFRunLoopGetMain(), g_pump_once, kCFRunLoopCommonModes);
  }
}

static void fit_children(void* parent_nsview) {
  /* The CEF child is a plain NSView: pinning it to the parent's bounds on
   * every attach keeps it in sync when the tile resizes. (The old crash was
   * TaoApp missing isHandlingSendEvent — not this.) */
  if (parent_nsview == NULL) {
    return;
  }
  NSView* parent = (__bridge NSView*)parent_nsview;
  NSRect bounds = parent.bounds;
  for (NSView* child in parent.subviews) {
    child.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    child.frame = bounds;
  }
}

static int load_framework(const char* framework_dir) {
  if (g_fw != NULL) {
    return 1;
  }
  if (framework_dir == NULL || framework_dir[0] == '\0') {
    set_err("framework_dir is empty");
    return 0;
  }
  char path[4096];
  int n = snprintf(path, sizeof(path),
                   "%s/Chromium Embedded Framework.framework/"
                   "Chromium Embedded Framework",
                   framework_dir);
  if (n < 0 || n >= (int)sizeof(path)) {
    set_err("framework path too long");
    return 0;
  }
  /* Also accept a path that already points at the .framework bundle. */
  if (strstr(framework_dir, "Chromium Embedded Framework.framework") != NULL) {
    n = snprintf(path, sizeof(path),
                 "%s/Chromium Embedded Framework", framework_dir);
    if (n < 0 || n >= (int)sizeof(path)) {
      set_err("framework path too long");
      return 0;
    }
  }
  g_fw = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
  if (g_fw == NULL) {
    snprintf(g_err, sizeof(g_err), "dlopen %s: %s", path, dlerror());
    return 0;
  }
  return 1;
}

static void unlink_under(const char* root, const char* rel) {
  char p[4096];
  int n = snprintf(p, sizeof(p), "%s/%s", root, rel);
  if (n > 0 && n < (int)sizeof(p)) {
    unlink(p);
  }
}

static void reap_stale_singleton(const char* cache_path) {
  /* A previous CHECK leaves SingletonLock behind; the next
   * cef_initialize then takes the "already running" path or FATAL. */
  if (cache_path == NULL || cache_path[0] == '\0') {
    return;
  }
  static const char* names[] = {
      "SingletonLock", "SingletonSocket", "SingletonCookie", NULL};
  for (int i = 0; names[i] != NULL; i++) {
    unlink_under(cache_path, names[i]);
  }
  /* LevelDB lockfiles survive SIGKILL. Best-effort; missing is fine. */
  static const char* locks[] = {
      "Default/LOCK",
      "Default/Network/LOCK",
      "Default/Extension State/LOCK",
      "Default/Local Storage/leveldb/LOCK",
      "Default/Session Storage/LOCK",
      "Default/Site Characteristics Database/LOCK",
      "Default/Sync Data/LevelDB/LOCK",
      "shared_proto_db/metadata/LOCK",
      NULL};
  for (int i = 0; locks[i] != NULL; i++) {
    unlink_under(cache_path, locks[i]);
  }
}

static void evict_profile_holders(const char* cache_path) {
  /* Sidecar Chrome fallback and leftover Ginno share this profile.
   * Two browsers on one user-data-dir pop the modal
   * "Something went wrong when opening your profile" and then the
   * GPU helper dies on locked DBs. Exclusive ownership, not a
   * second profile — cookies have to survive the engine swap. */
  if (cache_path == NULL || cache_path[0] == '\0') {
    return;
  }
  char needle[4096];
  int n = snprintf(needle, sizeof(needle), "user-data-dir=%s", cache_path);
  if (n <= 0 || n >= (int)sizeof(needle)) {
    return;
  }
  FILE* fp = popen("/bin/ps -ax -o pid= -o command=", "r");
  if (fp == NULL) {
    return;
  }
  pid_t me = getpid();
  char line[8192];
  while (fgets(line, sizeof(line), fp) != NULL) {
    if (strstr(line, needle) == NULL) {
      continue;
    }
    pid_t pid = (pid_t)atoi(line);
    if (pid > 1 && pid != me) {
      kill(pid, SIGKILL);
    }
  }
  pclose(fp);
  usleep(200000);
}

static int configure_api_version(void) {
  /* CEF 151 CToCpp wrappers FATAL with "invalid version -1" unless the
   * client process calls cef_api_hash before handing over any cef_*_t. */
  const char* hash = cef_api_hash(CEF_API_VERSION, 0);
  if (hash == NULL || hash[0] == '\0') {
    set_err("cef_api_hash failed");
    return 0;
  }
  return 1;
}

/* Chromium's event dispatch asks the NSApplication for the CrApp
 * protocol (-isHandlingSendEvent / -setHandlingSendEvent:). Tauri's
 * TaoApp implements neither; the first event that checks throws
 * NSInvalidArgumentException and kills the whole host. Patch them in. */
static BOOL g_handling_send_event;

static BOOL ginno_is_handling_send_event(id self, SEL cmd) {
  (void)self;
  (void)cmd;
  return g_handling_send_event;
}

static void ginno_set_handling_send_event(id self, SEL cmd, BOOL handling) {
  (void)self;
  (void)cmd;
  g_handling_send_event = handling;
}

static void install_crapp_support(void) {
  id app = [NSApplication sharedApplication];
  if (app == nil) {
    return;
  }
  Class cls = [app class];
  if (![app respondsToSelector:@selector(isHandlingSendEvent)]) {
    class_addMethod(cls, @selector(isHandlingSendEvent),
                    (IMP)ginno_is_handling_send_event, "B@:");
  }
  SEL set_sel = sel_registerName("setHandlingSendEvent:");
  if (![app respondsToSelector:set_sel]) {
    class_addMethod(cls, set_sel, (IMP)ginno_set_handling_send_event, "v@:B");
  }
}

static void life_on_after_created(cef_life_span_handler_t* self,
                                  cef_browser_t* browser) {
  (void)self;

  bc("after_created browser=%p parent=%p", (void*)browser, g_parent);

  if (g_browser != NULL) {
    g_browser->base.release(&g_browser->base);
    g_browser = NULL;
  }
  if (browser != NULL) {
    browser->base.add_ref(&browser->base);
    g_browser = browser;
  }
  g_creating = 0;

  /* CEF's child view should auto-resize with the wrapper. */
  if (g_parent != NULL) {
    NSView* parent = (__bridge NSView*)g_parent;
    FILE* log = fopen("/tmp/ginno-cef-create.log", "a");
    if (log != NULL) {
      fprintf(log, "life_on_after_created: parent subviews=%lu frame=(%.0f,%.0f,%.0f,%.0f)\n",
              (unsigned long)parent.subviews.count,
              parent.frame.origin.x, parent.frame.origin.y,
              parent.frame.size.width, parent.frame.size.height);
      for (NSView* child in parent.subviews) {
        fprintf(log, "  child %s frame=(%.0f,%.0f,%.0f,%.0f) hidden=%d\n",
                [[child className] UTF8String],
                child.frame.origin.x, child.frame.origin.y,
                child.frame.size.width, child.frame.size.height,
                child.hidden ? 1 : 0);
      }
      fclose(log);
    }
    for (NSView* child in parent.subviews) {
      child.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
      /* Also fit it to parent's current bounds in case wrapper was already
       * resized by the time this callback runs. */
      child.frame = parent.bounds;
    }
  }
}

static void life_on_before_close(cef_life_span_handler_t* self,
                                 cef_browser_t* browser) {
  (void)self;
  bc("on_before_close browser=%p", (void*)browser);
  if (g_browser != NULL && g_browser == browser) {
    g_browser->base.release(&g_browser->base);
    g_browser = NULL;
  }
}

static int life_on_before_popup(
    cef_life_span_handler_t* self,
    cef_browser_t* browser,
    cef_frame_t* frame,
    int popup_id,
    const cef_string_t* target_url,
    const cef_string_t* target_frame_name,
    cef_window_open_disposition_t target_disposition,
    int user_gesture,
    const cef_popup_features_t* popupFeatures,
    cef_window_info_t* windowInfo,
    cef_client_t** client,
    cef_browser_settings_t* settings,
    cef_dictionary_value_t** extra_info,
    int* no_javascript_access) {
  (void)self;
  (void)browser;
  (void)frame;
  (void)popup_id;
  (void)target_url;
  (void)target_frame_name;
  (void)target_disposition;
  (void)user_gesture;
  (void)popupFeatures;
  (void)windowInfo;
  (void)client;
  (void)settings;
  (void)extra_info;
  (void)no_javascript_access;
  /* No Chrome-style popup windows. Spaces open extra tabs via CDP. */
  return 1;
}

static cef_life_span_handler_t* client_get_life_span(cef_client_t* self) {
  (void)self;
  return &g_life.handler;
}

static void req_on_render_terminated(cef_request_handler_t* self,
                                     cef_browser_t* browser,
                                     cef_termination_status_t status,
                                     int error_code,
                                     const cef_string_t* error_string) {
  (void)self;
  (void)browser;
  char buf[128] = "";
  if (error_string != NULL && error_string->length > 0) {
    snprintf(buf, sizeof(buf), " errstr-len=%lu",
             (unsigned long)error_string->length);
  }
  bc("RENDER_PROCESS_TERMINATED status=%d error_code=%d%s", (int)status,
     error_code, buf);
}

static cef_request_handler_t* client_get_request_handler(cef_client_t* self) {
  (void)self;
  return &g_req.handler;
}

static void bph_on_context_initialized(cef_browser_process_handler_t* self) {
  (void)self;
  bc("on_context_initialized want_browser=%d parent=%p", g_want_browser,
     g_parent);
  g_ctx_ready = 1;
  if (g_want_browser && g_parent != NULL && g_browser == NULL) {
    create_browser(g_parent, g_pending_w, g_pending_h);
  }
}

static void append_switch(cef_command_line_t* cl, const char* name) {
  if (cl == NULL || cl->append_switch == NULL || name == NULL) {
    return;
  }
  cef_string_t s;
  memset(&s, 0, sizeof(s));
  set_cef_str(&s, name);
  cl->append_switch(cl, &s);
  cef_string_clear(&s);
}

static void app_on_before_command_line(cef_app_t* self,
                                       const cef_string_t* process_type,
                                       cef_command_line_t* command_line) {
  (void)self;
  (void)process_type;
  /* A leftover Chrome holding the shared profile pops a modal
   * "Something went wrong when opening your profile" and freezes
   * the Tauri run loop. Never show that from the host. */
  append_switch(command_line, "noerrdialogs");
  append_switch(command_line, "disable-session-crashed-bubble");
  append_switch(command_line, "hide-crash-restore-bubble");
}

static cef_browser_process_handler_t* app_get_bph(cef_app_t* self) {
  (void)self;
  return &g_bph.handler;
}

static void init_handlers(void) {
  memset(&g_client, 0, sizeof(g_client));
  atomic_store(&g_client.refs, 1);
  g_client.client.base.size = sizeof(cef_client_t);
  g_client.client.base.add_ref = client_add_ref;
  g_client.client.base.release = client_release;
  g_client.client.base.has_one_ref = client_has_one_ref;
  g_client.client.base.has_at_least_one_ref = client_has_at_least_one_ref;
  g_client.client.get_life_span_handler = client_get_life_span;
  g_client.client.get_request_handler = client_get_request_handler;

  memset(&g_life, 0, sizeof(g_life));
  atomic_store(&g_life.refs, 1);
  g_life.handler.base.size = sizeof(cef_life_span_handler_t);
  g_life.handler.base.add_ref = life_add_ref;
  g_life.handler.base.release = life_release;
  g_life.handler.base.has_one_ref = life_has_one_ref;
  g_life.handler.base.has_at_least_one_ref = life_has_at_least_one_ref;
  g_life.handler.on_before_popup = life_on_before_popup;
  g_life.handler.on_after_created = life_on_after_created;
  g_life.handler.on_before_close = life_on_before_close;

  memset(&g_req, 0, sizeof(g_req));
  atomic_store(&g_req.refs, 1);
  g_req.handler.base.size = sizeof(cef_request_handler_t);
  g_req.handler.base.add_ref = req_add_ref;
  g_req.handler.base.release = req_release;
  g_req.handler.base.has_one_ref = req_has_one_ref;
  g_req.handler.base.has_at_least_one_ref = req_has_at_least_one_ref;
  g_req.handler.on_render_process_terminated = req_on_render_terminated;

  memset(&g_bph, 0, sizeof(g_bph));
  atomic_store(&g_bph.refs, 1);
  g_bph.handler.base.size = sizeof(cef_browser_process_handler_t);
  g_bph.handler.base.add_ref = bph_add_ref;
  g_bph.handler.base.release = bph_release;
  g_bph.handler.base.has_one_ref = bph_has_one_ref;
  g_bph.handler.base.has_at_least_one_ref = bph_has_at_least_one_ref;
  g_bph.handler.on_context_initialized = bph_on_context_initialized;
  g_bph.handler.on_schedule_message_pump_work = bph_on_schedule;

  memset(&g_app, 0, sizeof(g_app));
  atomic_store(&g_app.refs, 1);
  g_app.app.base.size = sizeof(cef_app_t);
  g_app.app.base.add_ref = app_add_ref;
  g_app.app.base.release = app_release;
  g_app.app.base.has_one_ref = app_has_one_ref;
  g_app.app.base.has_at_least_one_ref = app_has_at_least_one_ref;
  g_app.app.on_before_command_line_processing = app_on_before_command_line;
  g_app.app.get_browser_process_handler = app_get_bph;
}

static void create_browser_now(void) {
  if (g_browser != NULL || !g_ctx_ready || g_parent == NULL) {
    g_creating = 0;
    return;
  }

  bc("create_browser_now parent=%p w=%d h=%d", g_parent, g_pending_w,
     g_pending_h);

  cef_window_info_t wi;
  memset(&wi, 0, sizeof(wi));
  wi.size = sizeof(wi);
  /* cefclient-mac configuration: parent_view only. CEF creates and owns
   * its child NSView; we only resize the parent. */
  wi.parent_view = g_parent;
  wi.hidden = 0;
  wi.bounds.x = 0;
  wi.bounds.y = 0;
  wi.bounds.width = g_pending_w > 0 ? g_pending_w : 800;
  wi.bounds.height = g_pending_h > 0 ? g_pending_h : 600;
  /* Child NSView ⇒ Alloy. DEFAULT in 151 is Chrome style and SIGTRAPs
   * when hosted inside a foreign NSView during launch. */
  wi.runtime_style = CEF_RUNTIME_STYLE_ALLOY;

  cef_browser_settings_t bs;
  memset(&bs, 0, sizeof(bs));
  bs.size = sizeof(bs);

  cef_string_t url;
  memset(&url, 0, sizeof(url));
  set_cef_str(&url, "about:blank");

  int ok = cef_browser_host_create_browser(&wi, &g_client.client, &url, &bs,
                                           NULL, NULL);
  cef_string_clear(&url);

  bc("cef_browser_host_create_browser ok=%d", ok);

  if (!ok) {
    g_creating = 0;
    set_err("cef_browser_host_create_browser failed");
    return;
  }
  fit_children(g_parent);
}

static int create_browser(void* parent_nsview, int width, int height) {
  if (parent_nsview != NULL) {
    g_parent = parent_nsview;
  }
  g_pending_w = width > 0 ? width : 800;
  g_pending_h = height > 0 ? height : 600;
  g_want_browser = 1;

  if (g_browser != NULL) {
    fit_children(g_parent);
    return 1;
  }
  if (!g_ctx_ready) {
    /* OnContextInitialized will retry. Do not create_browser_sync here. */
    return 1;
  }
  if (g_creating) {
    return 1;
  }
  if (g_parent == NULL) {
    set_err("create_browser: no parent NSView");
    return 0;
  }

  /* Never create on the didFinishLaunching / OnContextInitialized stack.
   * Hop to the next main-loop turn so Chrome-style CHECKs don't fire. */
  g_creating = 1;
  dispatch_async(dispatch_get_main_queue(), ^{
    create_browser_now();
  });
  return 1;
}

int ginno_cef_init(const char* framework_dir, const char* helper_exe,
                   const char* main_bundle, const char* cache_path,
                   int debug_port, void* parent_nsview) {
  if (g_inited) {
    if (parent_nsview != NULL) {
      g_parent = parent_nsview;
      if (g_want_browser || g_ctx_ready) {
        create_browser(parent_nsview, g_pending_w, g_pending_h);
      }
    }
    return 1;
  }
  g_err[0] = '\0';
  install_crapp_support();
  if (!load_framework(framework_dir)) {
    return 0;
  }
  if (!configure_api_version()) {
    return 0;
  }
  evict_profile_holders(cache_path);
  reap_stale_singleton(cache_path);

  static char* argv_store[6];
  static char argv0[] = "Ginno";
  static char argv1[] = "--remote-allow-origins=*";
  static char argv2[] = "--no-sandbox";
  static char argv3[] = "--noerrdialogs";
  static char argv4[] = "--disable-session-crashed-bubble";
  argv_store[0] = argv0;
  argv_store[1] = argv1;
  argv_store[2] = argv2;
  argv_store[3] = argv3;
  argv_store[4] = argv4;
  argv_store[5] = NULL;
  cef_main_args_t args;
  args.argc = 5;
  args.argv = argv_store;

  cef_settings_t settings;
  memset(&settings, 0, sizeof(settings));
  settings.size = sizeof(settings);
  settings.no_sandbox = 1;
  /* Windowed + embedded in Tauri's run loop. The sidecar drives the browser
   * exclusively over CDP (DevTools HTTP), which only answers while we run an
   * EXTERNAL message pump and call cef_do_message_loop_work ourselves — with
   * the default pump the DevTools server hangs in this foreign run loop.
   * The pump timers below provide that drive. */
  settings.external_message_pump = 1;
  settings.windowless_rendering_enabled = 0;
  settings.persist_session_cookies = 1;
  if (debug_port >= 1024 && debug_port <= 65535) {
    settings.remote_debugging_port = debug_port;
    g_port = debug_port;
  }

  /* framework_dir_path wants the .framework bundle itself. */
  char fw_bundle[4096];
  if (framework_dir != NULL &&
      strstr(framework_dir, "Chromium Embedded Framework.framework") != NULL) {
    snprintf(fw_bundle, sizeof(fw_bundle), "%s", framework_dir);
  } else {
    snprintf(fw_bundle, sizeof(fw_bundle),
             "%s/Chromium Embedded Framework.framework",
             framework_dir ? framework_dir : "");
  }
  set_cef_str(&settings.framework_dir_path, fw_bundle);
  set_cef_str(&settings.browser_subprocess_path, helper_exe);
  set_cef_str(&settings.main_bundle_path, main_bundle);
  set_cef_str(&settings.cache_path, cache_path);
  set_cef_str(&settings.root_cache_path, cache_path);
  set_cef_str(&settings.log_file, NULL);

  init_handlers();

  int ok = cef_initialize(&args, &settings, &g_app.app, NULL);
  bc("cef_initialize ok=%d", ok);
  cef_string_clear(&settings.framework_dir_path);
  cef_string_clear(&settings.browser_subprocess_path);
  cef_string_clear(&settings.main_bundle_path);
  cef_string_clear(&settings.cache_path);
  cef_string_clear(&settings.root_cache_path);
  if (!ok) {
    int code = cef_get_exit_code();
    snprintf(g_err, sizeof(g_err), "cef_initialize failed (exit_code=%d)",
             code);
    return 0;
  }
  g_inited = 1;
  start_pump();
  /* One tick so OnContextInitialized can run before we return. Never
   * call create_browser_sync from didFinishLaunching — that CHECKs. */
  cef_do_message_loop_work();

  if (parent_nsview != NULL) {
    create_browser(parent_nsview, 800, 600);
  }
  g_ready = 1;
  return 1;
}

int ginno_cef_attach(void* parent_nsview, int width, int height) {
  if (!g_inited) {
    set_err("ginno_cef_init has not run");
    return 0;
  }
  if (parent_nsview == NULL) {
    set_err("parent_nsview is NULL");
    return 0;
  }
  if (!create_browser(parent_nsview, width, height)) {
    return 0;
  }
  /* Fit on the next runloop turn: resizing CEF's private child view in the
   * middle of CEF's own layout pass has crashed inside Chromium. */
  dispatch_async(dispatch_get_main_queue(), ^{
    fit_children(parent_nsview);
  });

  NSView* parent = (__bridge NSView*)parent_nsview;
  bc("attach parent=(%.0f,%.0f,%.0f,%.0f) hidden=%d subviews=%lu req=%dx%d",
     parent.frame.origin.x, parent.frame.origin.y, parent.frame.size.width,
     parent.frame.size.height, parent.hidden ? 1 : 0,
     (unsigned long)parent.subviews.count, width, height);
  return 1;
}

void ginno_cef_set_hidden(int hidden) {
  bc("set_hidden %d", hidden);
  if (g_parent == NULL) {
    return;
  }
  NSView* parent = (__bridge NSView*)g_parent;
  parent.hidden = hidden ? YES : NO;
}

static IMP g_orig_hit_test;
static NSView* g_hit_webview;
static NSView* g_hit_wrapper;
static int g_passthrough;
static int g_ht_log_budget = 5000;

static NSView* ginno_hit_test(id self, SEL sel, NSPoint point) {
  NSView* orig = ((NSView * (*)(id, SEL, NSPoint)) g_orig_hit_test)(self, sel, point);
  /* Always log the pane-chrome band (top-right) so we can see whether
   * button clicks reach the webview at all. */
  if (point.x > 460 && point.y > 30 && point.y < 220) {
    bc("hittest chrome-band self=%s pt=(%.0f,%.0f) orig=%s",
       [[(id)self className] UTF8String], point.x, point.y,
       orig ? [[orig className] UTF8String] : "nil");
  } else if (g_ht_log_budget > 0) {
    g_ht_log_budget--;
    bc("hittest self=%s pt=(%.0f,%.0f) pass=%d orig=%s",
       [[(id)self className] UTF8String], point.x, point.y, g_passthrough,
       orig ? [[orig className] UTF8String] : "nil");
  }
  if (g_passthrough && g_hit_wrapper != nil && !g_hit_wrapper.hidden &&
      self == g_hit_webview) {
    NSView* parent = g_hit_webview.superview;
    NSPoint inParent = [g_hit_webview convertPoint:point toView:parent];
    if (NSPointInRect(inParent, g_hit_wrapper.frame)) {
      NSRect wf = g_hit_wrapper.frame;
      bc("hittest forward (%.0f,%.0f) wrapper=(%.0f,%.0f,%.0f,%.0f)", inParent.x,
         inParent.y, wf.origin.x, wf.origin.y, wf.size.width, wf.size.height);
      return [g_hit_wrapper hitTest:inParent];
    }
  }
  return orig;
}

void ginno_cef_install_hittest(void* webview, void* wrapper) {
  if (webview == NULL || wrapper == NULL) {
    return;
  }
  g_hit_webview = (__bridge NSView*)webview;
  g_hit_wrapper = (__bridge NSView*)wrapper;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    Method m = class_getInstanceMethod([WKWebView class], @selector(hitTest:));
    if (m != NULL) {
      g_orig_hit_test = method_setImplementation(m, (IMP)ginno_hit_test);
    }
  });
}

void ginno_cef_set_passthrough(int enabled) {
  bc("set_passthrough %d", enabled);
  g_passthrough = enabled ? 1 : 0;
}

int ginno_cef_ready(void) { return g_ready ? 1 : 0; }

int ginno_cef_debug_port(void) { return g_port; }

const char* ginno_cef_last_error(void) { return g_err; }
