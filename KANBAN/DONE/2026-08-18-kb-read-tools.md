---
kind: task
status: completed
phase: v0.10 能力
source: SPEC §7.2；OI-004 §1.5
priority: high
---

# v0.10-8a: KB 读取/检索工具（知识库侧）

依赖：`KANBAN/DONE/tool-plugin-and-manifest-tools`（T7，已完成——新工具走
`register_tool(manifest, handler)` 路径，manifest 自动生成 LLM 工具定义）。

## 目标
Agent 能够读取有权限的知识库内容（能力层新增只读工具），并支持关键词
检索。本质是"补齐 Agent 对 SharedKB 的读侧能力"：此前只有 `kb_write`
（OI-004 §1.5 的缺口：只能写、不能读）。

## 现状（执行前应确认的事实）
- `SharedKB.read(path, agent_id)` / `list_dir(path, agent_id)` **已存在且已带
  PermissionEngine 检查**（`PermissionOp.READ` / `LIST`），越权抛
  `SharedKBWriteError`；PermissionEngine 支持精确/前缀/`*` 通配。
- 缺口在工具层：无 `kb_read`/`kb_list`/`kb_search` manifest 与 handler；
  无关键词搜索能力（SharedKB 无 search 方法）。
- 审计事件 `AuditEventType.SHARED_KB_READ` 已定义（audit.py），读侧尚未使用。
- 内置工具注册已迁移到 `self.register_tool(...)`（T7），新工具照此办理。

## 要求 / 规则
- 新增 `kb_read`、`kb_list`、`kb_search` 三个工具及 manifest；**所有读取、
  检索必须经 PermissionEngine**（SPEC §7.2 硬性要求）。
- 三者均为 `ExecutionClass.READ_ONLY`：deterministic、idempotent、无 effect、
  不进 pending op 路径（与 `read`/`ls` 同级；不得误用 STAGED_MUTATION）。
- manifest 命名：capabilities 用 `kb:read` / `kb:list` / `kb:search`；
  filesystem_scopes=("shared-kb",)；description 与 required_inputs 齐全
  （T7 约定，LLM 工具定义由此生成）。
- `kb_search` 先做关键词/路径匹配，接口预留 embedding 演进（SPEC §7.2）。
- 读操作记录审计 `shared_kb.read`（记 agent_id/tick/path，不记内容）。
- 纯增量：只碰 SharedKB 读取路径与工具注册，不改邮件/上下文系统、不改
  KB 写路径与锁/版本机制。

## 设计决策（已定，勿在执行时重开）
1. **search 落在 SharedKB**：新增 `SharedKB.search(query, agent_id,
   base_path="", limit=20)`，权限过滤由 SharedKB 内部完成（与 read/list 的
   权限检查同源）。handler 只做参数透传与结果包装。
2. **search 的越权语义**：候选集 = `base_path` 前缀下 `exists` 且
   PermissionEngine 允许 READ 的路径；**无权条目既不匹配也不出现在结果中**
   （不泄露"存在但无权"信息，与 deny-by-default 一致）。
3. **匹配口径（v1）**：query 大小写不敏感子串匹配 path 或 content；
   命中上限 `limit`（默认 20）防输出膨胀；返回
   `[{path, version, snippet(前 200 字符), last_modified_by,
   last_modified_at_tick}]`，不返回全文（全文走 kb_read）。
4. **错误语义（v1）**：区分 `permission denied` 与 `not found`（沿用 SharedKB
   现有异常的区分）。KB 是团队内协作知识库，Agent 是协作主体而非攻击者，
   区分更利于排障与验收断言；如未来面向外部主体再收敛为不区分。
5. **不在本卡**（明确排除）：条目类型与轻量元数据（tags/terms/owner_task）、
   ContextCompiler 的 KB 术语自动注入（glossary injector）、embedding 检索、
   KB 写/锁/版本改动。前述属 v0.11（SPEC §7.2 / OI-004 §1.5 的注入侧），
   本卡只交付"读侧工具"这一半。
6. **下载无一等工具**："下载到本地" = Agent 显式两步组合：`kb_read`
   返回 content（纯读取，READ_ONLY 无副作用）→ `write` 写入自己
   PrivateStore（v0.8 已有）。不新增 kb_download 类工具——读取即落盘
   会引入隐式副作用，违背"引用 + 权限、不隐式拷贝"原则。Asset 对象侧
   的下载同样靠组合（T10 的 get → 私有写），二进制私有写缺口归 T10
   （其验收"二进制文件可写入"已覆盖）。

## 产出
- `SharedKB.search()` 方法与权限过滤单元测试。
- `tool_manifest.py` 新增 3 个 manifest；`simulation.py` 内置工具区新增
  3 个 handler（走 `self.register_tool`），内置工具总数 12 → 15。
- 读取审计接入（kb_read / kb_search 命中记 `shared_kb.read`）。

## 实施步骤（执行顺序）
1. `shared_kb.py`：实现 `search()`（决策 2/3）；补单元测试（直接构造
   SharedKB + PermissionEngine 规则）。
2. `tool_manifest.py`：3 个 manifest（READ_ONLY、capabilities、required_inputs、
   description、max_output_bytes 上限）。
3. `simulation.py`：3 个 handler——`kb_read` → `shared_kb.read`；`kb_list` →
   `list_dir`；`kb_search` → `shared_kb.search`；各自包装 ToolResult
   （success=False + 错误消息）。审计记录接入。
4. 验证：`manifest_to_tool_definition` 自动覆盖 15 个工具；新增功能测试 +
   tick 集成测试；`uv run pytest -q` 全量绿（注意 `UV_CACHE_DIR` 指 workspace
   内 tmp/uv-cache，见运行时环境）；ruff/mypy 通过。
5. 按看板纪律移动 TODO → DONE 并写完成卡。

## 验收标准
- [ ] Agent 可读取其权限范围内的 KB 条目；越权读取被拒（success=False）
- [ ] kb_list 只列出权限范围内条目；越权前缀被拒
- [ ] kb_search 能按关键词返回条目（path 与 content 均命中、大小写不敏感、
      有命中上限）；**无权条目绝不出现在结果中**
- [ ] 三工具为 READ_ONLY，经 register_tool 注册；LLM 工具定义自动生成
      （内置工具 12 → 15）；读取记 `shared_kb.read` 审计
- [ ] 纯增量确认：KB 写路径/邮件/上下文系统零改动，无 embedding/注入代码
- [ ] 新测试（含 tick 集成）；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
## 完成注记（2026-08-18）

实现要点：
- `shared_kb.py`：`SharedKB.search(query, agent_id, base_path="", limit=20)`——
  候选集 = base_path 前缀下 exists 且 READ 允许的条目；无权条目不匹配不出现在
  结果（deny-by-default，无"存在但无权"泄露）；大小写不敏感子串匹配 path 或
  content；返回 metadata + snippet（前 200 字符），不返回全文。
- `tool_manifest.py`：`kb_read`/`kb_list`/`kb_search` 三个 READ_ONLY manifest
  （capabilities `kb:read`/`kb:list`/`kb:search`，filesystem_scopes=
  ("shared-kb",)，input/output schema 与 required_inputs 齐全）；内置工具
  12 → 15。
- `simulation.py`：三个 handler 走 `self.register_tool`；`kb_read`/`kb_search`
  命中记 `shared_kb.read` 审计（agent_id/tick/path，不含内容）；错误语义区分
  `permission_denied`（SharedKBWriteError.reason 前缀判别）与 `not_found`/
  `INVALID_ARGUMENT`。
- `agent_runtime.py`：`action_plan_to_intents` 的 SubmitToolRequest 白名单补
  kb_read/kb_list/kb_search（真实 tick 集成路径通过）。
- 已确认的既有语义（如实记录）：`list_dir`/`read` 的权限检查在请求路径本身；
  `PermissionEngine` 通配作用域（`a/*`）覆盖 `a/` 之下但**不含根 `a`**——
  kb_list 列根需精确作用域规则（测试已按此写）。
- 测试：`tests/test_kb_read_tools.py`（14 个：读/越权拒/not_found/审计无内容/
  list 作用域/越权前缀拒/search 大小写不敏感/无权不泄露+正向对照/snippet 非
  全文/limit 与 base_path/空 query/READ_ONLY manifest/真实 tick 读取）+
  `test_shared_kb.py` 新增 TestSharedKBSearch（5 个单元测试）；工具数量断言
  12 → 15 更新（test_tool_plugin / test_tool_manifest / test_restricted_tools）。
- 全量 820 passed；mypy clean；ruff 通过；kanban_lint 0 violation。
- 纯增量确认：KB 写/锁/版本、邮件、上下文系统零改动；无 embedding/注入代码
  （v0.11 范围）。

## 验收核对
- [x] Agent 可读取其权限范围内的 KB 条目；越权读取被拒（success=False）
- [x] kb_list 只列出权限范围内条目；越权前缀被拒
- [x] kb_search 关键词返回条目（path 与 content 均命中、大小写不敏感、有命中
      上限）；无权条目绝不出现在结果中
- [x] 三工具为 READ_ONLY，经 register_tool 注册；LLM 工具定义自动生成
      （内置工具 12 → 15）；读取记 `shared_kb.read` 审计
- [x] 纯增量确认：KB 写路径/邮件/上下文系统零改动，无 embedding/注入代码
- [x] 新测试（含 tick 集成）；`uv run pytest -q` 820 passed；`ruff`/`mypy` 通过
