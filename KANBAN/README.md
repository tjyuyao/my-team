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

## 自动工具

```bash
# 检查看板不变量（frontmatter / 命名 / 日期 / 引用 / 版本规则）
python3 KANBAN/kanban_lint.py

# 按文件最后修改日检查/修正文件名日期前缀
python3 KANBAN/enforce_filename_dates.py --check   # 只检查
python3 KANBAN/enforce_filename_dates.py           # 应用改名
```

## 使用原则

1. **一个 TODO = 一个原子任务**：可单人完成、可验收、无需再拆分。
2. **IN_PROGRESS 有 WIP 上限（默认 3）**：达到上限先完成或移回任务。
3. **PLAN 与 TODO 分离**：PLAN 描述"达成什么、由哪些任务组成、依赖与
   验收门"；TODO 描述"这一个任务怎么做完"。
4. **DONE 与 MILESTONE 只读**：作为项目历史记录，不承担状态流转。
5. **README 只放操作原则**：当前看板状态以目录内容为准。