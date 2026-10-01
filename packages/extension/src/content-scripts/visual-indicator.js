/* Ginno visual indicator — Shadow-DOM isolated overlays (connector-module /
 * browser-companion design §7.3): pulsing border while the agent works,
 * element highlight, status badge, in-page Stop button.
 * Messages (from background): SHOW_HIGHLIGHT / HIDE_HIGHLIGHT / SHOW_BORDER /
 * HIDE_BORDER / SHOW_STOP / HIDE_STOP / STOP flags ride SHOW_BORDER.
 */
(function () {
  if (globalThis.__ginnoIndicator) return;
  var border = null, badge = null, stopWrap = null, active = false;

  function root() {
    var host = document.getElementById("ginno-shadow-container");
    if (host && host.shadowRoot) return host.shadowRoot;
    host = document.createElement("div");
    host.id = "ginno-shadow-container";
    host.style.cssText = "all: initial;";
    document.body.appendChild(host);
    return host.attachShadow({ mode: "open" });
  }

  function showBorder(stopFn) {
    active = true;
    var sr = root();
    if (!sr.getElementById("ginno-pulse-styles")) {
      var st = document.createElement("style");
      st.id = "ginno-pulse-styles";
      st.textContent =
        "@keyframes ginno-pulse{0%,100%{box-shadow:inset 0 0 4px rgba(124,92,250,.5)," +
        "inset 0 0 8px rgba(124,92,250,.25)}50%{box-shadow:inset 0 0 6px rgba(124,92,250,.7)," +
        "inset 0 0 12px rgba(124,92,250,.35)}}" +
        "@keyframes ginno-highlight{0%,100%{border-color:#7c5cfa;box-shadow:0 0 5px rgba(124,92,250,.5)}" +
        "50%{border-color:#a78bfa;box-shadow:0 0 16px rgba(124,92,250,.8)}}";
      sr.appendChild(st);
    }
    if (!border) {
      border = document.createElement("div");
      border.id = "ginno-border";
      border.style.cssText =
        "position:fixed;inset:0;pointer-events:none;z-index:2147483646;" +
        "animation:ginno-pulse 2s ease-in-out infinite;";
      sr.appendChild(border);
    }
    border.style.display = "";
    if (!stopWrap) {
      stopWrap = document.createElement("div");
      stopWrap.id = "ginno-stop-wrap";
      stopWrap.style.cssText =
        "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);" +
        "pointer-events:none;z-index:2147483647;";
      var btn = document.createElement("button");
      btn.id = "ginno-stop-btn";
      btn.textContent = "■ 停止 Ginno 操作";
      btn.style.cssText =
        "pointer-events:auto;cursor:pointer;padding:10px 16px;border-radius:12px;" +
        "border:0.5px solid rgba(31,30,29,.4);background:#FAF9F5;color:#141413;" +
        "font:600 14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
        "box-shadow:0 8px 24px rgba(124,92,250,.25),0 2px 8px rgba(0,0,0,.12);";
      btn.onmouseenter = function () { btn.style.background = "#F0EEF9"; };
      btn.onmouseleave = function () { btn.style.background = "#FAF9F5"; };
      btn.onclick = function () {
        btn.disabled = true;
        btn.textContent = "正在停止…";
        try { chrome.runtime.sendMessage({ type: "GINNO_STOP_TOOL" }); } catch (e) {}
        setTimeout(function () { hideBorder(); }, 600);
      };
      stopWrap.appendChild(btn);
      sr.appendChild(stopWrap);
    }
    stopWrap.style.display = "";
  }

  function hideBorder() {
    active = false;
    if (border) border.style.display = "none";
    if (stopWrap) stopWrap.style.display = "none";
  }

  var highlightEl = null;
  function highlight(ref) {
    var at = globalThis.__ginnoAT;
    var el = at && at.resolve && at.resolve(ref);
    if (!el) return;
    clearHighlight();
    var r = el.getBoundingClientRect();
    var sr = root();
    highlightEl = document.createElement("div");
    highlightEl.style.cssText =
      "position:absolute;left:" + (r.left - 5) + "px;top:" + (r.top - 5) + "px;" +
      "width:" + (r.width + 10) + "px;height:" + (r.height + 10) + "px;" +
      "border:3px solid #7c5cfa;border-radius:4px;pointer-events:none;" +
      "box-sizing:border-box;animation:ginno-highlight 1s ease-in-out infinite;";
    sr.appendChild(highlightEl);
    el.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  }

  function clearHighlight() {
    if (highlightEl) { highlightEl.remove(); highlightEl = null; }
  }

  function status(kind) {
    var sr = root();
    var old = sr.getElementById("ginno-badge");
    if (old) old.remove();
    var icons = { loading: "⏳", ok: "✅", error: "❌" };
    var colors = { loading: "#2196F3", ok: "#4CAF50", error: "#f44336" };
    badge = document.createElement("div");
    badge.id = "ginno-badge";
    badge.textContent = (icons[kind] || "") + " Ginno";
    badge.style.cssText =
      "position:fixed;top:20px;right:20px;padding:10px 16px;border-radius:8px;" +
      "color:#fff;background:" + (colors[kind] || "#666") + ";cursor:pointer;" +
      "font:600 13px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
      "box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:2147483647;";
    badge.onclick = function () { badge.remove(); badge = null; };
    sr.appendChild(badge);
    if (kind !== "loading") setTimeout(function () {
      if (badge) { badge.remove(); badge = null; }
    }, 4000);
  }

  chrome.runtime.onMessage.addListener(function (msg, _sender, sendResponse) {
    switch (msg && msg.type) {
      case "SHOW_BORDER": showBorder(); break;
      case "HIDE_BORDER": hideBorder(); break;
      case "SHOW_HIGHLIGHT": highlight(msg.ref); break;
      case "HIDE_HIGHLIGHT": clearHighlight(); break;
      case "STATUS": status(msg.kind); break;
    }
    sendResponse && sendResponse({ ok: true });
    return true;
  });

  globalThis.__ginnoIndicator = {
    showBorder: showBorder, hideBorder: hideBorder,
    highlight: highlight, clearHighlight: clearHighlight, status: status,
    get active() { return active; },
  };
  window.addEventListener("beforeunload", function () { hideBorder(); });
})();
