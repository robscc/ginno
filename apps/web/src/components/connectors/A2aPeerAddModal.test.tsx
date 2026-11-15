/* A2aPeerAddModal 纯逻辑单测（node 环境）：地址解析镜像后端 address.py、
 * Card 缺字段校验镜像 card.py、鉴权映射、凭据步跳过、提交 payload、
 * 管理 API 走 connector action 通道的接线。渲染层留给 packaged UI e2e。
 *
 * next-intl mock：node 环境无 provider，useTranslations mock 成
 * key→en catalog 值的查表（组件已切 useTranslations("a2a") 正式注册）。 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTranslations } from "next-intl";
import {
  A2A_HUB_ID,
  addA2aPeer,
  authSchemesFromCard,
  buildPeerPayload,
  cardMissingFields,
  credentialsStepNeeded,
  parsePeerAddress,
  previewA2aCard,
  type ParsedPeerAddress,
} from "./A2aPeerAddModal";
import { connectorAction, patchConnectorConfig } from "@/lib/api/connectors";

vi.mock("@/lib/api/connectors", () => ({
  connectorAction: vi.fn(),
  patchConnectorConfig: vi.fn(),
}));

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

const TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"; // 26 字符 base32（A-Z2-7）
const FP = "ZYXWVUTSRQPONMLKJIHGFEDCBA";

function mustParse(input: string): ParsedPeerAddress {
  const p = parsePeerAddress(input);
  if (!p.ok) throw new Error(`expected ok, got ${p.error}`);
  return p;
}

describe("parsePeerAddress（镜像 a2a/address.py parse_address）", () => {
  it("完整能力地址：endpoint 剥 fragment，token/fp 原样保留", () => {
    const p = mustParse(`https://192.168.1.5:8790/a2a/${TOKEN}#fp=${FP}`);
    expect(p.endpoint).toBe(`https://192.168.1.5:8790/a2a/${TOKEN}`);
    expect(p.endpoint).not.toContain("#"); // fragment 永不进消息端点
    expect(p.token).toBe(TOKEN);
    expect(p.fingerprint).toBe(FP);
    expect(p.oneTime).toBe(false);
  });

  it("token 大小写宽容且原样保留（token 是凭据，不归一化）", () => {
    const mixed = "AbCdEfGhIjKlMnOpQrStUvWxYz"; // 26 字符
    const p = mustParse(`https://h:8790/a2a/${mixed}`);
    expect(p.token).toBe(mixed);
  });

  it("一次性地址标记 ot=1（fragment 内，仅本地生效）", () => {
    const p = mustParse(`https://h:8790/a2a/${TOKEN}#fp=${FP}&ot=1`);
    expect(p.oneTime).toBe(true);
    expect(p.fingerprint).toBe(FP);
  });

  it("能力地址尾部斜杠容忍", () => {
    const p = mustParse(`https://h:8790/a2a/${TOKEN}/`);
    expect(p.token).toBe(TOKEN);
    expect(p.endpoint).toBe(`https://h:8790/a2a/${TOKEN}`);
  });

  it("token 截断（25 字符）→ 尽早报错，不等连上才 401", () => {
    const p = parsePeerAddress(`https://h:8790/a2a/${TOKEN.slice(0, 25)}`);
    expect(p).toEqual({ ok: false, error: "token" });
  });

  it("token 含 base32 字母表外字符（0/1/8/9）→ 报错", () => {
    for (const bad of ["0", "1", "8", "9"]) {
      const t = bad + TOKEN.slice(1);
      expect(parsePeerAddress(`https://h/a2a/${t}`)).toEqual({ ok: false, error: "token" });
    }
  });

  it("指纹非法（截断/改动）→ 报错", () => {
    expect(parsePeerAddress(`https://h/a2a/${TOKEN}#fp=SHORT`)).toEqual({
      ok: false,
      error: "fingerprint",
    });
    expect(parsePeerAddress(`https://h/a2a/${TOKEN}#fp=${FP.slice(0, 25)}`)).toEqual({
      ok: false,
      error: "fingerprint",
    });
  });

  it("裸域名补 https，endpoint 为 origin（well-known 发现由后端做）", () => {
    const p = mustParse("agent.example.com");
    expect(p.endpoint).toBe("https://agent.example.com");
    expect(p.token).toBe("");
    expect(p.fingerprint).toBe("");
  });

  it("标准 endpoint：保留 path 与 query，无 token", () => {
    const p = mustParse("https://host:9000/rpc?x=1");
    expect(p.endpoint).toBe("https://host:9000/rpc?x=1");
    expect(p.token).toBe("");
  });

  it("http 允许（环回自测场景）", () => {
    const p = mustParse(`http://127.0.0.1:8790/a2a/${TOKEN}`);
    expect(p.endpoint).toBe(`http://127.0.0.1:8790/a2a/${TOKEN}`);
  });

  it("伪 scheme（javascript:）不能当域名补 → scheme 错误", () => {
    expect(parsePeerAddress("javascript:alert(1)")).toEqual({ ok: false, error: "scheme" });
    expect(parsePeerAddress("ftp://host/a2a")).toEqual({ ok: false, error: "scheme" });
  });

  it("空输入 / 缺主机名 → 对应错误码", () => {
    expect(parsePeerAddress("")).toEqual({ ok: false, error: "empty" });
    expect(parsePeerAddress("   ")).toEqual({ ok: false, error: "empty" });
    expect(parsePeerAddress("https://")).toEqual({ ok: false, error: "host" });
  });

  it("首尾空白容忍（粘贴常见）", () => {
    const p = mustParse(`  https://h:8790/a2a/${TOKEN}#fp=${FP}  `);
    expect(p.token).toBe(TOKEN);
  });
});

describe("cardMissingFields（镜像 a2a/card.py validate_card）", () => {
  const fullCard = {
    name: "Expense Bot",
    url: "https://h/a2a",
    description: "报销助手",
    version: "1.0.0",
    capabilities: { streaming: true },
    skills: [{ id: "s1", name: "报销", description: "处理发票" }],
  };

  it("完整 Card → 无缺字段", () => {
    expect(cardMissingFields(fullCard)).toEqual([]);
  });

  it("空 Card → 六项全缺", () => {
    expect(cardMissingFields(null)).toEqual([
      "name", "url", "description", "version", "capabilities", "skills",
    ]);
  });

  it("部分缺失逐项列出；空白字符串算缺失", () => {
    expect(cardMissingFields({ ...fullCard, name: "  ", skills: [] })).toEqual([
      "name", "skills",
    ]);
    expect(cardMissingFields({ ...fullCard, capabilities: {} })).toEqual(["capabilities"]);
  });
});

describe("authSchemesFromCard（OpenAPI securitySchemes → 枚举）", () => {
  it("http bearer / apiKey / oauth2 映射", () => {
    const card = {
      securitySchemes: {
        b: { type: "http", scheme: "bearer" },
        k: { type: "apiKey", in: "header", name: "X-Key" },
        o: { type: "oauth2", flows: {} },
      },
    };
    expect(authSchemesFromCard(card).sort()).toEqual(["api_key", "bearer", "oauth2"]);
  });

  it("未声明鉴权 → [none]", () => {
    expect(authSchemesFromCard(null)).toEqual(["none"]);
    expect(authSchemesFromCard({})).toEqual(["none"]);
    expect(authSchemesFromCard({ securitySchemes: {} })).toEqual(["none"]);
  });

  it("未知 type 忽略、重复去重", () => {
    const card = {
      securitySchemes: {
        a: { type: "http", scheme: "bearer" },
        b: { type: "http", scheme: "Bearer" },
        c: { type: "mutualTLS" },
      },
    };
    expect(authSchemesFromCard(card)).toEqual(["bearer"]);
  });
});

describe("凭据步跳过与提交 payload", () => {
  it("能力地址已含 token → 跳过凭据步（设计 §6.1 第 3 步）", () => {
    expect(credentialsStepNeeded(mustParse(`https://h/a2a/${TOKEN}`))).toBe(false);
    expect(credentialsStepNeeded(mustParse("https://h/rpc"))).toBe(true);
  });

  it("buildPeerPayload：endpoint 无 fragment、指纹独立、默认最严", () => {
    const parsed = mustParse(`https://h:8790/a2a/${TOKEN}#fp=${FP}`);
    const payload = buildPeerPayload({
      parsed,
      name: "  报销助手  ",
      authScheme: "bearer",
      token: "secret-token",
      trust: "readonly",
    });
    expect(payload).toEqual({
      endpoint: `https://h:8790/a2a/${TOKEN}`,
      name: "报销助手",
      peer_fingerprint: FP,
      auth_scheme: "bearer",
      token: "secret-token",
      trust: "readonly",
      caps: ["L0"],
      confirm_mode: "every",
    });
    expect(String(payload.endpoint)).not.toContain("#");
  });
});

describe("管理 API 接线（connector action 通道）", () => {
  beforeEach(() => {
    vi.mocked(connectorAction).mockReset();
    vi.mocked(patchConnectorConfig).mockReset();
  });

  it("previewA2aCard → POST action a2a_preview_card，传原始地址（含 #fp）", async () => {
    vi.mocked(connectorAction).mockResolvedValue({ ok: true, card: { name: "X" } });
    const raw = `https://h:8790/a2a/${TOKEN}#fp=${FP}`;
    const r = await previewA2aCard(raw);
    expect(connectorAction).toHaveBeenCalledWith(A2A_HUB_ID, "a2a_preview_card", { url: raw });
    expect(r.card).toEqual({ name: "X" });
  });

  it("addA2aPeer → POST action a2a_add_peer，payload 原样透传", async () => {
    vi.mocked(connectorAction).mockResolvedValue({ ok: true, id: "a2a-expense-bot", slug: "expense-bot" });
    const payload = { endpoint: "https://h/a2a", name: "X", trust: "readonly" };
    const r = await addA2aPeer(payload);
    expect(connectorAction).toHaveBeenCalledWith(A2A_HUB_ID, "a2a_add_peer", payload);
    expect(r.ok).toBe(true);
    expect(r.slug).toBe("expense-bot");
  });

  it("后端未实现 action（{error}）→ 结果原样返回，由 UI 降级", async () => {
    vi.mocked(connectorAction).mockResolvedValue({ error: "unknown action: a2a_preview_card" });
    const r = await previewA2aCard("https://h");
    expect(r.error).toContain("unknown action");
    expect(r.card).toBeUndefined();
  });

  it("枢纽 id 恒为 a2a", () => {
    expect(A2A_HUB_ID).toBe("a2a");
  });
});

describe("向导文案（useTranslations mock 查表）", () => {
  it("步骤/错误/鉴权 key 可解析，{n} 插值正确", () => {
    // 测试侧放宽 key 类型（真实组件里是字面量严格检查，tsc 把关）
    const t = useTranslations("a2a") as unknown as (
      key: string,
      values?: Record<string, string | number>,
    ) => string;
    expect(t("wizard-title")).toBe("Connect external agent");
    expect(t("wizard-step-of", { n: 3 })).toBe("Step 3 of 4");
    expect(t("wizard-err-token")).toContain("26 base32");
    expect(t("auth-api-key")).toBe("API key");
    expect(t("trust-readonly")).toBe("Read-only (default)");
  });
});
