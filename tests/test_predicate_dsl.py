"""predicate DSL 三级分级测试（v0.11 N7，KANBAN/IN_PROGRESS/2026-08-24）。

覆盖验收标准：
  1. 任意 predicate 纯、有限、可审计、可重放
  2. transition when/gate 判定无法逃逸到网络/文件写/直接读当前时间/随机
  3. L0 表达式确定性重放（同输入同输出，无副作用）
  4. 越级谓词（L2 伪装）被静态校验拒绝并提示改为 Tool/op
  5. 最小测试向量段：transition `when` 用 L0 谓词驱动流程 通过

SPEC 引用：§3.4 执行真理（三级边界）；§3.8 容量（固定超时/资源上限）。
"""

from __future__ import annotations

import copy
import json

import pytest

from my_team import predicate as pred_mod
from my_team.predicate import (
    PredicateEscalationError,
    PredicateEvaluation,
    PredicateSyntaxError,
    PredicateTypeError,
    canonical,
    evaluate,
    evaluate_traced,
    parse,
    replay_key,
    run_l1_predicate,
    validate,
    validate_l1_python,
)
from my_team.python_worker import run_python_compute

# ---------------------------------------------------------------------------
# L0 文本语法
# ---------------------------------------------------------------------------


class TestL0Grammar:
    def test_parse_comparisons(self) -> None:
        for op in ("==", "!=", "<", "<=", ">", ">="):
            assert parse(f'status {op} 3') == [op, "status", 3]
        assert parse('name == "done"') == ["==", "name", "done"]
        assert parse('score >= 2.5') == [">=", "score", 2.5]
        assert parse("flag == true") == ["==", "flag", True]
        assert parse("flag == false") == ["==", "flag", False]

    def test_parse_dotted_field_and_neg_number(self) -> None:
        assert parse("a.b.c == 1") == ["==", "a.b.c", 1]
        assert parse("delta > -5") == [">", "delta", -5]

    def test_parse_in_set(self) -> None:
        assert parse('status in {"done", "todo"}') == [
            "in", "status", ["done", "todo"],
        ]
        assert parse('status in {"done"}') == ["in", "status", ["done"]]

    def test_parse_quantifiers(self) -> None:
        assert parse('all(items, status == "done")') == [
            "all", "items", ["==", "status", "done"],
        ]
        assert parse('any(items, score > 1)') == [
            "any", "items", [">", "score", 1],
        ]
        assert parse("exists(owner)") == ["exists", "owner"]
        assert parse("count(items) > 2") == ["count", "items", ">", 2]
        assert parse("count(items) >= 0") == ["count", "items", ">=", 0]

    def test_parse_and_or_not_nested(self) -> None:
        assert parse('not (status == "pending")') == [
            "not", ["==", "status", "pending"],
        ]
        assert parse('phase == "a" and count(items) > 0') == [
            "and", ["==", "phase", "a"], ["count", "items", ">", 0],
        ]
        assert parse('phase == "a" or phase == "b"') == [
            "or", ["==", "phase", "a"], ["==", "phase", "b"],
        ]
        assert parse('and(a == 1, or(b == 2, not(c == 3)))') == [
            "and", ["==", "a", 1], ["or", ["==", "b", 2],
                                    ["not", ["==", "c", 3]]],
        ]

    def test_parse_element_self_reference(self) -> None:
        assert parse("any(numbers, . > 10)") == [
            "any", "numbers", [">", ".", 10],
        ]

    def test_parse_string_escapes(self) -> None:
        assert parse(r"name == 'it\'s'") == ["==", "name", "it's"]
        assert parse(r'name == "a\"b"') == ["==", "name", 'a"b']
        assert parse(r"name == 'a\nb'") == ["==", "name", "a\nb"]

    def test_parse_rejects_escalation_constructs_with_hint(self) -> None:
        # 越级构造在语法层即被拒绝，消息带 L2 Tool/op 提示
        for text in (
            "import os",
            "socket.connect(1)",
            "exec(code)",
            "open('/etc/passwd')",
            "random()",
        ):
            with pytest.raises(PredicateSyntaxError) as ei:
                parse(text)
            msg = str(ei.value)
            assert "Tool/PendingOperation" in msg and "SPEC §3.4" in msg

    def test_parse_rejects_malformed(self) -> None:
        for text in ("", "   ", "status ==", "== 1", "status == 'x' extra",
                     "and()", "not(a == 1, b == 2)", "count(items) > x",
                     "count(items) >", "status in {}", "status in {a}",
                     "status in {'a'", "a == "):
            with pytest.raises(PredicateSyntaxError):
                parse(text)

    def test_parse_parenthesized_group(self) -> None:
        assert parse("(status == 'a') and phase == 'b'") == [
            "and", ["==", "status", "a"], ["==", "phase", "b"],
        ]

    def test_parse_infix_chain_and_functional_form_agree(self) -> None:
        # 中缀与函数式两种写法语义等价 ⇒ 同 canonical / 同 replay key
        a = parse('a == 1 and b == 2 and c == 3')
        b = parse('and(a == 1, b == 2, c == 3)')
        assert canonical(a) == canonical(b)
        assert replay_key(a) == replay_key(b)

    def test_parse_accepts_keyword_like_field_names(self) -> None:
        # and/or/not/count 等拼写可作字段名（非函数调用位置）
        assert parse("count > 3") == [">", "count", 3]
        assert parse("and == 1") == ["==", "and", 1]


# ---------------------------------------------------------------------------
# L0 求值语义
# ---------------------------------------------------------------------------


class TestL0Evaluation:
    def test_equality_strict_bool_vs_number(self) -> None:
        # bool 与 int 严格区分（True != 1），避免 Python 隐式相等
        assert evaluate(["==", "flag", True], {"flag": True}) is True
        assert evaluate(["==", "flag", True], {"flag": 1}) is False
        assert evaluate(["!=", "flag", True], {"flag": 1}) is True
        assert evaluate(["==", "n", 1.0], {"n": 1}) is True  # 数值相等

    def test_ordering_numbers_only(self) -> None:
        assert evaluate([">", "score", 0.5], {"score": 0.9}) is True
        assert evaluate(["<=", "n", 2], {"n": 2}) is True
        with pytest.raises(PredicateTypeError):
            evaluate(["<", "name", "b"], {"name": "a"})  # 字符串排序不支持

    def test_in_set_membership(self) -> None:
        st = {"status": "done"}
        assert evaluate(["in", "status", ["done", "todo"]], st) is True
        assert evaluate(["in", "status", ["pending"]], st) is False

    def test_missing_field_fail_closed(self) -> None:
        st: dict[str, object] = {}
        for node in (
            ["==", "x", 1],
            ["!=", "x", 1],
            [">", "x", 1],
            ["in", "x", [1]],
            ["count", "x", ">", 0],
            ["all", "x", ["==", "y", 1]],
            ["any", "x", ["==", "y", 1]],
            ["exists", "x"],
        ):
            assert evaluate(node, st) is False, node
            trace = evaluate_traced(node, st)
            assert trace.steps[-1].missing is True, node

    def test_exists_distinguishes_presence(self) -> None:
        assert evaluate(["exists", "owner"], {"owner": None}) is True
        assert evaluate(["exists", "owner"], {}) is False

    def test_quantifier_element_scope(self) -> None:
        st = {"items": [{"status": "done"}, {"status": "done"}]}
        assert evaluate(
            ["all", "items", ["==", "status", "done"]], st,
        ) is True
        st2 = {"items": [{"status": "done"}, {"status": "todo"}]}
        assert evaluate(
            ["all", "items", ["==", "status", "done"]], st2,
        ) is False
        assert evaluate(
            ["any", "items", ["==", "status", "done"]], st2,
        ) is True

    def test_quantifier_dot_self_reference(self) -> None:
        st = {"numbers": [1, 2, 3]}
        assert evaluate(["any", "numbers", [">", ".", 2]], st) is True
        assert evaluate(["all", "numbers", [">", ".", 0]], st) is True

    def test_nested_quantifier(self) -> None:
        st = {"groups": [{"items": [{"ok": True}]}, {"items": [{"ok": True}]}]}
        node = ["all", "groups", ["all", "items", ["==", "ok", True]]]
        assert evaluate(node, st) is True
        st2 = {"groups": [{"items": [{"ok": True}]}, {"items": [{"ok": False}]}]}
        assert evaluate(node, st2) is False

    def test_empty_list_quantifiers(self) -> None:
        st: dict[str, object] = {"items": []}
        assert evaluate(["all", "items", ["==", "x", 1]], st) is True  # 空全称
        assert evaluate(["any", "items", ["==", "x", 1]], st) is False

    def test_count_and_combinators(self) -> None:
        st = {"items": [1, 2, 3], "phase": "run"}
        assert evaluate(["count", "items", "==", 3], st) is True
        assert evaluate(["count", "items", "<", 2], st) is False
        assert evaluate(
            ["and", ["==", "phase", "run"], ["count", "items", ">", 0]], st,
        ) is True
        assert evaluate(
            ["or", ["==", "phase", "idle"], ["count", "items", ">", 0]], st,
        ) is True
        assert evaluate(["not", ["==", "phase", "idle"]], st) is True

    def test_type_errors(self) -> None:
        with pytest.raises(PredicateTypeError):
            evaluate(["count", "x", ">", 1], {"x": "abc"})  # 非列表
        with pytest.raises(PredicateTypeError):
            evaluate(["all", "x", ["==", "y", 1]], {"x": "abc"})
        with pytest.raises(PredicateTypeError):
            evaluate(["==", "x", 1], {"x": [1, 2]})  # 列表字段 == 不支持

    def test_state_type_must_be_dict(self) -> None:
        with pytest.raises(PredicateTypeError):
            evaluate(["==", "x", 1], [1, 2])  # type: ignore[arg-type]

    def test_evaluate_accepts_text(self) -> None:
        assert evaluate('status == "done"', {"status": "done"}) is True


# ---------------------------------------------------------------------------
# 验收 1 + 3：纯、有限、可审计、可重放、确定性
# ---------------------------------------------------------------------------


class TestL0PurityReplayAudit:
    def test_evaluate_is_pure_no_state_mutation(self) -> None:
        state = {"items": [{"status": "done"}], "n": 3}
        before = copy.deepcopy(state)
        node = parse('count(items) > 0 and all(items, status == "done")')
        for _ in range(3):
            evaluate(node, state)
        assert state == before

    def test_deterministic_replay_same_input_same_output(self) -> None:
        state = {"items": [{"status": "done"}, {"status": "todo"}]}
        node = parse('count(items) > 1 and any(items, status == "done")')
        assert evaluate(node, state) is evaluate(node, state)
        t1 = evaluate_traced(node, state)
        t2 = evaluate_traced(node, state)
        assert t1.value == t2.value
        assert t1.steps == t2.steps
        assert t1.replay_key == t2.replay_key

    def test_replay_key_stable_across_in_set_order(self) -> None:
        k1 = replay_key('status in {"a", "b", "c"}')
        k2 = replay_key('status in {"c", "a", "b"}')
        assert k1 == k2
        assert replay_key('status in {"a", "a"}') == replay_key('status in {"a"}')

    def test_parse_twice_same_canonical(self) -> None:
        text = 'phase == "x" and count(items) > 0'
        assert canonical(parse(text)) == canonical(parse(text))
        assert replay_key(text) == replay_key(parse(text))

    def test_canonical_is_json_serializable(self) -> None:
        node = parse('phase == "x" and status in {"a", "b"}')
        json.dumps(canonical(node))  # 可缓存/可审计

    def test_audit_trace_records_steps_and_first_failure(self) -> None:
        state = {"phase": "collect", "items": [{"status": "todo"}]}
        node = parse(
            'phase == "collect" and count(items) > 0 and '
            'all(items, status == "done")',
        )
        trace = evaluate_traced(node, state)
        assert trace.value is False
        assert trace.steps, "trace 必须有步骤"
        assert trace.first_failure is not None
        failing = trace.first_failure
        assert failing is not None and failing.path == "status"
        assert failing.op == "==" and failing.result is False

    def test_finite_deep_nesting_terminates(self) -> None:
        # 有限：语法封闭、结构递归，深嵌套必然终止
        node: pred_mod.PredicateNode = ["==", "x", 1]
        for _ in range(250):
            node = ["not", node]
        assert evaluate(node, {"x": 1}) is True  # 偶数层 not
        assert evaluate(node, {"x": 2}) is False

    def test_validate_ok_record(self) -> None:
        v = validate('phase == "x" and count(items) > 0')
        assert v.ok and v.level == "L0"
        assert v.replay_key == replay_key('phase == "x" and count(items) > 0')
        assert "==" in v.ops and "count" in v.ops
        assert "phase" in v.fields and "items" in v.fields


# ---------------------------------------------------------------------------
# 验收 2 + 4：无逃逸、越级拒绝（L2 伪装）
# ---------------------------------------------------------------------------


class TestEscalationRejection:
    def test_validate_rejects_unknown_op_with_l2_hint(self) -> None:
        v = validate(["import", "os"])
        assert v.ok is False
        assert "Tool/PendingOperation" in v.errors[0]
        assert "SPEC §3.4" in v.errors[0]

    def test_validate_rejects_escalation_like_ops(self) -> None:
        for node in (
            ["socket.connect", "1.2.3.4", 80],
            ["open", "/etc/passwd", "w"],
            ["random.random"],
            ["time.time"],
            ["subprocess.run", ["ls"]],
            ["exec", "code"],
        ):
            v = validate(node)
            assert v.ok is False, node
            assert "Tool/PendingOperation" in v.errors[0], node

    def test_evaluate_raises_escalation_on_unknown_op(self) -> None:
        with pytest.raises(PredicateEscalationError) as ei:
            evaluate(["import", "os"], {})
        assert "Tool/PendingOperation" in str(ei.value)

    def test_validate_rejects_invalid_fields_and_arity(self) -> None:
        assert validate(["==", "a..b", 1]).ok is False
        assert validate(["==", ".", 1]).ok is False  # "." 只能在量化内
        assert validate(["==", "a"]).ok is False
        assert validate(["==", "a", 1, 2]).ok is False
        assert validate(["not", ["==", "a", 1], ["==", "b", 2]]).ok is False
        assert validate(["in", "a", []]).ok is False  # 空集合
        assert validate(["count", "a", ">", "x"]).ok is False

    def test_validate_dot_allowed_inside_quantifier(self) -> None:
        assert validate(["any", "items", [">", ".", 1]]).ok is True


class TestL1PythonBoundary:
    @pytest.mark.parametrize("source,cap", [
        ("import socket\nresult = predicate(inputs)\n", "network"),
        ("import urllib.request\nresult = predicate(inputs)\n", "network"),
        ("import requests\nresult = predicate(inputs)\n", "network"),
        ("import os\nresult = predicate(inputs)\n", "file_write"),
        ("import subprocess\nresult = predicate(inputs)\n", "file_write"),
        ("import pathlib\nresult = predicate(inputs)\n", "file_write"),
        ("import random\nresult = predicate(inputs)\n", "random"),
        ("import secrets\nresult = predicate(inputs)\n", "random"),
        ("import uuid\nresult = predicate(inputs)\n", "random"),
        ("import time\nresult = predicate(inputs)\n", "clock"),
        ("import datetime\nresult = predicate(inputs)\n", "clock"),
    ])
    def test_l1_rejects_forbidden_module_imports(
        self, source: str, cap: str,
    ) -> None:
        v = validate_l1_python(source)
        assert v.ok is False
        assert cap in v.capabilities
        assert "Tool/PendingOperation" in v.errors[0]
        assert "SPEC §3.4" in v.errors[0]

    @pytest.mark.parametrize("source,cap", [
        ("import os as o\no.remove('x')\nresult = predicate(inputs)\n",
         "file_write"),
        ("from os import remove\nremove('x')\nresult = predicate(inputs)\n",
         "file_write"),
        ("from random import randint\nresult = randint(0, 1)\n"
         "result = predicate(inputs)\n",
         "random"),
        ("def predicate(inputs):\n"
         "    import time\n"
         "    return time.time() > 0\n"
         "result = predicate(inputs)\n",
         "clock"),
    ])
    def test_l1_rejects_alias_and_inner_import_evasion(
        self, source: str, cap: str,
    ) -> None:
        v = validate_l1_python(source)
        assert v.ok is False
        assert cap in v.capabilities

    @pytest.mark.parametrize("source,cap", [
        ("def predicate(inputs):\n"
         "    return open('/etc/passwd').read()\n"
         "result = predicate(inputs)\n",
         "file_write"),
        ("def predicate(inputs):\n"
         "    return eval('1 + 1')\n"
         "result = predicate(inputs)\n",
         "dynamic_code"),
        ("def predicate(inputs):\n"
         "    return __import__('os').getpid()\n"
         "result = predicate(inputs)\n",
         "dynamic_code"),
    ])
    def test_l1_rejects_builtin_escalation(
        self, source: str, cap: str,
    ) -> None:
        v = validate_l1_python(source)
        assert v.ok is False
        assert cap in v.capabilities

    @pytest.mark.parametrize("source", [
        "result = True\n",  # 无 predicate 函数
        "def predicate(a, b):\n    return True\nresult = predicate(1, 2)\n",
        "def predicate(inputs):\n    return True\npredicate(inputs)\n",  # 无 result 赋值
    ])
    def test_l1_schema_required(self, source: str) -> None:
        v = validate_l1_python(source)
        assert v.ok is False
        assert "固定 I/O schema" in v.errors[0]

    def test_l1_ok_pure_function(self) -> None:
        source = (
            "def predicate(inputs):\n"
            "    return len(inputs['items']) > 1 and all(\n"
            "        it['done'] for it in inputs['items'])\n"
            "result = predicate(inputs)\n"
        )
        v = validate_l1_python(source)
        assert v.ok is True
        assert v.capabilities == ()

    def test_run_l1_predicate_pure_function(self) -> None:
        source = (
            "def predicate(inputs):\n"
            "    return len(inputs['items']) > 1 and all(\n"
            "        it['done'] for it in inputs['items'])\n"
            "result = predicate(inputs)\n"
        )
        state = {"items": [{"done": True}, {"done": True}]}
        res = run_l1_predicate(source, state)
        assert res["success"] is True
        assert res["result"] is True
        assert res["level"] == "L1"
        assert isinstance(res["replay_key"], str) and len(res["replay_key"]) == 64

    def test_run_l1_predicate_rejects_before_spawn(self, monkeypatch) -> None:
        called: list[object] = []

        def _boom(*args: object, **kw: object) -> dict[str, object]:
            called.append((args, kw))
            return {"success": True, "result": True}

        monkeypatch.setattr(pred_mod, "run_python_compute", _boom)
        res = run_l1_predicate("import random\nresult = predicate(inputs)\n", {})
        assert res["success"] is False
        assert "random" in res["capabilities"]
        assert called == []  # 静态拒绝，不进入执行

    def test_run_l1_predicate_non_bool_result_rejected(self) -> None:
        source = "def predicate(inputs):\n    return 42\nresult = predicate(inputs)\n"
        res = run_l1_predicate(source, {})
        assert res["success"] is False
        assert "bool" in res["error"]

    @pytest.mark.parametrize("code,mod", [
        ("import time\nresult = 1\n", "time"),
        ("import random\nresult = 1\n", "random"),
        ("from time import time\nresult = 1\n", "time"),
        ("from random import randint\nresult = 1\n", "random"),
    ])
    def test_worker_gate_explicitly_forbids_time_random(
        self, code: str, mod: str,
    ) -> None:
        # python_worker L1 对齐：运行时 import 门显式禁 random/time
        res = run_python_compute(code, {})
        assert res["success"] is False
        err = res.get("error", "")
        assert "forbidden" in err
        assert mod in err


# ---------------------------------------------------------------------------
# 验收 5：transition `when` 用 L0 谓词驱动流程（最小向量段）
# ---------------------------------------------------------------------------


class TestTransitionWhenFlow:
    def test_transition_when_l0_predicate_drives_flow(self) -> None:
        """最小 L0 驱动流程（验收 5）。

        现有代码无 transition 载体（agent_state 是字符串状态机、无谓词
        gate），故以测试内最小载体演示语义：``when`` 用 L0 谓词评估
        状态快照，为 True 才推进（fail-closed，不满足则停留原态）。
        """

        def transition_when(
            gate_text: str, state: dict[str, object],
        ) -> tuple[bool, PredicateEvaluation]:
            gate = parse(gate_text)
            trace = evaluate_traced(gate, state)
            return trace.value, trace

        state: dict[str, object] = {
            "phase": "collect",
            "items": [{"status": "done"}, {"status": "todo"}],
        }
        gate_text = (
            'phase == "collect" and count(items) > 0 and '
            'all(items, status == "done")'
        )
        ok, trace = transition_when(gate_text, state)
        assert ok is False  # 存在未完成 item → gate 关闭，不推进
        assert trace.first_failure is not None
        assert trace.first_failure.path == "status"

        # 状态推进：item 完成 → gate 打开 → 流程前进
        state["items"][1]["status"] = "done"  # type: ignore[index]
        ok, _ = transition_when(gate_text, state)
        assert ok is True

        # 确定性重放：同 gate 同状态 ⇒ 同结果（对当前状态重复求值）
        ok2, trace2 = transition_when(gate_text, state)
        ok3, trace3 = transition_when(gate_text, state)
        assert ok2 is True and ok3 is True
        assert trace2.replay_key == trace3.replay_key
        assert trace2.value == trace3.value
        assert trace2.steps == trace3.steps
