/* Ginno CEF helper process.
 *
 * Chromium looks for Contents/Frameworks/<App> Helper.app (and the
 * GPU / Plugin / Renderer variants). Each bundle's executable is this
 * binary: load the sibling framework, then hand off to cef_execute_process.
 *
 * We dlopen instead of linking libcef so `tauri dev` / cargo check do not
 * pull a CEF compile-time dependency.
 *
 * CEF 151 CToCpp wrappers FATAL with "invalid version -1" unless the
 * process calls cef_api_hash before any other CEF C API — including
 * cef_execute_process in the GPU / Renderer helpers.
 */
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "include/cef_api_hash.h"

typedef struct {
  int argc;
  char** argv;
} cef_main_args_t;

typedef const char* (*cef_api_hash_fn)(int, int);
typedef int (*cef_execute_process_fn)(const cef_main_args_t*, void*, void*);

static int dirname_inplace(char* path) {
  char* slash = strrchr(path, '/');
  if (slash == NULL) {
    return -1;
  }
  *slash = '\0';
  return 0;
}

int main(int argc, char** argv) {
  char exe[PATH_MAX];
  uint32_t size = sizeof(exe);
  if (_NSGetExecutablePath(exe, &size) != 0) {
    fprintf(stderr, "ginno-helper: executable path too long\n");
    return 1;
  }
  char resolved[PATH_MAX];
  if (realpath(exe, resolved) == NULL) {
    perror("ginno-helper: realpath");
    return 1;
  }
  /* Ginno.app/Contents/Frameworks/<Helper>.app/Contents/MacOS/<Helper>
   * → <Helper>.app/Contents/MacOS → <Helper>.app/Contents → <Helper>.app
   * → Frameworks → Contents/Frameworks */
  if (dirname_inplace(resolved) != 0 || dirname_inplace(resolved) != 0 ||
      dirname_inplace(resolved) != 0 || dirname_inplace(resolved) != 0) {
    fprintf(stderr, "ginno-helper: unexpected layout: %s\n", exe);
    return 1;
  }
  char fw[PATH_MAX];
  int n = snprintf(
      fw, sizeof(fw),
      "%s/Chromium Embedded Framework.framework/Chromium Embedded Framework",
      resolved);
  if (n < 0 || n >= (int)sizeof(fw)) {
    fprintf(stderr, "ginno-helper: framework path too long\n");
    return 1;
  }
  void* handle = dlopen(fw, RTLD_NOW | RTLD_GLOBAL);
  if (handle == NULL) {
    fprintf(stderr, "ginno-helper: dlopen %s: %s\n", fw, dlerror());
    return 1;
  }
  cef_api_hash_fn api_hash = (cef_api_hash_fn)dlsym(handle, "cef_api_hash");
  if (api_hash == NULL) {
    fprintf(stderr, "ginno-helper: dlsym cef_api_hash: %s\n", dlerror());
    return 1;
  }
  if (api_hash(CEF_API_VERSION, 0) == NULL) {
    fprintf(stderr, "ginno-helper: cef_api_hash failed\n");
    return 1;
  }
  cef_execute_process_fn exec =
      (cef_execute_process_fn)dlsym(handle, "cef_execute_process");
  if (exec == NULL) {
    fprintf(stderr, "ginno-helper: dlsym cef_execute_process: %s\n", dlerror());
    return 1;
  }
  cef_main_args_t args;
  args.argc = argc;
  args.argv = argv;
  return exec(&args, NULL, NULL);
}
