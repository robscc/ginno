"use client";

import { useEffect, useRef, useState } from "react";
import { Zap, Server } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ProviderConfig, Providers } from "@/lib/types";
import { ProviderCard, type VerifyState } from "./ProviderCard";

const ORDER = ["anthropic", "openai", "custom"];
const META: Record<string, { name: string; subtitle: string; icon: React.ReactNode }> = {
  anthropic: {
    name: "Anthropic",
    subtitle: "Claude 3.5 / 3.7 Sonnet, Haiku, Opus",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange/15 text-orange">
        <Zap className="h-4.5 w-4.5 fill-orange" />
      </div>
    ),
  },
  openai: {
    name: "OpenAI",
    subtitle: "GPT-4o, GPT-4 Turbo, o1, o3-mini",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-green/15 text-green">
        <span className="h-3.5 w-3.5 rounded-full border-2 border-green" />
      </div>
    ),
  },
  custom: {
    name: "自定义端点 (OpenAI Compatible)",
    subtitle: "Ollama, DeepSeek, Qwen, Groq, LM Studio 等",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-violet/15 text-violet">
        <Server className="h-4.5 w-4.5" />
      </div>
    ),
  },
};

export function ModelApiSettings() {
  const g = useGinno();
  const [draft, setDraft] = useState<Providers>({});
  const [status, setStatus] = useState<Record<string, VerifyState>>({});
  const loaded = useRef(false);

  useEffect(() => {
    if (!loaded.current && Object.keys(g.providers).length) {
      setDraft(g.providers);
      setStatus(
        Object.fromEntries(Object.keys(g.providers).map((k) => [k, { state: "idle" }])),
      );
      loaded.current = true;
    }
  }, [g.providers]);

  const setField = (id: string, key: keyof ProviderConfig, value: unknown) =>
    setDraft((d) => ({ ...d, [id]: { ...d[id], [key]: value } }));

  const save = async (next: Providers) => {
    try {
      await api.putProviders(next);
    } catch {
      /* ignore */
    }
  };

  const onBlurSave = () => save(draft);

  const onToggle = (id: string) => {
    const next = { ...draft, [id]: { ...draft[id], enabled: !draft[id].enabled } };
    setDraft(next);
    save(next);
  };

  const onVerify = async (id: string) => {
    await save(draft);
    setStatus((s) => ({ ...s, [id]: { state: "checking" } }));
    const r = await api.verifyProvider(id);
    setStatus((s) => ({
      ...s,
      [id]: r.ok ? { state: "ok" } : { state: "fail", msg: r.error },
    }));
  };

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">模型 API 配置</h2>
      <p className="mt-1 text-sm text-muted">
        配置 Anthropic、OpenAI 等模型提供商的 API Key，用于驱动 Agent 推理能力。
      </p>

      <div className="mt-4 flex gap-2">
        <span className="pill border border-orange/40 text-orange">
          <Zap className="h-3 w-3 fill-orange" /> Anthropic Protocol
        </span>
        <span className="pill border border-green/40 text-green">
          <span className="h-1.5 w-1.5 rounded-full bg-green" /> OpenAI Protocol
        </span>
      </div>

      <div className="mt-5 space-y-4">
        {ORDER.filter((id) => draft[id]).map((id) => (
          <ProviderCard
            key={id}
            cfg={draft[id]}
            icon={META[id]?.icon}
            name={META[id]?.name || id}
            subtitle={META[id]?.subtitle || ""}
            status={status[id] || { state: "idle" }}
            setField={(k, v) => setField(id, k, v)}
            onBlurSave={onBlurSave}
            onToggle={() => onToggle(id)}
            onVerify={() => onVerify(id)}
          />
        ))}
      </div>
    </div>
  );
}
