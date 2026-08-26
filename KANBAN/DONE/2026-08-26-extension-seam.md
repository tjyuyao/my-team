---
kind: task
status: rejected
phase: v0.11 post-agent
source: 架构讨论（2026-08-25）；SPEC §5.1/§8；N9 资产边界
priority: high
---

# 扩展接缝与包结构（外部代码怎么进来）
> **废弃（2026-08-25，Owner 定）**：v0.11 计划整体归档（见
> `KANBAN/PLAN/2026-08-25-v0.11.0-plan.archived.md`），本卡随计划
> 一并废弃。原因：重构方案重议——原「增量功能 + 事后结构重排」
> 路线不满足三态质量前提（测试绿 ≠ 结构正确）。本卡内容留档备查，
> 不执行；新方案见 `docs/THREE_STATE_REFACTOR_PLAN.md`。


## 目标

v0.11 定好扩展模式：外部代码（设备 / Agent / 场景）**不经修改
src/my_team** 就能进入系统。用一个「外部风格」场景包证明接缝可用。

## 已定决策（2026-08-25 讨论收敛）

1. **接缝 = Device 基类 + ToolPlugin API + Authority 注册 + 场景包
   manifest + INSTALL_PACKAGE（审计制 N9）**。外部包继承 Device（从核心
   仓 import），声明 manifest，经安装加载。
2. **单仓 monorepo（v0.11）**：`src/my_team`（内核 + 协议 + 标准设备
   暂留）+ `packages/`（标准场景包 + 未来场景包）。**不拆多仓**。
3. **不做包管理器**（不包装 uv）：uv 是工具链，「包」是运行时工件
   （manifest + devices + agents，经 INSTALL_PACKAGE 加载），两者正交。
   分发走无聊方案（pip 包 / git 目录）。
4. **命名**：核心 `my-team`；标准包 `my-team-standard`（或先留仓内）；
   「一人公司」含义放 org 层面，不塞进包名。
5. **标准设备物理外移延后**：等 N1c 落地 + 一个真实外部包证明接缝 +
   核心自举路径设计好之后，再考虑拆出。

## 交付

- 扩展接缝设计文档（外部包结构 + Device 子类化 + 注册 + 安装全链路）；
- 一个「外部风格」场景包，加载一个**非内置设备**，全程不改 src/my_team；
- 单仓布局规范（packages/ 目录约定）。

## 验收标准

- [ ] 外部场景包经 INSTALL_PACKAGE 加载非内置设备并跑通（内核零改动）
- [ ] 未申报可执行条目加载即拒（N9 审计制）
- [ ] 设计文档明确「外部代码怎么进来」全链路（包 → 继承 → 注册 → 安装）

## 依赖

- N9 资产边界（审计制信任）——前置地基
- T13 场景包（包结构）
- N1c（设备边界干净，接缝才有意义）
