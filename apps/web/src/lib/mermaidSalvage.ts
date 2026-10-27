/**
 * mermaid 兼容层（mermaid-compat-plan.md）：只处理「模型爱写、mermaid
 * 11.x 真机实测 parse 失败」的构造。全部是纯文本变换，输入永不修改。
 *
 * 实测失败集（mermaid 11.17.2，Playwright + 官方 dist 逐一验证）：
 *  ① stateDiagram 不支持虚线/长横线箭头（-.-> / -.- / --- / ..->，
 *     flowchart 专属）→ L1 降级为实线；
 *  ② flowchart 节点文本含未加引号的 ( ) 或 | → L1 加引号；
 *  ③ flowchart 节点 id 为保留字 end → L1 改名 end_["end"]；
 *  ④ stateDiagram L1 修不动 → L2 整图迁移为 flowchart（虚线在那边合法，
 *     保真度反而高于 L1 的实线降级）。
 * 其余图型（sequence/class/er/gantt/mindmap 及全部已测写法）均 parse
 * 通过，不做任何变换——能渲的图一个字符都不碰。
 */

/** 命中的修复种类（徽标 tooltip 逐条展示）。 */
export type SalvageFix = "arrows" | "quoted" | "end" | "flowchart";

/** 按尝试顺序排列的候选修复；空数组 = 没有可修的构造。 */
export function salvageMermaid(source: string): Array<{ code: string; fixes: SalvageFix[] }> {
  const head = (source.split("\n")[0] || "").trim();
  const out: Array<{ code: string; fixes: SalvageFix[] }> = [];
  if (/^stateDiagram(-v2)?\b/i.test(head)) {
    const l1 = fixStateArrows(source);
    if (l1) out.push({ code: l1, fixes: ["arrows"] });
    const l2 = stateToFlowchart(source);
    if (l2 && (!l1 || l2 !== l1)) out.push({ code: l2, fixes: ["flowchart"] });
    return out;
  }
  if (/^(flowchart|graph)\b/i.test(head)) {
    const l1 = fixFlowchartText(source);
    if (l1) out.push({ code: l1.code, fixes: l1.fixes });
    return out;
  }
  return out;
}

// ---------------------------------------------------------------- L1: state

/** ① stateDiagram 的虚线/长横线箭头 → 实线 -->。箭头记号之外一字不动
 *  （状态 id 不含点；`--`、`<-->`、`==>` 是合法的，不受影响）。 */
export function fixStateArrows(source: string): string | null {
  let changed = false;
  const fixed = source.split("\n").map((line) => {
    const next = line.replace(/(-\.->|\.{2,}->|-\.->|-\.|-{3,})/g, "-->");
    if (next !== line) changed = true;
    return next;
  });
  return changed ? fixed.join("\n") : null;
}

// ---------------------------------------------------------------- L1: flowchart

/** 节点文本里需要引号的字符。 */
const FLOW_DANGER = /[()|]/;

/** 形状括号内容：允许圆括号嵌套一层（A(文本(注)) 的场景）。 */
const SHAPE_RE =
  /([\w一-鿿.-]+)(\[[^\]\n]*\]|\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)|\{[^\}\n]*\})/g;

/** ②③ flowchart：给含危险字符的节点文本加双引号；裸 end 节点改名。
 *  只在「未被引号包裹」时动手；引号内的 " 转成 #quot;（mermaid 的引号
 *  字符串里不能出现裸双引号）。返回 null = 没有可修的。 */
export function fixFlowchartText(source: string): { code: string; fixes: SalvageFix[] } | null {
  const fixes = new Set<SalvageFix>();
  const lines = source.split("\n").map((line) => {
    // ② 形状括号：id[...] / id(...) / id{...}，内部未以引号开头且含 ( ) |
    let next = line.replace(SHAPE_RE, (whole, id: string, shape: string) => {
      const open = shape[0];
      const inner = shape.slice(1, -1);
      if (!FLOW_DANGER.test(inner)) return whole;
      if (inner.startsWith('"')) return whole; // 已加引号
      fixes.add("quoted");
      const esc = inner.replace(/"/g, "#quot;");
      return `${id}${open}"${esc}"${shape.slice(-1)}`;
    });
    // ③ 保留字 end 作节点：整行仅 end/end; 是 subgraph 收口关键字，保留；
    //    箭头任一侧或带形状括号的 end 才改名（标签文本里的 end 不动）。
    const t = next.trim();
    if (t === "end" || t === "end;") return next;
    next = next.replace(/\bend\b/g, (m, off: number) => {
      // 引号内的 end 是文本（上一步已把危险文本包进引号）
      if ((next.slice(0, off).match(/"/g) || []).length % 2 === 1) return m;
      const before = next.slice(0, off).trimEnd();
      const after = next.slice(off + 3).trimStart();
      const arrowBefore = /(-{2,3}>|={2,}>|-\.->|<->|<--)$/.test(before);
      const arrowAfter = /^(-{2,3}>|={2,}>|-\.->|<->|-->)/.test(after);
      if (!(arrowBefore || arrowAfter)) return m;
      fixes.add("end");
      // 带形状的（end[完成]）只改 id 保留原形状与文本；裸 end 补显示文本
      return /^[\[\(\{]/.test(after) ? "end_" : 'end_["end"]';
    });
    return next;
  });
  return fixes.size ? { code: lines.join("\n"), fixes: [...fixes] } : null;
}

// ---------------------------------------------------------------- L2: state → flowchart

/** stateDiagram → flowchart 迁移。虚线在 flowchart 合法，因此保真度高于
 *  L1 的实线降级——只在 L1 修不动时作为兜底。
 *  映射：[*]→START/DONE 双圆；A --> B : label → A -->|label| B；
 *  state X {…} → subgraph X … end；state "d" as X → X["d"]；
 *  X : 描述 → X["描述"]（首次引用内联声明）；note 行丢弃；方向保留。 */
export function stateToFlowchart(source: string): string | null {
  const lines = source.split("\n");
  const out: string[] = [];
  const declared = new Set<string>(); // 已内联声明过展示文本的 id
  let dir = "TD";
  let sawState = false;
  let depth = 0;

  const safeId = (raw: string): string => {
    const id = raw.trim().replace(/\s+/g, "_").replace(/[^\w一-鿿.-]/g, "");
    return id || "N";
  };
  /** 首次引用时内联声明展示文本（原名含空格/特殊字符时必须）。 */
  const ref = (raw: string): string => {
    const id = safeId(raw);
    if (declared.has(id) || id === raw.trim()) return id;
    declared.add(id);
    return `${id}[${raw.trim().replace(/"/g, "#quot;")}]`;
  };

  for (const raw of lines) {
    const line = raw.trim();
    if (/^stateDiagram(-v2)?$/i.test(line)) {
      sawState = true;
      continue;
    }
    const dm = line.match(/^direction\s+(TB|TD|BT|LR|RL)$/i);
    if (dm) {
      dir = dm[1].toUpperCase() === "TB" ? "TD" : dm[1].toUpperCase();
      continue;
    }
    if (!line || line.startsWith("%%")) {
      out.push(raw);
      continue;
    }
    // 复合状态：state X { → subgraph X，收口 } → end
    const open = line.match(/^state\s+(.+?)\s*\{$/);
    if (open) {
      depth += 1;
      out.push(`subgraph ${safeId(open[1])}`);
      continue;
    }
    if (line === "}") {
      if (depth > 0) {
        depth -= 1;
        out.push("end");
      }
      continue;
    }
    // state X <<fork/join/choice>> → 纯节点；state "长名" as X → 声明
    const st = line.match(/^state\s+"([^"]+)"\s+as\s+(\S+)$/);
    if (st) {
      const id = safeId(st[2]);
      declared.add(id);
      out.push(`${id}[${st[1].replace(/"/g, "#quot;")}]`);
      continue;
    }
    const stMod = line.match(/^state\s+(\S+)$/);
    if (stMod) {
      out.push(ref(stMod[1]));
      continue;
    }
    if (/^note\b/i.test(line)) continue; // flowchart 无对应，丢弃

    // 迁移：X <arrow> Y : label / X <arrow> Y / X : 描述
    const tr = line.match(/^(.+?)\s*(-\.->|-\.->|-\.|-->|<-->|<--|--|==>|---|\.{2,}->)\s*(.+)$/);
    if (tr) {
      let lhs = tr[1].trim();
      const arrow = tr[2];
      let rhs = tr[3].trim();
      let label = "";
      const lb = rhs.match(/^(.*?)\s*:\s*(.+)$/);
      if (lb) {
        rhs = lb[1].trim();
        label = lb[2].trim();
      }
      if (lhs === "[*]") lhs = `START((•))`;
      else lhs = ref(lhs);
      if (rhs === "[*]") rhs = `DONE((•))`;
      else rhs = ref(rhs);
      out.push(label ? `${lhs} ${arrow}|${label.replace(/\|/g, "｜")}| ${rhs}` : `${lhs} ${arrow} ${rhs}`);
      continue;
    }
    const desc = line.match(/^(\S+)\s*:\s*(.+)$/);
    if (desc) {
      const id = safeId(desc[1]);
      declared.add(id);
      out.push(`${id}[${desc[2].replace(/"/g, "#quot;")}]`);
      continue;
    }
    out.push(raw); // 不认识的行原样透传，交给 parse 裁决
  }
  if (!sawState) return null;
  return [`flowchart ${dir}`, ...out].join("\n");
}
