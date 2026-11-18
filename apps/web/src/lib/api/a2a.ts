/**
 * A2A 出站 peer 的读面 + 管理写面（remote-session-design.md R3 +
 * 2026-11-18 连接器页改造）。
 *
 * 读面挂 api/connectors.py（GET /api/a2a/peers），前端按协议独立成
 * 域文件：connectors.ts 已是连接器管理大域，而 peer 列表的执行方选择器
 * （R3）与 @ 菜单 peer 项（R2）都消费同一读面，单独成文件避免两期并行
 * 施工挤在同一文件里。
 *
 * 写面（删除/重连）走 hub 的 connector action 通道——与 a2a_add_peer 同一
 * 分发器（POST /api/connectors/a2a/action），契约见 A2aPeersPane 头注释。
 *
 * 读面契约（后端已实现，脱敏）：{ok:true, peers:[{slug,id,name,endpoint,
 * enabled,trust,caps,status,statusDetail}]}，**不含 token**；后端读失败回
 * {ok:true, peers:[]}。
 */
import { BASE, json } from "./core";
import { connectorAction } from "./connectors";

/** GET /api/a2a/peers 的单条 peer（脱敏投影，绝无 token 字段）。 */
export interface A2aPeerPublic {
  slug: string;
  id: string;
  name: string;
  endpoint: string;
  enabled: boolean;
  trust: string;
  caps: string[];
  /** registry 上报的连接状态（connected/disconnected/…）；空串=未上报。 */
  status?: string;
  statusDetail?: string;
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

/** a2a_delete_peer：peer 仍有未终态任务时 ok:false + pending 清单。 */
export interface A2aDeletePeerResult {
  ok?: boolean;
  removed?: boolean;
  error?: string;
  pending?: Array<{ key: string; task_id: string; state: string }>;
}

export async function deleteA2aPeer(slug: string): Promise<A2aDeletePeerResult> {
  return connectorAction("a2a", "a2a_delete_peer", { slug });
}

/** a2a_reconnect_peer：P0 连通探针（fetch_card），status 回探针结果。 */
export interface A2aReconnectResult {
  ok?: boolean;
  status?: string;
  error?: string | null;
}

export async function reconnectA2aPeer(slug: string): Promise<A2aReconnectResult> {
  return connectorAction("a2a", "a2a_reconnect_peer", { slug });
}