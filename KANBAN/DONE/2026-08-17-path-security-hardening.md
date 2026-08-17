# Path Security Hardening (symlinks, TOCTOU)

**Phase:** 3 - Shared KB
**Source:** SPEC §15.1; review #22; report §7 P2
**Priority:** P2 — Security

## 目标

加固路径安全，防止符号链接攻击和 TOCTOU 竞争。

## 背景

当前仅阻止 `../` 遍历。对于真实文件系统实现，以下攻击路径未处理：

- symlink 指向外部路径
- absolute path 绕过
- nested symlink
- 检查和打开之间的 TOCTOU 竞争
- 文件替换

## 要求

1. 使用 `os.path.realpath()` 验证解析后路径仍在授权范围内
2. 使用 `O_NOFOLLOW` 打开文件（Linux）
3. 添加 symlink/absolute path/nested symlink 测试
4. 添加 `test_symlink_security.py`

## 产出

- [ ] 修改 `private_store.py` 路径解析逻辑
- [ ] 修改 `file_ops.py` 文件打开逻辑
- [ ] 添加 `test_symlink_security.py`

## 验收标准

- [ ] symlink 指向外部路径时被拒绝
- [ ] absolute path 绕过被阻止
- [ ] nested symlink 被检测并拒绝
