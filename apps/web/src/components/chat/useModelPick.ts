"use client";

// useModelPick — composer 模型 chip 的选择逻辑（自 ChatStream.tsx 机械拆出，
// 行为零变化）：home 态的待生效模型/挂载目录、服务端热切（pickModel），以及
// chip 展示用的解析（homeEffective / modelChipLabel 等）。

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import type { SessionMeta } from "@/lib/types";

interface ModelPickDeps {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  target: string | null;
}

export function useModelPick({ g, session, target }: ModelPickDeps) {
  const tc = useTranslations("chat");
  const router = useRouter();

  // Home-state model pick (composer model chip, M3) consumed on lazy creation.
  const [homeModel, setHomeModel] = useState<{ provider?: string; model?: string } | undefined>(
    undefined,
  );
  const homeCreatingRef = useRef(false);

  // 首页待挂载目录(home-mount-picker-design.md §2.3)：随首条消息的建会话请求
  // 生效，会话打开后 TopBar chip 以同一批 id 接管。初始值/发送后重置值都是
  // 库里 auto_mount 条目——手动改动只活到本次发送。
  const [pendingMounts, setPendingMounts] = useState<{ ids: string[]; primary: string | null }>({
    ids: [],
    primary: null,
  });
  const resetPendingMounts = useCallback(async () => {
    try {
      const r = await api.listFolders();
      const autos = (r.folders || []).filter((f) => f.auto_mount);
      setPendingMounts(
        autos.length ? { ids: autos.map((f) => f.id), primary: autos[0].id } : { ids: [], primary: null },
      );
    } catch {
      /* 无 sidecar：保持空 pending，chip 仍可手输路径 */
    }
  }, []);
  // 仅组件挂载时预填一次；发送成功后的重置在 createAndSend 里显式触发。
  useEffect(() => {
    void resetPendingMounts();
  }, [resetPendingMounts]);

  // ── composer inline controls (open-experience redesign M3) ──────────────
  // Model chip = per-session provider/model switch. In-place on the server
  // (model-assignment-design.md §3.2): the running graph resolves its model
  // per LLM call, so switching mid-turn takes effect at the next step — no
  // socket cycle, no stream interruption.
  const [modelOpen, setModelOpen] = useState(false);
  async function pickModel(pid: string, model: string) {
    setModelOpen(false);
    // Never select an empty model. The chip label falls back to the provider id
    // (`model || provider`), so an empty model leaves the label unchanged and the
    // click reads as "nothing happened" — the exact shape of the "cannot pick a
    // model" report. The menu cannot produce one; this is the guard behind it.
    if (!model) return;
    if (!session) {
      // 首页态：会话还没建，切换只写进本地 homeModel。旧实现完全无声，
      // 用户点完 chip 不知道到底生效没（下一个会话才可见）。
      setHomeModel({ provider: pid, model });
      pushNotice("info", tc("notice.homeModelPicked", { model }), {
        source: "model",
        dedupeKey: "model:__home__",
      });
      return;
    }
    const prevProvider = session.provider;
    const prevModel = session.model;
    g.applySessionPatch(session.id, { provider: pid, model });
    try {
      const r = await api.patchSession(session.id, { provider: pid, model });
      if (!r?.ok || !r.session) throw new Error(r?.error ?? "switch failed");
      g.applySessionPatch(session.id, r.session);
    } catch {
      // 旧实现是静默回滚：chip 弹回原样、零解释，读作「点了没反应」。
      // 现在走公共通知层常驻提示，并给一个去设置的出口。
      g.applySessionPatch(session.id, { provider: prevProvider, model: prevModel });
      pushNotice("error", tc("notice.modelSwitchFailed", { model }), {
        source: "model",
        duration: 0,
        action: {
          label: tc("notice.openModelSettings"),
          onClick: () => router.push("/settings/connections/model-api"),
        },
      });
    }
  }
  const enabledProviders = Object.entries(g.providers).filter(([, p]) => p.enabled);
  // At home nothing is chosen yet, so the chip should name what WOULD be used —
// the default provider's own model — not the provider id. A bare "custom" there
// reads as a model name and looks unchangeable (the "default is customer"
// report), and the id is the one thing the user cannot act on.
  const defaultProviderCfg = g.providers[g.defaultProvider];
  // 生效模型解析——镜像服务端 _resolve_provider_model / provider_is_deliberate
  // 的优先级:显式选择 > 目标 agent 的 deliberate 绑定 > 全局默认。chip 显示
  // 什么,首个会话就用什么;此前 label 只认全局默认、懒创建又只在主动点选时
  // 才下发,agent 绑定在服务端悄悄接管——「选了 glm 进去变 qwen」的根因。
  const targetAgent =
    g.agents.find((a) => a.id === (target ?? g.agents[0]?.id)) ?? null;
  const agentBindingPid =
    targetAgent?.provider &&
    (targetAgent.provider !== "custom" || targetAgent.provider_explicit) &&
    g.providers[targetAgent.provider]?.enabled
      ? targetAgent.provider
      : null;
  const homeEffective = homeModel
    ? { ...homeModel }
    : agentBindingPid
      ? {
          provider: agentBindingPid,
          model:
            targetAgent?.model ||
            g.providers[agentBindingPid]?.default_model ||
            g.providers[agentBindingPid]?.model ||
            "",
        }
      : {
          provider: g.defaultProvider,
          model:
            defaultProviderCfg?.default_model ||
            defaultProviderCfg?.model ||
            "",
        };
  // 首页态的模型来源:picked=用户点选 / agent=agent 绑定 / default=应用默认
  const homeModelSource: "picked" | "agent" | "default" = homeModel
    ? "picked"
    : agentBindingPid
      ? "agent"
      : "default";
  const modelChipLabel = session
    ? session.model || session.provider
    : homeEffective.model ||
      homeEffective.provider ||
      defaultProviderCfg?.name ||
      g.defaultProvider;

  return {
    homeModel, homeCreatingRef, pendingMounts, setPendingMounts, resetPendingMounts,
    modelOpen, setModelOpen, pickModel, enabledProviders, defaultProviderCfg,
    targetAgent, agentBindingPid, homeEffective, homeModelSource, modelChipLabel,
  };
}
