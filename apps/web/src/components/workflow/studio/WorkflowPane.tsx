"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Check, FlaskConical, Loader2, ShieldAlert, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { ContextEditor } from "../ContextEditor";

/** Right pane of the 设计 view when no node is selected: what the recipe is,
 *  what the linter says, and what it will run with. */
export function WorkflowPane({
  wf,
  ctxOverride,
  onCtxChange,
  unfilled,
}: {
  wf: WorkflowDef;
  ctxOverride: Record<string, unknown>;
  onCtxChange: (ctx: Record<string, unknown>) => void;
  unfilled: string[];
}) {
  const [doctor, setDoctor] = useState<{
    errors: Array<{ rule: string; node_id?: string; message: string }>;
    warnings: Array<{ rule: string; node_id?: string; message: string }>;
  } | null>(null);
  const [doctorOpen, setDoctorOpen] = useState(false);
  const [dry, setDry] = useState<{ busy: boolean; result: api.DryRunResult | null }>({
    busy: false,
    result: null,
  });

  useEffect(() => {
    let alive = true;
    setDoctor(null);
    setDoctorOpen(false);
    api
      .doctorWorkflow(wf.id)
      .then((r) => {
        if (alive && r.ok) setDoctor({ errors: r.errors || [], warnings: r.warnings || [] });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [wf.id, wf.version]);

  // A new version invalidates any receipt vouching for the old DSL.
  useEffect(() => {
    setDry((d) => (d.result ? { busy: false, result: null } : d));
  }, [wf.version]);

  const runDry = async () => {
    if (!wf.dsl) return;
    setDry({ busy: true, result: null });
    try {
      setDry({ busy: false, result: await api.dryRunWorkflow(wf.dsl) });
    } catch {
      setDry({ busy: false, result: null });
    }
  };

  const issues = (doctor?.errors.length || 0) + (doctor?.warnings.length || 0);

  return (
    <div className="space-y-3">
      <div>
        <div className="flex items-center gap-1.5">
          <span className="text-[12.5px] font-semibold text-txt">{wf.name}</span>
          <span className="rounded border border-line2 px-1 font-mono text-[10px] text-faint">
            v{wf.version ?? 1}
          </span>
          {wf.system && (
            <span
              className="rounded px-1 py-px text-[9.5px]"
              style={{ color: "#8b5cf6", background: "#8b5cf61a" }}
            >
              内置
            </span>
          )}
        </div>
        <div className="mt-0.5 font-mono text-[10px] text-faint">{wf.id}</div>
        {wf.description && <p className="mt-1.5 text-[11.5px] text-muted">{wf.description}</p>}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {doctor && issues > 0 && (
          <button
            onClick={() => setDoctorOpen((o) => !o)}
            className={`btn-press flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${
              doctor.errors.length
                ? "border-red/40 text-red hover:bg-red/10"
                : "border-yellow/40 text-yellow hover:bg-yellow/10"
            }`}
          >
            <ShieldAlert className="h-3 w-3" />
            数据流检查 {doctor.errors.length || doctor.warnings.length}
          </button>
        )}
        {doctor && issues === 0 && (
          <span className="flex items-center gap-1 rounded border border-green/30 px-1.5 py-0.5 text-[10px] text-green">
            <Check className="h-3 w-3" /> 数据流检查通过
          </span>
        )}
        {wf.dsl && (
          <button
            onClick={() => void runDry()}
            disabled={dry.busy}
            title="零成本试跑：不保存、不执行、不调 LLM"
            className="btn-press flex items-center gap-1 rounded border border-line2 px-1.5 py-0.5 text-[10px] text-muted hover:text-txt disabled:opacity-50"
          >
            {dry.busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
            试运行
          </button>
        )}
      </div>

      {doctorOpen && doctor && issues > 0 && (
        <div className="space-y-1 rounded-lg border border-line bg-base/30 p-2.5">
          {doctor.errors.map((e, i) => (
            <div key={`e${i}`} className="flex items-start gap-1.5 text-[11px]">
              <X className="mt-0.5 h-3 w-3 shrink-0 text-red" />
              <span className="text-red">{e.message}</span>
            </div>
          ))}
          {doctor.warnings.map((w, i) => (
            <div key={`w${i}`} className="flex items-start gap-1.5 text-[11px]">
              <ShieldAlert className="mt-0.5 h-3 w-3 shrink-0 text-yellow" />
              <span className="text-yellow">{w.message}</span>
            </div>
          ))}
        </div>
      )}

      {dry.result && (
        <div
          className={`space-y-0.5 rounded-lg border px-2.5 py-2 text-[11px] ${
            dry.result.ok
              ? "border-green/30 bg-green/[0.06] text-green"
              : "border-red/30 bg-red/[0.06] text-red"
          }`}
        >
          {dry.result.ok ? (
            <>
              <div className="flex items-center gap-1.5">
                <Check className="h-3.5 w-3.5" />
                <span>
                  试运行通过：{dry.result.node_count} 个节点，校验 / 数据流 / 编译 / 可达性全过
                </span>
                <button
                  onClick={() => setDry({ busy: false, result: null })}
                  className="ml-auto text-[10px] opacity-70 hover:opacity-100"
                >
                  收起 ▴
                </button>
              </div>
              {dry.result.unreachable.length > 0 && (
                <div className="pl-5 text-yellow">
                  不可达节点：{dry.result.unreachable.join(", ")}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="flex items-center gap-1.5">
                <X className="h-3.5 w-3.5" />
                <span>试运行未通过：</span>
                <button
                  onClick={() => setDry({ busy: false, result: null })}
                  className="ml-auto text-[10px] opacity-70 hover:opacity-100"
                >
                  收起 ▴
                </button>
              </div>
              {[...dry.result.errors, ...dry.result.doctor_errors.map((d) => d.message)]
                .slice(0, 5)
                .map((m, i) => (
                  <div key={i} className="pl-5">
                    · {m}
                  </div>
                ))}
            </>
          )}
        </div>
      )}

      {unfilled.length > 0 && (
        <div className="flex items-start gap-1.5 rounded-md border border-yellow/40 bg-yellow/[0.06] px-2 py-1.5 text-[11px] text-yellow">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>
            运行前需要填：{unfilled.join(", ")}（仍可直接运行，空值也是合法输入）
          </span>
        </div>
      )}

      <div>
        <div className="mb-1.5 text-[11.5px] font-semibold text-txt">运行参数</div>
        <ContextEditor dsl={wf.dsl as never} onChange={onCtxChange} />
      </div>

      <div className="rounded-lg border border-dashed border-line2 px-2.5 py-2 text-[10.5px] text-faint">
        点画布上的节点可编辑它的参数（改一次 = 一个新版本）。结构改动走「开发会话」或「从会话导入」。
      </div>
    </div>
  );
}