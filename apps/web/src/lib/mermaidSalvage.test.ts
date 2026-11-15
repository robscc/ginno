// mermaid 兼容层（lib/mermaidSalvage.ts）单测。
//
// 零 import 纯函数，无需任何桩。用例按「实测失败集」组织：
// ① stateDiagram 虚线/长横箭头降级；② flowchart 危险字符加引号；
// ③ 保留字 end 改名；④ L2 整图迁移 flowchart。
// 同时覆盖「能渲的图一个字符都不碰」的负路径（返回 null / []）。

import { describe, expect, it } from "vitest";

import {
  fixFlowchartText,
  fixStateArrows,
  salvageMermaid,
  stateToFlowchart,
} from "@/lib/mermaidSalvage";

describe("salvageMermaid（候选编排）", () => {
  it("空输入 / 非目标图型 → 无候选", () => {
    expect(salvageMermaid("")).toEqual([]);
    expect(salvageMermaid("sequenceDiagram\nA->>B: hi")).toEqual([]);
    expect(salvageMermaid("classDiagram\nA <|-- B")).toEqual([]);
  });

  it("stateDiagram 虚线：L1 实线降级 + L2 迁移两个候选，按序排列", () => {
    const out = salvageMermaid("stateDiagram-v2\n[*] --> A\nA -.-> B");
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({
      code: "stateDiagram-v2\n[*] --> A\nA --> B",
      fixes: ["arrows"],
    });
    expect(out[1].fixes).toEqual(["flowchart"]);
    expect(out[1].code).toBe("flowchart TD\nSTART((•)) --> A\nA -.-> B"); // 虚线在 flowchart 合法，保留
  });

  it("图型头大小写不敏感，首行前后空白容忍", () => {
    const out = salvageMermaid("  StateDiagram-V2\nA -.-> B");
    expect(out.length).toBeGreaterThan(0);
    expect(out[0].fixes).toEqual(["arrows"]);
  });

  it("stateDiagram 无虚线：L1 无可修，仅 L2 迁移候选", () => {
    const out = salvageMermaid("stateDiagram\nA --> B");
    expect(out).toEqual([{ code: "flowchart TD\nA --> B", fixes: ["flowchart"] }]);
  });

  it("头行带多余 token：salvage 认得（\\b），但 L2 要求整行精确匹配 → 只有 L1", () => {
    const out = salvageMermaid("stateDiagram-v2 extra\nA -.-> B");
    expect(out).toEqual([{ code: "stateDiagram-v2 extra\nA --> B", fixes: ["arrows"] }]);
  });

  it("flowchart 危险文本 → 单候选 quoted", () => {
    const out = salvageMermaid("flowchart TD\nA[文本(注)] --> B");
    expect(out).toEqual([{ code: 'flowchart TD\nA["文本(注)"] --> B', fixes: ["quoted"] }]);
  });

  it("graph 别名同样处理；end 节点 → fixes end", () => {
    const out = salvageMermaid("graph TD\nend --> B");
    expect(out).toEqual([{ code: 'graph TD\nend_["end"] --> B', fixes: ["end"] }]);
  });

  it("干净的 flowchart → 无候选（能渲的图不碰）", () => {
    expect(salvageMermaid("flowchart TD\nA --> B")).toEqual([]);
    expect(salvageMermaid('flowchart TD\nA["已加引号 (x)"] --> B')).toEqual([]);
  });
});

describe("fixStateArrows（L1 箭头降级）", () => {
  it("非法箭头记号降级为 -->（-.-> / ..-> / ...-> / --- / ---- / -.）", () => {
    for (const arrow of ["-.->", "..->", "...->", "---", "----", "-."]) {
      expect(fixStateArrows(`A ${arrow} B`)).toBe("A --> B");
    }
  });

  it("quirk：-.- 只吃掉「横+点」留下尾横（源码正则第 4 分支是 -\\. 两字符）", () => {
    // 这类 L1 修不干净的场景正是 L2 整图迁移兜底存在的理由。
    expect(fixStateArrows("A -.- B")).toBe("A -->- B");
  });

  it("合法箭头（--> / ==> / <--> / --）一字不动 → null", () => {
    expect(fixStateArrows("stateDiagram\nA --> B\nB ==> C\nC <--> D\nE -- F")).toBeNull();
  });

  it("多行混合：只改非法行，其余逐字节保留", () => {
    expect(fixStateArrows("A -.-> B\nC --> D\nE --- F")).toBe("A --> B\nC --> D\nE --> F");
  });

  it("空输入 → null", () => {
    expect(fixStateArrows("")).toBeNull();
  });
});

describe("fixFlowchartText（L1 引号 + end 改名）", () => {
  it("方括号文本含圆括号 → 加引号", () => {
    expect(fixFlowchartText("A[hello(world)] --> B")).toEqual({
      code: 'A["hello(world)"] --> B',
      fixes: ["quoted"],
    });
  });

  it("文本含竖线 → 加引号", () => {
    expect(fixFlowchartText("A[text|pipe] --> B")).toEqual({
      code: 'A["text|pipe"] --> B',
      fixes: ["quoted"],
    });
  });

  it("圆括号形状允许嵌套一层，整体加引号", () => {
    expect(fixFlowchartText("A(文本(注)) --> B")).toEqual({
      code: 'A("文本(注)") --> B',
      fixes: ["quoted"],
    });
  });

  it("花括号形状同样处理", () => {
    expect(fixFlowchartText("A{a|b} --> B")).toEqual({
      code: 'A{"a|b"} --> B',
      fixes: ["quoted"],
    });
  });

  it("文本内裸双引号 → #quot; 转义", () => {
    expect(fixFlowchartText('A[say "hi" (x)] --> B')).toEqual({
      code: 'A["say #quot;hi#quot; (x)"] --> B',
      fixes: ["quoted"],
    });
  });

  it("已加引号 / 无危险字符 → null（不重复加工）", () => {
    expect(fixFlowchartText('A["already (x)"] --> B')).toBeNull();
    expect(fixFlowchartText("A[plain] --> B")).toBeNull();
    expect(fixFlowchartText("")).toBeNull();
  });

  it("裸 end 在箭头任一侧 → end_[\"end\"]", () => {
    expect(fixFlowchartText("end --> B")).toEqual({ code: 'end_["end"] --> B', fixes: ["end"] });
    expect(fixFlowchartText("A --> end")).toEqual({ code: 'A --> end_["end"]', fixes: ["end"] });
    expect(fixFlowchartText("end --> end")).toEqual({
      code: 'end_["end"] --> end_["end"]',
      fixes: ["end"],
    });
  });

  it("end 作箭头目标且带形状：只改 id，保留原形状与文本", () => {
    expect(fixFlowchartText("A --> end[完成]")).toEqual({ code: "A --> end_[完成]", fixes: ["end"] });
  });

  it("quirk：end 后紧跟形状括号（箭头不紧邻 end token）→ 不改名", () => {
    // end[完成] --> B：end 与箭头之间隔着形状括号，arrowBefore/arrowAfter 均不命中。
    expect(fixFlowchartText("end[完成] --> B")).toBeNull();
  });

  it("其它箭头记号（==> / -.-> / <--）旁的 end 同样改名", () => {
    expect(fixFlowchartText("A ==> end")).toEqual({ code: 'A ==> end_["end"]', fixes: ["end"] });
    expect(fixFlowchartText("A -.-> end")).toEqual({ code: 'A -.-> end_["end"]', fixes: ["end"] });
    expect(fixFlowchartText("A <-- end")).toEqual({ code: 'A <-- end_["end"]', fixes: ["end"] });
  });

  it("quirk：arrowAfter 白名单不含 <-- → end <-- A 不改名（只认右侧 end）", () => {
    expect(fixFlowchartText("end <-- A")).toBeNull();
  });

  it("整行 end / end; 是 subgraph 收口关键字 → 保留，null", () => {
    expect(fixFlowchartText("end")).toBeNull();
    expect(fixFlowchartText("end;")).toBeNull();
    expect(fixFlowchartText("subgraph S\nA --> B\nend")).toBeNull();
  });

  it("引号内 / 标签文本里的 end 是文本 → 不动", () => {
    expect(fixFlowchartText('A["the end"] --> B')).toBeNull();
    expect(fixFlowchartText("A[the end] --> B")).toBeNull();
  });

  it("quoted + end 同时命中：fixes 按命中顺序列出", () => {
    expect(fixFlowchartText("A --> end[foo(x)]")).toEqual({
      code: 'A --> end_["foo(x)"]',
      fixes: ["quoted", "end"],
    });
  });
});

describe("stateToFlowchart（L2 整图迁移）", () => {
  it("无 stateDiagram 头 → null", () => {
    expect(stateToFlowchart("flowchart TD\nA --> B")).toBeNull();
    expect(stateToFlowchart("")).toBeNull();
  });

  it("[*] 起止态 → START/DONE 双圆节点", () => {
    expect(stateToFlowchart("stateDiagram\n[*] --> A\nA --> [*]")).toBe(
      "flowchart TD\nSTART((•)) --> A\nA --> DONE((•))",
    );
  });

  it("direction：LR 保留、TB 归一为 TD、大小写不敏感", () => {
    expect(stateToFlowchart("stateDiagram-v2\ndirection LR\nA --> B")).toBe("flowchart LR\nA --> B");
    expect(stateToFlowchart("stateDiagram\ndirection TB\nA --> B")).toBe("flowchart TD\nA --> B");
    expect(stateToFlowchart("stateDiagram\ndirection lr\nA --> B")).toBe("flowchart LR\nA --> B");
  });

  it("迁移标签 → |label| 语法；标签内竖线转全角", () => {
    expect(stateToFlowchart("stateDiagram\nA --> B : go")).toBe("flowchart TD\nA -->|go| B");
    expect(stateToFlowchart("stateDiagram\nA --> B : a|b")).toBe("flowchart TD\nA -->|a｜b| B");
  });

  it("虚线/双向/粗箭头在 flowchart 合法 → 原样保留", () => {
    expect(stateToFlowchart("stateDiagram\nA -.-> B")).toBe("flowchart TD\nA -.-> B");
    expect(stateToFlowchart("stateDiagram\nA <--> B")).toBe("flowchart TD\nA <--> B");
    expect(stateToFlowchart("stateDiagram\nA ==> B")).toBe("flowchart TD\nA ==> B");
  });

  it("复合状态 → subgraph/end，支持嵌套", () => {
    expect(stateToFlowchart("stateDiagram\nstate X {\nA --> B\n}")).toBe(
      "flowchart TD\nsubgraph X\nA --> B\nend",
    );
    expect(
      stateToFlowchart("stateDiagram\nstate Outer {\nstate Inner {\nA --> B\n}\n}"),
    ).toBe("flowchart TD\nsubgraph Outer\nsubgraph Inner\nA --> B\nend\nend");
  });

  it("深度 0 的游离 } → 丢弃（不产生多余 end）", () => {
    expect(stateToFlowchart("stateDiagram\n}\nA --> B")).toBe("flowchart TD\nA --> B");
  });

  it('state "长名" as X → 内联声明；后续引用只用 id', () => {
    expect(stateToFlowchart('stateDiagram\nstate "长 名字" as X\nX --> Y')).toBe(
      "flowchart TD\nX[长 名字]\nX --> Y",
    );
  });

  it("state X 裸声明 → 普通节点", () => {
    expect(stateToFlowchart("stateDiagram\nstate X")).toBe("flowchart TD\nX");
  });

  it("note 行丢弃（flowchart 无对应物）", () => {
    expect(stateToFlowchart("stateDiagram\nnote right of A : hi\nA --> B")).toBe(
      "flowchart TD\nA --> B",
    );
  });

  it("X : 描述 → 内联标签；描述内双引号转 #quot;", () => {
    expect(stateToFlowchart("stateDiagram\nA : 描述")).toBe("flowchart TD\nA[描述]");
    expect(stateToFlowchart('stateDiagram\nA : say "hi"')).toBe("flowchart TD\nA[say #quot;hi#quot;]");
  });

  it("含空格 id：首次引用内联声明，后续引用裸 id", () => {
    expect(stateToFlowchart("stateDiagram\nA B --> C D\nA B --> E")).toBe(
      "flowchart TD\nA_B[A B] --> C_D[C D]\nA_B --> E",
    );
  });

  it("id 内特殊字符被清洗，双引号转义", () => {
    expect(stateToFlowchart('stateDiagram\nA"B --> C')).toBe("flowchart TD\nAB[A#quot;B] --> C");
  });

  it("全符号 id 清洗为空 → 兜底 N", () => {
    expect(stateToFlowchart("stateDiagram\n??? --> B")).toBe("flowchart TD\nN[???] --> B");
  });

  it("空行与 %% 注释逐字保留；不认识的行原样透传", () => {
    expect(stateToFlowchart("stateDiagram\n\n%% comment\nA --> B")).toBe(
      "flowchart TD\n\n%% comment\nA --> B",
    );
    expect(stateToFlowchart("stateDiagram\nclick A callback")).toBe(
      "flowchart TD\nclick A callback",
    );
  });
});
