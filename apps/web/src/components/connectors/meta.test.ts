/* meta.tsx connectorName/connectorDesc 纯逻辑单测（node 环境）：连接器
 * 卡片/详情的 name 与 description 走 conn 域 card.name.<id> /
 * card.desc.<id> 双语映射，缺 key（mod、a2a peer 动态实例、专名）回落后端
 * 原值。渲染层留给 packaged UI e2e。 */

import { describe, expect, it } from "vitest";
import { connectorDesc, connectorName } from "./meta";
import type { ConnectorInfo } from "@/lib/runtime";
import connEn from "../../../messages/en/conn.json";
import connZh from "../../../messages/zh-CN/conn.json";

const en = connEn.conn as unknown as {
  card: { name: { [id: string]: string }; desc: { [id: string]: string } };
};
const zh = connZh.conn as unknown as typeof en;

/** 与 next-intl t.has/t 语义一致的查表（存在性判定 + 取值）：card 节的
 * name/desc 子对象拍平成 card.name.<id> / card.desc.<id> 完整键。 */
const lookup = (card: typeof en.card) => {
  const flat: Record<string, string> = {};
  for (const kind of ["name", "desc"] as const) {
    for (const [id, v] of Object.entries(card[kind] || {})) flat[`card.${kind}.${id}`] = v;
  }
  return Object.assign((key: string) => flat[key] ?? key, {
    has: (key: string) => Object.prototype.hasOwnProperty.call(flat, key),
  });
};

const tEn = lookup(en.card);
const tZh = lookup(zh.card);

function fake(id: string, name: string, description: string): ConnectorInfo {
  return { id, name, description } as unknown as ConnectorInfo;
}

describe("connectorName / connectorDesc：内置连接器双语映射", () => {
  it("内置 id 命中 card.name/desc.<id>（en/zh 各取各的）", () => {
    const c = fake("wecom-bot", "WeCom Smart Bot", "backend english desc");
    expect(connectorName(c, tEn)).toBe("WeCom Smart Bot");
    expect(connectorName(c, tZh)).toBe("企业微信智能机器人");
    expect(connectorDesc(c, tZh)).toBe(zh.card.desc["wecom-bot"]);
    expect(connectorDesc(c, tZh)).not.toBe("backend english desc");
  });

  it("缺 key（mod 连接器 / a2a peer 动态实例）→ 回落后端原值", () => {
    const mod = fake("mod-some-tool", "Some Mod", "mod-provided description");
    expect(connectorName(mod, tEn)).toBe("Some Mod");
    expect(connectorName(mod, tZh)).toBe("Some Mod");
    expect(connectorDesc(mod, tZh)).toBe("mod-provided description");
  });

  it("en/zh catalog 的 name/desc 键集对称（漏配 zh 不至于回落英文也应被发现）", () => {
    expect(Object.keys(en.card.name).sort()).toEqual(Object.keys(zh.card.name).sort());
    expect(Object.keys(en.card.desc).sort()).toEqual(Object.keys(zh.card.desc).sort());
  });
});
