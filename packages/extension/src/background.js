/* Ginno Browser Connector — service worker (browser-companion-extension-design.md).
 *
 * Speaks the relay protocol to the Ginno sidecar over ws://127.0.0.1:<port>/extension/v2
 * and executes the 16 browser_* tools against the user's real Chrome via
 * chrome.debugger (CDP trusted input events) + content-script bridges.
 *
 *   ginno → ext:  {method:"ping"} / {id, method:"tools/invoke", params:{tool,arguments}}
 *   ext → ginno:  {method:"extensionInfo"} / {id, result|error} /
 *                 {method:"tools/progress", params} / {method:"stopToolExecution" ← user}
 *
 * Port discovery order (设计 §7.3): native messaging host "app.ginno.connector"
 * → chrome.storage saved port → default candidates (8787).
 */

const NATIVE_HOST = "app.ginno.connector";
const DEFAULT_PORTS = [8787];
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 10000;
const IDLE_DETACH_MS = 10000;   // 用完即走:空闲即摘除调试器(去横幅)
const QUEUE_CAP = 32;

const S = {
  ws: null,
  port: null,
  reconnectAttempt: 0,
  reconnectTimer: null,
  nativePort: null,
  stopRequested: false,
  idleTimer: null,
  activeToolCount: 0,
  attachedTabs: new Set(),          // tabIds with debugger attached
  tabEventBuffers: new Map(),       // tabId -> {console:[], network:[]}
  screenshotCtx: new Map(),         // tabId -> {vw, vh, sw, sh}
  beforeunload: new Map(),          // tabId -> {policy, result}
  groupIdByWindow: new Map(),
  readyTabs: new Set(),             // content-script-present tabs
  pendingStop: null,
};

const log = (...a) => console.log("[Ginno Ext]", ...a);

/* ------------------------------------------------------------------ relay */

async function savedPort() {
  const { relayPort } = await chrome.storage.local.get("relayPort");
  return relayPort || null;
}

async function nativeDiscoverPort() {
  return new Promise((resolve) => {
    let done = false;
    let port;
    try {
      port = chrome.runtime.connectNative(NATIVE_HOST);
    } catch (e) {
      return resolve(null);
    }
    const timer = setTimeout(() => finish(null), 3000);
    function finish(v) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { port.disconnect(); } catch (e) {}
      S.nativePort = null;
      resolve(v);
    }
    port.onMessage.addListener((msg) => {
      if (msg && typeof msg.port === "number") finish(msg.port);
    });
    port.onDisconnect.addListener(() => finish(null));
    try { port.postMessage({ type: "getPort" }); } catch (e) { finish(null); }
  });
}

async function resolvePort() {
  const viaNative = await nativeDiscoverPort();
  if (viaNative) return viaNative;
  const saved = await savedPort();
  return saved || DEFAULT_PORTS[0];
}

function send(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    try { S.ws.send(JSON.stringify(obj)); } catch (e) { log("send failed", e); }
  }
}

function sendResult(id, content) {
  send({ id, result: { content } });
}
function sendError(id, code, message) {
  send({ id, error: { code, message } });
}
function sendProgress(params) {
  send({ method: "tools/progress", params });
}

function textBlock(text) { return { type: "text", text }; }
function imageBlock(data, mime) { return { type: "image", data, mimeType: mime || "image/jpeg" }; }

async function connectRelay() {
  if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) return;
  const port = await resolvePort();
  if (!port) return scheduleReconnect();
  const url = `ws://127.0.0.1:${port}/extension/v2`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    return scheduleReconnect();
  }
  S.ws = ws;
  S.port = port;
  ws.onopen = async () => {
    S.reconnectAttempt = 0;
    log("relay connected on port", port);
    send({
      method: "extensionInfo",
      params: {
        version: chrome.runtime.getManifest().version,
        browserType: detectBrowser(),
        browserClientId: await browserClientId(),
        capabilities: ["mcp-tools", "fifo-command-queue", "tool-progress"],
      },
    });
    await chrome.action.setBadgeText({ text: "" });
    await chrome.action.setTitle({ title: "Ginno Browser Connector · 已连接" });
    await chrome.storage.local.set({
      relayStatus: { connected: true, port, connectedAt: Date.now(),
                     version: chrome.runtime.getManifest().version,
                     browserType: detectBrowser() },
    });
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.method === "ping") return send({ method: "pong" });
    if (msg.method === "stopToolExecution") {
      S.stopRequested = true;
      return;
    }
    if (typeof msg.id === "number" && msg.method === "tools/invoke") {
      enqueue(msg.id, msg.params || {});
    }
  };
  ws.onclose = () => {
    if (S.ws === ws) S.ws = null;
    chrome.storage.local.set({ relayStatus: { connected: false } });
    scheduleReconnect();
  };
  ws.onerror = () => {};
  // 端口探测失败时标注 badge,便于用户发现配置问题
  setTimeout(() => {
    if (S.ws === ws && ws.readyState !== WebSocket.OPEN) {
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setTitle({ title: "Ginno Browser Connector · 未连接(检查 Ginno 是否在运行)" });
    }
  }, 2500);
}

function scheduleReconnect() {
  if (S.reconnectTimer) return;
  const delay = Math.min(RECONNECT_MIN_MS * Math.pow(2, S.reconnectAttempt), RECONNECT_MAX_MS);
  S.reconnectAttempt++;
  S.reconnectTimer = setTimeout(() => {
    S.reconnectTimer = null;
    connectRelay();
  }, delay);
}

function detectBrowser() {
  const ua = navigator.userAgent;
  return ua.includes("Edg/") ? "edge" : ua.includes("Chrome") ? "chrome" : "chromium";
}

async function browserClientId() {
  const { clientId } = await chrome.storage.local.get("clientId");
  if (clientId) return clientId;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ clientId: id });
  return id;
}

/* ------------------------------------------------------- FIFO + lifecycle */

const queue = [];
let queueRunning = false;

function enqueue(id, params) {
  if (queue.length >= QUEUE_CAP) {
    return sendError(id, "QUEUE_FULL", "Tool command queue is full");
  }
  queue.push({ id, params });
  processQueue();
}

async function processQueue() {
  if (queueRunning) return;
  queueRunning = true;
  try {
    while (queue.length > 0) {
      const { id, params } = queue.shift();
      S.stopRequested = false;
      await withActivity(async () => {
        try {
          const content = await invoke(params.tool || "", params.arguments || {});
          sendResult(id, content);
        } catch (e) {
          sendError(id, "TOOL_ERROR", (e && e.message) || String(e));
        }
      });
    }
  } finally {
    queueRunning = false;
  }
}

function withActivity(fn) {
  return new Promise(async (resolve) => {
    S.activeToolCount++;
    if (S.idleTimer) { clearTimeout(S.idleTimer); S.idleTimer = null; }
    const tabId = mostRecentToolTab;
    if (tabId != null) {
      try { await chrome.tabs.sendMessage(tabId, { type: "SHOW_BORDER" }); } catch (e) {}
    }
    try { await fn(); } finally {
      S.activeToolCount--;
      if (S.activeToolCount <= 0) {
        S.activeToolCount = 0;
        S.idleTimer = setTimeout(async () => {
          // idle: detach everything + hide overlays(设计 §5.4)
          if (mostRecentToolTab != null) {
            try { await chrome.tabs.sendMessage(mostRecentToolTab, { type: "HIDE_BORDER" }); } catch (e) {}
          }
          for (const t of [...S.attachedTabs]) await detach(t);
        }, IDLE_DETACH_MS);
      }
      resolve();
    }
  });
}

/* ----------------------------------------------------------- debugger ops */

let mostRecentToolTab = null;

async function ensureAttached(tabId) {
  if (S.attachedTabs.has(tabId)) return;
  const tab = await chrome.tabs.get(tabId);
  const url = tab.url || "";
  if (/^(chrome|edge|devtools|view-source|chrome-extension):/.test(url)) {
    throw new Error(`Blocked URL: ${url || "(empty)"} — Ginno 不能驱动浏览器内部页面`);
  }
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (e) {
    if (!String(e.message || "").includes("Already attached")) throw e;
  }
  S.attachedTabs.add(tabId);
  for (const m of ["Page.enable", "Runtime.enable", "Network.enable"]) {
    try { await chrome.debugger.sendCommand({ tabId }, m); } catch (e) {}
  }
  await ensureScripts(tabId);
}

async function detach(tabId) {
  try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  S.attachedTabs.delete(tabId);
}

chrome.debugger.onEvent.addListener(async (src, method, params) => {
  const tabId = src.tabId;
  if (!tabId) return;
  const buf = S.tabEventBuffers.get(tabId);
  if (!buf) return;
  if (method === "Runtime.consoleAPICalled") {
    buf.console.push({
      type: params.type || "log",
      args: (params.args || []).map((a) =>
        ["string", "number", "boolean"].includes(a.type) ? a.value
          : a.description || a.type || "").join(" "),
      timestamp: params.timestamp || Date.now(),
    });
    if (buf.console.length > 1000) buf.console.shift();
  } else if (method === "Runtime.exceptionThrown") {
    const d = params.exceptionDetails || {};
    buf.console.push({
      type: "exception",
      args: (d.exception && d.exception.description) || d.text || "",
      timestamp: Date.now(),
    });
  } else if (method === "Network.requestWillBeSent") {
    buf.network.push({
      requestId: params.requestId,
      method: (params.request || {}).method || "GET",
      url: (params.request || {}).url || "",
      type: params.type || "Other",
      timestamp: params.timestamp || Date.now(),
      status: null,
    });
    if (buf.network.length > 500) buf.network.shift();
  } else if (method === "Network.responseReceived") {
    for (let i = buf.network.length - 1; i >= 0; i--) {
      if (buf.network[i].requestId === params.requestId && buf.network[i].status === null) {
        buf.network[i].status = (params.response || {}).status || 0;
        break;
      }
    }
  } else if (method === "Page.javascriptDialogOpening") {
    const pol = S.beforeunload.get(tabId) || { policy: "dismiss" };
    const accept = params.type === "beforeunload" ? pol.policy === "accept" : true;
    if (params.type === "beforeunload") pol.result = { handled: true, accepted: accept };
    S.beforeunload.set(tabId, pol);
    try {
      await chrome.debugger.sendCommand({ tabId }, "Page.handleJavaScriptDialog", { accept });
    } catch (e) {}
  }
});

chrome.debugger.onDetach.addListener((src) => {
  if (src.tabId) S.attachedTabs.delete(src.tabId);
});

async function ensureScripts(tabId) {
  if (S.readyTabs.has(tabId)) return;
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => !!(globalThis.__ginnoAT && globalThis.__ginnoBridge),
    });
    if (r && r.result === true) { S.readyTabs.add(tabId); return; }
  } catch (e) {}
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content-scripts/accessibility-tree.js", "content-scripts/page-bridge.js"],
    });
    S.readyTabs.add(tabId);
  } catch (e) {
    log("script inject failed", tabId, e.message);
  }
}

chrome.tabs.onRemoved.addListener((tabId) => {
  S.readyTabs.delete(tabId);
  S.tabEventBuffers.delete(tabId);
  S.screenshotCtx.delete(tabId);
});
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading") S.readyTabs.delete(tabId);
});

function buf(tabId) {
  if (!S.tabEventBuffers.has(tabId)) S.tabEventBuffers.set(tabId, { console: [], network: [] });
  return S.tabEventBuffers.get(tabId);
}

async function evalInPage(tabId, expression) {
  const out = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
    expression, returnByValue: true, awaitPromise: true,
  });
  if (out.exceptionDetails) {
    const d = out.exceptionDetails;
    const desc = (d.exception && d.exception.description) || d.text || "script error";
    throw new Error("页面执行失败: " + desc);
  }
  const r = out.result || {};
  if (r.subtype === "null") return null;
  return r.value;
}

/* CDP input helpers — trusted events, human cadence (设计 §5) */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdp(tabId, method, params) {
  return chrome.debugger.sendCommand({ tabId }, method, params || {});
}

function mapCoords(tabId, x, y) {
  const c = S.screenshotCtx.get(tabId);
  if (!c) return [x, y];
  return [x * c.vw / c.sw, y * c.vh / c.sh];
}

const KEY_MAP = {
  return: "Enter", enter: "Enter", esc: "Escape", escape: "Escape", space: " ",
  spacebar: " ", tab: "Tab", backspace: "Backspace", delete: "Delete", del: "Delete",
  insert: "Insert", ins: "Insert", home: "Home", end: "End", pageup: "PageUp",
  pagedown: "PageDown", up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft",
  right: "ArrowRight", ctrl: "Control", alt: "Alt", shift: "Shift",
  cmd: "Meta", command: "Meta", meta: "Meta",
};
const VK = {
  Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, Insert: 45, Home: 36,
  End: 35, PageUp: 33, PageDown: 34, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37,
  ArrowRight: 39, " ": 32, Control: 17, Alt: 18, Shift: 16, Meta: 91,
};
function parseModifiers(spec) {
  if (!spec) return 0;
  let m = 0;
  for (const p of String(spec).toLowerCase().split("+")) {
    m |= { alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, command: 4, shift: 8 }[p.trim()] || 0;
  }
  return m;
}

/* ---------------------------------------------------------- tab grouping */

const GROUP_TITLE = "Ginno";

async function groupTab(tabId) {
  try {
    const { windowId } = await chrome.tabs.get(tabId);
    let gid = S.groupIdByWindow.get(windowId);
    if (gid != null) {
      try {
        const g = await chrome.tabGroups.get(gid);
        if (g.windowId === windowId) {
          await chrome.tabs.group({ tabIds: [tabId], groupId: gid });
          return;
        }
      } catch (e) {}
      S.groupIdByWindow.delete(windowId);
    }
    const groups = await chrome.tabGroups.query({ windowId });
    const found = groups.find((g) => g.title === GROUP_TITLE);
    if (found) {
      await chrome.tabs.group({ tabIds: [tabId], groupId: found.id });
      S.groupIdByWindow.set(windowId, found.id);
    } else {
      gid = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(gid, { title: GROUP_TITLE, color: "purple" });
      S.groupIdByWindow.set(windowId, gid);
    }
  } catch (e) { /* 分组失败不影响工具 */ }
}

async function ginnoTabs() {
  const groups = await chrome.tabGroups.query({ title: GROUP_TITLE });
  if (!groups.length) return [];
  const ids = new Set(groups.map((g) => g.id));
  const all = await chrome.tabs.query({});
  return all.filter((t) => ids.has(t.groupId));
}

/* ------------------------------------------------------------ tools (16) */

async function invoke(tool, a) {
  const needsTab = tool !== "browser_tabs_context" && tool !== "browser_tabs_create" &&
                   tool !== "browser_stop" && tool !== "browser_handoff";
  let tabId = a.tabId;
  if (needsTab) {
    if (!tabId) throw new Error("tabId is required. Call browser_tabs_context first.");
    mostRecentToolTab = tabId;
    await ensureAttached(tabId);
    buf(tabId);
  }
  switch (tool) {
    case "browser_computer": return computer(tabId, a);
    case "browser_read_page": return [textBlock(await readPage(tabId, a))];
    case "browser_find": return [textBlock(await findEls(tabId, a))];
    case "browser_form_input": return [textBlock(await formInput(tabId, a))];
    case "browser_navigate": return [textBlock(await navigate(tabId, a))];
    case "browser_page_text": return [textBlock(await pageText(tabId, a))];
    case "browser_js": return [textBlock(await jsTool(tabId, a))];
    case "browser_console": return [textBlock(consoleTool(tabId, a))];
    case "browser_network": return [textBlock(networkTool(tabId, a))];
    case "browser_file_upload": return [textBlock(await fileUpload(tabId, a))];
    case "browser_resize_window": return [textBlock(await resizeWindow(tabId, a))];
    case "browser_tabs_context": return [textBlock(await tabsContext())];
    case "browser_tabs_create": return [textBlock(await tabsCreate(a))];
    case "browser_tabs_close": return [textBlock(await tabsClose(a))];
    case "browser_handoff": return [textBlock("扩展轨 handoff 由 Ginno 侧 UI 承载;此调用已转交。")];
    case "browser_stop": S.stopRequested = true; return [textBlock("已请求停止。")];
    default: throw new Error(`未知工具: ${tool}`);
  }
}

async function resolveRef(tabId, ref) {
  const c = await evalInPage(tabId,
    `globalThis.__ginnoAT && __ginnoAT.coords(${JSON.stringify(ref)}, true)`);
  if (!c) throw new Error(`Element not found: ${ref}. Use browser_read_page or browser_find to get a fresh ref.`);
  return c;
}

async function computer(tabId, a) {
  const action = a.action;
  const mods = parseModifiers(a.modifiers);
  let coord = a.coordinate;
  if (a.ref && ["left_click", "right_click", "double_click", "triple_click", "hover", "scroll_to"].includes(action)) {
    const c = await resolveRef(tabId, a.ref);
    if (action === "scroll_to") return textBlock(`Scrolled to ${a.ref} at (${Math.round(c.x)}, ${Math.round(c.y)})`);
    coord = [c.x, c.y];
  }
  if (coord) coord = mapCoords(tabId, coord[0], coord[1]);
  const px = (v) => Math.round(v);

  if (["left_click", "right_click", "double_click", "triple_click"].includes(action)) {
    if (!coord) throw new Error(`computer "${action}" 需要 coordinate 或 ref`);
    const button = action === "right_click" ? "right" : "left";
    const count = action === "double_click" ? 2 : action === "triple_click" ? 3 : 1;
    try { await chrome.tabs.sendMessage(tabId, { type: "SHOW_HIGHLIGHT", ref: a.ref }); } catch (e) {}
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: coord[0], y: coord[1], modifiers: mods });
    await sleep(50);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x: coord[0], y: coord[1], button, clickCount: count, modifiers: mods });
    await sleep(50);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x: coord[0], y: coord[1], button, clickCount: count, modifiers: mods });
    const label = { left_click: "Clicked", right_click: "Right-clicked", double_click: "Double-clicked", triple_click: "Triple-clicked" }[action];
    return textBlock(`${label} at (${px(coord[0])}, ${px(coord[1])})`);
  }
  if (action === "hover") {
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: coord[0], y: coord[1] });
    return textBlock(`Hovered at (${px(coord[0])}, ${px(coord[1])})`);
  }
  if (action === "left_click_drag") {
    const s = mapCoords(tabId, a.start_coordinate[0], a.start_coordinate[1]);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: s[0], y: s[1], modifiers: mods });
    await sleep(50);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x: s[0], y: s[1], button: "left", clickCount: 1, modifiers: mods });
    await sleep(50);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: coord[0], y: coord[1], modifiers: mods });
    await sleep(100);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x: coord[0], y: coord[1], button: "left", clickCount: 1, modifiers: mods });
    return textBlock(`Dragged from (${px(s[0])},${px(s[1])}) to (${px(coord[0])},${px(coord[1])})`);
  }
  if (action === "type") {
    let typed = 0;
    for (const ch of a.text || "") {
      if (S.stopRequested) break;
      if (ch === "\n" || ch === "\r") {
        await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
        await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
      } else {
        const code = ch.charCodeAt(0);
        await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: ch, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
        await cdp(tabId, "Input.dispatchKeyEvent", { type: "char", key: ch, text: ch, unmodifiedText: ch, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
        await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: ch, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
      }
      await sleep(20);
      typed++;
    }
    return textBlock(`Typed ${typed} characters`);
  }
  if (action === "key") {
    const parts = String(a.text || "").split("+").map((s) => s.trim());
    const main = KEY_MAP[parts[parts.length - 1].toLowerCase()] || parts[parts.length - 1];
    const m = parseModifiers(parts.slice(0, -1).join("+"));
    const vk = VK[main];
    const n = Math.min(a.repeat || 1, 100);
    let done = 0;
    for (let i = 0; i < n; i++) {
      if (S.stopRequested) break;
      const base = { key: main, modifiers: m, ...(vk ? { windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk } : {}) };
      await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...base });
      await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
      done++;
    }
    return textBlock(`Pressed key: ${a.text}${done > 1 ? ` (${done}x)` : ""}`);
  }
  if (action === "scroll") {
    const dir = a.scroll_direction || "down";
    const amt = Math.min(a.scroll_amount || 3, 10);
    const c = coord || [640, 360];
    const d = { up: [0, -100], down: [0, 100], left: [-100, 0], right: [100, 0] }[dir];
    try {
      await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseWheel", x: c[0], y: c[1], deltaX: d[0] * amt, deltaY: d[1] * amt });
    } catch (e) {
      await evalInPage(tabId, `(function(){(document.scrollingElement||document.body).scrollBy({left:${d[0] * amt},top:${d[1] * amt},behavior:'instant'});return 1})()`);
    }
    await sleep(200);
    return textBlock(`Scrolled ${dir} x${amt}`);
  }
  if (action === "screenshot" || action === "zoom") return await screenshot(tabId, a);
  if (action === "wait") {
    await sleep(Math.min(a.duration || 1, 10) * 1000);
    return textBlock(`Waited ${a.duration || 1}s`);
  }
  throw new Error(`未知 computer action "${action}"`);
}

async function screenshot(tabId, a) {
  const t0 = Date.now();
  const prog = (stage, extra) => sendProgress({
    tool: "browser_computer", action: a.action || "screenshot", tabId,
    quality: a.quality || "low", stage, elapsedMs: Date.now() - t0, ...extra,
  });
  const q = { low: 5, medium: 40, high: 60 }[a.quality || "low"] || 5;
  const params = { format: "jpeg", quality: q, captureBeyondViewport: false };
  if (a.action === "zoom" && a.region) {
    const [x1, y1] = mapCoords(tabId, a.region[0], a.region[1]);
    const [x2, y2] = mapCoords(tabId, a.region[2], a.region[3]);
    params.clip = { x: Math.min(x1, x2), y: Math.min(y1, y2), width: Math.abs(x2 - x1), height: Math.abs(y2 - y1), scale: 1 };
  }
  let data = null;
  prog("capture_started", { attempt: 1 });
  for (const fromSurface of [true, false]) {
    try {
      params.fromSurface = fromSurface;
      const out = await cdp(tabId, "Page.captureScreenshot", params);
      if (out && out.data) { data = out.data; break; }
    } catch (e) { prog("capture_failed", { error: e.message }); }
  }
  if (!data) throw new Error("截图失败:页面可能处于错误状态,请先 browser_navigate 到有效页面。");
  const vp = await evalInPage(tabId,
    "({w: window.innerWidth, h: window.innerHeight})").catch(() => ({ w: 1280, h: 720 }));
  // decode jpeg dims
  const bin = atob(data);
  let sw = vp.w, sh = vp.h;
  for (let i = 2; i + 9 < bin.length; i++) {
    if (bin.charCodeAt(i) !== 0xff) continue;
    const m = bin.charCodeAt(i + 1);
    if (m >= 0xc0 && m <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(m)) {
      sh = bin.charCodeAt(i + 5) * 256 + bin.charCodeAt(i + 6);
      sw = bin.charCodeAt(i + 7) * 256 + bin.charCodeAt(i + 8);
      break;
    }
  }
  S.screenshotCtx.set(tabId, { vw: vp.w, vh: vp.h, sw, sh });
  prog("completed", { sizeKb: Math.round(data.length * 0.75 / 1024) });
  return [textBlock(`Screenshot captured (${sw}x${sh}, quality=${a.quality || "low"}, jpeg=${q})`), imageBlock(data)];
}

async function readPage(tabId, a) {
  await ensureScripts(tabId);
  const out = await evalInPage(tabId,
    `globalThis.__ginnoAT && __ginnoAT.generate({mode: ${JSON.stringify(a.filter || "all")},` +
    ` depth: ${a.depth || 15}, max_chars: ${a.max_chars || 50000},` +
    ` ref_id: ${JSON.stringify(a.ref_id || "")}})`);
  if (!out || out.error) throw new Error((out && out.error) || "无障碍树不可用(页面可能还在加载)");
  return out.tree;
}

async function findEls(tabId, a) {
  await ensureScripts(tabId);
  const out = await evalInPage(tabId,
    `globalThis.__ginnoBridge && __ginnoBridge.find(${JSON.stringify(a.query || "")}, ${a.max_results || 20})`);
  const rows = (out && out.results) || [];
  if (!rows.length) return `[find] 没有匹配 "${a.query}" 的元素。注意 find 只做字面匹配——换用页面上实际出现的词,或用 browser_read_page 直接浏览。`;
  return "[find] " + a.query + "\n" + rows.map((r) => `[${r.ref}] ${r.role} "${r.text}" (score ${r.score})`).join("\n");
}

async function formInput(tabId, a) {
  await ensureScripts(tabId);
  const out = await evalInPage(tabId,
    `globalThis.__ginnoBridge && __ginnoBridge.fill(${JSON.stringify(a.ref)}, ${JSON.stringify(a.value)})`);
  if (!out || !out.success) throw new Error((out && out.error) || "form_input 失败");
  return `Filled ${a.ref} (field: ${out.fieldName})`;
}

async function pageText(tabId, a) {
  await ensureScripts(tabId);
  const out = await evalInPage(tabId,
    `globalThis.__ginnoBridge && __ginnoBridge.pageText(${a.max_chars || 50000})`);
  if (!out || out.error) throw new Error((out && out.error) || "正文抽取不可用");
  return `Title: ${out.title}\nURL: ${out.url}\n\n${out.content}`;
}

async function jsTool(tabId, a) {
  const code = a.text || "";
  const wrapped =
    "(function(){try{var v=eval(" + JSON.stringify("(" + code + "\n)") + ");" +
    "return {ok:true,value:v}}catch(e1){try{eval(" + JSON.stringify(code) + ");" +
    "return {ok:true,value:undefined}}catch(e2){return {ok:false,error:e2.message||String(e2)}}}})()";
  const out = await evalInPage(tabId, wrapped);
  if (!out || out.ok === false) throw new Error("JavaScript 执行失败: " + (out && out.error));
  const v = out.value;
  if (v === undefined) return "undefined";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

function consoleTool(tabId, a) {
  const b = buf(tabId);
  let rows = [...b.console];
  if (a.onlyErrors) rows = rows.filter((r) => r.type === "error" || r.type === "exception");
  if (a.pattern) {
    let rx;
    try { rx = new RegExp(a.pattern, "i"); } catch (e) { return `Invalid pattern: ${a.pattern}`; }
    rows = rows.filter((r) => rx.test(r.args));
  }
  rows = rows.slice(-(a.limit || 100));
  if (a.clear) b.console = [];
  if (!rows.length) return "No console messages recorded";
  const lines = rows.map((r) => {
    const ts = new Date(r.timestamp).toISOString().split("T")[1].slice(0, 8);
    return `[${ts}] [${r.type.toUpperCase().padEnd(9)}] ${r.args}`;
  });
  return `Console messages (${rows.length}${a.clear ? ", buffer cleared" : ""}):\n\n${lines.join("\n")}`;
}

function networkTool(tabId, a) {
  const b = buf(tabId);
  let rows = [...b.network];
  if (a.urlPattern) rows = rows.filter((r) => r.url.includes(a.urlPattern));
  rows = rows.slice(-(a.limit || 100));
  if (a.clear) b.network = [];
  if (!rows.length) return "No network requests recorded";
  const lines = rows.map((r) =>
    `${r.method.padEnd(4)} ${String(r.status == null ? "pending" : r.status).padStart(7)} [${String(r.type).slice(0, 10).padEnd(10)}] ${r.url}`);
  return `Network requests (${rows.length}${a.clear ? ", buffer cleared" : ""}):\n\n${lines.join("\n")}`;
}

async function fileUpload(tabId, a) {
  await ensureScripts(tabId);
  const paths = a.paths || [];
  const attr = "data-ginno-file-ref";
  if (!paths.length) throw new Error("paths 不能为空");
  if (a.ref) {
    await evalInPage(tabId, `globalThis.__ginnoAT && __ginnoAT.markRef(${JSON.stringify(a.ref)}, ${JSON.stringify(attr)})`);
    try {
      const doc = await cdp(tabId, "DOM.getDocument");
      const sel = await cdp(tabId, "DOM.querySelector", {
        nodeId: doc.root.nodeId, selector: `input[type="file"][${attr}="1"]`,
      });
      if (!sel.nodeId) throw new Error("未能定位文件输入框的 DOM 节点");
      await cdp(tabId, "DOM.setFileInputFiles", { files: paths, nodeId: sel.nodeId });
    } finally {
      await evalInPage(tabId, `globalThis.__ginnoAT && __ginnoAT.clearMark(${JSON.stringify(a.ref)}, ${JSON.stringify(attr)})`).catch(() => {});
    }
    await sleep(300);
    const check = await evalInPage(tabId,
      `(function(){var at=globalThis.__ginnoAT;var el=at&&at.resolve(${JSON.stringify(a.ref)});` +
      `if(!el||el.tagName!=='INPUT'||el.type!=='file')return {ok:false,error:'input not found'};` +
      `var names=${JSON.stringify(paths)}.map(function(p){return String(p).split(/[\\\\/]/).pop()});` +
      `var files=Array.from(el.files||[]);` +
      `if(files.length!==names.length)return {ok:false,error:'期望 '+names.length+' 个文件,页面实际 '+files.length+' 个'};` +
      `return {ok:true,files:files.map(function(f){return {name:f.name,size:f.size}})}})()`);
    if (!check || !check.ok) throw new Error((check && check.error) || "文件选择校验失败");
    return `Files selected: ${check.files.map((f) => `${f.name} (${f.size}B)`).join(", ")}`;
  }
  // triggerRef mode: intercept the file chooser (设计 §4.5)
  if (!a.triggerRef) throw new Error("需要 ref(input[type=file])或 triggerRef(上传按钮)");
  await cdp(tabId, "Page.setInterceptFileChooserDialog", { enabled: true });
  try {
    const chooserP = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { chrome.debugger.onEvent.removeListener(h); reject(new Error("10s 内没有文件选择器打开")); }, 10000);
      function h(src, method, p) {
        if (src.tabId === tabId && method === "Page.fileChooserOpened") {
          clearTimeout(timer);
          chrome.debugger.onEvent.removeListener(h);
          resolve(p || {});
        }
      }
      chrome.debugger.onEvent.addListener(h);
    });
    const c = await resolveRef(tabId, a.triggerRef);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: c.x, y: c.y });
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x: c.x, y: c.y, button: "left", clickCount: 1 });
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x: c.x, y: c.y, button: "left", clickCount: 1 });
    const chooser = await chooserP;
    if (!chooser.backendNodeId) throw new Error("文件选择器打开了,但没有目标文件输入框");
    await cdp(tabId, "DOM.setFileInputFiles", { files: paths, backendNodeId: chooser.backendNodeId });
    return `Files selected via file-chooser interception: ${paths.join(", ")}`;
  } finally {
    await cdp(tabId, "Page.setInterceptFileChooserDialog", { enabled: false }).catch(() => {});
  }
}

async function resizeWindow(tabId, a) {
  const win = await chrome.windows.get((await chrome.tabs.get(tabId)).windowId);
  await chrome.windows.update(win.id, {
    width: Math.max(400, Math.min(a.width || 1280, 7680)),
    height: Math.max(300, Math.min(a.height || 800, 4320)),
  });
  S.screenshotCtx.delete(tabId);
  return `Resized window to ${a.width}x${a.height}`;
}

async function tabsContext() {
  const tabs = await ginnoTabs();
  if (!tabs.length) return "当前 Ginno 标签组为空。用 browser_tabs_create 新开一个(它会进组)。";
  const rows = [];
  for (const t of tabs) {
    try { await ensureAttached(t.id); } catch (e) {}
    rows.push(`- tabId=${t.id}${t.status === "loading" ? " (loading)" : ""} ${t.title || ""}\n  ${t.url || ""}`);
  }
  return "[tabs]\n" + rows.join("\n");
}

async function tabsCreate(a) {
  const tab = await chrome.tabs.create({ url: a.url || "about:blank", active: true });
  await groupTab(tab.id);
  return `Created tab tabId=${tab.id}`;
}

async function tabsClose(a) {
  const tabId = a.tabId;
  const mine = await ginnoTabs();
  if (!mine.some((t) => t.id === tabId)) throw new Error(`tabId=${tabId} 不在 Ginno 标签组内;不能关闭组外的标签。`);
  await chrome.tabs.remove(tabId);
  return `Closed tab ${tabId}`;
}

async function navigate(tabId, a) {
  let url = String(a.url || "").trim();
  S.beforeunload.set(tabId, { policy: a.force ? "accept" : "dismiss" });
  const t0 = Date.now();
  if (url === "back" || url === "forward") {
    await chrome.tabs.goBack?.(tabId) || await evalInPage(tabId, `history.${url}()`).catch(() => {});
    await waitComplete(tabId, 5000);
  } else {
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = "https://" + url;
    if (/^(chrome|edge|devtools|view-source|javascript|chrome-extension):/.test(url) && url !== "about:blank") {
      throw new Error(`Blocked URL: ${url}`);
    }
    await chrome.tabs.update(tabId, { url });
    await waitComplete(tabId, 10000);
  }
  await sleep(300);
  const tab = await chrome.tabs.get(tabId);
  const pol = S.beforeunload.get(tabId) || {};
  S.beforeunload.delete(tabId);
  if (pol.result && pol.result.handled && !pol.result.accepted) {
    throw new Error("页面的 beforeunload 处理器拦下了导航(有未保存的更改,已按默认策略保留)。确认要丢弃更改请带 force=true 重试。");
  }
  const dur = ((Date.now() - t0) / 1000).toFixed(1);
  return `Navigated to: ${tab.url}\nTitle: ${tab.title}\nDuration: ${dur}s`;
}

async function waitComplete(tabId, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.status === "complete") return true;
    } catch (e) { return false; }
    await sleep(100);
  }
  return false;
}

/* ------------------------------------------------- in-page Stop button msg */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "GINNO_STOP_TOOL") {
    S.stopRequested = true;
    send({ method: "stopToolExecution" });
    sendResponse({ ok: true });
  }
  if (msg && msg.type === "GINNO_RECONNECT") {
    if (S.ws) { try { S.ws.close(); } catch (e) {} }  // onclose → reconnect with new port
    connectRelay();
  }
  if (msg && msg.type === "GINNO_STATUS_QUERY") {
    chrome.storage.local.get("relayStatus").then((st) => sendResponse(st || {}));
    return true;  // async response
  }
  return false;
});

/* ------------------------------------------------------------ boot */

chrome.runtime.onInstalled.addListener(() => { connectRelay(); });
chrome.runtime.onStartup.addListener(() => { connectRelay(); });
connectRelay();
