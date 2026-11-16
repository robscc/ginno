/**
 * 执行方选择器的「远端 peer」target 编码（remote-session-design.md R3）。
 *
 * 设计选择：composer 的 `target` 保持 `string | null` 不变，远端 peer 用
 * `peer:<slug>` 前缀串与本地 agent id 区分——不另加 state、不改既有
 * onSelect/setTarget 签名，本地 agent 选择行为零改动。agent id 是配置
 * slug（不含冒号），前缀无碰撞面；所有消费 target 的发送/预览路径统一
 * 经 parsePeerTarget 分流。
 *
 * 纯函数模块（无 React），node 环境 vitest 直测。
 */

export const PEER_TARGET_PREFIX = "peer:";

/** peer slug → target 串。 */
export function peerTargetValue(slug: string): string {
  return PEER_TARGET_PREFIX + slug;
}

/** target 串 → peer slug；本地 agent id / null / 空前缀一律返回 null。 */
export function parsePeerTarget(target: string | null | undefined): string | null {
  if (!target || !target.startsWith(PEER_TARGET_PREFIX)) return null;
  const slug = target.slice(PEER_TARGET_PREFIX.length);
  return slug || null;
}

/** chip 上的地址提示：剥协议头与 #fp 指纹片段，只留 host:port/path
 *  （完整 endpoint 进 tooltip；指纹是凭据语义，不该当地址展示）。 */
export function peerEndpointHint(endpoint: string): string {
  return endpoint.replace(/^https?:\/\//, "").replace(/#.*$/, "");
}