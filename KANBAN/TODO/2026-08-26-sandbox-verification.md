---
kind: task
phase: v0.14
source: SPEC.md
priority: high
---

# 沙箱与出站通道验证 + 全量回归

## 内容

- 沙箱故事测试：写不进他人数据区、禁网拒绝、userns/pidns 防杀兄弟、
  bash 命令落数据区、needs_network 声明生效、**不同 position 设备挂载
  矩阵一致（无 per-position 物化）**；
- 出站通道故事：伪造 source 被读侧盖章覆盖；重装同身份后旧出站队列
  注销；
- 全量回归（tmp/check_*.py + demo）PASS；
- 文档与代码一致性收口（提交前；落文档不入卡）。

## 验收

全部通过；文档与代码一致。

## 依赖

data-dir-convention、sandbox-wrapper、network-declaration、bash-sandbox-adapt、
outbound-channel（全部已提交）。
