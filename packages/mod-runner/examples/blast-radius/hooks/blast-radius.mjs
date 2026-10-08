// Blast Radius — Ginno P1 acceptance mod.
// Guards dangerous shell commands with three layers, mirroring the official
// Blast Radius scenario (tool.call interception + band button + $.ui.ask):
//   1. HIGH-RISK commands (rm -rf /, fork bombs, curl|sh, raw disk writes) → denied outright.
//   2. MEDIUM-RISK commands (package installs, force pushes, recursive rm) →
//      strict mode: denied; normal mode: $.ui.ask the user.
//   3. Band: live counters + a Button toggling strict mode (press → state → redraw).

const STATE = { plugin: "blast-radius", key: "core" };

const HIGH = [
  /rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*\/(\s|$)/,   // rm -rf /
  /rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*~/,          // rm -rf ~
  /:\(\)\s*\{.*\};\s*:/,                          // fork bomb
  /curl[^|]*\|\s*(ba)?sh/,                        // curl | sh
  /wget[^|]*\|\s*(ba)?sh/,
  /mkfs\./,                                       // filesystem format
  /dd\s+if=.*of=\/dev\//,                         // raw disk write
];

const MEDIUM = [
  /\b(brew|apt|apt-get|npm|pnpm|yarn|pip3?|uv|cargo|gem)\s+(install|add|addGroup)\b/,
  /git\s+push\s+(--force|-f)/,
  /rm\s+-[a-zA-Z]*r[a-zA-Z]*/,                    // recursive rm on some path
  /chmod\s+-R\s+777/,
  />\s*\/etc\//,                                  // writing into /etc
];

const DEFAULTS = { strict: false, denied: 0, allowed: 0 };

async function loadCore($) {
  const { value } = await $.state.get(STATE);
  return { ...DEFAULTS, ...(value ?? {}) };
}

function classify(command) {
  const s = String(command ?? "");
  if (HIGH.some((re) => re.test(s))) return "high";
  if (MEDIUM.some((re) => re.test(s))) return "medium";
  return "ok";
}

export function register(on) {
  // Guard Bash tool calls. Pass-through is `next(e)`; deny/ask per contract.
  on("tool.call", { tool: "Bash" }, async ($, e, next) => {
    const command = e?.payload?.args?.command ?? e?.payload?.input?.command ?? "";
    const level = classify(command);
    if (level === "ok") return next(e);
    const core = await loadCore($);

    if (level === "high") {
      await $.state.set(STATE, { ...core, denied: core.denied + 1 });
      await $.ui.toast(`Blast Radius: 高危命令已拦截 — ${String(command).slice(0, 80)}`);
      return { deny: "Blast Radius: high-risk command denied outright" };
    }

    // medium risk
    if (core.strict) {
      await $.state.set(STATE, { ...core, denied: core.denied + 1 });
      await $.ui.toast(`Blast Radius: strict 模式拦截 — ${String(command).slice(0, 80)}`);
      return { deny: "Blast Radius: denied by strict mode" };
    }

    const answer = await $.ui.ask(`Blast Radius：允许执行？\n${String(command).slice(0, 120)}`, {
      choices: ["allow", "deny"],
    });
    if (String(answer) === "allow") {
      await $.state.set(STATE, { ...core, allowed: core.allowed + 1 });
      return next(e);
    }
    await $.state.set(STATE, { ...core, denied: core.denied + 1 });
    return { deny: "Blast Radius: denied by user" };
  });

  // Band: mode + counters + the strict toggle.
  on("ui.render", { component: "AbovePrompt" }, async ($, e, next) => {
    if (e.props?.hasSurvey) return next(e);
    const core = await loadCore($);
    const { Box, Text, Button } = $.ui.resolve(e);
    const mode = core.strict ? "STRICT" : "normal";
    return Box({
      flexDirection: "row",
      paddingX: 1,
      gap: 1,
      children: [
        Text({ color: core.strict ? "red" : "green", bold: true, children: `◍ Blast Radius[${mode}]` }),
        Text({ dimColor: true, children: `denied ${core.denied} · allowed ${core.allowed}` }),
        Button({
          label: core.strict ? "loosen" : "tighten",
          onPress: async () => {
            const cur = await loadCore($);
            await $.state.set(STATE, { ...cur, strict: !cur.strict });
            await $.ui.toast(`Blast Radius: strict = ${!cur.strict}`);
          },
        }),
      ],
    });
  });
}
