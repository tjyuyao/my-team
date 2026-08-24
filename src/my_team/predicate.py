"""谓词三级分级（v0.11 N7）: L0 声明式 / L1 受限纯函数 / L2 外部能力.

执行真理：SPEC §3.4「执行真理：执行器分级、沙箱与受限解释器」。
谓词只能解释状态，不能改变状态，也不能偷偷调用外部世界。能力分三级并
强制降级；L1 受限解释器与工具组合环境**同源**（同一边界、同一沙箱，
受限 python 模组与谓词 L1 同边界：无网络/无时钟/无随机/固定 I/O）。

三级边界
--------
L0 纯声明式（本模块的有限 DSL）
  谓词以 JSON 可序列化的位置列表（AST）表示，或由其文本语法解析而来：

      pred  := chain
      chain := term (("and" | "or") term)*       # 中缀，左结合；混用请加括号
      term  := "not" term | atom
      atom  := "(" pred ")"
             | "and" "(" pred ("," pred)* ")" | "or" "(" pred ("," pred)* ")"
             | "not" "(" pred ")"
             | "all" "(" IDENT "," pred ")" | "any" "(" IDENT "," pred ")"
             | "exists" "(" IDENT ")"
             | "count" "(" IDENT ")" CMP NUMBER
             | IDENT CMP literal | IDENT "in" "{" literal ("," literal)* "}"
      CMP     := "==" | "!=" | "<" | "<=" | ">" | ">="
      literal := STRING | NUMBER | true | false

  例：``phase == "collect" and count(items) > 0`` →
  ``["and", ["==", "phase", "collect"], ["count", "items", ">", 0]]``；
  ``all(items, status == "done")`` →
  ``["all", "items", ["==", "status", "done"]]``。
  ``and(...)/or(...)/not(...)`` 函数式与中缀 ``p and q`` 等价
  （canonical 展平结合律，两种写法同 replay key）。

  语义与不变量：
  - 解释器只遍历 AST 结构，不执行任何代码 ⇒ **结构性无副作用**（语法
    有限，不可能表达调用/导入/时钟/随机/文件/网络）；
  - 同 canonical + 同 state ⇒ 同结果：确定性重放（``replay_key`` =
    sha256(canonical JSON)）；in 集合乱序不改变键；
  - 字段缺失 ⇒ 该子表达式为 False（fail-closed），trace 记 ``missing``；
    ``exists`` 是唯一判定"存在性"的操作；
  - ``all/any`` 的子谓词在**元素作用域**内求值（字段路径相对元素，
    ``.`` 表示元素本身）；嵌套量化同理；
  - 类型错误（如对字符串做 ``<``、对非列表做 ``count``）抛
    ``PredicateTypeError``（诚实的求值失败，trace 记录到失败点）。
  - bash 不在本卡范围（v0.13，SPEC §12）。

L1 受限纯函数（python 模组）
  边界 = python_worker 受限解释器（compute 模式同源：同 builtins
  白名单 + import 门 + ``-I`` 隔离）：

  - 无网络 / 无文件写 / 无随机 / 无直接读当前时间；
  - 固定 I/O schema：模块定义 ``def predicate(inputs)``（恰一个位置
    参数）并以模块级 ``result = predicate(inputs)`` 求值，结果必须为
    bool；
  - 固定超时与资源上限（默认 10s / 200KB，容量语义见 SPEC §3.8）；
  - 结果可缓存可重放：同 source + 同 state ⇒ 同结果，``replay_key``
    随结果返回；
  - ``validate_l1_python`` 静态（AST）分析 detect 越级能力
    （network/file_write/random/clock/dynamic_code）→ 拒绝并提示；
    运行时 import 门显式禁 ``random``/``time``（python_worker），
    L1 谓词模组白名单进一步排除 ``datetime``/``uuid``
    （``L1_ALLOWED_MODULES``）。

L2 外部能力（设备能力）
  网络、真实文件写、真实时钟、随机数等必须建模为 Tool/PendingOperation
  （SPEC §3.4 出站工具），**不能伪装成 predicate**；越级谓词（L2
  伪装）被静态校验拒绝并提示改走 Tool/op 路径。本模块不实现 L2 执行器。

校验器（拒绝越级）
  - ``validate``（L0）：结构/字段/字面量检查；任何未知构造（含形如
    网络/文件/时钟/随机的操作符名）被拒绝，消息带 L2 Tool/op 提示；
  - ``validate_l1_python``（L1）：AST 静态分析，detect 即拒绝；
  - 静态校验是准入闸；运行时受限解释器（builtins 白名单 + import 门）
    是兜底边界。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from my_team.python_worker import DEFAULT_ALLOWED_MODULES, run_python_compute

# 类型别名：L0 字面量只允许标量（有限 DSL，不支持 null/容器字面量）。
JsonScalar = str | int | float | bool
# canonical AST：位置列表，如 ["==", "status", "done"]。
PredicateNode = list[Any]

_CMP_OPS = frozenset({"==", "!=", "<", "<=", ">", ">="})
_KNOWN_OPS = frozenset(
    {"==", "!=", "<", "<=", ">", ">=", "in", "all", "any",
     "exists", "count", "and", "or", "not"}
)
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# L1 边界：能力 → 禁止的模块根（静态检测；运行时 import 门是兜底）。
# datetime/uuid 在通用 compute 工具白名单中，但属时钟/随机向量，L1
# 谓词边界显式排除（见 L1_ALLOWED_MODULES）。
L1_FORBIDDEN_MODULES: dict[str, tuple[str, ...]] = {
    "network": (
        "socket", "http", "urllib", "requests", "ssl", "ftplib",
        "smtplib", "poplib", "imaplib", "telnetlib", "httpx", "aiohttp",
    ),
    "file_write": (
        "os", "subprocess", "pathlib", "shutil", "tempfile", "io", "glob",
        "pickle", "marshal", "shelve", "sqlite3", "ctypes", "importlib",
        "pkgutil", "runpy", "sys",
    ),
    "random": ("random", "secrets", "uuid"),
    "clock": ("time", "datetime", "calendar"),
}
_L1_FORBIDDEN_BUILTIN_CALLS = frozenset(
    {"open", "input", "eval", "exec", "compile", "__import__",
     "globals", "locals", "vars", "breakpoint"}
)

# L1 谓词模组白名单 = 通用白名单剔除时钟/随机向量。
L1_ALLOWED_MODULES: tuple[str, ...] = tuple(
    m for m in DEFAULT_ALLOWED_MODULES if m not in ("datetime", "uuid")
)

_ESCALATION_KEYWORDS = (
    "socket", "http", "url", "open", "write", "time", "clock", "random",
    "import", "exec", "eval", "subprocess", "os.", "requests",
)

_ESCALATION_HINT = (
    "外部能力（网络/文件写/时钟/随机）属 L2，必须建模为 "
    "Tool/PendingOperation，不能伪装成 predicate（SPEC §3.4）。"
)


class PredicateError(Exception):
    """谓词相关错误基类（L0 语法/语义/越级拒绝）。"""


class PredicateSyntaxError(PredicateError):
    """L0 文本语法错误。"""


class PredicateTypeError(PredicateError):
    """谓词求值时的类型错误（字段类型与操作不匹配）。"""


class PredicateEscalationError(PredicateError):
    """越级拒绝：谓词试图表达 L2 外部能力（或未知构造）。"""


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool))


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _strict_eq(a: Any, b: Any) -> bool:
    """标量严格相等：bool 与 int/float 视为不同类型（True != 1）。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(isinstance(a, bool) and isinstance(b, bool) and a == b)
    return bool(a == b)


def _compare(op: str, a: Any, b: Any) -> bool:
    table: dict[str, bool] = {
        "==": a == b, "!=": a != b,
        "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
    }
    return table[op]


def _is_escalation_like(op: str) -> bool:
    return any(k in op.lower() for k in _ESCALATION_KEYWORDS)


def _escalation_message(op: str) -> str:
    if _is_escalation_like(op):
        detected = f"检测到疑似外部能力构造（{op!r}）。"
    else:
        detected = "检测到疑似外部能力构造。"
    return (
        f"操作符 {op!r} 不属于 L0 声明式 DSL（有限语法："
        f"==/!=/</<=/>/>=/in/all/any/exists/count/and/or/not）。"
        f"{detected} {_ESCALATION_HINT}"
    )


# ---------------------------------------------------------------------------
# L0 文本语法 → canonical AST
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<lparen>\() | (?P<rparen>\)) | (?P<lbrace>\{) | (?P<rbrace>\})
    | (?P<comma>,)
    | (?P<cmp>==|!=|<=|>=|<|>)
    | (?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<word>[A-Za-z_.][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise PredicateSyntaxError(
                f"无法解析的字符 {text[pos]!r}（位置 {pos}）。"
            )
        pos = m.end()
        if m.lastgroup != "ws":
            toks.append((m.lastgroup or "", m.group()))
    return toks


def _unescape(raw: str) -> str:
    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(mapping.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class _Parser:
    def __init__(self, toks: list[tuple[str, str]]) -> None:
        self._toks = toks
        self._i = 0

    def _peek(self) -> tuple[str, str] | None:
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _next(self) -> tuple[str, str]:
        t = self._peek()
        if t is None:
            raise PredicateSyntaxError("谓词意外结束（L0 语法不完整）。")
        self._i += 1
        return t

    def _expect(self, kind: str) -> tuple[str, str]:
        t = self._next()
        if t[0] != kind:
            raise PredicateSyntaxError(f"期望 {kind}，得到 {t!r}。")
        return t

    def _parse_field(self) -> str:
        t = self._next()
        if t[0] != "word":
            raise PredicateSyntaxError(f"期望字段路径，得到 {t!r}。")
        value = t[1]
        if value != "." and not _FIELD_RE.match(value):
            raise PredicateSyntaxError(
                f"非法字段路径 {value!r}（点分标识符，如 a.b.c）。"
            )
        return value

    def _parse_cmp(self) -> str:
        t = self._next()
        if t[0] == "cmp":
            return t[1]
        raise PredicateSyntaxError(
            f"期望比较运算符（==/!=/</<=/>/>=），得到 {t!r}。"
        )

    def _parse_number(self) -> int | float:
        t = self._next()
        if t[0] != "num":
            raise PredicateSyntaxError(f"期望数字，得到 {t!r}。")
        s = t[1]
        return float(s) if ("." in s or "e" in s or "E" in s) else int(s)

    def _parse_literal(self) -> JsonScalar:
        t = self._next()
        kind, value = t
        if kind == "str":
            return _unescape(value[1:-1])
        if kind == "num":
            return float(value) if ("." in value or "e" in value or "E" in value) else int(value)
        if kind == "word" and value == "true":
            return True
        if kind == "word" and value == "false":
            return False
        raise PredicateSyntaxError(
            f"期望字面量（字符串/数字/true/false），得到 {t!r}；"
            "L0 字面量不支持 null/容器/裸标识符（集合成员请用引号，"
            "如 status in {\"done\"}）。"
        )

    def _looks_like_call(self) -> bool:
        nxt = self._toks[self._i + 1] if self._i + 1 < len(self._toks) else None
        return bool(nxt and nxt[0] == "lparen")

    def parse(self) -> PredicateNode:
        node = self._parse_chain()
        if self._peek() is not None:
            raise PredicateSyntaxError(
                f"多余的 token {self._peek()!r}：L0 谓词应为单一表达式。"
            )
        return node

    def _parse_chain(self) -> PredicateNode:
        """中缀 and/or 链（左结合）；连续同操作符合并为扁平节点。"""
        terms: list[PredicateNode] = [self._parse_term()]
        ops: list[str] = []
        while True:
            nxt = self._peek()
            if nxt and nxt[0] == "word" and nxt[1] in ("and", "or"):
                self._next()
                ops.append(nxt[1])
                terms.append(self._parse_term())
            else:
                break
        if not ops:
            return terms[0]
        node = terms[0]
        for op, t in zip(ops, terms[1:]):
            node = [op, node, t]
        return node

    def _parse_term(self) -> PredicateNode:
        nxt = self._peek()
        if nxt and nxt[0] == "word" and nxt[1] == "not":
            self._next()
            return ["not", self._parse_term()]
        return self._parse_atom()

    def _parse_atom(self) -> PredicateNode:
        t = self._peek()
        if t is None:
            raise PredicateSyntaxError("谓词意外结束（L0 语法不完整）。")
        if t[0] == "lparen":  # 括号分组 ( pred )
            self._next()
            p = self._parse_chain()
            self._expect("rparen")
            return p
        if t[0] != "word":
            raise PredicateSyntaxError(f"期望谓词，得到 {t!r}。")
        value = t[1]
        call = self._looks_like_call()

        if value in ("and", "or") and call:
            self._next()
            self._expect("lparen")
            preds: list[PredicateNode] = []
            while True:
                preds.append(self._parse_chain())
                nxt = self._peek()
                if nxt and nxt[0] == "comma":
                    self._next()
                    continue
                break
            self._expect("rparen")
            return [value, *preds]
        if value in ("all", "any") and call:
            self._next()
            self._expect("lparen")
            f = self._parse_field()
            self._expect("comma")
            p = self._parse_chain()
            self._expect("rparen")
            return [value, f, p]
        if value == "exists" and call:
            self._next()
            self._expect("lparen")
            f = self._parse_field()
            self._expect("rparen")
            return ["exists", f]
        if value == "count" and call:
            self._next()
            self._expect("lparen")
            f = self._parse_field()
            self._expect("rparen")
            cmp = self._parse_cmp()
            num = self._parse_number()
            return ["count", f, cmp, num]

        # 字段比较 / in 集合
        f = self._parse_field()
        nxt = self._peek()
        if nxt and nxt[0] == "word" and nxt[1] == "in":
            self._next()
            self._expect("lbrace")
            members: list[JsonScalar] = []
            while True:
                members.append(self._parse_literal())
                nxt2 = self._peek()
                if nxt2 and nxt2[0] == "comma":
                    self._next()
                    continue
                break
            self._expect("rbrace")
            return ["in", f, members]
        cmp = self._parse_cmp()
        lit = self._parse_literal()
        return [cmp, f, lit]


def parse(text: str) -> PredicateNode:
    """解析 L0 文本 DSL → canonical AST。

    语法见模块 docstring。解析失败抛 PredicateSyntaxError，消息带
    L0 语法有限 + L2 越级提示（外部能力不会静默混入）。
    """
    if not text or not text.strip():
        raise PredicateSyntaxError(
            "空谓词：L0 谓词不能为空。" + _ESCALATION_HINT
        )
    try:
        return _Parser(_tokenize(text)).parse()
    except PredicateSyntaxError as e:
        raise PredicateSyntaxError(
            f"{e} L0 语法有限（字段比较/in/all/any/exists/count/and/or/"
            f"not）；{_ESCALATION_HINT}"
        ) from e


# ---------------------------------------------------------------------------
# canonical / replay key（确定性重放、可缓存、可审计）
# ---------------------------------------------------------------------------


def canonical(node: Any) -> Any:
    """规范化 AST：递归 tuple；``in`` 集合按 JSON 键排序去重；
    ``and/or`` 展平结合律嵌套（中缀与函数式两种写法同键）。

    同语义（含集合乱序、and/or 结合）⇒ 同 canonical ⇒ 同 replay key。
    """
    if isinstance(node, list):
        if node and node[0] == "in" and len(node) == 3:
            _, f, members = node
            uniq: dict[str, Any] = {}
            for m in members:
                uniq[json.dumps(m)] = m
            return ("in", f, tuple(uniq[k] for k in sorted(uniq)))
        if node and node[0] in ("and", "or"):
            kids: list[Any] = []
            for x in node[1:]:
                c = canonical(x)
                if isinstance(c, tuple) and c and c[0] == node[0]:
                    kids.extend(c[1:])
                else:
                    kids.append(c)
            return (node[0], *kids)
        return tuple(canonical(x) for x in node)
    return node


def replay_key(node: PredicateNode | str) -> str:
    """确定性重放键：sha256(canonical JSON)。

    同输入（canonical 相同的谓词 + 同状态）⇒ 同输出；键用于缓存与审计。
    """
    if isinstance(node, str):
        node = parse(node)
    payload = json.dumps(canonical(node), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# L0 解释器（只遍历 AST，结构性无副作用；确定性可重放）
# ---------------------------------------------------------------------------

_MISSING = object()


def _resolve(scope: Any, path: str) -> Any:
    """沿点分路径取字段；缺失或路径中断返回 _MISSING（fail-closed）。"""
    if path == ".":
        return scope
    cur = scope
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


@dataclass(frozen=True)
class EvaluationStep:
    """单步求值记录（审计）：操作、字段、结果、缺失标记、备注。"""

    op: str
    path: str
    result: bool
    missing: bool = False
    note: str = ""


@dataclass(frozen=True)
class PredicateEvaluation:
    """一次求值的完整审计：结果 + 步骤 + 重放键。"""

    value: bool
    steps: tuple[EvaluationStep, ...]
    replay_key: str

    @property
    def first_failure(self) -> EvaluationStep | None:
        """最早的 False 步骤（gate 为何关闭的第一现场）。"""
        return next((s for s in self.steps if s.result is False), None)


def evaluate_traced(node: PredicateNode, state: dict[str, Any]) -> PredicateEvaluation:
    """求值并记录审计步骤（确定性：同输入 ⇒ 同 steps 同结果）。"""
    if not isinstance(state, dict):
        raise PredicateTypeError(
            f"谓词状态必须是 dict，得到 {type(state).__name__}。"
        )
    steps: list[EvaluationStep] = []

    def _missing(op: str, path: str) -> bool:
        steps.append(EvaluationStep(op, path, False, missing=True))
        return False

    def eval_node(n: Any, scope: Any) -> bool:
        if not isinstance(n, list) or not n or not isinstance(n[0], str):
            raise PredicateTypeError(
                f"非法谓词节点 {n!r}：L0 节点必须是操作符列表。"
            )
        op = n[0]
        if op not in _KNOWN_OPS:
            raise PredicateEscalationError(_escalation_message(op))

        if op in ("and", "or"):
            if len(n) < 2:
                raise PredicateTypeError(f"{op} 至少需要 1 个子谓词。")
            if op == "and":
                for child in n[1:]:
                    r = eval_node(child, scope)
                    steps.append(EvaluationStep("and", "", r))
                    if not r:
                        return False
                return True
            for child in n[1:]:
                r = eval_node(child, scope)
                steps.append(EvaluationStep("or", "", r))
                if r:
                    return True
            return False
        if op == "not":
            if len(n) != 2:
                raise PredicateTypeError("not 需要恰好 1 个子谓词。")
            r = eval_node(n[1], scope)
            steps.append(EvaluationStep("not", "", not r))
            return not r

        if op in ("all", "any"):
            if len(n) != 3:
                raise PredicateTypeError(f"{op} 需要 (field, predicate)。")
            _, f, child = n
            val = _resolve(scope, f)
            if val is _MISSING:
                return _missing(op, f)
            if not isinstance(val, list):
                raise PredicateTypeError(
                    f"{op} 的字段 {f!r} 必须是列表，得到 {type(val).__name__}。"
                )
            if op == "all":
                for elem in val:
                    if not eval_node(child, elem):
                        steps.append(EvaluationStep("all", f, False))
                        return False
                steps.append(EvaluationStep("all", f, True))
                return True
            for elem in val:
                if eval_node(child, elem):
                    steps.append(EvaluationStep("any", f, True))
                    return True
            steps.append(EvaluationStep("any", f, False))
            return False

        if op == "exists":
            if len(n) != 2:
                raise PredicateTypeError("exists 需要 (field)。")
            _, f = n
            ok = _resolve(scope, f) is not _MISSING
            steps.append(EvaluationStep("exists", f, ok, missing=not ok))
            return ok

        if op == "count":
            if len(n) != 4:
                raise PredicateTypeError("count 需要 (field, cmp, number)。")
            _, f, cmp, num = n
            val = _resolve(scope, f)
            if val is _MISSING:
                return _missing("count", f)
            if not isinstance(val, list):
                raise PredicateTypeError(
                    f"count 的字段 {f!r} 必须是列表，得到 {type(val).__name__}。"
                )
            if cmp not in _CMP_OPS:
                raise PredicateTypeError(f"count 的比较运算符 {cmp!r} 非法。")
            result = _compare(cmp, len(val), num)
            steps.append(EvaluationStep("count", f, result))
            return result

        if len(n) != 3:
            raise PredicateTypeError(f"{op} 需要 (field, literal)。")
        _, f, lit = n
        val = _resolve(scope, f)
        if val is _MISSING:
            return _missing(op, f)

        if op in ("==", "!="):
            if isinstance(val, (dict, list)):
                raise PredicateTypeError(
                    f"{op} 仅支持标量字段比较，字段 {f!r} 是 "
                    f"{type(val).__name__}；列表字段请用 in/all/any/count。"
                )
            result = _strict_eq(val, lit) if op == "==" else not _strict_eq(val, lit)
            steps.append(EvaluationStep(op, f, result))
            return result
        if op in ("<", "<=", ">", ">="):
            if not _is_number(val) or not _is_number(lit):
                raise PredicateTypeError(
                    f"{op} 仅支持数字比较，字段 {f!r} 是 {type(val).__name__}，"
                    f"字面量是 {type(lit).__name__}。"
                )
            result = _compare(op, val, lit)
            steps.append(EvaluationStep(op, f, result))
            return result
        if op == "in":
            try:
                result = val in lit
            except TypeError:
                raise PredicateTypeError(
                    f"in 集合成员必须是标量（str/int/float/bool），字段 {f!r} "
                    f"的值 {val!r} 不可哈希。"
                ) from None
            steps.append(EvaluationStep("in", f, result))
            return result
        raise PredicateEscalationError(_escalation_message(op))  # pragma: no cover

    value = eval_node(node, state)
    return PredicateEvaluation(
        value=value,
        steps=tuple(steps),
        replay_key=replay_key(node),
    )


def evaluate(node: PredicateNode | str, state: dict[str, Any]) -> bool:
    """求值（确定性）：同 canonical + 同 state ⇒ 同结果，无副作用。"""
    if isinstance(node, str):
        node = parse(node)
    return evaluate_traced(node, state).value


# ---------------------------------------------------------------------------
# L0 静态校验器（结构检查 + 拒绝越级）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredicateValidation:
    """L0 静态校验结果（可审计）：ok + 结构信息 + 重放键。"""

    ok: bool
    errors: tuple[str, ...] = ()
    level: str = "L0"
    replay_key: str = ""
    ops: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


def _check_field(
    f: Any,
    errors: list[str],
    fields: list[str],
    in_quant: bool,
) -> None:
    if not isinstance(f, str):
        errors.append(f"字段路径必须是字符串，得到 {f!r}。")
        return
    if f == ".":
        if not in_quant:
            errors.append("'.' 只能用于 all/any 元素作用域内引用元素本身。")
        else:
            fields.append(".")
        return
    if _FIELD_RE.match(f):
        fields.append(f)
    else:
        errors.append(f"非法字段路径 {f!r}（点分标识符，如 a.b.c）。")


def validate(node: PredicateNode | str) -> PredicateValidation:
    """L0 静态校验：结构/字段/字面量检查；拒绝越级（未知构造）。

    越级构造（网络/文件写/时钟/随机等外部能力伪装成 predicate）被拒绝
    并提示改走 Tool/op 路径（SPEC §3.4）。
    """
    if isinstance(node, str):
        try:
            node = parse(node)
        except PredicateSyntaxError as e:
            return PredicateValidation(ok=False, errors=(str(e),))

    errors: list[str] = []
    ops: list[str] = []
    fields: list[str] = []

    def walk(n: Any, in_quant: bool) -> None:
        if not isinstance(n, list) or not n or not isinstance(n[0], str):
            errors.append(f"非法谓词节点 {n!r}：L0 节点必须是操作符列表。")
            return
        op = n[0]
        if op not in _KNOWN_OPS:
            errors.append(_escalation_message(op))
            return
        ops.append(op)

        if op in ("and", "or"):
            if len(n) < 2:
                errors.append(f"{op} 至少需要 1 个子谓词。")
            for c in n[1:]:
                walk(c, in_quant)
        elif op == "not":
            if len(n) != 2:
                errors.append("not 需要恰好 1 个子谓词。")
            else:
                walk(n[1], in_quant)
        elif op in ("all", "any"):
            if len(n) != 3:
                errors.append(f"{op} 需要 (field, predicate)。")
                return
            _check_field(n[1], errors, fields, in_quant)
            walk(n[2], True)
        elif op == "exists":
            if len(n) != 2:
                errors.append("exists 需要 (field)。")
            else:
                _check_field(n[1], errors, fields, in_quant)
        elif op == "count":
            if len(n) != 4:
                errors.append("count 需要 (field, cmp, number)。")
                return
            _check_field(n[1], errors, fields, in_quant)
            if n[2] not in _CMP_OPS:
                errors.append(f"count 的比较运算符 {n[2]!r} 非法。")
            if not _is_number(n[3]):
                errors.append(f"count 的界必须是数字，得到 {n[3]!r}。")
        elif op == "in":
            if len(n) != 3:
                errors.append("in 需要 (field, set)。")
                return
            _check_field(n[1], errors, fields, in_quant)
            members = n[2]
            if not isinstance(members, list) or not members:
                errors.append("in 的集合必须是非空字面量列表。")
            else:
                for m in members:
                    if not _is_scalar(m):
                        errors.append(
                            f"in 集合成员必须是标量（str/int/float/bool），"
                            f"得到 {m!r}。"
                        )
        else:  # 比较叶子
            if len(n) != 3:
                errors.append(f"{op} 需要 (field, literal)。")
                return
            _check_field(n[1], errors, fields, in_quant)
            if not _is_scalar(n[2]):
                errors.append(
                    f"{op} 的字面量必须是标量（str/int/float/bool），"
                    f"得到 {n[2]!r}。"
                )
            if op in ("<", "<=", ">", ">=") and not _is_number(n[2]):
                errors.append(f"{op} 的字面量必须是数字，得到 {n[2]!r}。")

    walk(node, False)
    if errors:
        return PredicateValidation(ok=False, errors=tuple(errors))
    return PredicateValidation(
        ok=True,
        replay_key=replay_key(node),
        ops=tuple(ops),
        fields=tuple(dict.fromkeys(fields)),
    )


# ---------------------------------------------------------------------------
# L1 静态校验器（python 模组：AST 分析，detect 越级能力）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class L1Validation:
    """L1 受限纯函数静态校验结果。"""

    ok: bool
    errors: tuple[str, ...] = ()
    level: str = "L1"
    capabilities: tuple[str, ...] = ()


def _mark_import(root: str, caps: set[str]) -> None:
    for cap, mods in L1_FORBIDDEN_MODULES.items():
        if root in mods:
            caps.add(cap)


def _attr_root(attr: ast.Attribute) -> str:
    """time.time() → 'time'；os.path.join() → 'os'（取根段）。"""
    parts: list[str] = []
    cur: ast.expr = attr
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts)).split(".")[0]


def _detect_l1_capabilities(source: str) -> tuple[str, ...]:
    """静态（AST）检测 L1 越级能力：network/file_write/random/clock/
    dynamic_code。

    保守近似（含 import 别名映射）；静态校验是准入闸，运行时受限
    解释器（builtins 白名单 + import 门）是兜底边界。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    caps: set[str] = set()
    aliases: dict[str, str] = {}
    # 第一遍：import（含别名）
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if a.asname:
                    aliases[a.asname] = root
                _mark_import(root, caps)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if len(node.names) == 1 and node.names[0].asname:
                    aliases[node.names[0].asname] = root
                _mark_import(root, caps)
    # 第二遍：调用检测（含别名解析）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == "open":
                caps.add("file_write")
            elif func.id in _L1_FORBIDDEN_BUILTIN_CALLS:
                caps.add("dynamic_code")
            else:
                _mark_import(aliases.get(func.id, func.id), caps)
        elif isinstance(func, ast.Attribute):
            root = _attr_root(func)
            _mark_import(aliases.get(root, root), caps)
            if func.attr in _L1_FORBIDDEN_BUILTIN_CALLS:
                caps.add("dynamic_code")
    return tuple(sorted(caps))


def _check_l1_schema(tree: ast.Module) -> list[str]:
    """固定 I/O schema：def predicate(inputs) + 模块级
    result = predicate(inputs)。"""
    errors: list[str] = []
    has_predicate = False
    has_result_assign = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "predicate":
                has_predicate = True
                args = node.args
                positional = args.posonlyargs + args.args
                if len(positional) != 1 or args.vararg is not None or args.kwonlyargs:
                    errors.append(
                        "固定 I/O schema：def predicate(inputs) 必须恰有一个"
                        "位置参数。"
                    )
        elif isinstance(node, ast.Assign):
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "result"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "predicate"
                and len(node.value.args) == 1
            ):
                has_result_assign = True
    if not has_predicate:
        errors.append("固定 I/O schema：必须定义 def predicate(inputs)。")
    if not has_result_assign:
        errors.append(
            "固定 I/O schema：必须以模块级 result = predicate(inputs) 求值。"
        )
    return errors


def validate_l1_python(source: str) -> L1Validation:
    """L1 受限纯函数静态校验（AST 分析，不执行）。

    固定 I/O schema：模块必须定义 ``def predicate(inputs)``（恰一个位置
    参数）并以模块级 ``result = predicate(inputs)`` 求值，运行时必须返回
    bool。越级能力（network/file_write/random/clock/dynamic_code）被
    检测即拒绝，并提示外部能力属 L2，必须建模为 Tool/PendingOperation
    （SPEC §3.4）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return L1Validation(ok=False, errors=(f"L1 python 语法错误: {e}",))
    caps = _detect_l1_capabilities(source)
    errors: list[str] = []
    for cap in caps:
        errors.append(
            f"L1 受限纯函数边界禁止 {cap}（静态检测）。"
            f"{_ESCALATION_HINT}"
        )
    errors.extend(_check_l1_schema(tree))
    if errors:
        return L1Validation(ok=False, errors=tuple(errors), capabilities=caps)
    return L1Validation(ok=True, capabilities=caps)


# ---------------------------------------------------------------------------
# L1 受限执行器（与工具组合环境同源的受限解释器）
# ---------------------------------------------------------------------------


def run_l1_predicate(
    source: str,
    state: dict[str, Any],
    *,
    timeout_ms: int = 10_000,
    max_output_bytes: int = 200_000,
) -> dict[str, Any]:
    """L1 受限纯函数执行：静态校验 → 受限解释器（python_worker.compute
    同源边界）→ 固定 I/O schema（必须返回 bool）。

    确定性：同 source + 同 state ⇒ 同结果（可缓存可重放，replay_key
    随结果返回）。越级（L2 伪装）在静态校验阶段即被拒绝，不进入执行。
    """
    validation = validate_l1_python(source)
    if not validation.ok:
        return {
            "success": False,
            "level": "L1",
            "error": "；".join(validation.errors),
            "capabilities": list(validation.capabilities),
        }
    res = run_python_compute(
        code=source,
        inputs=state,
        allowed_modules=L1_ALLOWED_MODULES,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
    )
    res["level"] = "L1"
    if res["success"]:
        if not isinstance(res["result"], bool):
            res["success"] = False
            res["error"] = (
                f"L1 固定 I/O schema：predicate 必须返回 bool，"
                f"得到 {type(res['result']).__name__}。"
            )
        else:
            res["replay_key"] = _digest((source, state))
    return res


__all__ = [
    "EvaluationStep",
    "L1Validation",
    "L1_ALLOWED_MODULES",
    "L1_FORBIDDEN_MODULES",
    "PredicateError",
    "PredicateEscalationError",
    "PredicateEvaluation",
    "PredicateSyntaxError",
    "PredicateTypeError",
    "PredicateValidation",
    "canonical",
    "evaluate",
    "evaluate_traced",
    "parse",
    "replay_key",
    "run_l1_predicate",
    "validate",
    "validate_l1_python",
]
