---
kind: task
phase: v0.14
source: SPEC.md
priority: high
---

# 数据区约定落地（data/<identity>）

## 内容

设备数据区 = 其源码所在 workdir 的 `data/<identity>/`（约定即默认，零配置）：
- agent：workdir 已有（私有区）；必要时建 `workdir/data/` 自数据子区；
- 设备：安装时从 `source_file` 推导 workdir → `data/<identity>`，装载时
  确保目录存在并归属该设备。

## 验收

- 装载时校验 identity 不含 `/` 与 `..`（防 `data/` 绑定根逃逸）；
- 安装设备后 `workdir/data/<identity>/` 存在且仅该设备进程可写（沙箱卡
  落地后由挂载矩阵强制）；
- 访问矩阵文档化：系统路径只读、自己数据区读写、他人不可见、源码只读。

## 依赖

—（前置卡，被 sandbox-wrapper 消费）
