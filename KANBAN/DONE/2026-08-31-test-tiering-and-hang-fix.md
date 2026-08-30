---
kind: task
status: completed
phase: v0.14-fix
source: SPEC.md
priority: high
---

# 测试分层 + 挂起根因修复（含维护流程 4 处隐藏 bug）

**背景**：v0.14 报告称 "14 passed / 全量回归 PASS"，实测单测可挂起数分钟，
且断言是"空心"的（注册簿含身份，设备进沙箱即崩却仍 PASS）。v0.14 报告对
测试健康的表述过乐观，此处为记录纠正。

## 根因（挂几分钟不是"慢"，是"挂"）

1. **进程泄漏卡解释器退出**（主因）：维护测试内联创建 AgentOS 从不
   terminate 进程；泄漏的非 daemon 子进程在 pytest 退出时被
   `multiprocessing._exit_function` 无限 join。
2. **reader 无进程死亡兜底**：bwrap 命名空间链的后代可能持住 child 端 fd，
   纯 EOF 不可达，`_read_loop` 永久阻塞。

## 修复

- `process_handle._read_loop`：`poll(0.2)` 限时 + `is_alive()` 兜底，进程
  死亡即退出（后代持 fd 场景不再堵死）。
- 集成测试补显式 teardown（terminate 全部用户态进程）。
- **维护流程 4 处隐藏 bug**（被空心测试掩盖）：
  1. `actor_entity._load_spec` 访问不存在的 ProcessHandle 属性（设备身份
     判定永远崩，被 try/except 吞掉）→ 改为 `._process._load_spec`。
  2. 维护进程注册 `position=None` → register 强拒 → 改从 Authority
     `_auth_context` 取 position。
  3. `spawn_maintenance` 为 async，register 需同步 spawn → 改同步。
  4. 卸载撤销全部设备布线含 maintain → 维护授权随卸载消失 → 卸载时保留
     maintain scope（Authority 新增 `device_grants_request`，token 解释留
     kernel）。

## 测试改造

- 快层（T1，进程内，`-m "not integration"`）：15 测 0.22s。新增
  `test_maintenance_authorization.py`（维护拒绝路径，零 spawn）。
- 集成层（T2，`-m integration`）：3 测。设备端到端往返
  `test_device_e2e.py`（spawn→serve→respond→读侧盖章→总线，断言真实
  pong）；维护测试改真实设备协议 + 真实断言。
- CI `quick` job 原 `--ignore=tests/integration` 指向不存在的目录（分层
  从未落地）→ 改 `-m "not integration"`。

## 结果

- 全量 18 passed ≈ 3s（原：单测可挂起数分钟）。
- ruff / mypy / kanban_lint 全绿。
- SPEC「测试原则」重写为双层策略。
