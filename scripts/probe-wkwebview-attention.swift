// WKWebView 注意力语义探针 v2（docs/notifications-design.md §10 待核实项）
//
// v1 的教训：裸命令行可执行文件没有 bundle 身份，NSApp.activate 拿不到真正的
// frontmost 状态 —— hasFocus() 全程 false、window.focus 事件从未触发，数据不可用。
// v2 打成正规 .app bundle 并用 `open` 启动，结果写文件（open 不方便捕获 stdout）。
//
// 要回答三个问题：
//   A) 窗口可见但焦点在别处（同 App 的另一窗口 / 别的 App 前台）时，
//      visibilityState 与 hasFocus() 各报什么？→ 决定「该弹不弹」的缺口有多大
//   B) 完全遮挡 / 最小化 / App 隐藏时 visibilityState 何时翻 hidden？
//   C) 焦点位于页面内 iframe 时，顶层 document.hasFocus() 是否仍为 true？
//      → 这是**纳入 hasFocus 的风险**：Ginno 有 MCP app 卡与代码面板 viewer 两类
//        iframe，若此时 hasFocus() 变 false，用户明明在用 Ginno 却会被判成
//        「没在看」而狂弹通知，等于把 P0 的病换个方向再得一次。
import AppKit
import WebKit

let OUT = "/tmp/wkprobe/result.json"

let HTML = """
<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<button id="btn">btn</button>
<iframe id="f" srcdoc="<body><input id='in'></body>" style="width:240px;height:90px;border:0"></iframe>
<script>
window.__log = [];
window.__snap = function (tag) {
  var ae = document.activeElement;
  window.__log.push({
    tag: tag,
    vis: document.visibilityState,
    hidden: document.hidden,
    focus: document.hasFocus(),
    active: ae ? (ae.tagName + (ae.id ? '#' + ae.id : '')) : null
  });
};
document.addEventListener('visibilitychange', function(){ window.__snap('ev:visibilitychange'); });
window.addEventListener('focus', function(){ window.__snap('ev:window.focus'); });
window.addEventListener('blur',  function(){ window.__snap('ev:window.blur'); });
window.__snap('load');
</script></body></html>
"""

final class Probe: NSObject, WKNavigationDelegate {
  var winA: NSWindow!
  var winB: NSWindow!
  var wv: WKWebView!
  var step = 0

  let steps: [(String, (Probe) -> Void)] = [
    ("01_baseline_A_key",            { p in p.bringAToFront() }),
    ("02_iframe_focused",            { p in p.focusIframe() }),
    ("03_body_refocused",            { p in p.focusBody() }),
    ("04_B_key_side_by_side",        { p in p.sideBySide() }),
    ("05_other_app_frontmost",       { _ in activateFinder() }),
    ("06_probe_app_frontmost_again", { p in p.bringAToFront() }),
    ("07_A_fully_occluded_by_B",     { p in p.occlude() }),
    ("08_A_miniaturized",            { p in p.rekey(); p.winA.miniaturize(nil) }),
    ("09_A_deminiaturized",          { p in p.winA.deminiaturize(nil); p.bringAToFront() }),
    ("10_app_hidden",                { p in NSApp.hide(nil) }),
    ("11_app_unhidden",              { p in NSApp.unhide(nil); p.bringAToFront() }),
  ]

  func start() {
    let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 900, height: 700)
    let frame = NSRect(x: screen.midX - 320, y: screen.midY - 110, width: 420, height: 220)

    wv = WKWebView(frame: NSRect(x: 0, y: 0, width: 420, height: 220))
    wv.navigationDelegate = self
    winA = NSWindow(contentRect: frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
    winA.title = "probe-A(webview)"
    winA.contentView = wv
    winA.isReleasedWhenClosed = false
    winA.makeKeyAndOrderFront(nil)

    winB = NSWindow(contentRect: frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
    winB.title = "probe-B(occluder)"
    winB.backgroundColor = .darkGray
    winB.isOpaque = true
    winB.isReleasedWhenClosed = false

    NSApp.activate(ignoringOtherApps: true)
    wv.loadHTMLString(HTML, baseURL: nil)
  }

  func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { self.advance() }
  }

  func advance() {
    guard step < steps.count else { finish(); return }
    let (name, mutate) = steps[step]
    mutate(self)
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
      self.wv.evaluateJavaScript("window.__snap && window.__snap('\(name)')") { _, err in
        if let e = err { FileHandle.standardError.write("snap \(name) 失败: \(e.localizedDescription)\n".data(using: .utf8)!) }
        self.step += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.advance() }
      }
    }
  }

  func bringAToFront() {
    winB.orderOut(nil)
    NSApp.activate(ignoringOtherApps: true)
    winA.makeKeyAndOrderFront(nil)
  }
  func occlude() {
    winB.setFrame(winA.frame, display: true)
    winB.makeKeyAndOrderFront(nil)
  }
  func sideBySide() {
    var f = winA.frame
    f.origin.x += 450
    winB.setFrame(f, display: true)
    winB.makeKeyAndOrderFront(nil)
  }
  func rekey() {
    winB.orderOut(nil)
    winA.makeKeyAndOrderFront(nil)
  }
  func focusIframe() {
    bringAToFront()
    wv.evaluateJavaScript(
      "var f=document.getElementById('f'); f.focus(); try{ f.contentWindow.focus(); f.contentDocument.getElementById('in').focus(); }catch(e){} 1"
    ) { _, _ in }
  }
  func focusBody() {
    bringAToFront()
    wv.evaluateJavaScript("document.getElementById('btn').focus(); try{ window.frames[0].blur(); }catch(e){} 1") { _, _ in }
  }

  func finish() {
    wv.evaluateJavaScript("JSON.stringify(window.__log)") { raw, err in
      var out = "{}"
      if err == nil, let s = raw as? String { out = s }
      try? out.write(toFile: OUT, atomically: true, encoding: .utf8)
      NSApp.terminate(nil)
    }
  }
}

func activateFinder() {
  let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.finder")
  if let f = apps.first {
    f.activate(options: [.activateIgnoringOtherApps])
  }
}

let app = NSApplication.shared
let probe = Probe()
probe.start()
DispatchQueue.main.asyncAfter(deadline: .now() + 60) { NSApp.terminate(nil) }
app.run()
