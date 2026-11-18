/**
 * remoteGuard — 远端会话前端守卫的单一判定源（2026-11-18 远程特性审计 C2）。
 *
 * 背景：remote 会话（session.remote_peer 非空，如 97b8c71e→ginno-node-vps）
 * 执行在远端节点——api/stream 的 remote 转发只送 user_text，附件 / @mention
 * / slash skill 全部到不了对端（后端 fail-loud 归审计单 C1）。前端先堵入口：
 * composer 附件与 slash 按钮、@ 菜单、文件/计划面板的空列表，全部以本函数
 * 为判据。判据必须是 **remote_peer 字段本身**：session.type === "channel"
 * 是「渠道只读会话」，与「执行方在远端节点」是两个正交概念，绝不互推。
 *
 * 与既有守卫的关系：ModelChipMenu（chip 禁用 + tooltip）与 useModelPick
 * （pick 前 fail-closed）直接内联判 session?.remote_peer；新守卫集中到
 * 本函数，旧两处不回改（行为一致，改动面最小）。
 */

export interface RemoteGuardSessionLike {
  /** 对端节点 slug（PeerStore 注册名）；非空 = 本会话在远端执行。 */
  remote_peer?: string | null;
  /** 对端显示名（后端 PeerStore 解析，#199 随 session 载荷下发）。 */
  remote_peer_name?: string | null;
  /** 会话类型；仅用于防混淆的负例，判定本身不看它。 */
  type?: string | null;
}

/**
 * 会话是否执行在远端节点。truthy 检查兼容 null/undefined/空串载荷；
 * 旧 sidecar 不下发 remote_peer 时恒 false（fail-open 是安全的：等同
 * 现状，本地会话不受影响）。
 */
export function isRemoteSession(session: RemoteGuardSessionLike | null | undefined): boolean {
  return typeof session?.remote_peer === "string" && session.remote_peer.length > 0;
}

/**
 * 对端显示名：session 载荷真源（后端 PeerStore 就地解析，#199 随载荷下发）
 * → slug 回退。与 ModelChipMenu 的三级回退相比少了本地 peers 查找——面板
 * 说明条非身份徽标，slug 兜底可接受，避免每个面板都挂 useA2aPeers 订阅。
 */
export function remotePeerDisplayName(
  session: RemoteGuardSessionLike | null | undefined,
): string {
  if (!isRemoteSession(session)) return "";
  return session?.remote_peer_name || session?.remote_peer || "";
}
