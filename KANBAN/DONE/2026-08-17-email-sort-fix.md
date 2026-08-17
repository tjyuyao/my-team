# 邮件排序修正

**Phase:** 6 - 系统集成
**Source:** SPEC §13.3
**Priority:** P1
**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

- 更新 `mailbox.py` 中的 `_sort_key` 函数
- 排序规则: system_notice > human_message > priority > created_at_tick > email_id

### 验收标准

- [x] system_notice 排在最前
- [x] human_message 排在普通邮件之前
- [x] 高优先级排在低优先级之前
- [x] 同优先级按创建时间排序
