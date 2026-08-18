---
kind: task
phase: v0.10 能力
source: SPEC §7.2；OI-004 §1.4
priority: high
---

# v0.10-8a: KB 读取/检索工具（知识库侧）


## 目标
Agent 能够读取有权限的知识库内容（能力层新增只读工具）。

## 要求 / 规则
- 新增 `kb_read`、`kb_list`、`kb_search` 工具及 manifest；
  所有读取经 PermissionEngine。
- `kb_search` 先做关键词/路径匹配，接口预留 embedding 演进。
- 纯增量：只碰 SharedKB 读取路径与工具注册，不改邮件/上下文系统。

## 产出
- kb_read/kb_list/kb_search 工具 + manifest + 测试。

## 验收标准
- [ ] Agent 可读取其权限范围内的 KB 条目；越权读取被拒
- [ ] kb_search 能按关键词返回条目
- [ ] 新测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
