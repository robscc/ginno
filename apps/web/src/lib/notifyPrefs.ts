/**
 * Notification preferences — single source of truth for the desktop
 * notification gate and its sound.
 *
 * Persisted in `~/.ginno/settings.json` (runtime `/api/settings`) under the
 * `"notifications"` key: `{enabled, sound, sound_name}`. A module-level cache
 * fronts the async API because the gate sites (socket completion callbacks in
 * store.tsx / ChatStream.tsx) must read the CURRENT value synchronously —
 * long-lived callbacks capture stale React state, which is why the previous
 * implementation read localStorage inside each callback. The cache is
 * refreshed on app boot (store boot effect) and on every save (Settings →
 * Notifications).
 */

import * as api from "./runtime";

export interface NotifyPrefs {
  /** Master switch (Settings → Notifications → 启用桌面提醒). */
  enabled: boolean;
  /** Whether completion notifications play a macOS system sound. */
  sound: boolean;
  /** macOS system sound name, e.g. "Glass" (/System/Library/Sounds/<name>.aiff). */
  soundName: string;
  /** V2: per-kind switches — each background event class can be muted
   *  independently (master `enabled` still gates everything). Defaults ON. */
  kinds: NotifyKinds;
  /** V2: notification body privacy — `full` (default) shows the reply
   *  summary, `titleOnly` shows just the session/event title (screen-share /
   *  lock-screen caution). */
  privacy: NotifyPrivacy;
  /** V2: do-not-disturb window — inside the window no banners/sounds are
   *  fired (events still land in Notification Center). Default off. */
  dnd: NotifyDnd;
}

/** Notification event classes — the gate axis.
 *
 *  2026-11-14 重整（docs/notifications-audit-2026-11-14.md P1-2）：
 *   - **删掉 `subagent`**：它在 V2 里就是「预留」（i18n 原文如此），至今没有
 *     任何生产者——`linked.done` 在服务端已过滤掉 subagent/schedule origin
 *     （engine.py:1303 `if _origin in ("interactive", "deliver")`），子代理结果
 *     走的是「注入父对话 + worker.* 应用内 notice」，从不发 OS 通知。留着一个
 *     拨了没反应的开关比没有更糟。将来真要做子代理 OS 通知，应作为**新功能**
 *     连同它的事件源一起加回来。
 *   - **新增 `schedule` / `bgTask`**：定时任务完成（notifyScheduleRunFinished）
 *     与后台任务终态（notifyTaskTransitions）此前只受总开关约束，没有自己的
 *     分类开关，DnD 与隐私模式也管不到它们。
 *   - `workflow` 从「死开关」接真到 notifyRunTransitions。 */
export type NotifyKind =
  | "reply"
  | "askUser"
  | "failure"
  | "workflow"
  | "schedule"
  | "bgTask"
  | "team";

export type NotifyKinds = Record<NotifyKind, boolean>;

export type NotifyPrivacy = "full" | "titleOnly";

export interface NotifyDnd {
  enabled: boolean;
  /** Inclusive window bounds, "HH:MM" 24h; may wrap midnight ("22:00"→"08:00"). */
  from: string;
  to: string;
}

/** Sounds offered in the settings dropdown — all ship with macOS. Keep in
 *  sync with the allow-list in apps/desktop/src/notify.rs. */
export const SOUND_NAMES = ["Glass", "Ping", "Pop", "Funk", "Hero", "Tink"] as const;

/** Failure notifications always use Basso (macOS failure cue) regardless of
 *  the completion sound — one glance/earful tells done from broken. */
export const FAILURE_SOUND = "Basso";

export const KINDS: readonly NotifyKind[] = [
  "reply",
  "askUser",
  "failure",
  "workflow",
  "schedule",
  "bgTask",
  "team",
];

/** kinds 语义版本。bump 时在 loadNotifyPrefs 里跑一次 migrateKinds。 */
export const KINDS_SCHEMA = 2;

export const DEFAULT_PREFS: NotifyPrefs = {
  enabled: true,
  sound: true,
  soundName: "Glass",
  // 全部默认 ON：workflow 的开关在 v1 里是死的（无代码消费），通知照发——
  // 接真之后若默认 OFF，等于把用户一直在收的通知静默掉。schedule/bgTask 同理
  // （此前只受总开关约束，一直在发）。要静音是用户的显式选择，不是默认值。
  kinds: { reply: true, askUser: true, failure: true, workflow: true, schedule: true, bgTask: true, team: true },
  privacy: "full",
  dnd: { enabled: false, from: "22:00", to: "08:00" },
};

/** v1 → v2 的 kinds 迁移：v1 持久化下来的 `workflow:false` / `subagent:false`
 *  **不是用户的选择**，而是「死开关的默认值被 saveNotifyPrefs 整对象写盘」的
 *  副产物。接真之后照抄会把用户一直在收的工作流通知静默掉，所以强制拉回 true
 *  并盖上 schema 标记；此后再改就是真选择，不再被迁移覆盖。
 *  `subagent` 已从 KINDS 移除，fromWire 只读已知键，下次存盘自然清掉。 */
export function migrateKinds(raw: unknown, p: NotifyPrefs): NotifyPrefs {
  const schema =
    raw && typeof raw === "object"
      ? Number((raw as Record<string, unknown>).kinds_schema ?? 1)
      : 1;
  if (Number.isFinite(schema) && schema >= KINDS_SCHEMA) return p;
  const next = { ...p, kinds: { ...p.kinds, workflow: true } };
  return next;
}

/** Pre-settings.json master switch (localStorage). One-shot migrated. */
const LEGACY_KEY = "ginno-notify";

let prefs: NotifyPrefs = { ...DEFAULT_PREFS };

/** Synchronous read for the gate sites (see module doc). */
export function notifyPrefs(): NotifyPrefs {
  return prefs;
}

/** settings.json wire shape is `{enabled, sound, sound_name, kinds, privacy,
 *  dnd}`. Unknown / malformed fields fall back to defaults rather than
 *  breaking the gate; pre-V2 files (first three keys only) migrate with all
 *  kinds on, full privacy, DnD off. Exported for unit tests. */
export function fromWire(raw: unknown): NotifyPrefs {
  // ⚠️ dnd 必须**深**拷贝一层。`{...DEFAULT_PREFS}` 是浅拷贝，d.dnd 与
  // DEFAULT_PREFS.dnd 是同一个对象引用，下面的 `d.dnd.enabled = …` 会就地改写
  // 模块级默认值——用户设过一次勿扰窗（如 00:00-23:59）后，DEFAULT_PREFS 就
  // 永久带上它，此后任何 settings.json 里缺 dnd 键 / dnd 畸形的回退都会落到
  // 那个被污染的「默认」上，勿扰再也关不掉。kinds 原本就单独展开过，同理补齐。
  // 2026-11-14 由 P1 闸门统一的测试暴露（审计文档 P1-1）。
  const d: NotifyPrefs = {
    ...DEFAULT_PREFS,
    kinds: { ...DEFAULT_PREFS.kinds },
    dnd: { ...DEFAULT_PREFS.dnd },
  };
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (typeof o.enabled === "boolean") d.enabled = o.enabled;
    if (typeof o.sound === "boolean") d.sound = o.sound;
    if (
      typeof o.sound_name === "string" &&
      (SOUND_NAMES as readonly string[]).includes(o.sound_name)
    ) {
      d.soundName = o.sound_name;
    }
    if (o.kinds && typeof o.kinds === "object") {
      for (const k of KINDS) {
        const v = (o.kinds as Record<string, unknown>)[k];
        if (typeof v === "boolean") d.kinds[k] = v;
      }
    }
    if (o.privacy === "titleOnly") d.privacy = "titleOnly";
    if (o.dnd && typeof o.dnd === "object") {
      const w = o.dnd as Record<string, unknown>;
      if (typeof w.enabled === "boolean") d.dnd.enabled = w.enabled;
      if (typeof w.from === "string" && /^\d{2}:\d{2}$/.test(w.from)) d.dnd.from = w.from;
      if (typeof w.to === "string" && /^\d{2}:\d{2}$/.test(w.to)) d.dnd.to = w.to;
    }
  }
  return d;
}

function toWire(p: NotifyPrefs): Record<string, unknown> {
  return {
    enabled: p.enabled,
    sound: p.sound,
    sound_name: p.soundName,
    kinds: { ...p.kinds },
    // 盖上 schema 标记：此后 kinds 里的值都是用户的真选择，migrateKinds 不再覆盖
    kinds_schema: KINDS_SCHEMA,
    privacy: p.privacy,
    dnd: { ...p.dnd },
  };
}

/** True when `at` (minutes since midnight) falls inside the DnD window.
 *  Windows may wrap midnight: from 22:00 → to 08:00 covers 23:59 and 07:00. */
export function inDndWindow(dnd: NotifyDnd, atMin: number): boolean {
  if (!dnd.enabled) return false;
  const toMin = (s: string) => {
    const [h, m] = s.split(":").map(Number);
    return h * 60 + m;
  };
  const f = toMin(dnd.from);
  const t = toMin(dnd.to);
  if (Number.isNaN(f) || Number.isNaN(t)) return false;
  return f <= t ? atMin >= f && atMin < t : atMin >= f || atMin < t;
}

/** Minutes since local midnight — input for inDndWindow. */
export function minutesOfDay(d = new Date()): number {
  return d.getHours() * 60 + d.getMinutes();
}

/** Sound for a kind: failures always get Basso; others follow prefs.
 *  `undefined` = silent (prefs.sound off). */
export function soundFor(kind: NotifyKind): string | undefined {
  const np = notifyPrefs();
  if (!np.sound) return undefined;
  return kind === "failure" ? FAILURE_SOUND : np.soundName;
}

/** Body text after the privacy gate: `titleOnly` strips the content. */
export function applyPrivacy(body: string, np: NotifyPrefs = notifyPrefs()): string {
  return np.privacy === "titleOnly" ? "" : body;
}

/** 偏好侧总闸：总开关 ∧ 分类型开关 ∧ 不在勿扰窗内。
 *
 *  **不含**「用户是否在看」——那只有调用点知道（要比对 activeSid / 路由），
 *  见 notifyHub 的 isAppAttentive / isSessionAttentive。
 *
 *  `"test"`（设置页测试按钮）刻意绕过全部三层：它是用户的显式动作，必须在
 *  任何偏好下都能给出可见反馈，否则「按了没反应」会被误读成通知系统坏了。
 *  测试按钮的说明文案已经写明它绕过总开关。
 *
 *  由 desktop.ts 的 notifyNative（所有 OS 通知的唯一出口）调用；调用点自己的
 *  shouldNotify 判定与此幂等重叠，属有意的双保险——漏判的站点仍被出口拦住。 */
export function osGateOpen(kind: NotifyKind | "test", atMin = minutesOfDay()): boolean {
  if (kind === "test") return true;
  const np = notifyPrefs();
  return np.enabled && np.kinds[kind] && !inDndWindow(np.dnd, atMin);
}

/**
 * Fetch prefs from the runtime. One-shot migration: the legacy master switch
 * lived in localStorage ("ginno-notify", "0" = off); if settings.json has no
 * "notifications" key yet, carry that choice over, then drop the legacy key.
 * Never throws — on failure (sidecar down) the defaults stay active and the
 * next boot retries.
 */
export async function loadNotifyPrefs(): Promise<NotifyPrefs> {
  // 纯读（settings-integrity）：历史上这里会在 boot 期做「读整文档 → 改
  // notifications 键 → PUT 整文档」的迁移播种。两个问题（v0.5.1 复现实测）：
  // ① 与其它写入方并发互踩（整文档 read-modify-write 丢更新）；
  // ② PUT 整文档会轮换 env 的 CAS version token，正在编辑 env 的窗口内
  //    保存必吃 409。改为：迁移只在内存生效，落盘推迟到用户显式
  //    saveNotifyPrefs；legacy localStorage 键保留到首次成功保存才移除。
  try {
    const legacy =
      typeof localStorage !== "undefined" ? localStorage.getItem(LEGACY_KEY) : null;
    const s = (await api.getSettings()) as Record<string, unknown>;
    if (s.parseError !== undefined) {
      // settings.json 损坏（契约 §4）：保持默认偏好，不迁移、不写。
      return prefs;
    }
    const existing = s.notifications;
    const parsed = fromWire(existing);
    // v1 持久化的 workflow:false 是死开关的默认值、不是用户选择——内存里
    // 迁移拉回 true；落盘由用户下一次显式保存完成。
    prefs = migrateKinds(existing, parsed);
    // legacy 总开关（localStorage "0" = off）且从未持久化过 → 内存携带该
    // 选择；legacy 键不动，等 saveNotifyPrefs 成功落盘后才移除。
    if (existing === undefined && legacy === "0") {
      prefs = { ...prefs, enabled: false };
    }
  } catch {
    /* keep defaults until next boot/save */
  }
  return prefs;
}

/** Persist prefs into settings.json and refresh the sync cache — the cache is
 *  updated only after the write succeeds, so a failed save never lies. */
export async function saveNotifyPrefs(next: Partial<NotifyPrefs>): Promise<NotifyPrefs> {
  const merged = { ...prefs, ...next };
  const s = (await api.getSettings()) as Record<string, unknown>;
  if (s.parseError !== undefined) {
    // 损坏态：不做写尝试，抛带 ginnoStatus 的错误让面板走损坏专属文案
    // （后端对普通 PUT 也是 409 拒写——这里是前端侧的同一纪律）。
    throw Object.assign(new Error("settings.json is corrupt"), { ginnoStatus: 409 });
  }
  s.notifications = toWire(merged);
  await api.putSettings(s);
  prefs = merged;
  // legacy 迁移此刻才算真正完成（选择已持久化），才允许移除旧键。
  if (typeof localStorage !== "undefined") localStorage.removeItem(LEGACY_KEY);
  return prefs;
}
