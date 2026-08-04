---
title: "Ginno 多类型文件解析与分析方案研究（CSV / Excel / Word / PPT / PDF）"
date: 2026-08-02T00:00:00.000Z
topic: "多类型文件解析与分析方案"
tags: [research, file-parsing, knowledge-base, rag, ginno]
sources: 32
confidence: high
status: completed
aliases: [文件解析方案, office 文件分析, document parsing]
---

# Ginno 多类型文件解析与分析方案研究

> [!abstract] 摘要
> Ginno 知识库目前只摄入 Markdown（`indexer.py` 中 `INDEX_EXTENSIONS = {".md", ".markdown"}`），`read_file` 工具也只能读 UTF-8 文本。要支持 CSV/Excel/Word/PPT/PDF，需要区分**两类本质不同的需求**：①「内容提取 → 入库检索」（适合 Word/PPT/PDF，转 Markdown 后喂给现有 indexer）；②「数据分析」（适合 CSV/Excel，文本抽取无法回答"求平均值"，需要 LLM 生成 pandas 代码在沙箱执行）。
>
> **核心结论**：采用**分层混合架构**——以 **MarkItDown**（微软，MIT，模块化 extras）作统一「任意格式 → Markdown」转换器驱动 KB 入库；以 **python-calamine / python-docx / python-pptx / PyMuPDF** 等专用库支撑按需工具的高保真/高性能场景;另建一个**沙箱化 pandas 代码执行工具**专门做表格数据分析。所有重依赖放在可选 extra 后、懒加载，以契合 Ginno「PyInstaller 打包 + 无数据库 + 默认零 embedding」的约束。

---

## 1. 背景：Ginno 现状与需求拆解

### 1.1 现状（来自代码库勘察）

| 维度 | 现状 |
|---|---|
| 架构 | Tauri 壳 + Next.js UI + **Python sidecar**（FastAPI + LangGraph），PyInstaller `--onedir` 打包 |
| 知识库 | `knowledge/` 包：`indexer` 扫描 Obsidian vault → 内存索引（无 DB）→ `retriever` 词法多信号打分 + TF-IDF → `injection` 注入 system prompt |
| 索引范围 | **仅 `.md/.markdown`**（`indexer.py:19`），跳过 `.obsidian/.git/node_modules` 等 |
| 文件工具 | `tools/builtin.py` 的 `read_file` 只读 UTF-8 文本；`grep/glob/bash` |
| 多模态 | 图片走 base64 进 `HumanMessage`（vision），有 `IMAGE_KEEP_TURNS` 控制上下文膨胀 |
| 依赖现状 | runtime venv 共 **124 个包，无任何文档解析库**（pandas/openpyxl/docx/pptx/pymupdf 均无）；`rag` extra（lancedb/sentence-transformers）默认不装 |
| 既有参照 | 用户全局已有一个 `office-file` skill，基于 **Node 的 `officeparser`** CLI，支持 xlsx/docx/pptx/pdf/odt 的 text/json/metadata 三档输出 + OCR |

### 1.2 「分析文件」其实是两个问题

把需求混为一谈是这类方案最常见的坑。两种场景的目标、产物、技术栈都不同：

| | **A. 内容提取 / 入库（Extract & Index）** | **B. 数据分析（Analyze）** |
|---|---|---|
| 典型格式 | Word、PPT、PDF（叙述性文档） | CSV、Excel（结构化表格） |
| 用户意图 | "这份合同/报告讲了什么" "搜到并注入相关知识" | "这个表的销售总额/环比/Top10 是什么" |
| 产物 | Markdown 文本 + 元数据 → 进 KB 检索/注入 | 计算结果（数字/子表/图）+ 解释 |
| 核心技术 | 格式解析 + 文档→Markdown 转换 | LLM 生成 pandas/SQL 代码 + 沙箱执行 |
| 关键挑战 | 表格/图片/版面保真、OCR | token 预算（万行表不能全塞进 prompt）、代码安全 |

> **判断**：Word/PPT/PDF 走 A 路线（转 Markdown 入库）；CSV/Excel **以 B 路线为主**（代码执行分析），A 路线为辅（小表可直接转 Markdown 表格入库）。两者共享底层解析库，但上层管线分开。

---

## 2. 统一文档→Markdown 框架对比

这是 A 路线的核心选型：是否用一个统一框架把 N 种格式都转成 Markdown。

### 2.1 主流框架横评

| 框架 | 开发方 | 安装体积 | 依赖数 | 定位 | 许可 |
|---|---|---|---|---|---|
| **MarkItDown** | 微软 | **~251 MB** | 25 | 速度快、简单易用，覆盖 12+ 格式 | MIT |
| **Docling** | IBM | **~1032 MB** | 88 | 复杂版面/表格精度最高，ML 驱动 | MIT/Apache-2.0 |
| **Unstructured** | Unstructured.io | ~146 MB | 54 | 通用抽取管线，分区(partition) | Apache-2.0 |
| **Apache Tika** | Apache | JVM | — | 1000+ 格式，企业级，REST server | Apache-2.0 |
| **Pandoc** | 社区 | 单二进制 | — | 结构化格式互转，弱于二进制格式 | GPL |
| **officeparser** | 社区(Node) | npm | — | 轻量，已有 skill 在用 | MIT |

数据来源：一份对 4 个 Python 库的实测基准（安装体积/依赖数）[2][3]，以及多篇横评[4][6][32]。

### 2.2 各框架要点

**MarkItDown（微软）**[7][13][15]
- 支持格式：Word(.docx)、PowerPoint(.pptx)、Excel(.xlsx/.xls)、PDF、**CSV/JSON/XML**、HTML、Outlook(.msg)、图片(JPG/PNG，EXIF + OCR)、音频(转写)、IPYNB、ZIP。
- **模块化 extras**：`pip install markitdown[docx]`、`[pptx]`、`[xlsx]`、`[xls]`、`[outlook]` 等，按需安装——**这对 PyInstaller 打包体积非常友好**。
- 内部实现：DOCX 用 **mammoth**、PDF 用 pdfminer 系、XLSX 用 **openpyxl**[13][14][17]。
- **LLM 挂钩**：构造函数可传 `llm_client`/`llm_model`，对图片生成描述；`markitdown-ocr` 插件用 LLM Vision 提取 PDF/DOCX/PPTX/XLSX 内嵌图片文字[16]——**与 Ginno 既有 vision 能力天然契合**。
- 已知短板：复杂文件质量一般[3]；XLSX 首行表头处理、空行空列不裁剪、合并单元格/NaN 等边角问题[17]。

**Docling（IBM）**[1][4][30]
- ML 驱动：版面分析、表格结构检测、公式、阅读顺序、OCR；输出 Markdown + 结构化 JSON；有 LangChain/LlamaIndex 集成。
- 代价：~1GB 安装、需下载模型、**复杂文档单文件可耗时 60+ 分钟**[3]——桌面单机场景偏重。

**Unstructured**[6] —— 通用 partition 管线，体积小但 ML 能力弱于 Docling，更适合服务端批量。

**Apache Tika**[31] —— 1000+ 格式、`tika-server` REST，企业级；但需 **JVM**，打进 Tauri/PyInstaller 桌面包不现实。

**Pandoc** —— 单二进制、零运行时，适合 docx↔md 等结构化互转；**不擅长 PDF/表格等二进制抽取**[31][32]。

**officeparser（Node）** —— 用户已有 skill 在用，轻量、三档输出 + OCR；但 Ginno sidecar 是 Python，从 Python 调 Node CLI 会给打包引入 Node 运行时依赖，**不如 Python 原生干净**。可作为「不写 Python 解析时的兜底参照」。

### 2.3 选型结论

> **默认选 MarkItDown** 作为统一转换器，理由：① MIT + 模块化 extras 契合打包约束；② 覆盖全部目标格式（含 CSV/JSON/XML）；③ LLM-vision 挂钩复用 Ginno 现有能力;④ 轻量（相对 Docling 的 1GB）。**精度敏感场景**（复杂表格/扫描 PDF）保留 Docling/PyMuPDF 作为可选升级，放 extra 后默认不装。

---

## 3. 各格式专用解析库（B 路线 + 高保真兜底）

统一框架便于入库，但按需工具需要更快的读取和更细的结构控制，这时用专用库。

### 3.1 Excel / 表格读取

| 库 | 定位 | 关键数据 |
|---|---|---|
| **python-calamine** | 纯 Rust，**读取最快** | 读 50 万行 ~**3.58s** vs pandas 默认(openpyxl) ~**32.98s**[8]；官方称比 openpyxl 快 9.43×[10] |
| pandas | 数据分析入口 | **2.2+ 支持 `engine="calamine"`**[8]；`read_csv/read_excel` 一步到 DataFrame |
| openpyxl | 功能最全（读+写+样式+公式） | 最慢，但 calamine 只读、写/样式仍需它[9][11] |

> **要点**：calamine 是**只读**引擎，且与 openpyxl 在单元格类型上有行为差异[11]。分析场景用 `pandas.read_excel(engine="calamine")`；需要写回/读样式时用 openpyxl。

### 3.2 Word（.docx）

| 库 | 定位 |
|---|---|
| **mammoth** | docx → 语义 HTML/Markdown 一行搞定；利用标题/列表样式而非原始格式[12]。MarkItDown 内部即用它[13] |
| **python-docx** | 低层读写；需保真表格（合并单元格/样式）、抽取图片时手动构建[12] |

> 常见模式：**mammoth/MarkItDown 做批量转换，python-docx 做边角表格/图片后处理**[12]。mammoth 的 Markdown 图片定位、表格样式偶有问题，HTML 输出更稳[14]。

### 3.3 PowerPoint（.pptx）

- **python-pptx** 是事实标准：逐 slide 遍历 shape，抽文本框、表格、**备注(notes)**[18][19]。
- 结构化封装可参考 `pptx-parser`、`PPTXExtractor`（基于 python-pptx 的现成工具类）[19]。
- 商业备选：Aspose/Spire（含 notes、更强但要付费）。

### 3.4 PDF

| 库 | 速度 | 表格 | 适用 |
|---|---|---|---|
| **PyMuPDF (fitz)** | **最快**（C 引擎，比纯 Python 快 10–50×）[20][21] | 内置表格方法，复杂表需调参 | RAG/批量、速度+精度均衡 |
| **pdfplumber** | 最慢（坐标级版面分析）[20] | **表格抽取最佳**[20] | 复杂版面/表格密集 |
| **pypdf** | 轻、无 C 依赖 | 弱 | 简单文本、轻量任务 |

> 学术对比研究：PyMuPDF 与 pypdfium 在多种文档上文本抽取整体领先[22]。**Ginno 默认用 PyMuPDF**（快），表格密集时回落 pdfplumber。

### 3.5 CSV / JSON / XML
- Python 标准库 `csv`/`json` 即可；分析场景直接 `pandas.read_csv`。
- MarkItDown 也能把 CSV/JSON/XML 转 Markdown 表格用于入库（小表）。

---

## 4. 表格/结构化数据的 RAG 与分析策略

### 4.1 表格如何进 RAG（A 路线的表格处理）

| 策略 | 说明 | 来源 |
|---|---|---|
| **统一转 Markdown 表格** | 把表格规范化为 Markdown，提升 embedding 质量与 LLM 理解 | [25] |
| **每个 chunk 保留表头行** | 切分大表时每片都带列名，避免脱离上下文 | [24] |
| **表级检索** | 整表作为一个检索单元，支持"给我费用表""对比表1和表2" | [23] |
| **结构感知分块** | 用格式线索（Markdown 标题/HTML 标签）切，尊重文档结构 | [26] |

> **对 Ginno 的落地**：现有 indexer 以「整文件 = 一个 WikiEntry」做 summary 级检索，没有 chunk。短期可保持「整表/整文档转 Markdown 作为一页」；若表很大，按「表头 + 行组分块」并各自带 frontmatter（source 指回原文件）。

### 4.2 数据分析（B 路线）——代码生成模式

业界共识是 **「LLM 写 pandas/SQL 代码 → 沙箱执行 → 观察结果 → 迭代」** 的 agent 循环，而非把整表塞进 prompt[27][28][29]：

| 模式 | 说明 |
|---|---|
| **代码生成** | LLM 由自然语言问题生成 pandas/SQL，在沙箱执行[28][29] |
| **Schema 感知提示** | 把列名、dtype、样例行注入 prompt，而非全量数据[28] |
| **Agent 循环** | 生成→执行→观察→修正，逐步逼近答案[27] |
| **工具调用** | 以 `run_pandas`/`run_sql` 工具接入现有 agent 框架[29] |

> **关键**：结构化数据「需要与 RAG 不同的方法」[28]。对万行表，先 `df.head()/dtypes/shape` 生成 schema 摘要进 prompt，LLM 产出分析代码，沙箱跑完只回传**结果**（而非原始数据）。

---

## 5. 落地到 Ginno 的集成方案

### 5.1 约束清单（决定架构取舍）

1. **PyInstaller 单文件打包**：重依赖（Docling ~1GB、MarkItDown 全量 ~251MB）会显著膨胀 sidecar → **按需 extras + 懒加载**，核心包保持小。
2. **无数据库、全文件**：解析产物写成 `.md` 落到 vault，indexer 自然接管，**不新增存储**。
3. **默认零 embedding**：入库走现有词法检索即可，不依赖向量库。
4. **Python sidecar**：优先 Python 原生库，避免从 Python 反调 Node（officeparser）/JVM（Tika）带来的运行时依赖。

### 5.2 推荐架构：三条管线 + 一个解析核心

```
                        ┌──────────────────────────────────────────┐
                        │  knowledge/extractors.py（新增·解析核心）   │
                        │   dispatch(ext) → Markdown + metadata      │
                        │   懒加载: markitdown / calamine / docx /    │
                        │           pptx / pymupdf（缺依赖优雅降级）   │
                        └───────┬───────────────────────┬────────────┘
            ┌───────────────────┘                        └───────────────────┐
   A. 入库管线（Word/PPT/PDF/小CSV）                              B. 分析管线（CSV/Excel）
   ① 原始文件 → extractors → 派生 .md                           tools/document_tools.py（新增）
     （frontmatter: source/type/作者/时间）                       ├─ parse_document(path, format)
   ② 写入 vault 的 Raw/ 或同源目录                                │    按需抽取文本/表格/元数据
   ③ indexer 扩展 INDEX_EXTENSIONS + parse_file                  └─ analyze_table(path, question)
      对非 md 走 extractors 派生；或索引派生 md                         schema 摘要 → LLM 生成 pandas
   ④ 现有 retriever/injection 零改动复用                              代码 → 沙箱执行 → 回传结果
```

**两个接入点**：

- **接入点 1（入库）**：
  - 新增 `knowledge/extractors.py`：`extract(path) -> (markdown, metadata)`，按扩展名分发；统一用 MarkItDown 作底座，Excel 大数据用 calamine、PDF 需速度用 PyMuPDF 兜底。
  - 两种索引方式二选一：
    - **(a) 派生 md 入库**（推荐）：转换后把 `.md` 写到 vault（如 `Raw/_derived/xxx.md`，frontmatter 记 `source: 原文件`），indexer **零改动**即可索引——最省事、最契合「全文件」哲学。
    - **(b) indexer 直读多格式**：扩 `INDEX_EXTENSIONS` 并在 `parse_file` 里对非 md 调 extractors。索引更"实时"，但每次增量扫描要解析二进制，慢且重。
- **接入点 2（按需工具）**：
  - 新增 `tools/document_tools.py`，注册进 `graph.build_graph` 的 `all_tools`（命名同现有风格）：
    - `parse_document(path, format="text|json|metadata")` —— 对标 office-file skill 的三档输出。
    - `analyze_table(path, question)` —— schema 感知 + pandas 代码生成 + **受限执行**（复用/收紧现有 `bash` 的沙箱思路，或 `RestrictedPython`/子进程白名单）。
  - 权限：读类 `allow`，写 vault / 执行代码走 `ask`（沿用现有 permission 模型）。

### 5.3 依赖与打包策略

| 能力 | 依赖 | 打包建议 |
|---|---|---|
| 统一转换 | `markitdown[docx,pptx,xlsx,xls]` | 放新 extra `docs`，**懒加载**；不装时 `parse_document` 返回"缺依赖"提示 |
| Excel 快读/分析 | `pandas` + `python-calamine` | 同上 `docs` extra（或独立 `data` extra） |
| PDF 高速 | `pymupdf` | `docs` extra |
| 高精度版面/扫描 | `docling` 或 `pdfplumber` + OCR | **可选增强 extra**，默认不装（体积/模型下载） |
| 图片内嵌文字 | markitdown-ocr + 现有 vision | 复用 `build_model` 的 LLM client |

> 由于 runtime 目前**零解析依赖**，上述全部为增量；务必用 extra + import-time 检测，保证未安装时核心 agent 不受影响（与现有 `rag` extra 缺依赖自动退回词法的做法一致）。

### 5.4 分阶段路线图

| 阶段 | 范围 | 完成标准 |
|---|---|---|
| **P0 · 按需解析工具** | `extractors.py`（MarkItDown 底座）+ `parse_document` 工具 + `docs` extra | agent 能对 docx/pptx/xlsx/pdf 调 `parse_document` 拿到 text/json/metadata |
| **P1 · 表格数据分析** | `analyze_table` 工具 + schema 摘要 + 沙箱 pandas | "这个表 Top10/环比" 能生成代码、执行、回传结果 |
| **P2 · 文档入库检索** | 派生 md 写 vault + frontmatter(source) + KB 页「导入文件」入口 | Word/PPT/PDF 转 md 后可被 `/kb/wiki/search` 搜到并自动注入 |
| **P3 · 高保真/OCR 增强（可选）** | PyMuPDF/pdfplumber 兜底 + markitdown-ocr + 可选 Docling | 复杂表格/扫描件精度提升 |

> 建议 **P0 → P1 → P2** 为 MVP 闭环（先让 agent「看得懂、算得了」，再「记得住、搜得到」）。

### 5.5 测试方案（复用既有框架）

沿用 `packages/runtime/tests/` 三层 + `isolated_home`：
- **unit**：`test_extractors`（各格式 → markdown/元数据，缺依赖降级）；`test_schema_summary`（DataFrame → 列名/dtype/样例行）。
- **api**：`test_kb_import` 扩展——投放 docx/xlsx 到 vault，断言派生 md 被索引、可搜到。
- **e2e**：`ScriptedChatModel` 驱动真图，断言 `parse_document`/`analyze_table` 工具被调用、沙箱执行与结果回传；用 `GINNO_FAKE_LLM` seam 避免真实调用。
- 样本文件：仓库内放小型 `.docx/.xlsx/.pptx/.pdf/.csv` fixtures（体积小、可离线）。

---

## 6. 与既有 `office-file` skill 的关系

| 维度 | office-file skill（Node officeparser） | 本方案（Python 原生） |
|---|---|---|
| 运行位置 | Claude Code CLI 侧 | Ginno Python sidecar 内 |
| 依赖 | Node + officeparser | Python 库（随 sidecar 打包） |
| 能力 | text/json/metadata + OCR | 同 + **数据分析** + **KB 入库** + 复用 vision |
| 取舍 | 已是可用参照，验证了三档输出 UX | 与 Ginno 运行时同栈、可深度集成 indexer/工具/权限 |

> skill 可作为**交互设计的参照**（三档输出、错误文案），但 Ginno 内部实现走 Python 原生，避免打包引入 Node 运行时。

---

## 7. 产品流程设计：拖入文件 → 分析 → Excel 预览

### 7.1 现状（前端勘察结论）

| 能力 | 现状 |
|---|---|
| 拖拽入口 | `components/chat/ChatStream.tsx` 的 composer 已有 `onDrop`/`onPaste`/文件选择按钮三种入口 |
| 附件处理 | `addFiles()` **只接受 `image/*`**，其他类型静默丢弃；图片经 FileReader → base64（>400KB 用 canvas 缩到 1600px） |
| 发送协议 | WS `invoke {message, agent_id, turn_id, images:[{data, media_type}]}` → runtime `_run_stream` 组多模态 HumanMessage |
| 右侧面板 | `RightPanel.tsx` 四个 tab：todo / workflow / artifacts / memory（固定 380px）；ArtifactsPanel **只列名字不渲染内容** |
| 文件上传 | **无任何上传端点**（全 JSON API）；UI 只传内容不传路径 |
| 弹窗查看器 | KB 页有 PageViewer 弹窗查看 wiki 页 markdown——Excel 预览弹窗可复用此模式 |
| Tauri | 壳层无 file-drop 事件处理；webview 与 sidecar 同源（127.0.0.1:8787）、共享文件系统 |

### 7.2 目标用户旅程

```
① 拖入 sales.xlsx 到聊天框
   ├─ UI 按扩展名分类:图片→既有 images 通道;表格(xlsx/csv)→上传+预览;文档(docx/pptx/pdf)→上传
   ├─ POST /api/files (multipart) → sidecar 落盘 sessions/<session>/uploads/<uuid>-<name>（会话目录）
   │    返回 {id, name, path, kind, size}
   ├─ 表格类:立即 GET /api/files/{id}/preview → 打开 SheetViewer 弹窗
   │    (sheet 标签页 + 虚拟滚动网格 + 粘性表头 + dtype 标记 + 「1-100 / 12,408 行」)
   └─ 输入框上方出现文件 chip:📊 sales.xlsx · 3 sheets · 12,408×24   [×]

② 用户输入「分析这个文件,找出销售额 Top10 的产品」→ 发送
   └─ WS invoke {message, files:[{id, name, path, kind}]}   (图片仍走 images,大文件不走 base64)

③ Runtime
   ├─ _run_stream 把 files 落为会话级附件,注入 HumanMessage 附加上下文:
   │    「用户附加文件:sales.xlsx (xlsx, 路径 <path>)。schema: date(datetime), product(str),
   │      amount(float64)…共 12,408 行 × 24 列。可用 parse_document / analyze_table 工具。」
   ├─ Agent 判断:数据问题 → analyze_table(path, question)
   │    → 生成 pandas 代码(engine=calamine 读)→ 沙箱子进程执行(白名单+超时)
   └─ 产出:文字结论 + render_widget(stat_list: Top10 卡片) + attach_ref(结果 csv 进 Artifacts)

④ 后续交互
   ├─ 点消息里的文件 chip → 重开 SheetViewer 预览
   ├─ 追问「按地区拆一下」→ 复用同一会话附件,无需重传
   └─ Artifacts 面板 → 查看/打开分析产物
```

**空文本 + 仅拖文件**:runtime 合成默认指令「请概览这份数据:结构、质量、关键指标」;UI 可在 chip 旁给快捷 prompt(概览 / 质量检查 / Top N)。

### 7.3 关键设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 文件如何到 runtime | **HTTP 上传端点 → 落盘 → invoke 传 ref/路径** | 大 Excel 的 base64 会撑爆 WS;落盘后所有工具可直接按路径访问;与「全文件」哲学一致 |
| 预览数据来源 | **服务端渲染**(pandas+calamine → JSON 分页) | 与 Python 栈统一;巨表可分页;不在浏览器重复造解析 |
| 预览位置 | **弹窗(复用 PageViewer 模式)** | 右面板 380px 放不下表格;弹窗可用全宽;不打断对话 |
| 图片 | 维持 base64 现状 | 已有缩略+多模态链路,无需改 |
| Tauri 原生 file-drop | 暂缓(未来优化) | Web API 上传在 dev(浏览器)与 prod(Tauri)行为统一;原生路径模式可省一次拷贝,作后续增强 |

### 7.4 API 契约

```
POST /api/files            (multipart: file, session_id)
  → {ok, file: {id, name, path, kind, size, mime}}

GET  /api/files/{id}/preview?sheet=&offset=0&limit=100
  → {ok, kind: "xlsx",
     sheets: [{name, rows, cols}],
     sheet: "Sheet1",
     columns: [{name, dtype}],
     rows: [["2026-01-01", "A产品", 12340.5], ...],
     total_rows, offset, limit}
  (docx/pptx/pdf 预览 → 返回提取的 markdown,复用 PageViewer 渲染)

WS invoke 扩展:
  {type:"invoke", message, files:[{id, name, path, kind}], images:[...]}
```

### 7.5 响应式刷新层:文件即 Resource(MCP-as-app)

**目标体验**:agent 操作完 Excel(清洗/新增列/导出结果)后,用户打开的预览**自动刷新**;分析产出的结果表自动弹出预览。

**核心架构**:不是「所有读取都用 tool」,而是**单一文件访问层 + 两个门面 + 一条变更事件总线**。工具调用与 REST 是同一数据层的两个入口,谁改动文件,总线都能感知——预览订阅该文件即可自动刷新。这正是 MCP 协议里 **resources + subscriptions** 的模式(预览 = resource,UI subscribe,工具写入 → `resources/updated` 通知);Ginno 先内部实现同思想,未来可把该层包装成 MCP server 对外开放。

```
                    ┌────────────────────────────────┐
                    │  FileRegistry (files/ 模块)      │
                    │  · file_id ↔ 规范路径 双向映射     │
                    │  · touch(path, reason) 发事件     │
                    │  · 预览分页读取的唯一数据层        │
                    └───────┬────────────────┬───────┘
           tools 门面        │                │  REST 门面
  ┌─────────────────────────┘                └──────────────────────┐
  │ analyze_table / parse_document(声明 writes/reads)               │ POST /api/files(上传)
  │ write_file / edit_file(包装后 touch)                            │ GET /api/files/{id}/preview
  │ bash / MCP 工具(args 路径匹配 + mtime 兜底)                      │ (带 etag/mtime)
  └──────────────┬──────────────────────────────────────────────────┘
                 │ _stream_graph 的 tools 节点:tool.end 时做 touch 检测
                 ▼
      WS 事件  preview.invalidate {file_id, reason, turn_id}   ← 广播即可,会话级单用户无需订阅簿记
      (或分析结果:preview.emit {file_id, open: true} 自动弹结果表)
                 ▼
      UI: SheetViewer 若正打开该 file_id → 防抖 500ms → 重取当前页
```

**三种 touch 检测机制(确定性递减)**:

| 机制 | 覆盖 | 实现 |
|---|---|---|
| ① 工具显式声明 | analyze_table / parse_document / write_file / edit_file | 工具返回结构化结果 `{touched: [paths]}` 或包装器声明,`_stream_graph` 在 tool.end 时调 `registry.touch()` |
| ② args 路径匹配 | bash、MCP 工具等无声明者 | 扫描工具参数中的路径字符串,命中已注册文件则 touch(尽力而为) |
| ③ mtime 看门狗 | bash 间接改写(如 `python clean.py` 覆写)、外部改动 | 仅对「正被预览打开」的文件轮询 stat(桌面单机,2s 一次,极廉价),变化则 touch |

**stat 监视集 = artifact 账本(两级)**:artifact store 已有 `session_id` 字段,`kind=file` 的 artifact 即「本会话关心的文件」——监视集天然有界(只有附件 + agent 产出,不是整个 workspace),无需 OS watcher、无新依赖、PyInstaller 友好。

| 级别 | 集合 | 频率 | 动作 |
|---|---|---|---|
| 🔥 热 | **当前被预览打开**的文件(UI 发 `preview.open/close` WS 消息显式告知) | ~2s stat | 变化 → `preview.invalidate`(立即刷新) |
| 🌤 温 | 会话其余 `kind=file` artifacts | ~10–15s 慢扫 | 变化 → 仅标记 artifact `stale`(Artifacts 面板小红点),打开时再取新数据 |

> 为何足够:agent 的确定性写入已被机制①(工具声明 touch)覆盖,mtime 只是 bash/MCP 间接写入的兜底——而间接写入最要紧的时刻正是用户**正盯着看**的时候(热级)。不监视整个 workspace(可能上万文件);artifact 集通常 <20 个,轮询 stat 几乎零成本。

**artifact 即文件身份记录**:

```
Artifact(kind=file) = {id, name, ref: 规范路径, session_id, mtime, stale?}
  · 上传        → 建 artifact + FileRegistry 索引(file_id ↔ path ↔ artifact_id)
  · 工具声明写入 → 路径查到 artifact 就 touch;查不到(新产出文件)→ 自动建 artifact
                  再 touch(→ 自动出现在 Artifacts 面板,与现有 attach_ref 自动注册一致)
  · touch 路由  → path → 引用它的 session 集合 → 各 WS 发 invalidate(单用户也支持多会话引用同文件)
```

**落点极小**:`_stream_graph` 的 tools 节点(server.py 现有 `tool.end` 发射处、紧邻 widget.emit/ref.emit 拦截逻辑)加 ~20 行 touch 检测;`files/` 模块内一个 asyncio 监视协程(热/温两级循环);前端 SheetViewer 监听 `preview.invalidate` 重取——与现有 `artifacts.changed → reload` 模式同构。

**分析结果的「自动弹出」**:`analyze_table` 产出为表格且小于阈值时,注册为派生文件(`result:<turn_id>`)并发 `preview.emit {open: true}` → UI 在源表旁自动打开结果表预览;同时照旧 render_widget 文字卡片。这是「操作完即见结果」的关键瞬间。

**泛化**:同一条 file.changed 总线日后可驱动 docx→md 预览刷新、Artifacts 面板高亮、KB 增量索引——成为 Ginno 内部的文件状态响应式骨干。

### 7.6 Artifacts 面板自动跟随

**需求**:session 有新 artifact 产生时,右栏自动切到 Artifacts tab(不再纯手动)。

**现状接线**:`RightPanel.tsx` 的 tab 是局部 `useState<Tab>("todo")`;`artifacts.changed` WS 事件 → `ChatStream.tsx:491 g.reloadArtifacts()`(store.tsx 全量拉 `GET /api/artifacts`)。

**改造**:
1. **tab 状态提升到 store**:`useGinno` 增加 `rightTab / setRightTab / autoFollow / flashIds`;`RightPanel` 改为读 store,手动点 tab 时 `autoFollow=false`(防反复抢夺焦点),用户手动点回 Artifacts 或新一轮对话开始时复位 `true`。
2. **新增检测放在 store.reloadArtifacts**:reload 前后 diff id 集合 → 新增项中 `session_id === 当前会话` 的 → 若 `autoFollow` 则 `rightTab="artifacts"` + `flashIds`(ArtifactsPanel 对新条目做 2s 脉冲高亮)。非当前会话的后台更新不抢焦点。
3. **前置修复**:`server.py:2126/2130` 的 `add_artifact(...)` 调用**未传 `session_id`**(签名支持但调用省略),artifact 无会话归属 → 这两处补传(WS handler 内 session_id 就在作用域里),否则无法按会话过滤。

**与文件预览的分工**:新 artifact 是**可预览文件**(xlsx/csv/派生结果表)→ 按 §7.5 自动弹 SheetViewer(更强的「看见结果」);同时 tab 也切到 Artifacts 并高亮该条(二者不冲突,弹窗关闭后用户落在已高亮的面板上)。其余类型(workflow/doc/link)→ 仅切 tab + 高亮。

### 7.7 实现块映射

| 层 | 新增/改动 |
|---|---|
| runtime | `POST /api/files`、`GET /api/files/{id}/preview`(新建 `files/` 模块:upload 落盘 + calamine 分页预览);`_run_stream` 接受 `files` 并注入附件上下文;`tools/document_tools.py`(parse_document / analyze_table);`knowledge/extractors.py` |
| web | `ChatStream.addFiles` 放开非图片 + 走上传;composer 文件 chip 组件;`SheetViewer` 弹窗(sheet 标签/虚拟网格/分页,参照 PageViewer);`lib/runtime.ts` 增加 uploadFile/getFilePreview;消息块增加 `{kind:"file"}` 渲染 |
| 存储 | `sessions/<session_id>/uploads/<uuid>-<name>`(会话级目录,随项目);派生结果在 `sessions/<session_id>/results/`;预览不落盘 |
| 权限 | 上传/预览 allow;`analyze_table` 代码执行走 ask(同 bash) |

## 8. 风险与注意点

1. **打包体积**：MarkItDown 全量 ~251MB、Docling ~1GB[2][3]——必须 extra + 懒加载，否则 sidecar 膨胀。
2. **Excel 边角**：首行非表头、空行空列、合并单元格、NaN——MarkItDown 已知问题[17]；分析场景改用 `pandas.read_excel(engine="calamine")` 自行规整。
3. **mammoth 局限**：Markdown 图片定位/表格样式偶发问题，必要时用其 HTML 输出或 python-docx 兜底[12][14]。
4. **代码执行安全**：`analyze_table` 执行 LLM 生成代码须沙箱化（子进程 + 白名单/超时），参考现有 `bash` 工具的权限收敛。
5. **大文件 token**：绝不把整表塞 prompt；schema 摘要 + 结果回传[28]。
6. **行为差异**：calamine 与 openpyxl 单元格类型可能不同[11]——读写混用时注意。

---

## References

[1] [Docling: An Efficient Open-Source Toolkit for AI-driven Document Conversion (arXiv)](https://arxiv.org/html/2501.17887v1) — Retrieved 2026-08-02
[2] [I benchmarked 4 Python text extraction libraries (Reddit)](https://www.reddit.com/r/Python/comments/1ls6hj5/i_benchmarked_4_python_text_extraction_libraries/) — Retrieved 2026-08-02
[3] [I Benchmarked 4 Python Text Extraction Libraries (2025) (dev.to)](https://dev.to/nhirschfeld/i-benchmarked-4-python-text-extraction-libraries-2025-4e7j) — Retrieved 2026-08-02
[4] [docling vs markitdown vs marker (cnblogs)](https://www.cnblogs.com/itech/p/19186240) — Retrieved 2026-08-02
[5] [PDF Data Extraction Benchmark 2025 (Procycons)](https://procycons.com/en/blogs/pdf-data-extraction-benchmark/) — Retrieved 2026-08-02
[6] [Docling vs Unstructured (Respan.ai)](https://www.respan.ai/market-map/compare/docling-vs-unstructured) — Retrieved 2026-08-02
[7] [MarkItDown: PDF to Markdown for RAG Pipelines (AIBuilderClub)](https://www.aibuilderclub.com/blog/markitdown-microsoft-convert-files-markdown-llm) — Retrieved 2026-08-02
[8] [Fastest Way to Read Excel in Python (Haki Benita)](https://hakibenita.com/fast-excel-python) — Retrieved 2026-08-02
[9] [python-calamine (PyPI)](https://pypi.org/project/python-calamine/) — Retrieved 2026-08-02
[10] [tafia/calamine (GitHub)](https://github.com/tafia/calamine) — Retrieved 2026-08-02
[11] [BUG: Difference between calamine and openpyxl readers (pandas #59186)](https://github.com/pandas-dev/pandas/issues/59186) — Retrieved 2026-08-02
[12] [mammoth (PyPI)](https://pypi.org/project/mammoth/) — Retrieved 2026-08-02
[13] [Python MarkItDown: Convert Documents Into LLM-Ready Markdown (Real Python)](https://realpython.com/python-markitdown/) — Retrieved 2026-08-02
[14] [Support mammoth options in docx converter (markitdown #1549)](https://github.com/microsoft/markitdown/issues/1549) — Retrieved 2026-08-02
[15] [microsoft/markitdown (GitHub)](https://github.com/microsoft/markitdown) — Retrieved 2026-08-02
[16] [MarkItDown OCR Plugin README (GitCode)](https://gitcode.com/brawide/markitdown/blob/main/packages/markitdown-ocr/README.md) — Retrieved 2026-08-02
[17] [markitdown xlsx "N" columns + "NaN" cells issue (#2124)](https://github.com/microsoft/markitdown/issues/2124) — Retrieved 2026-08-02
[18] [Extracting Text from PPT Using python-pptx (Clay Atlas)](https://clay-atlas.com/us/blog/2024/10/04/python-extracting-text-from-ppt-using-the-python-pptx-library/) — Retrieved 2026-08-02
[19] [Turning PowerPoint Presentations into Structured Data (dev.to)](https://dev.to/divyanshusinha136/turning-powerpoint-presentations-into-structured-data-with-pythonaibrain-15fh) — Retrieved 2026-08-02
[20] [py-pdf/benchmarks (GitHub)](https://github.com/py-pdf/benchmarks) — Retrieved 2026-08-02
[21] [Features Comparison — PyMuPDF docs](https://pymupdf.readthedocs.io/en/latest/about.html) — Retrieved 2026-08-02
[22] [A Comparative Study of PDF Parsing Tools (arXiv)](https://arxiv.org/html/2410.09871v1) — Retrieved 2026-08-02
[23] [RAG Chunking Techniques for Tabular Data: 10 Strategies (Towards AI)](https://pub.towardsai.net/rag-chunking-techniques-for-tabular-data-10-powerful-strategies-aba887de331e) — Retrieved 2026-08-02
[24] [Embeddings/Chunking for Markdown Content (r/Rag)](https://www.reddit.com/r/Rag/comments/1lcqw1x/embeddingschunking_for_markdown_content/) — Retrieved 2026-08-02
[25] [Mastering RAG: Precision Techniques for Table-Heavy Documents (KX)](https://kx.com/blog/mastering-rag-precision-techniques-for-table-heavy-documents/) — Retrieved 2026-08-02
[26] [Structure-Aware Chunking for Tabular Data in RAG (arXiv)](https://arxiv.org/html/2605.00318v1) — Retrieved 2026-08-02
[27] [LLM/Agent-as-Data-Analyst: A Survey (arXiv)](https://arxiv.org/html/2509.23988v1) — Retrieved 2026-08-02
[28] [Chat with CSV & Excel Files Using Local AI (LocalAIMaster)](https://localaimaster.com/blog/local-ai-data-analyst) — Retrieved 2026-08-02
[29] [Ask Your CSV Anything: Build a Data Analysis Agent (dev.to)](https://dev.to/klement_gunndu/ask-your-csv-anything-build-a-data-analysis-agent-in-python-2641) — Retrieved 2026-08-02
[30] [docling-project/docling (GitHub)](https://github.com/docling-project/docling) — Retrieved 2026-08-02
[31] [Apache Tika](https://tika.apache.org/) — Retrieved 2026-08-02
[32] [Best Document Parsing Tools in 2026 (Mixpeek)](https://mixpeek.com/curated-lists/best-document-parsing-tools) — Retrieved 2026-08-02

---

## 11. 实现注记（落地后的偏差与现状）

> 本节记录按 §5/§7 实施后的**实际形态**与对原方案的偏差，供后续维护参考。

### 11.1 已实现（对应路线图）

| 模块 | 文件 | 说明 |
|---|---|---|
| 解析器 | `files/extractors.py` | xlsx/csv/docx/pptx/pdf/json/xml/txt → markdown + metadata；`schema_summary` 产出紧凑 schema |
| 身份账本 | `files/registry.py` | id↔path 双向映射，持久化到 `projects/<slug>/files.json`；`touch()` + `subscribe()` 响应层 |
| 预览 | `files/preview.py` | 表格分页网格 / 文档 markdown 两种 payload |
| 工具 | `tools/document_tools.py` | `parse_document`(text/json/metadata)、`analyze_table`(沙箱子进程跑 pandas，DataFrame 结果落派生 CSV) |
| API | `server.py` | `POST /api/files`(multipart 上传)、`GET /api/files`、`GET /api/files/{id}/preview` |
| WS 接线 | `server.py` | invoke 接受 `files` → 注入 `<attached_files>` prompt；`preview.emit`/`preview.invalidate`/`artifacts.changed`；`_file_watcher` 协程；`_tool_file_effects` touch 检测 |
| Web | `SheetViewer.tsx` 等 | 拖拽/粘贴/选择多格式附件、上传、文件 chip、预览弹窗、Artifacts 自动跟随 + 高亮 |

依赖放在 `[project.optional-dependencies] docs`（pandas/python-calamine/openpyxl/python-docx/python-pptx/pypdf），**懒加载 + 缺依赖优雅降级**（`ExtractorUnavailable`）。

### 11.2 对原方案的偏差（及理由）

1. **解析用「专用库直连」而非 MarkItDown 封装**（§2.3 推荐 MarkItDown）。落地时改为直接调用 python-docx/python-pptx/openpyxl+calamine/pypdf：测试确定性更好、打包更轻、控制更细。MarkItDown 仍可日后作为统一适配器叠加，不冲突。
2. **PDF 用 pypdf 而非 PyMuPDF**：pypdf 为 BSD 许可，PyMuPDF 为 AGPL；桌面包许可更干净。复杂表格/扫描件的高保真（§5 P3）仍可再引入 pdfplumber/OCR。
3. **取消 `preview.open/close` 订阅协议**（§7.5 原设计）。原因：WS recv 循环在一轮对话运行期间被 `_run_stream` 阻塞，无法及时处理中途的订阅消息。改为**服务端直接向会话连接广播 `preview.invalidate`**，由 UI 判断当前是否打开该文件再决定是否重取——协议更简、无中途间隙。
4. **watcher 简化为单级 5s 扫描**（§7.5 原为热 2s/温 10s 两级）。会话内注册文件通常 <20 个，单级扫描足够；命中变化即发 invalidate + 打 stale 红点（取预览时自动清除）。
5. **`analyze_table` 结果自动弹出**经 `preview.emit {open:true}`（§7.5），派生 CSV 由服务端 relocate 到会话目录 `sessions/<sid>/results/`，同时注册 artifact（带 session_id）。

### 11.3 测试覆盖（全部通过）

- **unit**：`test_extractors`(各格式抽取/schema/NaN)、`test_files_registry`(注册/幂等/持久化/touch 订阅/stale)、`test_files_preview`(分页/sheet 选择/文档 markdown)、`test_document_tools`(parse 三档/analyze 标量与表格结果/派生 CSV/超时/拒绝非表格)
- **api**：`test_files_api`(上传+artifact 带 session_id/文件名清洗/预览/分页/不支持格式/按会话列表)
- **e2e**：`test_files_ws`(附件 schema 注入 prompt、空文本默认意图、history file 块、analyze_table 派生结果 `preview.emit{open}`、write_file 触发 `preview.invalidate`、取预览清 stale)
- 全量 runtime 套件 **420+ passed**；web `tsc --noEmit` 零错误、`next build` 成功、新增文件 ruff 全干净。

### 11.4 已知边界 / 后续

- `analyze_table` 沙箱为 `python -I` + 超时，**尽力隔离**而非强沙箱；权限默认 `ask`（bypass 模式除外）。
- 旧 `.xls/.doc/.ppt`（OLE 二进制）依赖 calamine/第三方，未覆盖；当前面向 OOXML(.docx/.pptx/.xlsx) 与 csv/pdf。
- KB 入库（路线图 P2：派生 md 写 vault 供检索）尚未接，解析层已就绪，缺一个「extract → 写 Raw/_derived → 索引」的编排。
