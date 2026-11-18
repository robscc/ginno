/* A2aInboundCard 纯逻辑单测（node 环境，照 src/lib/*.test.ts 风格）。
 * 渲染层（开关/复制/重置的 DOM 交互）留给 packaged UI e2e（集成轮补）；
 * 这里证明：地址明文派生不掩码、开启/重置二次确认门、PATCH payload、
 * i18n 双语齐全无缺 key、安全提示文案存在、useTranslations 切换不漏 key。
 *
 * next-intl mock：node 环境无 provider，useTranslations mock 成
 * key→en catalog 值的查表（{x} 插值与简单 ICU 参数等价）。 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { useTranslations } from "next-intl";
import {
  confirmReducer,
  inboundToggleAction,
  inboundTogglePatch,
  inboundView,
  needsEnableConfirm,
} from "./A2aInboundCard";
import a2aEn from "../../../messages/en/a2a.json";
import a2aZh from "../../../messages/zh-CN/a2a.json";

vi.mock("next-intl", async (importOriginal) => {
  const actual = await importOriginal<typeof import("next-intl")>();
  const en = (await import("../../../messages/en/a2a.json")).default
    .a2a as Record<string, string>;
  const lookup = (key: string, values?: Record<string, string | number>) => {
    let s = en[key] ?? key;
    if (values) {
      for (const [k, v] of Object.entries(values)) {
        s = s.split(`{${k}}`).join(String(v));
      }
    }
    return s;
  };
  return { ...actual, useTranslations: () => lookup };
});

const TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"; // 26 字符 base32
const FP = "ZYXWVUTSRQPONMLKJIHGFEDCBA"; // 26 字符 base32
const ADDRESS = `https://192.168.1.5:8790/a2a/${TOKEN}#fp=${FP}`;

describe("inboundView（config + extra → 展示模型）", () => {
  it("缺省配置：入站关、总开关关、loopback、默认端口 8790", () => {
    const v = inboundView(undefined, undefined);
    expect(v.enabled).toBe(false);
    expect(v.masterOn).toBe(false);
    expect(v.bindMode).toBe("loopback");
    expect(v.bindLabelKey).toBe("bind-loopback");
    expect(v.port).toBe(8790);
    expect(v.hasAddress).toBe(false);
    expect(v.address).toBe("");
    expect(v.listenAddress).toBe("127.0.0.1:8790");
  });

  it("读取开关与绑定配置", () => {
    const v = inboundView(
      { inbound_enabled: true, a2a_enabled: true, bind_mode: "lan", port: 9000 },
      {},
    );
    expect(v.enabled).toBe(true);
    expect(v.masterOn).toBe(true);
    expect(v.bindMode).toBe("lan");
    expect(v.bindLabelKey).toBe("bind-lan");
    expect(v.port).toBe(9000);
    // lan 的实际监听地址前端无法枚举——后端没上报就留空，不造假。
    expect(v.listenAddress).toBe("");
  });

  it("接入地址明文透传（含 token 与 #fp，绝不掩码——§12.C-2）", () => {
    const v = inboundView(
      { inbound_enabled: true },
      { inboundAddress: ADDRESS, inboundFingerprint: FP },
    );
    expect(v.hasAddress).toBe(true);
    expect(v.address).toBe(ADDRESS);
    expect(v.address).toContain(TOKEN); // token 完整可见
    expect(v.address).not.toContain("*");
    expect(v.fingerprint).toBe(FP);
  });

  it("实际监听地址：后端上报优先于前端推导", () => {
    const v = inboundView({}, { listenAddress: "10.0.0.3:8790" });
    expect(v.listenAddress).toBe("10.0.0.3:8790");
  });

  it("custom 绑定：推导监听地址带指定网卡", () => {
    const v = inboundView({ bind_mode: "custom", bind_interface: "192.168.1.10" }, {});
    expect(v.bindMode).toBe("custom");
    expect(v.bindInterface).toBe("192.168.1.10");
    expect(v.listenAddress).toBe("192.168.1.10:8790");
  });

  it("非法 bind_mode / port 回落默认值", () => {
    const v = inboundView({ bind_mode: "wildcard", port: -1 }, {});
    expect(v.bindMode).toBe("loopback");
    expect(v.port).toBe(8790);
    const v2 = inboundView({ port: "8790" }, {});
    expect(v2.port).toBe(8790);
  });

  it("extra 非字符串值不污染展示", () => {
    const v = inboundView({}, { inboundAddress: 123, inboundFingerprint: null });
    expect(v.address).toBe("");
    expect(v.fingerprint).toBe("");
    expect(v.hasAddress).toBe(false);
  });
});

describe("开启入站二次确认门（§12.C-4）", () => {
  it("关→开需要确认；开→关不需要", () => {
    expect(needsEnableConfirm(false, true)).toBe(true);
    expect(needsEnableConfirm(true, false)).toBe(false);
    expect(needsEnableConfirm(true, true)).toBe(false);
    expect(needsEnableConfirm(false, false)).toBe(false);
  });

  it("confirmReducer：request 进确认态，accept/cancel 回 idle", () => {
    expect(confirmReducer("idle", "request")).toBe("confirm");
    expect(confirmReducer("confirm", "accept")).toBe("idle");
    expect(confirmReducer("confirm", "cancel")).toBe("idle");
    expect(confirmReducer("idle", "accept")).toBe("idle");
    expect(confirmReducer("idle", "cancel")).toBe("idle");
  });

  it("重置确认走同一状态机：未 accept 不会触发重置", () => {
    // 组件只在 accept 分支调 doReset；这里验证门本身不会跳过确认态。
    let s = confirmReducer("idle", "request");
    expect(s).toBe("confirm");
    s = confirmReducer(s, "cancel");
    expect(s).toBe("idle"); // 取消后回到 idle，重置未发生
  });
});

describe("入站开关交互门（2026-11-18 UX 陷阱修复）", () => {
  it("总开关关 → 一律 blocked（开/关两个方向都不许 PATCH）", () => {
    expect(inboundToggleAction(false, false, true)).toBe("blocked");
    expect(inboundToggleAction(false, true, false)).toBe("blocked");
  });

  it("总开关开 → 保留既有确认流：关→开 confirm，开→关 apply", () => {
    expect(inboundToggleAction(true, false, true)).toBe("confirm");
    expect(inboundToggleAction(true, true, false)).toBe("apply");
  });

  it("JSX 层钉住：按钮 disabled 绑定 !view.masterOn + aria-disabled（node 环境无 DOM，源码钉住防回归）", () => {
    const src = readFileSync(
      fileURLToPath(new URL("./A2aInboundCard.tsx", import.meta.url)),
      "utf8",
    );
    expect(src).toContain('disabled={busy === "toggle" || !view.masterOn}');
    expect(src).toContain("aria-disabled={!view.masterOn}");
    expect(src).toContain("disabled:cursor-not-allowed");
    // onToggleClick 必须走 inboundToggleAction 门（blocked 分支 no-op）
    expect(src).toContain("const action = inboundToggleAction(view.masterOn, view.enabled, next);");
  });

  it("PATCH 成功提示「重启后生效」：key 双语齐全 + 文案语义 + 源码钉住成功路径", () => {
    const en = a2aEn.a2a as Record<string, string>;
    const zh = a2aZh.a2a as Record<string, string>;
    expect(zh["inbound-saved-restart"]).toContain("已保存");
    expect(zh["inbound-saved-restart"]).toContain("重启");
    expect(en["inbound-saved-restart"]).toMatch(/restart/i);
    expect(en["inbound-saved-restart"]).toContain("take effect");
    const src = readFileSync(
      fileURLToPath(new URL("./A2aInboundCard.tsx", import.meta.url)),
      "utf8",
    );
    expect(src).toContain('setMsg({ kind: "ok", text: t("inbound-saved-restart") });');
  });
});

describe("inboundTogglePatch", () => {
  it("只动 inbound_enabled，不夹带其余配置键", () => {
    expect(inboundTogglePatch(true)).toEqual({ inbound_enabled: true });
    expect(inboundTogglePatch(false)).toEqual({ inbound_enabled: false });
  });
});

describe("a2a i18n catalog（双语齐全 / 扁平 key / 安全文案）", () => {
  const en = a2aEn.a2a as Record<string, string>;
  const zh = a2aZh.a2a as Record<string, string>;

  it("zh-CN 与 en 键集完全一致（无缺 key）", () => {
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
  });

  it("key 全部扁平且不含点（IntlError 教训）", () => {
    for (const k of [...Object.keys(en), ...Object.keys(zh)]) {
      expect(k).not.toContain(".");
      expect(k).toMatch(/^[a-z0-9-]+$/);
    }
  });

  it("所有值非空", () => {
    for (const v of [...Object.values(en), ...Object.values(zh)]) {
      expect(typeof v).toBe("string");
      expect(v.trim().length).toBeGreaterThan(0);
    }
  });

  it("安全提示文案存在且语义正确", () => {
    // 地址等同密码提示
    expect(zh["inbound-address-secret-hint"]).toContain("等同密码");
    expect(en["inbound-address-secret-hint"]).toContain("password");
    // 重置后果说明：对端需重新粘贴
    expect(zh["inbound-reset-confirm-body"]).toContain("重新粘贴");
    expect(en["inbound-reset-confirm-body"]).toContain("paste");
    // 第三方信任提示（设计 §6.1 第 2 步必须出现）
    expect(zh["wizard-trust-warning"]).toContain("第三方");
    expect(zh["wizard-trust-warning"]).toContain("信任");
    expect(en["wizard-trust-warning"]).toContain("third party");
    // 开启入站的安全提示
    expect(zh["inbound-enable-confirm-body"]).toContain("人工审批");
  });

  it("后端 config_schema 每个键都有 title/description 文案", () => {
    const hubKeys = [
      "a2a_enabled", "inbound_enabled", "bind_mode", "bind_interface",
      "port", "admin_enabled", "audit_enabled",
    ];
    const peerKeys = [
      "enabled", "endpoint", "peer_fingerprint", "auth_scheme", "token",
      "trust", "caps", "confirm_mode", "allow_local_files",
      "task_timeout_s", "max_concurrent",
    ];
    for (const k of [...hubKeys, ...peerKeys]) {
      const base = `field-${k.replace(/_/g, "-")}`;
      expect(en[`${base}-title`], `missing en ${base}-title`).toBeTruthy();
      expect(zh[`${base}-title`], `missing zh ${base}-title`).toBeTruthy();
      expect(en[`${base}-desc`], `missing en ${base}-desc`).toBeTruthy();
      expect(zh[`${base}-desc`], `missing zh ${base}-desc`).toBeTruthy();
    }
  });

  it("useTranslations（mock 查表）：en 值 + {x} 插值 + 缺 key 回落 key 本身", () => {
    // 测试侧放宽 key 类型（真实组件里是字面量严格检查，tsc 把关）
    const t = useTranslations("a2a") as unknown as (
      key: string,
      values?: Record<string, string | number>,
    ) => string;
    expect(t("inbound-copy")).toBe("Copy");
    expect(t("wizard-added", { name: "Expense Bot" })).toBe("Connected Expense Bot");
    expect(t("wizard-step-of", { n: 2 })).toBe("Step 2 of 4");
    expect(t("wizard-card-incomplete", { fields: "name, skills" })).toBe(
      "Card is incomplete, missing: name, skills",
    );
    expect(t("no-such-key")).toBe("no-such-key");
  });
});

describe("useTranslations 切换逐 key 核对（防漏 key / 防注册回退）", () => {
  const en = a2aEn.a2a as Record<string, string>;
  const zh = a2aZh.a2a as Record<string, string>;

  /** 扫描组件源码里 t("…") 字面量 key（模板 key 另按家族断言）。 */
  function literalKeys(file: string): string[] {
    const src = readFileSync(fileURLToPath(new URL(file, import.meta.url)), "utf8");
    return [...src.matchAll(/\bt\("([a-z0-9-]+)"/g)].map((m) => m[1]);
  }

  it("两个组件的 t(\"…\") 字面量 key 全部存在于 en/zh-CN catalog", () => {
    for (const file of ["./A2aInboundCard.tsx", "./A2aPeerAddModal.tsx"]) {
      const keys = literalKeys(file);
      // 扫描结果为空说明正则失效，必须报错而不是假绿
      expect(keys.length, `${file} 字面量 key 扫描为空`).toBeGreaterThan(20);
      for (const k of keys) {
        expect(en[k], `${file}: en 缺 ${k}`).toBeTruthy();
        expect(zh[k], `${file}: zh 缺 ${k}`).toBeTruthy();
      }
    }
  });

  it("动态 key 家族（bind-* / wizard-err-* / auth-* / trust-*）双语齐全", () => {
    const dynamic = [
      ...["loopback", "lan", "custom"].map((m) => `bind-${m}`),
      ...["empty", "scheme", "host", "token", "fingerprint"].map((e) => `wizard-err-${e}`),
      ...["none", "bearer", "api-key", "oauth2"].map((s) => `auth-${s}`),
      ...["readonly", "normal", "full"].map((x) => `trust-${x}`),
    ];
    for (const k of dynamic) {
      expect(en[k], `en 缺 ${k}`).toBeTruthy();
      expect(zh[k], `zh 缺 ${k}`).toBeTruthy();
    }
  });

  it("三处同步注册：global.d.ts intersection + provider import/merge", () => {
    const dts = readFileSync(
      fileURLToPath(new URL("../../i18n/global.d.ts", import.meta.url)),
      "utf8",
    );
    expect(dts).toContain('type A2aMessages = typeof import("../../messages/en/a2a.json")');
    expect(dts).toMatch(/InstinctsMessages &\s*A2aMessages;/);
    const provider = readFileSync(
      fileURLToPath(new URL("../../i18n/provider.tsx", import.meta.url)),
      "utf8",
    );
    expect(provider).toContain('import a2aEn from "../../messages/en/a2a.json"');
    expect(provider).toContain('import a2aZh from "../../messages/zh-CN/a2a.json"');
    expect(provider).toMatch(/a2aEn,\s*\) as Messages/);
    expect(provider).toMatch(/a2aZh,\s*\) as Messages/);
  });
});
