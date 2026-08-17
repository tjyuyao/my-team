# 邮件排序修正

**Phase:** 6 - 系统集成
**Source:** SPEC §13.3
**Priority:** P1
**Review ref:** 差距 §8.5

## 目标

修正邮件排序以符合 SPEC §13.3。

## SPEC 排序规则

1. 系统通知 (system_notice)
2. 人类邮件 (human_message)
3. 高优先级邮件
4. 截止时间更近的任务
5. 创建时间
6. email_id 字典序

## 当前实现

仅按优先级 + 创建时间 + email_id 排序。

## 修正

更新 `mailbox.py` 中的 `_sort_key` 函数：

```python
def _sort_key(email: Email) -> tuple[int, int, int, int, str]:
    return (
        0 if email.email_type == EmailType.SYSTEM_NOTICE else 1,
        0 if email.email_type == EmailType.HUMAN_MESSAGE else 1,
        _PRIORITY_ORDER.get(email.priority, 2),
        email.created_at_tick,
        email.email_id,
    )
```

## 产出

- 更新 `src/my_team/mailbox.py`
- 更新 `tests/test_mailbox.py`

## 验收标准

- [ ] system_notice 排在最前
- [ ] human_message 排在普通邮件之前
- [ ] 高优先级排在低优先级之前
- [ ] 同优先级按创建时间排序
