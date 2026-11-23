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
import type { SessionMeta, ThinkingEffort } from "@/lib/types";
import { inheritHomeEffort, modelEffortLevels, normalizeHomeEffort } from "./modelEfforts";

interface ModelPickDeps {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  target: string | null;
}

export function useModelPick({ g, session, target }: ModelPickDeps) {
  const tc = useTranslations("chat");
  const router = useRouter();

  // Home-state model pick (composer model chip, M3) consumed on lazy creation.
  // thinking_effort 混在同一个对象里(thinking-effort-design §5):随
  // createAndSend 的 ...(homeModel ?? {}) 一并下发。
  const [homeChoice, setHomeChoice] = useState<
    { provider: string; model: string; thinking_effort?: ThinkingEffort } | undefined
  >(undefined);
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
    // 远端会话 fail-closed（remote_turn.py 不读 model/provider，模型由远端
    // 节点配置决定）：chip 已在 ModelChipMenu 禁用，这里再守一道——任何
    // 未来路径重开菜单也发不出 PATCH。本地会话（无 remote_peer）零影响。
    if (session?.remote_peer) return;
    // Never select an empty model. The chip label falls back to the provider id
    // (`model || provider`), so an empty model leaves the label unchanged and the
    // click reads as "nothing happened" — the exact shape of the "cannot pick a
    // model" report. The menu cannot produce one; this is the guard behind it.
    if (!model) return;
    if (!session) {
      const destinationLevels = modelEffortLevels(g.providers[pid], model);
      const inherited = inheritHomeEffort(homeEffort, homeLevels, destinationLevels);
      const destinationDefault = normalizeHomeEffort(targetAgent?.thinking_effort, destinationLevels);
      setHomeChoice({
        provider: pid,
        model,
        thinking_effort: homeChoice?.thinking_effort !== undefined || inherited !== destinationDefault
          ? inherited
          : undefined,
      });
      const message = homeEffort !== "auto" && inherited === "auto"
        ? tc("model.thinkingReset")
        : tc("notice.homeModelPicked", { model });
      pushNotice("info", message, {
        source: "model",
        dedupeKey: "model:__home__",
      });
      return;
    }
    try {
      const r = await api.patchSession(session.id, { provider: pid, model });
      if (!r?.ok || !r.session) throw new Error(r?.error ?? "switch failed");
      g.applySessionPatch(session.id, r.session);
    } catch {
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
  const homeProvider = agentBindingPid ?? g.defaultProvider;
  const homeProviderCfg = g.providers[homeProvider];
  const homeEffective = homeChoice
    ? { provider: homeChoice.provider, model: homeChoice.model }
    : {
        provider: homeProvider,
        model: targetAgent?.model || homeProviderCfg?.default_model || homeProviderCfg?.model || "",
      };
  const homeModelSource: "picked" | "agent" | "default" = homeChoice
    ? "picked"
    : agentBindingPid || targetAgent?.model
      ? "agent"
      : "default";
  const modelChipLabel = session
    ? session.model || session.provider
    : homeEffective.model ||
      homeEffective.provider ||
      defaultProviderCfg?.name ||
      g.defaultProvider;

  const homeLevels = modelEffortLevels(g.providers[homeEffective.provider], homeEffective.model);
  const homeEffort = normalizeHomeEffort(homeChoice?.thinking_effort ?? targetAgent?.thinking_effort, homeLevels);
  // 两个建会话入口共用这个草稿。能力列表更新后，提交值与首页角标保持一致。
  const homeModel = homeChoice?.thinking_effort !== undefined
    ? { ...homeChoice, thinking_effort: homeEffort }
    : homeChoice;
  const thinkingEffort = session ? session.thinking_effort || "auto" : homeEffort;

  // 选模型 + 设思考档位的**原子**操作（2026-11-14 用户反馈「难度要点两次才到位」）。
  // 旧实现里 ModelChipMenu 的难度档 onClick 分别调 pickModel + pickThinking，两条
  // 并发 PATCH 各自用完整 r.session 覆盖本地状态——模型 PATCH 的应答带着服务端处理
  // 它那一刻的旧 thinking_effort，只要它晚于思考 PATCH 到达，就把乐观更新的新档位
  // 冲回旧值，读作「点了没生效」；第二次点时服务端已存住新值，模型应答不再陈旧，
  // 才到位。合并成单条 PATCH（provider+model+thinking_effort 一次下发）根除竞态，
  // 顺带省掉一次冗余往返。
  async function pickModelAndThinking(pid: string, model: string, value: ThinkingEffort) {
    setModelOpen(false);
    // 远端会话 fail-closed（同 pickModel）：模型与思考档位都由远端节点决定。
    if (session?.remote_peer) return;
    if (!model) return;
    if (!session) {
      // 首页态：会话未建，三字段一并写进 homeModel，随懒创建下发。
      setHomeChoice({ provider: pid, model, thinking_effort: value });
      pushNotice("info", tc("notice.homeModelPicked", { model }), {
        source: "model",
        dedupeKey: "model:__home__",
      });
      return;
    }
    const prevProvider = session.provider;
    const prevModel = session.model;
    const prevEffort = session.thinking_effort || "auto";
    g.applySessionPatch(session.id, {
      provider: pid,
      model,
      thinking_effort: value,
    });
    try {
      const r = await api.patchSession(session.id, {
        provider: pid,
        model,
        thinking_effort: value,
      });
      if (!r?.ok || !r.session) throw new Error(r?.error ?? "switch failed");
      g.applySessionPatch(session.id, r.session);
    } catch {
      g.applySessionPatch(session.id, {
        provider: prevProvider,
        model: prevModel,
        thinking_effort: prevEffort,
      });
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

  return {
    homeModel, homeCreatingRef, pendingMounts, setPendingMounts, resetPendingMounts,
    modelOpen, setModelOpen, pickModel, enabledProviders, defaultProviderCfg,
    targetAgent, agentBindingPid, homeEffective, homeModelSource, modelChipLabel,
    thinkingEffort, pickModelAndThinking,
  };
}
