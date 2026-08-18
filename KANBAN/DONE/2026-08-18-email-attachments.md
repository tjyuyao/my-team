---
kind: task
status: completed
phase: v0.10 边界
source: SPEC §4.3、§7.1/§7.4；OI-004 §1.5
priority: high
---

# v0.10-8b: 邮件附件模型与传输（邮件系统侧）


## 目标
邮件可以携带结构化附件引用，收件人上下文能看见附件清单并可读取
被授权附件。

## 要求 / 规则
- `Email` 增加 `attachments: list[AttachmentRef]`；
  `AttachmentRef = {ref_type, path, version, hash, size, mime}`。
- `send_email` 工具支持 attachments 参数。
- 传输机制：
  - SharedKB 附件：直接引用 `path@version`；
  - 私人文件附件：**暂缓**（见设计注记），不做"复制到共享中转区"。
- ContextCompiler 渲染附件清单；`read` 工具可读取被授权附件。

## 设计注记（与扩展表面 trust-boundary 对齐）
原方案"私人文件 → 共享中转区 + 只读授权 + 过期"是**跨主体数据流**，
属静态校验器敏感数据流检查（Customer 数据不进无权 Agent / PrivateStore
不跨 Deployment 泄漏）的管辖范围。该规则未落地前，v1 仅支持 SharedKB
引用式附件；私人文件附件待数据流规则定义后另行设计。

## 产出
- AttachmentRef 模型 + send_email 扩展。
- 授权读取路径（read 工具 + PermissionEngine）。
- ContextCompiler 附件清单渲染。

## 验收标准
- [ ] 邮件可携带 SharedKB 附件引用，收件人上下文可见附件清单
- [ ] 收件人可读取被授权附件；越权附件不可读
- [ ] 私人文件附件未实现（无中转区代码）
- [ ] 新测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过

## 完成注记（2026-08-18，承接 T10 AttachmentRef/AssetStore）

实现要点：
- `models/email.py`：`Email.attachments: list[AttachmentRef]`（SPEC §4.3，
  大内容只存引用）。
- `outbox.py`：`OutboxEntry.attachments` + `stage(..., attachments=)`——附件
  引用随 outbox 条目传至 deliver（回滚丢弃语义同 outbox）。
- 传输链：send_email 工具 attachments 参数 → EMAIL_SEND effect data →
  outbox.stage → `_deliver` create_email(attachments=...) → Email.attachments。
- `SendEmailIntent.attachments` + `action_plan_to_intents` 映射 attachments
  （真实 tick 集成路径）。
- `_build_snapshot` pending_emails 含附件清单（ref_type/path/version/hash/
  size/mime，不含 payload）→ 收件人上下文（ContextCompiler._add_emails 直接
  渲染 snapshot dict）可见附件清单。
- 授权读取：SharedKB 附件（path@version）收件人用 kb_read 读取（经
  PermissionEngine，T8a 已实现）——越权即拒。私人文件附件未实现（无中转区
  代码，符合"暂缓"）。
- 测试 `tests/test_email_attachments.py`（8 个）：AttachmentRef/REST 模型、
  send_email 带附件、outbox 条目携带附件、收件人上下文清单、授权/越权读取、
  outbox 模型往返、真实 tick intent 带附件。全量 850 passed（842+8）；
  mypy clean；ruff 通过；kanban_lint 0。

## 遗留注记（2026-08-19，分析成果固化）

- **ContextCompiler 附件清单端到端渲染断言未做**：本卡只验证 snapshot 含
  清单（`_build_snapshot` pending_emails + `_add_emails` 渲染），未验证
  ContextCompiler 实际产出的上下文文本中含清单。补一个 e2e 断言（收件人
  上下文渲染出清单）即可，小活，并入下一批。

## 验收核对
- [x] 邮件可携带 SharedKB 附件引用，收件人上下文可见附件清单
- [x] 收件人可读取被授权附件（kb_read 经 PermissionEngine）；越权附件不可读
- [x] 私人文件附件未实现（无中转区代码）
- [x] 新测试；`uv run pytest -q` 850 passed；`ruff`/`mypy` 通过
