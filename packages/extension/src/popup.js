/* Popup: connection status + manual port override (fallback when the native
 * messaging host isn't installed yet). The background owns the WS; this only
 * reads state and writes chrome.storage.
 *
 * i18n: 文案单一来源 apps/web/messages/{en,zh-CN}/ext.json（"ext" 域）——
 * build.py 生成 Chrome 标准 _locales/{en,zh_CN}/messages.json（按浏览器语言
 * 选边）。这里的 en 兜底串仅在 _locales 缺失时生效（runtime native_host.py
 * 的物化走固定文件清单、不带 _locales，见 i18n-design.md §8「关键文案内嵌
 * 兜底」）；en 是 catalog 的 source 语言，缺失时行为与迁移前一致。 */
const i18nMsg = (key, fallback, subs) => {
  try {
    const m = chrome.i18n.getMessage(key, subs);
    if (m) return m;
  } catch (e) { /* _locales 缺失等——走内嵌兜底 */ }
  if (typeof fallback === "function") return fallback(subs || []);
  if (subs && subs.length) {
    let out = fallback;
    subs.forEach((s, i) => { out = out.split(`{${i}}`).join(s); });
    return out;
  }
  return fallback;
};

const $ = (id) => document.getElementById(id);

// 静态 HTML 节点(data-i18n 指向 _locales key;节点原有英文文本即兜底)
document.querySelectorAll("[data-i18n]").forEach((el) => {
  el.textContent = i18nMsg(el.getAttribute("data-i18n"), el.textContent);
});
document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
  el.setAttribute("placeholder",
    i18nMsg(el.getAttribute("data-i18n-placeholder"), el.getAttribute("placeholder")));
});

async function refresh() {
  const { relayPort, relayStatus } = await chrome.storage.local.get(["relayPort", "relayStatus"]);
  $("port").value = relayPort || "";
  const st = relayStatus || {};
  const ok = st.connected;
  $("dot").className = "dot " + (ok ? "ok" : "err");
  $("status").textContent = ok
    ? (st.port
        ? i18nMsg("ext_popup_connectedPort", "✅ Connected (port {0})", [String(st.port)])
        : i18nMsg("ext_popup_connected", "✅ Connected"))
    : i18nMsg("ext_popup_notConnected", "❌ Not connected to Ginno — make sure Ginno is running");
  const bits = [];
  if (st.version) bits.push(i18nMsg("ext_popup_metaVersion", "Extension v{0}", [st.version]));
  if (st.browserType) bits.push(st.browserType);
  if (st.connectedAt) {
    bits.push(i18nMsg("ext_popup_metaConnectedAt", "Connected at {0}",
      [new Date(st.connectedAt).toLocaleTimeString()]));
  }
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
    $("status").textContent =
      i18nMsg("ext_popup_invalidPort", "Invalid port (1024-65535)");
    return;
  }
  await chrome.storage.local.set({ relayPort: v || null });
  chrome.runtime.sendMessage({ type: "GINNO_RECONNECT" });
  $("status").textContent = i18nMsg("ext_popup_saved", "Saved, reconnecting…");
  setTimeout(refresh, 800);
};

// 发送此页面(设计 §7.2 M2 反向入口):background 转发 openInChat,
// Ginno 连接器页出卡片,可一键预填进聊天。
$("send").onclick = () => {
  $("sendMsg").style.display = "block";
  $("sendMsg").textContent = i18nMsg("ext_popup_sending", "Sending…");
  chrome.runtime.sendMessage({ type: "GINNO_SEND_PAGE" }, () => {
    if (chrome.runtime.lastError) {
      $("sendMsg").textContent = i18nMsg("ext_popup_sendFailedReason",
        "Send failed: {0}", [chrome.runtime.lastError.message]);
      return;
    }
  });
};
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "GINNO_SEND_PAGE_ACK") {
    $("sendMsg").style.display = "block";
    $("sendMsg").textContent = msg.ok
      ? i18nMsg("ext_popup_sendOk", "✅ Sent — see the Connectors page in Ginno")
      : i18nMsg("ext_popup_sendErr", "❌ {0}",
          [msg.error || i18nMsg("ext_popup_sendFailed", "Send failed")]);
  }
  return false;
});
