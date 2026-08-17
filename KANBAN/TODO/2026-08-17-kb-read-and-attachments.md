# v0.10-8: KB 读取/检索工具与邮件附件

**Phase:** v0.10 能力
**Source:** SPEC §7.2、§4.3；OI-004 §1.4/§1.5
**Priority:** high

## 目标
Agent 能够读取有权限的知识库内容；邮件可以携带结构化附件引用，
收件人上下文能看见附件清单并可读取被授权附件。

## 要求 / 规则
- 新增 `kb_read`、`kb_list`、`kb_search` 工具及 manifest；
  所有读取经 PermissionEngine。
- `kb_search` 先做关键词/路径匹配，接口预留 embedding 演进。
- `Email` 增加 `attachments: list[AttachmentRef]`；
  `AttachmentRef = {ref_type, path, version, hash, size, mime}`。
- `send_email` 工具支持 attachments 参数；私人文件附件通过
  "发送时复制到共享中转区 + 收件人只读授权 + 过期" 或等价
  机制实现；SharedKB 附件直接引用 path@version。
- ContextCompiler 渲染附件清单；`read` 工具可读取被授权附件。

## 产出
- kb_read/kb_list/kb_search 工具。
- 附件模型与传输机制（先支持 SharedKB 引用 + 私人中转区）。

## 验收标准
- [ ] Agent 可读取其权限范围内的 KB 条目；越权读取被拒
- [ ] kb_search 能按关键词返回条目
- [ ] 邮件附件可被收件人在上下文与 read 工具中访问
- [ ] 越权附件不可读
- [ ] 新测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
