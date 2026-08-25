---
kind: task
status: rejected
phase: v0.10 收尾（T8b 补强）
source: KANBAN/DONE/email-attachments 遗留注记（2026-08-19）
priority: low
---

# v0.10-f1: ContextCompiler 附件清单端到端渲染断言（T8b 补强）

> **否决（2026-08-25）**：不做了。邮件系统可能重新实现，附件渲染断言
> 随邮件系统重做一起处理，不再单独补。

## 目标
补 T8b 的测试覆盖缺口：收件人上下文（ContextCompiler 实际产出）含附件清单
的端到端断言。

## 背景 / 现状事实
- T8b 验收项「收件人上下文可见附件清单」以 snapshot 层判据通过
  （`test_attachment_manifest_visible_in_snapshot` 断言 `snapshot["emails"]`
  结构：含 attachments 清单、无 content）。
- `ContextCompiler._add_emails` 整体 `str()` 渲染 email dict（attachments
  字段随行）——功能在工作，但**无测试断言** `result["emails"]` 含清单。
- 性质：测试覆盖缺口，非功能缺陷；小活。

## 要求 / 规则
- 断言 ContextCompiler 产出（`result["emails"]`）含附件清单、不含 payload
  （content 字段不泄漏）。
- 不改变功能路径。

## 产出
- 测试补强用例。

## 验收标准
- [ ] 端到端断言通过（ContextCompiler 产出含清单且无 payload）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
