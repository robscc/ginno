import { BASE, H, jsonStrict } from "./core";

export type VoiceShortcut = "fn" | "ctrl-alt-v" | "off";
export type VoiceSettings = {
  selected: string;
  shortcut: VoiceShortcut;
  models: { id: string; name: string; model: string; region: string }[];
};

export const getVoiceSettings = () => jsonStrict<VoiceSettings>(`${BASE}/voice/settings`, { cache: "no-store" });
export const saveVoiceSettings = (selected: string, shortcut: VoiceShortcut) =>
  jsonStrict<{ ok: boolean }>(`${BASE}/voice/settings`, {
    method: "PUT", headers: H, body: JSON.stringify({ selected, shortcut }),
  });
