---
kind: issue
status: open
phase: v0.14
priority: medium
---

# 设备生命周期管理（安装/升级/灰度/分发）

## 背景

v0.14 实现了最小的 config 声明 + 自动安装，解决了 bash/llm 的开发可用性。
但设备的完整生命周期管理（升级、数据迁移、灰度发布、内核源码分发）仍是开放问题。

## 开放问题

### 1. 设备升级
- 当前：设备被覆盖式安装（复制 device.py 到 home 目录）
- 需要：版本管理、升级脚本、数据迁移路径
- 设计考虑：升级时是否需要卸载→迁移→重载的完整生命周期？

### 2. 灰度发布
- 当前：设备全局安装
- 需要：按 agent/position/percentage 灰度
- 设计考虑：灰度状态存储在哪里？Authority 还是设备配置？

### 3. 内核源码分发
- 当前：PYTHONPATH 继承宿主路径
- 需要：打包为 wheel/package，或在 chroot 内映射到 /lib/my_team/
- 设计考虑：沙箱内源码访问方式是否需要改变？

### 4. root 管理接口
- 当前：install_device 事件支持 root 权限
- 需要：统一的 root 管理接口（安装/卸载/升级/回滚）
- 设计考虑：是否需要 CLI 工具？

## 依赖

- v0.14 设备私家与维护会话设计
- v0.15 executable core

## 参考

- SPEC.md §4 设计取向（"一切皆数据"）
- KANBAN/DONE/2026-08-29-device-private-home-correction.md
