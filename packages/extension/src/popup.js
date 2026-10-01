/* Popup: connection status + manual port override (fallback when the native
 * messaging host isn't installed yet). The background owns the WS; this only
 * reads state and writes chrome.storage. */
const $ = (id) => document.getElementById(id);

async function refresh() {
  const { relayPort, relayStatus } = await chrome.storage.local.get(["relayPort", "relayStatus"]);
  $("port").value = relayPort || "";
  const st = relayStatus || {};
  const ok = st.connected;
  $("dot").className = "dot " + (ok ? "ok" : "err");
  $("status").textContent = ok
    ? `✅ 已连接${st.port ? `(端口 ${st.port})` : ""}`
    : "❌ 未连接到 Ginno — 确认 Ginno 正在运行";
  const bits = [];
  if (st.version) bits.push(`扩展 v${st.version}`);
  if (st.browserType) bits.push(st.browserType);
  if (st.connectedAt) bits.push(`连接于 ${new Date(st.connectedAt).toLocaleTimeString()}`);
  $("meta").textContent = bits.join(" · ");
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "GINNO_RELAY_STATUS") refresh();
  return false;
});

// ask background for a status snapshot now
chrome.runtime.sendMessage({ type: "GINNO_STATUS_QUERY" }, () => {
  // background replies via storage + GINNO_RELAY_STATUS broadcast; also poll once
  setTimeout(refresh, 150);
});
refresh();

$("save").onclick = async () => {
  const v = parseInt($("port").value, 10);
  if ($("port").value && (!v || v < 1024)) {
    $("status").textContent = "端口无效(1024-65535)";
    return;
  }
  await chrome.storage.local.set({ relayPort: v || null });
  chrome.runtime.sendMessage({ type: "GINNO_RECONNECT" });
  $("status").textContent = "已保存,正在重连…";
  setTimeout(refresh, 800);
};
