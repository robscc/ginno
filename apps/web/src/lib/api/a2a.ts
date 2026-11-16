/**
 * A2A 出站 peer 的读面（remote-session-design.md R3）。
 *
 * 后端路由挂在 api/connectors.py（GET /api/a2a/peers），前端按协议独立成
 * 域文件：connectors.ts 已是连接器管理大域，而 peer 列表的执行方选择器
 * （R3）与 @ 菜单 peer 项（R2）都消费同一读面，单独成文件避免两期并行
 * 施工挤在同一文件里。
 *
 * 契约（后端已实现，脱敏）：{ok:true, peers:[{slug,id,name,endpoint,
 * enabled,trust,caps}]}，**不含 token**；后端读失败回 {ok:true, peers:[]}。
 */
import { BASE, json } from "./core";

/** GET /api/a2a/peers 的单条 peer（脱敏投影，绝无 token 字段）。 */
export interface A2aPeerPublic {
  slug: string;
  id: string;
  name: string;
  endpoint: string;
  enabled: boolean;
  trust: string;
  caps: string[];
}

/** 取已接入的远端 peer 列表。网络异常/旧 sidecar 无此端点一律回落空表
 *  ——执行方选择器少几个候选可以接受，崩掉首页不行。 */
export async function listA2aPeers(): Promise<A2aPeerPublic[]> {
  try {
    const r = await json<{ ok: boolean; peers?: A2aPeerPublic[] }>(`${BASE}/a2a/peers`);
    return r?.peers ?? [];
  } catch {
    return [];
  }
}