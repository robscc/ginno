"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

interface Skill {
  name: string;
  description: string;
  trigger: string;
  tools: string[];
}

export function SkillsSettings() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => {
    api.listSkills().then(setSkills).catch(() => {});
  };
  useEffect(() => {
    load();
  }, []);

  async function create() {
    const r = await api.createSkill({ name: name.trim(), body });
    setMsg(r.ok ? "created" : "error: " + (r.error || ""));
    if (r.ok) {
      setName("");
      setBody("");
      load();
    }
  }
  async function del(n: string) {
    await api.deleteSkill(n);
    load();
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Skills</h2>
      <p className="mt-1 text-sm text-muted">一次性指令模板（/&lt;name&gt; 触发）。存于 ~/.ginno/skills/。</p>
      <div className="mt-4 space-y-2">
        {skills.map((s) => (
          <div key={s.name} className="rounded-xl border border-line bg-card p-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-violet">/{s.name}</span>
              <span className="pill border border-line2 text-muted">{s.trigger}</span>
              <button onClick={() => del(s.name)} className="ml-auto text-xs text-faint hover:text-red">
                delete
              </button>
            </div>
            <div className="mt-1 text-xs text-muted">{s.description}</div>
            {s.tools.length > 0 && (
              <div className="mt-1 text-[11px] text-faint">tools: {s.tools.join(", ")}</div>
            )}
          </div>
        ))}
        {skills.length === 0 && <div className="text-xs text-faint">No skills yet.</div>}
      </div>
      <div className="mt-5 rounded-xl border border-line bg-card p-3">
        <div className="mb-2 text-sm font-medium text-txt">New skill</div>
        <input
          className="field mb-2"
          placeholder="name (kebab-case)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <textarea
          className="field mb-2 font-mono text-xs"
          rows={6}
          placeholder={"---\nname: ...\ndescription: ...\ntrigger: user-invocable\n---\n\n# instructions"}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <button onClick={create} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
            Create
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
