# KANBAN 目录使用约定

## 目录结构

```
KANBAN/
├── PLAN/         # 计划与路线图（多任务；不是任务）
├── OPEN_ISSUE/   # 问题、风险、设计审查（尚未确认为任务）
├── TODO/         # 原子任务（可验收、可单人完成）
├── IN_PROGRESS/  # 正在执行的任务（严格 WIP 限制）
├── DONE/         # 已完成任务 / 已关闭问题 / 已完成计划
└── MILESTONE/    # 已发布版本报告（只读历史）
```

## 各目录职责

| 目录 | 放什么 | 不放什么 |
|---|---|---|
| PLAN/ | 版本计划、路线图、多任务实施方案 | 原子任务 |
| OPEN_ISSUE/ | 问题分析、设计审查、风险提示 | 已确认的任务、计划 |
| TODO/ | 单个原子任务，含验收标准 | 计划、问题、报告 |
| IN_PROGRESS/ | 正在做的任务（从 TODO 移入） | 计划、问题 |
| DONE/ | 完成的任务、否决或已转化的问题、完成的计划 | 进行中的任何文件 |
| MILESTONE/ | 已发布版本报告 | 未完成的计划 |

## 文件命名

```text
{YYYY-MM-DD}-{topic}.md
```

- **日期** = 该文件**最后一次被移动**到当前列的日期（即最近一次状态变更的日期）
- **topic** = 小写英文短横线分隔的简短描述
- OPEN_ISSUE 中的长期问题可保留 `OI-XXX-` 前缀，但文件名仍以日期开头

移动文件时，**必须更新文件名中的日期**为当天日期。

## 文件格式

每个 `.md` 文件应包含以下字段（按需调整）：

```markdown
# 标题

**Kind:** plan | issue | task | report   (可选，但推荐)
**Phase:** vX.Y                            (可选，标记所属实现阶段)
**Source:** SPEC §X.Y / OI-XXX            (可选，关联依据)
**Priority:** high/medium/low              (可选)

## 目标
简述此任务要解决的问题。

## 要求 / 规则
具体的技术要求。

## 产出
完成后应交付的内容。

## 验收标准
- [ ] 条件 1
- [ ] 条件 2
```

## 状态流转

```text
PLAN ──拆分──→ TODO ──开始──→ IN_PROGRESS ──完成──→ DONE
  ↑                ↑                     │
  │                └── 阻塞/重规划 ──┘
  └──────── 计划完成后归档 DONE（可选：在 MILESTONE 写报告）

OPEN_ISSUE ──确认可做──→ TODO
OPEN_ISSUE ──否决或已转化──→ DONE
```

### 流转规则

| 动作 | 操作 |
|------|------|
| 计划拆分为任务 | 在 PLAN 中列出 TODO 文件名；每个任务单独写入 `TODO/` |
| 确认问题可转化为任务 | 将问题内容改写为任务文件放入 `TODO/`；原 OPEN_ISSUE 移入 DONE（已转化） |
| 开始执行任务 | **必须**将文件从 `TODO/` 移至 `IN_PROGRESS/`，更新日期 |
| 完成任务 | 将文件从 `IN_PROGRESS/` 移至 `DONE/`，更新日期 |
| 任务阻塞或需要重新计划 | 将文件从 `IN_PROGRESS/` 移回 `TODO/`，更新日期 |
| 任务需要重做 | 将文件从 `DONE/` 移回 `IN_PROGRESS/` 或 `TODO/`，更新日期 |
| 问题被否决或已转化 | 将文件从 `OPEN_ISSUE/` 移至 `DONE/`，更新日期 |
| 计划全部完成 | 将 PLAN 文件移至 `DONE/`，更新日期；版本报告写入 `MILESTONE/` |

### 日期更新示例

```bash
# 2026-08-20 开始执行 p0-write-path-security 任务
mv KANBAN/TODO/2026-08-17-p0-write-path-security.md \
   KANBAN/IN_PROGRESS/2026-08-20-p0-write-path-security.md

# 2026-08-25 完成该任务
mv KANBAN/IN_PROGRESS/2026-08-20-p0-write-path-security.md \
   KANBAN/DONE/2026-08-25-p0-write-path-security.md
```

### 日期前缀自动工具

用脚本按文件最后修改日期自动检查/修正文件名日期前缀：

```bash
# 只检查，不改动
python3 KANBAN/enforce_filename_dates.py --check

# 自动重命名，使文件名以最后修改日期开头
python3 KANBAN/enforce_filename_dates.py
```

## 使用原则

1. **Plan 与 TODO 分离**：
   - PLAN = "我们要达成什么，由哪些任务组成，依赖是什么"。
   - TODO = "这一个任务怎么做完，验收标准是什么"。
   - 不要在 TODO/ 中放计划文件，也不要把多任务计划写成一个 TODO。
2. **一个 TODO = 一个原子任务**：可单人完成、可验收、不需要再拆分。
3. **文件名中的日期反映最近一次状态变更**，而非创建日期。
4. **IN_PROGRESS 必须使用**：
   - 开始执行任务前，先移动文件到 `IN_PROGRESS/`；
   - WIP 上限默认 3；达到上限时不得再开始新任务，除非先完成或
     移回一个任务。
5. **DONE 中的文件保留不动**，作为项目历史记录。
6. **OPEN_ISSUE 是暂存区**：定期清理，将确认的问题改写为 TODO，
   将否决或已转化的移入 DONE。
7. **MILESTONE 只读**：只放已发布版本报告，不承担状态流转。
8. **README 只放操作原则**：当前看板状态以目录内容为准，不在
   README 中维护清单。
