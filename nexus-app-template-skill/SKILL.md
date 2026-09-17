---
name: nexus-app-template-builder
description: Turn a business requirement (forward) or an existing apps/ application (reverse) into a nexus-kit application template entry in the app-templates/ knowledge base — nothing is implemented; plugin and tool positions are placeholders written as short "processing-step cards" / "tool description cards". Use when the user wants to add an entry to the template library, wants an application blueprint without implementation, or wants to distill an existing app into a reusable template.
---

# nexus-app-template-builder

Turn a business requirement into a nexus-kit application **template**: one
Pattern graph (nodes/edges/graph type) plus a set of **description-placeholder
plugins** (executor / stage / tool positions written as "processing-step
cards" or "tool description cards", never implementation code), landing as
one entry of the template knowledge base `app-templates/`. A template is a
knowledge-base asset: structurally uniform, single-file self-contained,
machine-verifiable — any later implementer (human or agent) builds the code
straight from the cards.

This skill is fully self-contained: framework knowledge lives in its own
`references/`, the structure-gate script is
`references/verify_template.py`; it depends on no other skill's assets in
this repo.

Drive forward autonomously, never waiting for confirmation; but the Phase 1
decision statement and the Phase 2 five artifacts remain mandatory outputs —
a wrong graph type or a missing node interaction table invalidates the whole
template.

## Framework references (own references/, paths relative to this directory)

| File | Covers |
|---|---|
| `references/architecture.md` | Layering; runtime semantics of both graph types (fsm/agent) |
| `references/pattern-schema.md` | Pattern/BaseNode field tables, YAML shape |
| `references/plugin-and-tool-guide.md` | Plugin contracts; the three capability-triage routes |
| `references/template-index.md` | app-templates/ knowledge-base entries → business shape / borrow-discipline mapping |
| `references/pitfalls.md` | Node interaction table template, graph_state rules, loop inheritance |
| `references/verify_template.py` | Structure-gate validation script (used in Phase 4) |

Cross-check with the CLI: `python nexus-introspect-skill/introspect.py apps`
/ `pattern <code> --view yaml` / `plugin executor <code>` — source code is
the only truth.

## Knowledge-base layout

```
app-templates/
├── INDEX.md                       # master catalog: code/name/business shape/graph type/source
└── <code>/                        # one directory per template (code = pattern code)
    └── TEMPLATE.md                # single-file self-contained entry
```

## TEMPLATE.md section skeleton (mandatory, fixed order)

> The skeleton's Chinese section and card headings are **machine-grepped
> anchors** — verify_template.py matches them verbatim. Copy them into
> TEMPLATE.md exactly as shown; never translate or reword them. Free text
> inside tables and cards may be written in any language.

```markdown
# <code> — <名称>（应用模板）

> 元信息：业务形态=<一句话> ｜ 图类型=<fsm/agent> ｜ 借用来源=<模板应用/零借用>
> 来源=<业务需求描述 或 逆向自 apps/<name>>

## 节点清单
（表格：code / 名称 / 用途 / sub_nodes / is_end）

## 节点交互表
（表格：节点 / 读 graph_state / 写 graph_state / 出边与路由；每个循环
必须写检查点与经验继承策略；表后可加循环继承说明段落）

## Pattern 声明
（一个 ```yaml 围栏块，首行必须是标记注释 `# nexus-pattern: <code>`，
字段见下；这是 verify_template.py 的抽取锚点）

## 插件步骤卡
（每卡一个 `#### 插件卡：<code>（<kind>）` 四级标题；kind ∈
stage / executor / messages_builder / agent_hooks；**内置件被引用也要
有卡**，绑定位置注明"内置"。卡内固定字段：

- 绑定位置：pattern.stages 的 <槽位> 槽 / node.plugins / pattern.plugins
- 触发时机：何时执行
- 读（graph_state）：读哪些键
- 处理步骤：几句话自由描述做什么、怎么做——这是占位实现的核心描述，
  后续实现者照此实现
- 写（graph_state）：写哪些键
- 出边影响：是否/如何改写 next_node 与回复）

## 工具描述卡
（每卡一个 `#### 工具卡：<name>（<toolset>）` 四级标题，固定字段：
用途 / 参数 / 返回 / 为什么是工具而非 prompt。参数要写约束（枚举/
范围/长度），错误语义要可自纠——错误文本就是模型下一轮的自我修正
提示（细则见 `references/plugin-and-tool-guide.md` 工具设计规则）。
无工具则写"（无）"）

## 实现注意事项
（给未来实现者的提示：时序陷阱、预算、密钥来源、测试范式等）
```

Skeleton legend (English):

- `## 节点清单` — node list: table of code / name / purpose / sub_nodes / is_end.
- `## 节点交互表` — node interaction table: one row per node — reads
  graph_state / writes graph_state / out-edges & routing; every loop MUST
  state its checkpoint & experience-inheritance strategy; a
  loop-inheritance paragraph may follow the table.
- `## Pattern 声明` — Pattern declaration: one ```yaml fenced block whose
  first line MUST be the marker comment `# nexus-pattern: <code>`; this is
  verify_template.py's extraction anchor.
- `## 插件步骤卡` — plugin step cards: one `#### 插件卡：<code>（<kind>）`
  H4 per card; kind ∈ stage / executor / messages_builder / agent_hooks;
  builtin parts being referenced also get a card (binding marked "builtin").
  Fixed fields per card: binding (绑定位置) / when it runs (触发时机) /
  reads (读) / processing steps (处理步骤 — a few free-form sentences on
  what it does and how; the core placeholder description implementers build
  from) / writes (写) / out-edge impact (出边影响 — whether/how it rewrites
  next_node and the reply).
- `## 工具描述卡` — tool description cards: one `#### 工具卡：<name>（<toolset>）`
  H4 per card; fixed fields: purpose / parameters / returns / why a tool and
  not a prompt. Parameters carry constraints (enum/range/length); error
  semantics must be self-correcting (the error text IS the model's
  next-round self-correction prompt — rules in the tool design rules of
  `references/plugin-and-tool-guide.md`). Write "（无）" when there are no
  tools.
- `## 实现注意事项` — implementation notes: timing traps, budgets, secret
  sources, test idiom, etc., for the future implementer.

The four-level heading formats (`#### 插件卡：<code>（<kind>）`,
`#### 工具卡：<name>（<toolset>）`) are a machine-greppable contract —
verify_template.py relies on them for card-coverage checks; **no variants
allowed**.

Write the Pattern-declaration YAML block as a **lean declaration**: code /
name / description / pattern_type / entry_node_code / stages (fsm) / nodes
(code, name, description, task_description, sub_nodes, slots, is_end,
plugins). No large prompt bodies — prompts belong to implementation; refer
to them in the implementation notes instead.

## Phase 0 — Recon

1. Read whichever of the reference files above you need.
2. Inventory the knowledge base: existing `app-templates/` entries
   (authoritative catalog: `INDEX.md`; avoid duplicating a business shape —
   if the shape exists, state the difference or merge).
3. Study closely the 1–2 existing entries closest in business shape
   (forward mode; selection guidance in the "borrow it for" column of
   `references/template-index.md`) or the target application's full source
   (reverse mode).

## Phase 1 — Graph type decision

Criteria in `references/architecture.md`: guided conversation where each
user message advances one step, slot/form collection, per-turn confirmation,
natural cycles → **fsm**; a single user message should trigger an end-to-end
deliverable (report / artifact / research conclusion) via a multi-node
autonomous pipeline → **agent**; when ambiguous, default to agent and say
why; a vague requirement does not block drafting — record the defaults and
assumptions you filled, and put open questions into the implementation
notes. **State the decision before writing**: pattern_type, the signals that
fired, a one-line 5–10 node sketch. The decision directly shapes
TEMPLATE.md's node list and interaction table.

## Phase 2 — The five-artifact plan

1. **Borrow list + diff analysis**: declare what you borrow from existing
   `app-templates/` entries (graph shape / guard discipline / loop strategy
   / card idiom — mechanism-level borrowing beats shape copying); zero
   borrow must be stated explicitly with reasons.
2. **Full node list**: code / name / purpose / sub_nodes / is_end. Start
   from the smallest graph that satisfies the requirement — every node
   beyond the trunk cites the requirement signal that demands it
   (signal→topology map: `references/pitfalls.md` §11).
3. **Node interaction table** (mandatory): per node, which graph_state keys
   it reads/writes; every loop (design→verify→fix…) states its checkpoint
   & experience-inheritance strategy (history array / best checkpoint /
   tried-and-failed log). Template in `references/pitfalls.md`.
4. **Capability triage table**: one row per capability, routed to —
   **prompt-native node** (semantic judgment / phrasing) / **tool
   description card** (precise computation, external API, deterministic
   transforms) / **executor or stage step card** (orchestration, state
   bridging, convergence gates). Triage rules as in
   `references/plugin-and-tool-guide.md`; in a template they land as
   cards, not code.
5. **Artifact list**: `app-templates/<code>/TEMPLATE.md` (new directory) +
   one appended `INDEX.md` line; confirm the code does not collide
   (`introspect.py apps` + existing entries).

## Phase 3 — Write the template

- Fill in section by section following the skeleton; the interaction-table
  row count must equal the node count; the Pattern YAML block's first line
  carries the marker comment.
- Every declared plugin code (pattern.plugins / node.plugins / stages
  skeleton, builtin parts included) gets a plugin step card; every tool
  gets a tool description card.
- The processing-steps field is "a few sentences": input→transform→output
  plus the failure path; no pseudocode detail. Determinism requirements
  (zero LLM, anti-fabrication, anti-replay) go into out-edge impact or the
  implementation notes. In a design→verify→fix loop, gate parameters
  (thresholds / stale-N / pass rules) are read-only — a repair card must
  not rewrite the verify card (anti-gate-tampering,
  `references/pitfalls.md` §12).
- Append one `INDEX.md` line (code / name / business shape / graph type /
  source / relative link to the entry).

**Reverse mode** (existing app → template):

1. Read the full `apps/<name>/` source (route/prompts/stages/executor/
   tools); cross-check the declaration with
   `introspect.py pattern <code> --view yaml`.
2. Derive the five artifacts backwards: nodes and edges from route.py; the
   interaction table from the executor/stage graph_state reads/writes; the
   triage table from "which behaviors are done deterministically in code".
3. A plugin card's processing steps = a **condensed retelling** of that
   plugin's real implementation (what it reads, the steps, what it writes,
   what happens on failure) — not pasted source.
4. Mark the source "reverse-engineered from apps/<name>"; the
   implementation notes state that the app already exists and the template
   is its structural distillation.

## Phase 4 — Dual-gate verification (all mandatory; red = not done)

**Content gate (checklist)**:

- [ ] Section skeleton complete and in order; interaction-table rows equal node count
- [ ] Every loop has a checkpoint & experience-inheritance strategy
- [ ] Every declared plugin code (builtin parts included) has a card; every tool has a card; no orphan cards
- [ ] YAML block and card declarations agree (codes, kinds, binding slots)
- [ ] Borrow list explicit; code collision-free; INDEX.md appended

**Structure gate (script)**:

```bash
python nexus-app-template-skill/references/verify_template.py \
    app-templates/<code>/TEMPLATE.md
```

The script extracts the YAML block → registers placeholder factories for
declared plugin codes → constructs the Pattern → `validate_pattern` +
structural assertions (entry / dangling edges / end nodes / card coverage /
interaction-table rows / INDEX listing). Zero implementation is needed to
prove structural soundness.

Report honestly: what passed, what is missing. Never declare done while any
gate is red.

## Template → implementation relay

A template entry IS the implementation blueprint: with the node list,
interaction table, and triage table in place, an implementer (human or
agent) builds the code from the plugin step cards and tool description
cards — each card's fields (binding / reads / processing steps / writes /
out-edge impact) are the implementation contract; the implementation notes
carry timing and test requirements. The reverse relay is this skill's
reverse mode: implemented apps distill back into the knowledge base.

## Safety red lines (hard, no exceptions)

1. **Write only**: `app-templates/<code>/` (new directory),
   `app-templates/INDEX.md` (appended line), `nexus-app-template-skill/`
   (this skill's own maintenance).
2. **Never modify**: `nexus/`, `atoms/`, `host/`, `ui/`, `apps/`, `tests/`,
   `docs/`, other existing entries.
3. Templates produce no executable code (the YAML fenced block is
   declaration data, not an imported module); never write `host/config/`.
4. Secrets never enter a template (mention secret sources by
   environment-variable name only).
5. Tool authorization descriptions follow deny-by-default semantics
   (allow_toolset ∩ use_tools); a template must never describe "widening
   grants to make it run".
