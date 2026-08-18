---
kind: task
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
