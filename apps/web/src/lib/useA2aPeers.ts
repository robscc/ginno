/**
 * useA2aPeers — 远端 peer 列表的轻量读钩子（remote-session-design.md R3）。
 *
 * 消费方：首页执行方选择器（AgentChips，active=!session）与会话头远端
 * 徽标（RemotePeerBadge，active=!!remote_peer）。两者生命周期错开，不做
 * 全局缓存——端点是本机脱敏读，成本可忽略；peer 在连接器页新增后回到
 * 首页即重新拉取，天然新鲜。
 */
import { useEffect, useState } from "react";
import { listA2aPeers, type A2aPeerPublic } from "./api/a2a";

export function useA2aPeers(active: boolean): A2aPeerPublic[] {
  const [peers, setPeers] = useState<A2aPeerPublic[]>([]);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void listA2aPeers().then((rows) => {
      if (alive) setPeers(rows);
    });
    return () => {
      alive = false;
    };
  }, [active]);
  return peers;
}