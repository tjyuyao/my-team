---
kind: task
phase: v0.14
source: SPEC.md
priority: medium
---

# 信任边界与三原则落档

## 内容

SPEC.md / AUTHORITY.md 与代码进度对齐：
- 权限主体 = Agent（设备无岗、服务账户范式、委托自带权限）；
- 沙箱与权限解耦（固定矩阵、无 root 越权物化、调用级裁决）；
- 信任边界（信任假设、声明≠事实、不信任清单、审计兜底、推翻触发条件）。

## 验收

- 三原则/信任边界/触发条件写入 SPEC/AUTHORITY.md 且无被推翻旧措辞残留；
- 与代码的最终一致性核对归 sandbox-verification 收口。

## 依赖

—（独立，随时可做）
