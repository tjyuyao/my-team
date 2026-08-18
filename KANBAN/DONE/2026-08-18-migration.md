---
kind: task
status: completed
phase: v0.9 完成后
source: 用户意见
r7_exempt: v0.8.0-implementation-plan, v080-p2-remaining
---

# KANBAN 格式严格化迁移

把 KANBAN 目录下所有看板文件迁移到新的严格格式（YAML frontmatter +
统一的目录语义），并让两个校验器全绿。**只动 `KANBAN/` 和 `tests/test_kanban_invariants.py`（如需要）下的内容，不碰 `src/`、不碰 `git` 提交、不碰
`KANBAN/MILESTONE/2026-08-18-v0.9.0.md`（该文件保持原样、不修改、不加入任何提交）。**

验收标准（两条都满足才算完成）：

```bash
python3 KANBAN/kanban_lint.py             # 输出 "0 violation(s)."，exit 0
python3 KANBAN/enforce_filename_dates.py --check   # 输出 "0 file(s) need renaming"
.venv/bin/pytest -q tests/test_kanban_invariants.py  # 通过
```

---

## 一、YAML frontmatter 规范（每个 `.md` 文件最顶部，README.md 除外）

文件第一行起必须是：

```yaml
---
kind: task
status: completed
phase: v0.10
source: SPEC §6.2
priority: high
---
```

然后空一行，再是原有的 `# 标题` 和正文。

字段规则：

- `kind`：**必填**，枚举 `plan | issue | task | report`。
- `status`：按 kind 取值，见下表；**终态列必填**。
- `phase`：可选，自由字符串（原样搬运 `**Phase:**` 的值）。
- `source`：可选，自由字符串；**若值含英文冒号 `:` 或 `#`，必须用双引号包裹**。
- `priority`：可选，枚举 `high | medium | low`。

### 目录 → kind / status 映射表

| 目录 | kind | status | 说明 |
|---|---|---|---|
| `PLAN/`（活跃计划） | plan | `active` | 3 个 |
| `PLAN/`（归档计划） | plan | `archived` | 见下文特殊处理 |
| `OPEN_ISSUE/` | issue | `open` | 3 个 |
| `CLOSED_ISSUE/` | issue | `closed` | 见下文特殊处理 |
| `TODO/` | task | （省略） | 18 个 |
| `IN_PROGRESS/` | task | （省略） | 0 个 |
| `DONE/` | task | `completed` 或 `rejected` | 见下文 |
| `MILESTONE/` | report | （省略） | 9 个 |

## 二、字段迁移映射（旧 markdown 元信息 → frontmatter）

对每个文件，把正文开头的以下行**迁移进 frontmatter 后删除原文**：

| 旧行 | frontmatter | 注意 |
|---|---|---|
| `**Kind:** x` | `kind: x` | 若无此行为按目录推断 kind |
| `**Status:** x` | `status:` | 值改为枚举（按下表推断），**原文保留不动**（它含叙述，勿删）|
| `**Phase:** x` | `phase: x` | 值原样 |
| `**Source:** x` | `source: x` | 值原样；含 `:`/`#` 加双引号 |
| `**Priority:** x` | `priority: x` | 值须 high/medium/low |

其余 `**X:**` 行（`Date`、`Created`、`Opened`、`Completed`、`Label`、
`Tests`、`Acceptance` 等）**全部保留在正文，不动**。

> 注意：`**Status:**` 的中文叙述值（如"计划中""P1 全部完成"）**保留原文**；
> frontmatter 的 `status` 单独用枚举值（active/completed/…）重写。两者并存是
> 有意为之：正文保留人读历史，frontmatter 供机器校验。

## 三、特殊处理（逐个执行，不能遗漏）

1. **DONE 里 4 个 issue 文件迁到 CLOSED_ISSUE**（新规范：DONE 只放 task）：
   移动这 4 个文件 `KANBAN/DONE/` → `KANBAN/CLOSED_ISSUE/`（目录不存在则建），
   移动时**更新文件名日期为当天**，frontmatter `kind: issue, status: closed`：

   - `2026-08-17-oi-003-design-review.md`
   - `2026-08-17-oi-004-architecture-review.md`
   - `2026-08-17-oi-005-ecommerce-scenario.md`
   - `2026-08-17-oi-006-scenario-synthesis.md`

2. **v0.8 计划从 DONE 移回 PLAN 并归档**：
   `KANBAN/DONE/2026-08-18-v0.8.0-implementation-plan.md`
   → `KANBAN/PLAN/2026-08-18-v0.8.0-plan.archived.md`，
   frontmatter `kind: plan, status: archived`。正文里的归档注记保留。

3. **修正一处断链引用**：`KANBAN/TODO/2026-08-17-v080-p2-remaining.md`
   的 `Source:` 里 `KANBAN/DONE/2026-08-18-v0.8.0-implementation-plan.md`
   改为 `KANBAN/PLAN/v0.8.0-plan`（用 topic、不带日期）。

4. **DONE 里 task 的 status**：逐个检查，默认 `completed`；仅当标题/正文
   明确表示"否决 / 放弃 / rejected / won't do"才标 `rejected`。预期几乎
   全部是 `completed`。

5. **让 enforce 脚本识别 CLOSED_ISSUE**：`KANBAN/enforce_filename_dates.py`
   的 `KANBAN_COLUMNS` 元组加入 `"CLOSED_ISSUE"`（插在 `OPEN_ISSUE` 之后）。

6. **归档 v0.9.0-plan**（R10 要求：MILESTONE 已有 v0.9.0 报告，v0.9 已发布）：
   `KANBAN/PLAN/2026-08-17-v0.9.0-plan.md`
   → `KANBAN/PLAN/2026-08-18-v0.9.0-plan.archived.md`，
   frontmatter `kind: plan, status: archived`。MILESTONE 文件本身不动。

## 四、日期统一

全部迁移完成后，运行（会批量把日期前缀更新为最后修改日）：

```bash
python3 KANBAN/enforce_filename_dates.py
```

改名后交叉引用不会断——校验器按 topic（去掉日期和 `.archived`）匹配引用，
所以 `Source:` 里的旧日期文件名依然有效。

## 五、更新 KANBAN/README.md 与各列 README

把 `KANBAN/README.md` 的语义表更新为以下七列（替换现有六列表），并同步
`KANBAN/PLAN/README.md`、`KANBAN/IN_PROGRESS/README.md`，新建
`KANBAN/CLOSED_ISSUE/README.md`：

| 目录 | 语义 | kind | 归档规则 |
|---|---|---|---|
| `PLAN/` | 版本计划（活跃 + 归档都留原地） | plan | 归档改名 `-plan.archived.md`，status=archived |
| `OPEN_ISSUE/` | 开放议题 | issue | status=open |
| `CLOSED_ISSUE/` | 已关闭/否决/已转化的议题 | issue | status=closed |
| `TODO/` | 待办原子任务 | task | — |
| `IN_PROGRESS/` | WIP 任务（上限 3） | task | — |
| `DONE/` | 任务终态 | task | status=completed/rejected |
| `MILESTONE/` | 只读版本报告 | report | — |

README 还需写明（替换旧规则）：

- 文件名：`{YYYY-MM-DD}-{topic}.md`，topic 小写短横线；日期 = **文件最后修改
  日期（mtime）**，由 `enforce_filename_dates.py` 强制。
- 每个文件必须带 YAML frontmatter（schema 见本提示词第一节），`kind` 必填
  且与所在目录一致；终态列（DONE/CLOSED_ISSUE）与归档计划必须带 `status`。
- 交叉引用写 topic（不带日期、不带 `.archived`），校验器按 topic 匹配。
- 计划完成 = 其引用的 TODO 全在 DONE 或有显式否决；完成后**留在 PLAN 原地**
  改名 `-plan.archived.md`，版本报告另写 MILESTONE。
- 反向约束（R10）：MILESTONE 报告一旦写出，对应版本的活跃计划必须归档
  （不能"报告已发、计划仍 active"）。
- 状态流转：TODO → IN_PROGRESS → DONE（移动时更新日期）；OPEN_ISSUE →
  CLOSED_ISSUE；PLAN 归档 = 原地改名。

## 六、明确不做

- 不改 `src/` 任何代码。
- 不执行 `git` 操作（不 add、不 commit）。
- 不碰 `KANBAN/MILESTONE/2026-08-18-v0.9.0.md`。
- 不新写业务内容、不改 TODO 的验收标准或需求（只做格式迁移）。
- 不引入任何第三方依赖（frontmatter 只用标量 `key: value`）。

完成标准以第一节的三条命令全绿为准。若某文件存在歧义无法自动判断
（例如无法确定 status 是 completed 还是 rejected），**宁可保守**：按
`completed` 处理，并在交付说明里列出该文件名供人工复核。
