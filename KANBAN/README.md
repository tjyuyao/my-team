# KANBAN 目录使用约定

## 目录结构

```
KANBAN/
├── OPEN_ISSUE/   # 已识别但尚未确认为任务的问题
├── TODO/         # 待执行的任务
├── IN_PROGRESS/  # 正在执行的任务
└── DONE/         # 已完成的任务
```

## 文件命名

```text
{YYYY-MM-DD}-{topic}.md
```

- **日期** = 该文件**最后一次被移动**到当前列的日期（即最近一次状态变更的日期）
- **topic** = 小写英文短横线分隔的简短描述

移动文件时，**必须更新文件名中的日期**为当天日期。

## 文件格式

每个 `.md` 文件应包含以下字段（按需调整）：

```markdown
# 标题

**Phase:** N - 阶段名称        (可选，标记所属实现阶段)
**Source:** SPEC §X.Y          (可选，关联 SPEC 章节)
**Priority:** high/medium/low  (可选)

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
OPEN_ISSUE → TODO → IN_PROGRESS → DONE
```

### 流转规则

| 动作 | 操作 |
|------|------|
| 确认问题可转化为任务 | 将文件从 `OPEN_ISSUE/` 移至 `TODO/`，更新日期 |
| 开始执行任务 | 将文件从 `TODO/` 移至 `IN_PROGRESS/`，更新日期 |
| 完成任务 | 将文件从 `IN_PROGRESS/` 移至 `DONE/`，更新日期 |
| 任务需要重新处理 | 将文件从 `DONE/` 移回 `IN_PROGRESS/` 或 `TODO/`，更新日期 |
| 问题被否决 | 将文件从 `OPEN_ISSUE/` 移至 `DONE/`，更新日期 |

### 日期更新示例

```bash
# 2026-08-20 开始执行 agent-hierarchy 任务
mv KANBAN/TODO/2026-08-17-agent-hierarchy.md \
   KANBAN/IN_PROGRESS/2026-08-20-agent-hierarchy.md

# 2026-08-25 完成该任务
mv KANBAN/IN_PROGRESS/2026-08-20-agent-hierarchy.md \
   KANBAN/DONE/2026-08-25-agent-hierarchy.md
```

## 使用原则

1. **一个文件 = 一个原子任务**。如果任务过大，拆分为多个文件。
2. **文件名中的日期反映最近一次状态变更**，而非创建日期。
3. **IN_PROGRESS 应尽量保持精简**——同一时间并行进行的任务不宜过多。
4. **DONE 中的文件保留不动**，作为项目历史记录。
5. **OPEN_ISSUE 是暂存区**——定期清理，将确认的问题转入 TODO，将否决的转入 DONE。
