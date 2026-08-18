# CLOSED_ISSUE 目录

存放**已关闭 / 否决 / 已转化的议题**（kind=issue，status=closed）。

- 议题从 `../OPEN_ISSUE/` 关闭时移入本目录，文件名日期更新为当天。
- 常见关闭原因：已确认并转化为 TODO（原议题保留本目录作为决策记录，
  正文注明转化去向）、已明确否决、或已被后续设计取代。
- frontmatter：`kind: issue`、`status: closed` 必填。
- 正文中人的可读叙述（`**Status:**`、`**Opened:**` 等）保留不动；
  frontmatter 的 `status: closed` 供机器校验。
- 本目录只读，不承担状态流转；不再重新打开（确有需要则新开
  OPEN_ISSUE 议题）。