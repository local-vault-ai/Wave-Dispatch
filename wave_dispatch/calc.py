"""Exact arithmetic for bots: a bot writes its sums in a ```calc block, the controller computes them, the bot answers
with the real numbers (small models slip at mental arithmetic: live runs 2026-11-04, "24.5 x 17 = 416.25").

An expression is DATA, never code: it is parsed with `ast` and only numbers, + - * / // % **, parentheses, unary signs
and a few named functions (round, abs, min, max, sqrt, floor, ceil) are allowed. Sizes are capped (length, count,
exponent, result magnitude) so nothing can run long or blow up memory. Thousands separators ("2,450") are accepted.
"""
from __future__ import annotations

import ast
import math
import re

MAX_EXPRS = 12
MAX_LEN = 200
MAX_ABS = 1e15            # results / operands larger than this are refused
MAX_POW = 64              # exponent cap
FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil}
BLOCK = re.compile(r"```calc[^\n]*\n(.*?)```", re.S | re.I)
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
HELP = ("Calculator: for arithmetic, don't work it out in your head. Write a block\n```calc\n0.17 * 2450\n```\n"
        "with one expression per line. It takes ONLY numbers and + - * / // % **, parentheses and round abs min max "
        "sqrt floor ceil: no words, no units, no code, no function calls of your own. For 'x% of y' write x/100 * y; "
        "turn a clock time into minutes first (14:35 -> 14*60+35). The controller computes them exactly and gives you "
        "the results. Code is NOT for the calculator. Only write code when the question asks for code (then in a ```python "
        "block); a maths or text question gets a plain answer, no code.")


class CalcError(ValueError):
    pass


def _check(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CalcError("not a number")
    if isinstance(v, float) and not math.isfinite(v):
        raise CalcError("not a finite number")
    if abs(v) > MAX_ABS:
        raise CalcError("number too large")
    return v


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        return _check(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return _check(+v if isinstance(node.op, ast.UAdd) else -v)
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left), _eval(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return _check(a + b)
        if isinstance(op, ast.Sub):
            return _check(a - b)
        if isinstance(op, ast.Mult):
            return _check(a * b)
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if b == 0:
                raise CalcError("division by zero")
            return _check(a / b if isinstance(op, ast.Div) else a // b if isinstance(op, ast.FloorDiv) else a % b)
        if isinstance(op, ast.Pow):
            if abs(b) > MAX_POW or (abs(a) > 1e6 and b > 3):
                raise CalcError("power too large")
            return _check(a ** b)
        raise CalcError("operator not allowed")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
        if len(node.args) > 8:
            raise CalcError("too many arguments")
        args = [_eval(a) for a in node.args]
        if node.func.id == "sqrt" and args and args[0] < 0:
            raise CalcError("sqrt of a negative number")
        try:
            return _check(FUNCS[node.func.id](*args))
        except (TypeError, ValueError) as e:
            raise CalcError(str(e)) from None
    raise CalcError("only numbers, + - * / // % ** ( ) and round/abs/min/max/sqrt/floor/ceil are allowed")


def evaluate(expr: str):
    """One expression -> its exact value (raises CalcError). Never executes anything."""
    text = (expr or "").strip().replace("×", "*").replace("÷", "/").replace("−", "-")
    if not text or len(text) > MAX_LEN:
        raise CalcError(f"expression must be 1-{MAX_LEN} characters")
    last: CalcError = CalcError("not a valid expression")
    for candidate in dict.fromkeys((text, THOUSANDS.sub("", text))):   # then "2,450" read as 2450
        try:
            tree = ast.parse(candidate, mode="eval")
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        try:
            return _eval(tree)
        except RecursionError:
            last = CalcError("expression too deep")
        except (OverflowError, ZeroDivisionError) as e:
            last = CalcError(f"can't compute: {e}")
        except CalcError as e:
            last = e
    raise last


def fmt(v) -> str:
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return format(v, ".12g")
    return str(v)


def requests(text: str) -> list[str]:
    """The expressions in a reply's ```calc blocks (one per line; `# comments` and `name =` prefixes dropped)."""
    out = []
    for block in BLOCK.findall(text or ""):
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line and "==" not in line:               # "total = 0.17*2450" -> the right-hand side
                line = line.split("=", 1)[1].strip()
            if line:
                out.append(line)
    return out[:MAX_EXPRS]


def run(exprs: list[str]) -> list[dict]:
    rows = []
    for e in exprs[:MAX_EXPRS]:
        try:
            rows.append({"expr": e[:MAX_LEN], "result": fmt(evaluate(e)), "error": ""})
        except CalcError as err:
            rows.append({"expr": e[:MAX_LEN], "result": "", "error": str(err)})
    return rows


def describe(rows: list[dict]) -> str:
    return "\n".join(f"{r['expr']} = {r['result']}" if not r["error"] else f"{r['expr']} -> NOT computed: {r['error']}"
                     for r in rows) or "(no expressions)"


# ---------------------------------------------------------------- checking what a bot wrote (like clang for C++)

# Sums are found by a one-pass TOKEN SCANNER, not one big regex: nested optional spaces in a regex backtracked
# exponentially on hostile replies ("1 + " * 12 + "1 !" ran for seconds: Astra security reviews 2026-11-06).
_TOK = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?|\*\*|[-+*/×÷^()=x]|[^\S\n]+|.")   # a number never ends in ","
_OPS = {"-", "+", "*", "/", "×", "÷", "^", "**", "x"}
LINE_MAX = 1000                                             # longer lines are not checked for sums


def _claims(line: str) -> list[tuple[str, str]]:
    """[(left side, claimed number)] for every "<numbers and operators> = <number>" in one line. Linear time."""
    toks = [(m.group(0), m.start(), m.end()) for m in _TOK.finditer(line)]
    kind = []
    for i, (t, a, b) in enumerate(toks):
        if t[0].isdigit():
            kind.append("num")
        elif t.isspace():
            kind.append("sp")
        elif t == "x":                                       # "3 x 4", "3x(4+1)" are sums; the x in "max" is not
            prev = next((kind[j] for j in range(i - 1, -1, -1) if kind[j] != "sp"), "")
            nxt = next((u for u, _, _ in toks[i + 1:] if not u.isspace()), "")
            glued = line[a - 1:a].isalpha() or line[b:b + 1].isalpha()
            kind.append("op" if prev in ("num", ")") and (nxt[:1].isdigit() or nxt == "(") and not glued else "other")
        elif t in _OPS:
            kind.append("op")
        elif t in ("(", ")", "="):
            kind.append(t)
        else:
            kind.append("other")
    out = []
    for i, k in enumerate(kind):
        if k != "=":
            continue
        j = i + 1                                            # the right side: a number, or an expression ("(2 * 6)")
        while j < len(toks) and kind[j] == "sp":
            j += 1
        rstart = j
        sign = toks[j][0] if j < len(toks) and toks[j][0] in ("-", "+") else ""
        j += bool(sign)
        if j >= len(toks) or kind[j] not in ("num", "("):
            continue
        k2 = j
        while k2 < len(toks) and kind[k2] in ("num", "op", "(", ")", "sp"):
            k2 += 1
        rhs_end = k2
        while rhs_end > j and kind[rhs_end - 1] == "sp":
            rhs_end -= 1
        after = re.sub(r"^[\d.]*", "", line[toks[rhs_end - 1][2]:toks[rhs_end - 1][2] + 40])
        if after[:1].isalnum() or after[:1] == "_":      # "= 4kg", "= 2x": not a plain number
            continue
        chained = rhs_end - j > 1                            # "= 2 * 6 = 12": every step is checked (review 4/5)
        if not chained and not re.fullmatch(r"\d(?:[\d,]*\d)?(?:\.\d+)?", toks[j][0]):
            continue
        start = i                                            # the left side: numbers, operators, brackets, spaces
        while start > 0 and kind[start - 1] in ("num", "op", "(", ")", "sp"):
            start -= 1
        while start < i and not (kind[start] in ("num", "(") or (toks[start][0] == "-" and start + 1 < i
                                                                 and kind[start + 1] == "num")):
            start += 1
        while start < i and line[toks[start][1] - 1:toks[start][1]] and \
                (line[toks[start][1] - 1].isalnum() or line[toks[start][1] - 1] in "._"):   # glued to a word
            start += 1
            while start < i and not (kind[start] in ("num", "(") or toks[start][0] == "-"):
                start += 1
        seg = [k3 for k3 in kind[start:i] if k3 != "sp"]
        if seg.count("num") >= 2 and "op" in seg:            # "2,450 = 0.17 x 2450" restates, it is no sum
            rhs = line[toks[rstart][1]:toks[rhs_end - 1][2]].strip() if chained else ("-" if sign == "-" else "") + toks[j][0]
            out.append((line[toks[start][1]:toks[i][1]].strip(), rhs))
    return out


PCT_CLAIM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d[\d,]*(?:\.\d+)?)\s*"
                       r"(?:is|=|equals|comes to|would be|will be)\s+(?:about\s+|approximately\s+)?(-?\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\d.]*[\w])"
                       r"(?!\s*(?:[-+*/×÷^]|x\s*\d))", re.I)   # "= 0.17 x 2450 = 416.5": 0.17 is a step, not the answer
NUMBER = re.compile(r"(?<![\w.])-?\d(?:[\d,]*\d)?(?:\.\d+)?(?![\w])")


def _num(text: str) -> float:
    return float(text.replace(",", ""))


_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_TEXT = re.compile(r"\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}")


def plain_math(text: str) -> str:
    """LaTeX maths as plain sums, so check_sums can read them (50-question benchmark 2026-11-06: "\\( 14 + 22 + 37 + 41
    + 56 = 160 \\)" and "\\[ 09 \\times 60 + 47 = 597 \\]" were never checked)."""
    t = re.sub(r"(?<=[=:\-\u2212]\s)\*\*(?=[\d.(-])|(?<=[=:\-\u2212])\*\*(?=[\d.(-])|(?<=[\d.%)])\*\*(?=[\s.,;:!?)]|$)",
               "", text or "", flags=re.M)                 # bold around a number ("= **99**"); "2 ** 3" stays a power
    t = re.sub(r"[\u2212\u2012\u2013\u2014\ufe63\uff0d](?=\s*[\d.(])", "-", t)   # Unicode minus = "-"
    for _ in range(3):                                     # nested \frac{\frac{a}{b}}{c}
        t = _FRAC.sub(r"(\1)/(\2)", t)
    t = _TEXT.sub(r" \1 ", t)
    t = re.sub(r"\\(?:times|cdot|ast)\b", "*", t)
    t = re.sub(r"\\div\b", "/", t)
    t = re.sub(r"\\(?:approx|simeq|eq)\b", "=", t)
    t = re.sub(r"\\left|\\right|\\[()\[\]]|\\[,;! ]|\$", " ", t)
    t = re.sub(r"(?<![\d.,])0+(?=\d)", "", t)                 # "09 * 60" -> "9 * 60"; "1,000" stays (review 3)
    return t.replace("{", "(").replace("}", ")")


RESULT_AFTER = re.compile(r"\A[^\S\n]*\n?[^\n]{0,40}?(?:result|answer|=|is|gives)\W{0,6}(-?\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\d.]*[\w])",
                          re.I)


def _calc_results(text: str) -> list[tuple[str, str]]:
    """(expression, the number written right after its ```calc block): live 2026-11-06 the Math Bot wrote
    ```calc 500 / 342 * 27``` then "Result: **41.2**" (it is 39.47)."""
    out = []
    for m in BLOCK.finditer(text or ""):
        exprs = [e.split("#", 1)[0].strip() for e in m.group(1).splitlines() if e.split("#", 1)[0].strip()]
        after = RESULT_AFTER.search(text[m.end():m.end() + 120].replace("*", ""))
        if len(exprs) == 1 and after:
            out.append((exprs[0].split("=", 1)[1].strip() if "=" in exprs[0] and "==" not in exprs[0] else exprs[0],
                        after.group(1)))
    return out


def check_sums(text: str) -> tuple[list[dict], list[dict]]:
    """Every written sum "<numbers and operators> = <number>" (plain or LaTeX), and every number written right after
    a ```calc block, recomputed exactly. Returns (wrong, right) rows {expr, claimed, actual}. A sum the calculator
    can't read is skipped (never a false alarm); a value rounded to the digits written counts as right."""
    wrong, right = [], []
    flat = BLOCK.sub(" ", (text or "")[:200_000])
    flat = plain_math(flat)                                 # LaTeX read as plain sums (plain text is unchanged)
    lines = [re.sub(r"[^\S\n]+", " ", ln) for ln in flat.splitlines() if len(ln) <= LINE_MAX]   # bounded work
    pairs = [c for ln in lines for c in _claims(ln)][:40] \
        + [(f"{p}/100 * {w}", c) for ln in lines if len(ln) <= 300 and ("%" in ln or "percent" in ln.lower())
           for p, w, c in PCT_CLAIM.findall(ln)][:20] \
        + _calc_results((text or "")[:200_000])[:12]
    for lhs, rhs in pairs:   # "17% of 2,450 is 416.25" (live 2026-11-05) is checked as 17/100 * 2,450
        expr = re.sub(r"(?<=[\d)])\s*[x×]\s*(?=[\d(])", "*", lhs).replace("^", "**")
        try:
            actual = evaluate(expr)
        except CalcError:
            continue
        try:
            claimed = _num(rhs)
            decimals = len(rhs.split(".")[1]) if "." in rhs else 0
        except ValueError:                                  # a chained step: "= 2 * 6 = 12" -> the right side
            try:                                            # is an expression of its own
                claimed = evaluate(re.sub(r"(?<=[\d)])\s*[x×]\s*(?=[\d(])", "*", rhs).replace("^", "**"))
            except CalcError:
                continue
            decimals = 9
        ok = abs(actual - claimed) <= max(0.5 * 10 ** -decimals, 1e-9 * max(1.0, abs(actual)))
        row = {"expr": lhs.strip(), "claimed": rhs, "actual": fmt(actual)}
        if row not in right and row not in wrong:          # the LaTeX copy repeats plain sums
            (right if ok else wrong).append(row)
    return wrong, right


PY_BLOCK = re.compile(r"```(?:python|py)[^\n]*\n(.*?)```", re.S | re.I)


CODE_START = re.compile(r"^(?:(?:async\s+)?def\s+\w+\s*\(.*\)\s*(?:->[^:]*)?:|class\s+\w+\s*(?:\(.*\))?\s*:|import\s+\w[\w.]*\s*$|from\s+[\w.]+\s+import\s+\w)")


def _bare_code(text: str) -> list[str]:
    """Python written without a fence: from a def/class/import line through the indented lines (and calls) after it."""
    out, cur = [], []
    for line in (text or "").splitlines():
        if CODE_START.match(line) or (cur and (line.startswith((" ", "\t")) or not line.strip()
                                                or re.match(r"^[A-Za-z_][\w.]*\(.*\)\s*$", line) or line.startswith("return "))):
            cur.append(line)
        elif cur:
            out.append("\n".join(cur).strip("\n")); cur = []
    if cur:
        out.append("\n".join(cur).strip("\n"))
    return [c for c in out if CODE_START.match(c)]


def code_snippets(text: str) -> list[str]:
    """The code a bot wrote: ```python blocks, "code" fields of a JSON answer (the Code Bot's {"code": ...}), else
    bare (unfenced) Python."""
    import json as _json
    out = [b for b in PY_BLOCK.findall(text or "")]
    t = (text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            v = _json.loads(t[i:j + 1], strict=False)
        except ValueError:
            v = None
        stack = [v]
        while stack and len(out) < 8:
            x = stack.pop()
            if isinstance(x, dict):
                for k, y in x.items():
                    looks_like_code = isinstance(y, str) and ("\n" in y.strip() or y.lstrip().startswith(("def ", "class ", "import ")))
                    if k.lower() in ("code", "python", "function") and looks_like_code:
                        out.append(y)
                    elif isinstance(y, (dict, list)):
                        stack.append(y)
            elif isinstance(x, list):
                stack.extend(x)
    if not out and "```" not in (text or ""):               # no fences, no JSON code: bare Python lines
        out = _bare_code(text)
    return out[:8]


def check_python(text: str) -> list[str]:
    """Python a bot wrote (```python blocks and JSON "code" fields): parsed, never run. Errors as 'block N, line L: msg'."""
    import ast as _ast
    errors = []
    for i, block in enumerate(code_snippets(text), 1):
        try:
            _ast.parse(block)
        except SyntaxError as e:
            errors.append(f"code block {i}, line {e.lineno}: {e.msg}")
        except (ValueError, RecursionError, MemoryError):
            errors.append(f"code block {i}: can't be parsed")
    return errors


def numbers_in(text: str) -> set[str]:
    """Normalised numbers in a text ("2,450" -> "2450", "416.50" -> "416.5")."""
    out = set()
    for m in NUMBER.findall(text or ""):
        try:
            out.add(fmt(_num(m)))
        except ValueError:
            pass
    return out



def strip_blocks(text: str) -> str:
    """The reply without its ```calc blocks (what's left is the bot's own answer)."""
    return BLOCK.sub("", text or "").strip()


FOLLOWUP_OK = ("\n\nNow give your FULL answer again: keep everything from your first reply (code, explanations, "
               "every part of the question) and use these exact numbers. Do not write another calc block.")
FOLLOWUP_NONE = ("[controller] The calculator could not read those lines (it takes only numbers and operators, no "
                 "words, units or code). Answer the question directly now, without a calc block (code only if the question asks for code).")
