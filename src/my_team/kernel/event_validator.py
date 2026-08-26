"""Kernel：事件校验规则（协议层）。

事件 = (source, target, kind, payload)。kind 分两级：
- "system"（内核语义）：payload 须含 command（terminate / install_device /
  uninstall_device）
- "application"（业务语义）：payload 任意，内核不校验
"""

SYSTEM_COMMANDS = {"terminate", "install_device", "uninstall_device"}
KIND_LEVELS = {"system", "application"}


class EventError(Exception):
    """事件不合协议。"""


class EventValidationRule:
    """单条校验规则：validate(event) 非法即抛 EventError。"""

    def validate(self, event):
        raise NotImplementedError


class IsDict(EventValidationRule):
    def validate(self, event):
        if not isinstance(event, dict):
            raise EventError(f"事件必须是 dict，got {type(event).__name__}")


class HasSource(EventValidationRule):
    def validate(self, event):
        source = event.get("source")
        if not isinstance(source, str) or not source:
            raise EventError(f"source 必须是非空字符串，got {source!r}")


class HasTarget(EventValidationRule):
    def validate(self, event):
        target = event.get("target")
        if not isinstance(target, str) or not target:
            raise EventError(f"target 必须是非空字符串，got {target!r}")


class KnownKind(EventValidationRule):
    def validate(self, event):
        kind = event.get("kind")
        if kind not in KIND_LEVELS:
            raise EventError(f"未知 kind 层级: {kind!r}（合法: {sorted(KIND_LEVELS)}）")


class SystemCommand(EventValidationRule):
    """system 层事件必须带已知 command。"""

    def validate(self, event):
        if event.get("kind") != "system":
            return
        command = event.get("payload", {}).get("command")
        if command not in SYSTEM_COMMANDS:
            raise EventError(
                f"未知 system command: {command!r}（合法: {sorted(SYSTEM_COMMANDS)}）"
            )


class SourceRegistered(EventValidationRule):
    """source 必须是已注册进程（防冒充）。registry 为 pid → handle 的注册表。"""

    def __init__(self, registry):
        self.registry = registry

    def validate(self, event):
        if event.get("source") not in self.registry:
            raise EventError(f"source 未注册: {event.get('source')!r}")


class TargetRegistered(EventValidationRule):
    """target 必须是已注册进程（否则校验失败，走 print 路径）。"""

    def __init__(self, registry):
        self.registry = registry

    def validate(self, event):
        if event.get("target") not in self.registry:
            raise EventError(f"target 未注册: {event.get('target')!r}")


DEFAULT_RULES = [IsDict(), HasSource(), HasTarget(), KnownKind(), SystemCommand()]


def validate_event(event, rules=None):
    """按规则集校验事件，任一规则非法即抛 EventError。"""
    for rule in rules or DEFAULT_RULES:
        rule.validate(event)
    return event
