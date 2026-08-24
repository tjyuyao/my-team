# KANBAN 目录使用约定

## 目录结构

```
KANBAN/
├── PLAN/          # 版本计划（活跃 + 归档都留原地）
├── OPEN_ISSUE/    # 开放议题（未确认 / 讨论中）
├── CLOSED_ISSUE/  # 已关闭 / 否决 / 已转化的议题
├── TODO/          # 待办原子任务（可验收、可单人完成）
├── IN_PROGRESS/   # 正在执行的任务（严格 WIP 限制）
├── DONE/          # 任务终态（已完成 / 已否决）
└── MILESTONE/     # 只读版本报告（已发布历史）
```

## 各目录语义

| 目录 | 语义 | kind | 归档规则 |
|---|---|---|---|
| `PLAN/` | 版本计划（活跃 + 归档都留原地） | plan | 归档改名 `-plan.archived.md`，status=archived |
| `OPEN_ISSUE/` | 开放议题 | issue | status=open |
| `CLOSED_ISSUE/` | 已关闭/否决/已转化的议题 | issue | status=closed |
| `TODO/` | 待办原子任务 | task | — |
| `IN_PROGRESS/` | WIP 任务（上限 3） | task | — |
| `DONE/` | 任务终态 | task | status=completed/rejected |
| `MILESTONE/` | 只读版本报告 | report | — |

## 文件命名

```text
{YYYY-MM-DD}-{topic}.md            # 普通文件
{YYYY-MM-DD}-{topic}.archived.md   # 归档计划
```

- **日期 = 文件最后修改日期（mtime）**，由 `enforce_filename_dates.py`
  检查并强制；移动/编辑文件后运行它以同步日期前缀。
- **topic** = 小写英文短横线分隔的简短描述。
- `README.md` 是唯一豁免 frontmatter 与日期规则的文件。

## YAML frontmatter（每个 `.md` 文件最顶部，README.md 除外）

```yaml
---
kind: task
status: completed
phase: v0.10
source: SPEC §6.2
priority: high
---
```

- `kind`：**必填**，枚举 `plan | issue | task | report`，且必须与所在
  目录一致。
- `status`：按 kind 取值 —— plan: `active|archived`；issue:
  `open|closed`；task: `completed|rejected`；report: 无。**终态列必填**：
  `DONE/` 与 `CLOSED_ISSUE/`、归档计划必须带 `status`。
- `phase`：可选，自由字符串。
- `source`：可选，自由字符串；**若值含英文冒号 `:` 或 `#`，必须用
  双引号包裹**。
- `priority`：可选，枚举 `high | medium | low`。
- `r7_exempt`：可选，逗号分隔的 topic 列表，声明本文件内允许悬空
  的引用（见「R7 悬空引用」一节）。

> frontmatter 是**标量式**：每行一个 `key: value`，不支持嵌套、列表、
> 多行值——校验器用自己的手工解析器读取，复杂 YAML 会被判为缺失
> frontmatter（R3）。

正文里的 `**Status:**` 等旧式元信息行可保留供人读历史；frontmatter 是
机器校验的唯一依据。

## 交叉引用

引用其它看板文件时写 **topic（不带日期、不带 `.archived`）**，例如：

- `KANBAN/PLAN/v0.8.0-plan`
- `KANBAN/DONE/p0-write-path-security`

校验器按 topic 匹配引用，因此日期改名与计划归档不会切断引用。

## 状态流转

```text
TODO ──开始──→ IN_PROGRESS ──完成──→ DONE        （每次移动更新日期）
OPEN_ISSUE ──关闭 / 否决 / 已转化──→ CLOSED_ISSUE
PLAN ──完成──→ 留在 PLAN 原地改名 -plan.archived.md（另写 MILESTONE 报告）
```

- **计划完成** = 其引用的 TODO 全在 DONE 或有显式否决；完成后留在
  `PLAN/` 原地改名 `-plan.archived.md`，版本报告另写 `MILESTONE/`。
- 版本一旦发布（MILESTONE 报告存在），其计划必须归档，不得保持
  active（校验器 R10 强制）。
- 任务否决：标题/正文明确"否决/放弃/rejected/won't do"时 DONE 标
  `status=rejected`，否则默认 `completed`。

## 工具：kanban_lint.py（看板不变量校验器）

> 本节写给直接操作看板的执行者（人或 agent）：**只依赖本节即可正确使用，
> 无需阅读源码**。实现与本节不一致时以本节为准，并同步修正代码。

### 运行

```bash
python3 KANBAN/kanban_lint.py                # 检查默认看板（脚本所在 KANBAN/）
python3 KANBAN/kanban_lint.py --root <路径>  # 检查指定看板目录
```

- 输出：每条违规一行「`{相对路径}: R{n} …`」，末尾 `N violation(s).`
- 退出码：`0` = 无违规；`1` = 存在违规。**任何看板改动提交前必须为 0。**
- 零依赖（仅标准库 + git 命令；R2 日期基准用 git 提交日期）；只扫描七个列
  目录下的 `*.md`（`README.md` 豁免），`__pycache__`、根目录等一律不检查。

### 作为库调用（CI 门禁）

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("KANBAN").resolve()))
import kanban_lint

violations = kanban_lint.check_board(Path("KANBAN"))  # -> list[str]；空 = 合法
```

`tests/test_kanban_invariants.py` 正是这么用的：看板一旦违规 pytest 即失败，
因此它是 CI 门禁，不是可选的辅助脚本。

### 检查规则 R1–R10（触发例 → 修复）

| 规则 | 含义 | 触发示例 → 修复 |
|---|---|---|
| R1 | 文件名 `YYYY-MM-DD-{小写短横主题}.md`（归档计划为 `….archived.md`） | 日期缺前导零、含大写 → 改名 |
| R2 | 文件名日期前缀 == 该文件最近一次提交日期（`git log`；未提交的新文件回退 mtime） | 编辑/移动后没同步日期 → 运行 `enforce_filename_dates.py` |
| R3 | frontmatter 存在；`kind` 合法且等于所在列要求（PLAN=plan，OPEN_ISSUE/CLOSED_ISSUE=issue，TODO/IN_PROGRESS/DONE=task，MILESTONE=report） | 无 frontmatter；`kind: task` 放进 PLAN → 补/改 frontmatter |
| R4 | PLAN 中版本号 `vX.Y.Z` 唯一 | 两个计划同是 v0.10 → 合并或改版本 |
| R5 | 列与 kind 对应（由 R3 蕴含）：DONE 只放任务、CLOSED_ISSUE 只放议题、MILESTONE 只放报告 | 议题文件进了 DONE → 移列 |
| R6 | `status` 合法（task: completed\|rejected；plan: active\|archived；issue: open\|closed；report 无 status）；终态列必填——DONE 必填 `completed\|rejected`、CLOSED_ISSUE 必填 `closed`、`.archived` 计划必填 `archived`；`priority` 若填限 high\|medium\|low | DONE 里没写 status → 补上 |
| R7 | 交叉引用必须解析到存在的看板 topic（见下「哪些算引用」「引用怎么写」） | 引用了已删除/改名前的路径 → 改引用为现存 topic，或声明 `r7_exempt` |
| R8 | pyproject.toml 版本 ≥ 看板已完成最高版本（已完成 = MILESTONE 报告 + 归档计划） | 归档了 v0.9 计划但 pyproject 仍 0.8.0 → 提升 pyproject 版本 |
| R9 | 活跃（未归档）计划版本不得 < pyproject 版本：已发布版本的计划必须归档 | v0.9 已发布而 v0.9.0-plan 仍 active → 改名 `-plan.archived.md` + `status: archived` |
| R10 | 写了 MILESTONE 报告的版本，其计划必须已归档 | `v0.9.0.md` 报告已写而计划未归档 → 同 R9 |

版本比较按数字（v0.10 > v0.9），校验器从文件名 / pyproject.toml 中提取
`v?N.N.N` 进行比较。

### 哪些文本算「交叉引用」（R7 只查这些）

只有两种形态会被检查：

1. `**Source:**` 字段里的 `.md` 路径，如 `**Source:** KANBAN/PLAN/v0.8.0-plan.md`；
2. 反引号包裹且**像看板路径**的 `.md`：含 `/`，或以日期开头
   （`KANBAN/PLAN/v0.8.0-plan.md`、`2026-08-18-foo.md` 都算）。

**不算引用**（不触发 R7）：正文裸写的 `foo.md`（如测试场景里"写
report.md"）、通配符（`*.md`）、shell 示例（`git diff … SPEC.md`）。

### 引用怎么写（避免断链与假阳性）

- 引用看板文件时写 **topic**：`KANBAN/PLAN/v0.8.0-plan`（去掉日期、去掉
  `.archived`）。校验器按 topic 匹配，因此日期改名与计划归档都不切断引用。
- 写死的日期路径（`KANBAN/PLAN/2026-08-18-v0.8.0-plan.md`）当前也能通过，
  但归档/改名后会变断链——**新写引用一律用 topic 形式**。
- 固定豁免、无需声明：`README`、`SPEC`、`SPEC.v0.8.legacy`。
- 真正回不去的引用（如历史记录引用迁移前的旧路径）用 `r7_exempt` 显式豁免；
  豁免只对声明它的那个文件生效：

```yaml
---
kind: task
status: completed
r7_exempt: v0.8.0-implementation-plan   # 逗号分隔多个 topic
---
```

优先把引用改回现存 topic，实在改不了才豁免（豁免可审计，写在文件头，
不会隐式放过真正的断链）。

### 典型工作流（每次动看板文件）

1. 新建/编辑/移动文件 → frontmatter 齐全、引用写 topic、终态列写 status。
2. `python3 KANBAN/enforce_filename_dates.py` 同步日期前缀（会改名，
   `--check` 只检查不下手）。
3. `python3 KANBAN/kanban_lint.py` → 必须 **0 violation(s)**；有则按上表修复。
4. 提交前跑 `pytest tests/test_kanban_invariants.py` 确认门禁通过。

## 使用原则

1. **一个 TODO = 一个原子任务**：可单人完成、可验收、无需再拆分。
2. **IN_PROGRESS 有 WIP 上限（默认 3）**：达到上限先完成或移回任务。
3. **PLAN 与 TODO 分离**：PLAN 描述"达成什么、由哪些任务组成、依赖与
   验收门"；TODO 描述"这一个任务怎么做完"。
4. **DONE 与 MILESTONE 只读**：作为项目历史记录，不承担状态流转。
5. **README 是操作与工具手册**：当前看板状态以目录内容为准，不在
   README 里记录状态。