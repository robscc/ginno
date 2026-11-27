import { BASE, H, jsonStrict } from "./core";

export type VoiceShortcut = "fn" | "ctrl-alt-v" | "off";
export type VoiceSettings = {
  selected: string;
  shortcut: VoiceShortcut;
  unavailable?: { id: string; name: string; models: string[]; reason: "disabled" | "unsupported_model" | "missing_key" | "service_url" }[];
  models: { id: string; name: string; model: string; region: string }[];
};

export const getVoiceSettings = () => jsonStrict<VoiceSettings>(`${BASE}/voice/settings`, { cache: "no-store" });
export const VOICE_SHORTCUT_CHANGED = "ginno:voice-shortcut-changed";

export function watchVoiceShortcut(listener: (shortcut: VoiceShortcut) => void) {
  const receive = (event: Event) => listener((event as CustomEvent<VoiceShortcut>).detail);
  window.addEventListener(VOICE_SHORTCUT_CHANGED, receive);
  const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(VOICE_SHORTCUT_CHANGED);
  if (channel) channel.onmessage = ({ data }) => listener(data);
  return () => {
    window.removeEventListener(VOICE_SHORTCUT_CHANGED, receive);
    channel?.close();
  };
}

export async function saveVoiceSettings(selected: string, shortcut: VoiceShortcut) {
  const result = await jsonStrict<{ ok: boolean }>(`${BASE}/voice/settings`, {
    method: "PUT", headers: H, body: JSON.stringify({ selected, shortcut }),
  });
  if (result.ok) {
    window.dispatchEvent(new CustomEvent(VOICE_SHORTCUT_CHANGED, { detail: shortcut }));
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel(VOICE_SHORTCUT_CHANGED);
      channel.postMessage(shortcut);
      channel.close();
    }
  }
  return result;
}
