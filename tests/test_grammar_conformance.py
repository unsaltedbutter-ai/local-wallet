"""Grammar conformance tests for ``agent/grammar/envelope.gbnf`` (TCK-P1-003 F1).

Pins the CURRENT grammar file so any loosening/tightening that changes the
accepted envelope language fails the test suite. The matrix is a **drift
pin**, not a substitute for llama.cpp: the authoritative decoder check
remains the llama-gated test (``tests/test_agent_loop.py`` / remote-runtime
eval). We read the real grammar from disk on every run, so a grammar edit
that shifts any accept/reject sample here fails loudly.

The matcher is a *test-side approximation* of llama.cpp GBNF semantics for
the small subset the envelope grammar actually uses: rule definitions
``name ::= body``, JSON string literals, character classes ``[...]`` with
ranges and negation, alternation ``|``, grouping ``(...)``, repetition
``*``, ``?``, and bounded ``{m}``/``{m,}``/``{m,n}`` (the grammar's
``[0-9a-fA-F]{4}`` for ``\\uXXXX`` escapes), plus the recursive ``ws``
rule. It deliberately does NOT implement the full GBNF feature set (named
alternation branches ``(a|b)`` suffixes, ``+`` shorthand, ...) because the
envelope grammar never uses them; an unsupported construct raises so a
future grammar edit that reaches for them fails loudly here instead of
silently passing.

Approximation caveat (documented, accepted): GBNF literal matching is
byte-oriented and character classes operate on single UTF-8 bytes; our
matcher operates on Python ``str`` code points. Within the envelope grammar
this is equivalent for every construct in use (the only character class is
``[^"\\\x00-\x1f]`` plus ASCII digit/whitespace classes, and literals are
pure ASCII). The llama-gated test remains authoritative for real decoding
semantics.
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GRAMMAR = (
    _REPO / "src" / "localwallet" / "agent" / "grammar" / "envelope.gbnf"
)

#: Pinned GGUF used for the real-parse probe (ADR-0021-era models/bin, the
#: same weights the Phase 6 adjudication runs). The path mirrors the runtime
#: precedence (``LOCALWALLET_MODEL_PATH`` then the models/bin default). A
#: vocab-only load is cheap (~0.3s, no weights touched), so this only needs
#: the file to exist, not the model to actually generate.
_MODEL_PATH_ENV_VAR = "LOCALWALLET_MODEL_PATH"
_DEFAULT_GGUF = _REPO / "models" / "bin" / "gemma-4-E2B-it-Q4_K_M.gguf"


def _llama_cpp_available() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def _parse_probe_model() -> Path | None:
    env = os.environ.get(_MODEL_PATH_ENV_VAR)
    if env and Path(env).is_file():
        return Path(env)
    return _DEFAULT_GGUF if _DEFAULT_GGUF.is_file() else None


_LLAMA_CPP_AVAILABLE = _llama_cpp_available()
_PROBE_MODEL = _parse_probe_model()


# ---------------------------------------------------------------- matcher
#
# Tokenizer + recursive-descent parser for the GBNF subset, then a
# backtracking matcher over the resulting AST. Matching returns the set of
# end positions reachable from a start position (alternation may consume
# differently), so a full match is ``len(s) in match(root, s, 0)``.


class _Class:
    """A character class: a set of singles plus ranges, with optional negation."""

    def __init__(self, negate: bool):
        self.negate = negate
        self._ranges: list[tuple[int, int]] = []
        self._singles: set[str] = set()

    def add_char(self, ch: str) -> None:
        self._singles.add(ch)

    def add_range(self, lo: str, hi: str) -> None:
        self._ranges.append((ord(lo), ord(hi)))

    def matches(self, ch: str) -> bool:
        if not ch:
            return False
        o = ord(ch)
        hit = ch in self._singles or any(lo <= o <= hi for lo, hi in self._ranges)
        return (not hit) if self.negate else hit


def _parse_escape(src: str, i: int) -> tuple[str, int]:
    """Decode a ``\\x``-style escape at src[i] (i points at the backslash)."""
    nxt = src[i + 1]
    if nxt == "x":
        return chr(int(src[i + 2 : i + 4], 16)), i + 4
    simple = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\", '"': '"', "/": "/", "b": "\b", "f": "\f"}
    if nxt in simple:
        return simple[nxt], i + 2
    if nxt == "u":
        return chr(int(src[i + 2 : i + 6], 16)), i + 6
    raise ValueError(f"unsupported escape at offset {i}")


def _parse_class(src: str, i: int) -> tuple[_Class, int]:
    """Parse a ``[...]`` class. ``i`` points just after the ``[``."""
    negate = src[i] == "^"
    if negate:
        i += 1
    cls = _Class(negate)
    pending: str | None = None  # last single char, may become a range lo
    while True:
        if i >= len(src):
            raise ValueError("unterminated character class")
        ch = src[i]
        if ch == "]":
            if pending is not None:
                cls.add_char(pending)
            return cls, i + 1
        if ch == "\\":
            parsed, i = _parse_escape(src, i)
            if pending is not None:
                cls.add_char(pending)
            pending = parsed
            continue
        if ch == "-" and pending is not None and src[i + 1 : i + 2] != "]":
            nxt = src[i + 1]
            if nxt == "\\":
                hi, i = _parse_escape(src, i + 1)
            else:
                hi, i = nxt, i + 2
            cls.add_range(pending, hi)
            pending = None
            continue
        if pending is not None:
            cls.add_char(pending)
        pending = ch
        i += 1


def _tokenize(body: str) -> list[tuple[str, object]]:
    """Split a rule body into atoms (ignoring comments/whitespace)."""
    tokens: list[tuple[str, object]] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in " \t\n":
            i += 1
            continue
        if ch == "#":
            break  # comment to end of line
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and body[j] != '"':
                if body[j] == "\\":
                    parsed, j = _parse_escape(body, j)
                    buf.append(parsed)
                else:
                    buf.append(body[j])
                    j += 1
            if j >= n:
                raise ValueError("unterminated string literal")
            tokens.append(("str", "".join(buf)))
            i = j + 1
        elif ch == "[":
            cls, i = _parse_class(body, i + 1)
            tokens.append(("class", cls))
        elif ch == "(":
            tokens.append(("lparen", None))
            i += 1
        elif ch == ")":
            tokens.append(("rparen", None))
            i += 1
        elif ch == "|":
            tokens.append(("alt", None))
            i += 1
        elif ch == "*":
            tokens.append(("star", None))
            i += 1
        elif ch == "?":
            tokens.append(("qmark", None))
            i += 1
        elif ch == "{":
            # bounded repetition {m} / {m,} / {m,n} — used by the grammar's
            # ``[0-9a-fA-F]{4}`` (exactly 4 hex digits in \uXXXX escapes)
            j = i + 1
            lo_digits = ""
            while j < n and body[j].isdigit():
                lo_digits += body[j]
                j += 1
            if not lo_digits:
                raise ValueError(f"unexpected character {ch!r} at offset {i}")
            lo = int(lo_digits)
            hi: int | None = lo
            if j < n and body[j] == ",":
                j += 1
                hi_digits = ""
                while j < n and body[j].isdigit():
                    hi_digits += body[j]
                    j += 1
                hi = int(hi_digits) if hi_digits else None
            if j >= n or body[j] != "}":
                raise ValueError(f"malformed {{m,n}} repetition at offset {i}")
            tokens.append(("braced", (lo, hi)))
            i = j + 1
        elif ch.isalnum() or ch in "_-":
            # llama.cpp GBNF rule names are [A-Za-z0-9-] (dashes, NOT
            # underscores — see the PARSER DIALECT note in envelope.gbnf and
            # TCK-P6-004); underscores are kept here too so the matcher can
            # still read the pre-TCK-P6-004 grammar for the regression probes.
            j = i
            while j < n and (body[j].isalnum() or body[j] in "_-"):
                j += 1
            tokens.append(("name", body[i:j]))
            i = j
        else:
            raise ValueError(f"unexpected character {ch!r} at offset {i}")
    return tokens


def _parse_rule(body: str) -> dict:
    tokens = _tokenize(body)
    pos = 0

    def peek() -> tuple[str, object] | None:
        return tokens[pos] if pos < len(tokens) else None

    def advance() -> tuple[str, object]:
        nonlocal pos
        t = tokens[pos]
        pos += 1
        return t

    def parse_atom() -> dict:
        nonlocal pos
        kind, val = advance()
        if kind == "str":
            return {"type": "str", "value": val}
        if kind == "class":
            return {"type": "class", "value": val}
        if kind == "name":
            return {"type": "ref", "value": val}
        if kind == "lparen":
            node = parse_alt()
            if peek() is None or peek()[0] != "rparen":
                raise ValueError("unbalanced '(' in rule body")
            advance()
            return node
        raise ValueError(f"unexpected token {kind!r} where an atom was expected")

    def parse_item() -> dict:
        node = parse_atom()
        k = peek()[0] if peek() else None
        if k == "star":
            advance()
            return {"type": "rep", "child": node, "op": "*"}
        if k == "qmark":
            advance()
            return {"type": "rep", "child": node, "op": "?"}
        if k == "braced":
            lo, hi = advance()[1]  # type: ignore[misc]
            return {"type": "rep", "child": node, "op": "{}", "lo": lo, "hi": hi}
        return node

    def parse_seq() -> dict:
        items: list[dict] = []
        while peek() is not None and peek()[0] not in ("alt", "rparen"):
            items.append(parse_item())
        return {"type": "seq", "items": items} if len(items) != 1 else items[0]

    def parse_alt() -> dict:
        branches = [parse_seq()]
        while peek() is not None and peek()[0] == "alt":
            advance()
            branches.append(parse_seq())
        return {"type": "alt", "branches": branches} if len(branches) > 1 else branches[0]

    return parse_alt()


class GbnfMatcher:
    """Minimal read-only GBNF matcher for the envelope grammar's constructs."""

    def __init__(self, grammar_text: str):
        self.rules: dict[str, dict] = {}
        current: str | None = None
        for line in grammar_text.splitlines():
            code = line.split("#", 1)[0].rstrip()
            if "::=" in code:
                name, body = code.split("::=", 1)
                current = name.strip()
                self.rules[current] = body
            elif current is not None:
                # continuation lines of the current rule's body (GBNF rules
                # may span multiple lines until the next ``::=``)
                self.rules[current] += "\n" + code
        self.rules = {name: _parse_rule(body) for name, body in self.rules.items()}
        if "root" not in self.rules:
            raise ValueError("grammar has no root rule")

    def accepts(self, s: str) -> bool:
        return len(s) in self._match(self.rules["root"], s, 0)

    def _match(self, node: dict, s: str, pos: int) -> set[int]:
        t = node["type"]
        if t == "seq":
            positions = {pos}
            for item in node["items"]:
                nxt: set[int] = set()
                for p in positions:
                    nxt |= self._match(item, s, p)
                positions = nxt
                if not positions:
                    break
            return positions
        if t == "alt":
            ends: set[int] = set()
            for branch in node["branches"]:
                ends |= self._match(branch, s, pos)
            return ends
        if t == "rep":
            child = node["child"]
            if node["op"] == "?":
                return {pos} | self._match(child, s, pos)
            if node["op"] == "{}":
                return self._match_bounded(child, s, pos, node["lo"], node["hi"])
            ends = {pos}
            frontier = {pos}
            while frontier:
                p = frontier.pop()
                for nxt in self._match(child, s, p):
                    if nxt not in ends:
                        ends.add(nxt)
                        frontier.add(nxt)
            return ends
        if t == "str":
            val = node["value"]
            return {pos + len(val)} if s.startswith(val, pos) else set()
        if t == "class":
            if pos < len(s) and node["value"].matches(s[pos]):
                return {pos + 1}
            return set()
        if t == "ref":
            return self._match(self.rules[node["value"]], s, pos)
        raise ValueError(f"unsupported node type {t!r}")

    def _match_bounded(self, node: dict, s: str, pos: int, lo: int, hi: int | None) -> set[int]:
        """Match ``node`` ``lo..hi`` times (``hi is None`` ⇒ ``lo`` or more).

        ``levels[k]`` holds positions reachable after exactly ``k``
        repetitions; we extend it until we have all levels up to ``hi`` (or
        no further progress when ``hi`` is unbounded), then union the ones
        within ``[lo, hi]``.
        """
        levels = [{pos}]
        k = 0
        while (hi is None and k < lo) or (hi is not None and k < hi):
            nxt: set[int] = set()
            for p in levels[k]:
                nxt |= self._match(node, s, p)
            if not nxt:
                break
            levels.append(nxt)
            k += 1
        ends: set[int] = set()
        for count in range(lo, len(levels)):
            if hi is not None and count > hi:
                break
            ends |= levels[count]
        return ends


@pytest.fixture(scope="module")
def matcher() -> GbnfMatcher:
    return GbnfMatcher(_GRAMMAR.read_text(encoding="utf-8"))


# --------------------------------------------------------------- drift pin

ACCEPT = [
    # one valid envelope per intent (8)
    '{"v":0,"intent":"respond","params":{"text":"hi"}}',
    '{"v":0,"intent":"clarify","params":{"question":"how fast?"}}',
    '{"v":0,"intent":"get_balance","params":{}}',
    '{"v":0,"intent":"get_history","params":{}}',
    '{"v":0,"intent":"get_utxos","params":{}}',
    '{"v":0,"intent":"new_address","params":{}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000}}',
    '{"v":0,"intent":"confirm_tx","params":{"tx_ref":"3f2a9c"}}',
    # both optional-params variants
    '{"v":0,"intent":"get_history","params":{"limit":20}}',
    '{"v":0,"intent":"new_address","params":{"branch":1}}',
    '{"v":0,"intent":"new_address","params":{"branch":0}}',
    # strict key order (canonical v, intent, params) already exercised above
    # whitespace is legal anywhere ws is (tests the recursive ws rule)
    '{\n "v": 0,\n "intent": "get_history",\n "params": {"limit": 20}\n}',
    # limit boundary 100 and grammar-legal 999 (schema rejects >100 later)
    '{"v":0,"intent":"get_history","params":{"limit":100}}',
    '{"v":0,"intent":"get_history","params":{"limit":999}}',
    # ---- Phase 2 (TCK-P2-003): create_tx amount-pair alternation variants
    # amount_sats with optional fee_target tail (each enum literal)
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_target":"fast"}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_target":"medium"}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_target":"slow"}}',
    # amount_usd: decimal and integer forms (JSON has one number type; the
    # schema widens "10" to 10.0 USD)
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":10.5}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":10}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":0.01,"fee_target":"fast"}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":100.0}}',
    # grammar-legal values the schema later bounds: "0" sats (schema floor
    # is 546) and a 16-digit sats amount
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":0}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":9999999999999999}}',
    # whitespace around every token still accepted
    '{\n "v" : 0 ,\n "intent" : "create_tx" ,\n "params" : { "recipient" : "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx" , "amount_usd" : 1.5 , "fee_target" : "medium" }\n}',
    # ---- Phase 3 (TCK-P3-004): sign_tx / broadcast_tx / tx_status drift pins
    # sign_tx: bare tx_ref (no signer tail) and each signer enum literal
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"3f2a9c"}}',
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"3f2a9c","signer":"file"}}',
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"3f2a9c","signer":"hwi"}}',
    # broadcast_tx: tx_ref only (same shape as confirm_tx)
    '{"v":0,"intent":"broadcast_tx","params":{"tx_ref":"3f2a9c"}}',
    # tx_status: exactly 64 lowercase hex chars (the hex_txid class)
    '{"v":0,"intent":"tx_status","params":{"txid":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}',
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 64 + '"}}',
    # whitespace around tokens in the new branches too
    '{\n "v" : 0 ,\n "intent" : "tx_status" ,\n "params" : { "txid" : "' + "b" * 64 + '" }\n}',
    # ---- Phase 4 (TCK-P4-003): node_status drift pin (empty params)
    '{"v":0,"intent":"node_status","params":{}}',
    '{\n "v" : 0 ,\n "intent" : "node_status" ,\n "params" : { }\n}',
]

REJECT = [
    "missing v",
    '{"intent":"respond","params":{"text":"hi"}}',
    "wrong key order (intent before v)",
    '{"intent":"respond","v":0,"params":{"text":"hi"}}',
    "unknown intent",
    '{"v":0,"intent":"fly_to_moon","params":{}}',
    "extra top-level key",
    '{"v":0,"intent":"respond","params":{"text":"hi"},"extra":1}',
    "respond params extra key",
    '{"v":0,"intent":"respond","params":{"text":"hi","lang":"en"}}',
    "clarify params extra key",
    '{"v":0,"intent":"clarify","params":{"question":"q","x":1}}',
    "get_balance params extra key",
    '{"v":0,"intent":"get_balance","params":{"verbose":true}}',
    "get_history params extra key",
    '{"v":0,"intent":"get_history","params":{"limit":5,"since":1}}',
    "get_utxos params extra key",
    '{"v":0,"intent":"get_utxos","params":{"verbose":true}}',
    "new_address params extra key",
    '{"v":0,"intent":"new_address","params":{"branch":0,"count":1}}',
    "get_utxos with limit (whole-object alternation)",
    '{"v":0,"intent":"get_utxos","params":{"limit":5}}',
    "get_history leading-zero limit",
    '{"v":0,"intent":"get_history","params":{"limit":020}}',
    "new_address branch=2",
    '{"v":0,"intent":"new_address","params":{"branch":2}}',
    "malformed JSON truncation: missing closing braces",
    '{"v":0,"intent":"respond","params":{"text":"hi"',
    "malformed JSON truncation: unterminated params object",
    '{"v":0,"intent":"get_balance","params":{',
    "malformed JSON truncation: unterminated string",
    '{"v":0,"intent":"respond","params":{"text":"hi',
    "malformed JSON truncation: mid-envelope",
    '{"v":0,"intent":"respond"',
    # ---- Phase 2 (TCK-P2-003): create_tx / confirm_tx drift pins
    "create_tx BOTH amounts (amount_pair alternation makes this syntactically impossible)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"amount_usd":10}}',
    "create_tx missing recipient (recipient is a required first key)",
    '{"v":0,"intent":"create_tx","params":{"amount_sats":1000}}',
    "create_tx wrong key order (amount before recipient)",
    '{"v":0,"intent":"create_tx","params":{"amount_sats":1000,"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"}}',
    "create_tx wrong key order (fee_target before the amount)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","fee_target":"fast","amount_sats":1000}}',
    "create_tx fee_target with neither amount (tail requires an amount first)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","fee_target":"fast"}}',
    "create_tx amount_usd exponent notation (conservative grammar narrowing; schema would accept 1e2)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":1e2}}',
    "create_tx amount_usd trailing point without fraction digits",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":10.}}',
    "create_tx amount_usd point without integer part",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":.5}}',
    "create_tx amount_sats leading zeros",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":01000}}',
    "create_tx amount_sats 17 digits (16-digit syntactic cap; schema ceiling is semantic)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":21000000000000000}}',
    "create_tx negative amount (no sign in the number rules)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":-5}}',
    "create_tx unknown fee_target literal",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_target":"urgent"}}',
    "create_tx fee_target case-sensitive literal",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_target":"FAST"}}',
    "create_tx params extra key",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"memo":"x"}}',
    "create_tx null amount",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":null}}',
    "confirm_tx params extra key (decision params rejected: gate is app code, ADR-0013)",
    '{"v":0,"intent":"confirm_tx","params":{"tx_ref":"abc","decision":"yes"}}',
    "confirm_tx missing tx_ref",
    '{"v":0,"intent":"confirm_tx","params":{}}',
    "confirm_tx with create_tx keys (intent->params coupling)",
    '{"v":0,"intent":"confirm_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000}}',
    # ---- Phase 3 (TCK-P3-004): sign_tx / broadcast_tx / tx_status drift pins
    "sign_tx params extra key (device policy is the handler's decision)",
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"abc","device":"ledger"}}',
    "sign_tx unknown signer literal (closed enum file|hwi)",
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"abc","signer":"ledger"}}',
    "sign_tx signer case-sensitive literal",
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"abc","signer":"FILE"}}',
    "sign_tx signer before tx_ref (strict key order)",
    '{"v":0,"intent":"sign_tx","params":{"signer":"file","tx_ref":"abc"}}',
    "sign_tx signer null (omission is expressed by leaving the key out)",
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"abc","signer":null}}',
    "sign_tx missing tx_ref",
    '{"v":0,"intent":"sign_tx","params":{}}',
    "broadcast_tx params extra key",
    '{"v":0,"intent":"broadcast_tx","params":{"tx_ref":"abc","signed":true}}',
    "broadcast_tx missing tx_ref",
    '{"v":0,"intent":"broadcast_tx","params":{}}',
    "broadcast_tx with signer key (broadcast has no signer tail)",
    '{"v":0,"intent":"broadcast_tx","params":{"tx_ref":"abc","signer":"hwi"}}',
    "tx_status txid 63 chars (hex_txid is exactly 64)",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 63 + '"}}',
    "tx_status txid 65 chars",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 65 + '"}}',
    "tx_status txid uppercase hex (lowercase-only contract, no normalization)",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "A" * 64 + '"}}',
    "tx_status txid non-hex characters",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "g" * 64 + '"}}',
    "tx_status txid path-traversal fragment (charset is the URL guard)",
    '{"v":0,"intent":"tx_status","params":{"txid":"../' + "a" * 61 + '"}}',
    "tx_status txid with spaces",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 32 + " " + "a" * 31 + '"}}',
    "tx_status txid is a JSON string, not an integer",
    '{"v":0,"intent":"tx_status","params":{"txid":' + "1" * 64 + '}}',
    "tx_status missing txid",
    '{"v":0,"intent":"tx_status","params":{}}',
    "tx_status params extra key",
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 64 + '","verbose":true}}',
    "tx_status with tx_ref key (intent->params coupling)",
    '{"v":0,"intent":"tx_status","params":{"tx_ref":"abc"}}',
    # ---- Phase 4 (TCK-P4-003): node_status drift pins
    "node_status params extra key (closed world: {} exactly)",
    '{"v":0,"intent":"node_status","params":{"verbose":true}}',
    "node_status params text key (intent->params coupling)",
    '{"v":0,"intent":"node_status","params":{"text":"x"}}',
    "node_status params refresh key",
    '{"v":0,"intent":"node_status","params":{"refresh":1}}',
]


@pytest.mark.parametrize("sample", ACCEPT, ids=[s.splitlines()[0][:40] for s in ACCEPT])
def test_grammar_accepts(matcher: GbnfMatcher, sample: str):
    assert matcher.accepts(sample), f"grammar should ACCEPT: {sample!r}"


@pytest.mark.parametrize(
    "sample",
    [s for s in REJECT[1::2]],
    ids=[s for s in REJECT[0::2]],
)
def test_grammar_rejects(matcher: GbnfMatcher, sample: str):
    assert not matcher.accepts(sample), f"grammar should REJECT: {sample!r}"


def test_grammar_parses_without_unsupported_constructs(matcher: GbnfMatcher):
    """The matcher must be able to parse every rule in the current grammar.

    If a future grammar edit reaches for a construct the test-side matcher
    does not implement, it raises here (fail loudly, not silently).
    """
    assert set(matcher.rules) >= {
        "root",
        "envelope",
        "intent-body",
        "respond",
        "clarify",
        "get-balance",
        "get-history",
        "params-history",
        "limit-int",
        "get-utxos",
        "new-address",
        "params-new-address",
        "branch-digit",
        "create-tx",
        "params-create-tx",
        "recipient-kv",
        "amount-pair",
        "amount-sats-kv",
        "amount-usd-kv",
        "create-tx-tail",
        "fee-target-kv",
        "fee-target",
        "sats-int",
        "usd-num",
        "confirm-tx",
        "sign-tx",
        "params-sign-tx",
        "sign-tx-tail",
        "signer-enum",
        "broadcast-tx",
        "tx-status",
        "hex-txid",
        "node-status",
        "string",
        "ws",
    }


def test_grammar_intent_branches_cover_the_closed_enum(matcher: GbnfMatcher):
    """Every closed intent name appears as an alternation of intent_body."""
    text = _GRAMMAR.read_text(encoding="utf-8")
    for intent in (
        "respond",
        "clarify",
        "get_balance",
        "get_history",
        "get_utxos",
        "new_address",
        "create_tx",
        "confirm_tx",
        "sign_tx",
        "broadcast_tx",
        "tx_status",
        "node_status",
    ):
        assert f'"{intent}"' in text


def test_grammar_hex_txid_is_exactly_64_lowercase_hex(matcher: GbnfMatcher):
    """Pin the hex-txid rule shape: one class, bounded to exactly 64.

    Lowercase-only ([0-9a-f], no A-F) is the documented contract (ADR-0002
    Phase 3): quoted txids stay verbatim-comparable and URL-safe without
    normalization; the rule text is pinned so a silent widening fails here.
    """
    text = _GRAMMAR.read_text(encoding="utf-8")
    assert "hex-txid ::= [0-9a-f]{64}" in text


# ------------------------------------------------------- installed-parser pin
#
# TCK-P6-004: the whole reason model mode used to segfault was that the test
# suite never actually parsed the grammar through the installed llama.cpp
# wheel. ``LlamaGrammar.from_string`` in 0.3.35 is a no-op holder (it just
# stashes the text; the vendored C++ parser only runs at *generate* time when
# ``llama_sampler_init_grammar`` builds the grammar against a vocab). These
# checks force that real build so a dialect the wheel rejects fails the test
# suite instead of crashing a live model run.

_RULE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")

# Minimal grammars reproducing the two idioms the installed parser rejects.
_UNDERSCORE_NAME_GBNF = 'root ::= env_body\nenv_body ::= "{" "}"\n'
_MULTILINE_CONTINUATION_GBNF = 'root ::= a\na ::= "x"\n   | "y"\n'


@pytest.fixture(scope="module")
def _probe_vocab():
    """A real llama.cpp vocab for the vendored grammar parser (module-scoped).

    Loads the pinned GGUF in **vocab-only** mode (weights are never touched,
    ~0.3s) and returns the ``(vocab, ctypes-lib)`` pair. This is the exact
    handle passed to ``llama_sampler_init_grammar`` by
    ``llama_cpp._internals.LlamaSampler.add_grammar`` — the same call
    ``ModelRuntime.generate`` makes at generate time, which is where the
    pre-TCK-P6-004 grammar produced a NULL sampler and then crashed the run.
    The ``Llama`` object is stashed on the closure so its vocab outlives the
    tests.
    """
    from llama_cpp import Llama  # gated by skipif on every using test
    from llama_cpp import llama_cpp as _lib

    llm = Llama(model_path=str(_PROBE_MODEL), vocab_only=True, verbose=False)
    return llm._model.vocab, _lib


def _parses(vocab, lib, text: str) -> bool:
    """True iff the installed C++ parser builds a non-NULL grammar sampler."""
    import ctypes

    sampler = lib.llama_sampler_init_grammar(vocab, text.encode("utf-8"), b"root")
    null = sampler is None or ctypes.cast(sampler, ctypes.c_void_p).value in (None, 0)
    if not null:
        lib.llama_sampler_free(sampler)
    return not null


_WHEEL = pytest.mark.skipif(
    not _LLAMA_CPP_AVAILABLE, reason="llama-cpp-python wheel not installed"
)
_MODEL = pytest.mark.skipif(
    _PROBE_MODEL is None,
    reason=f"no GGUF (set {_MODEL_PATH_ENV_VAR} or place models/bin/gemma-4-E2B-it-Q4_K_M.gguf)",
)


@_WHEEL
@_MODEL
def test_grammar_parses_under_installed_parser(_probe_vocab):
    """The installed llama.cpp parser genuinely accepts envelope.gbnf.

    This is the authoritative decoder check that replaces the old
    ``LlamaGrammar.from_string``-only assertion (which passed vacuously and
    let the segfaulting grammar ship). A dialect regression that the wheel
    rejects — an underscore rule name or a multi-line body — returns a NULL
    sampler here and fails this test before any model run can crash.
    """
    vocab, lib = _probe_vocab
    text = _GRAMMAR.read_text(encoding="utf-8")
    assert _parses(vocab, lib, text), (
        "envelope.gbnf must parse under the installed llama-cpp-python wheel; "
        "a NULL grammar sampler means the vendored GBNF parser rejected a "
        "construct (rule-name charset or multi-line body)."
    )


@_WHEEL
@_MODEL
def test_installed_parser_rejects_underscore_rule_name(_probe_vocab):
    """Regression (TCK-P6-004): underscores in rule names crash the model run.

    The pre-fix grammar used ``intent_body`` / ``get_balance`` / ... as rule
    NAMES; the vendored parser stops a name at ``_`` and then errors
    ("expecting newline or end at _body"), handing the sampler chain a NULL
    grammar. The rewritten grammar is dash-only, so this probe documents the
    trap the rewrite must never re-introduce.
    """
    vocab, lib = _probe_vocab
    assert not _parses(vocab, lib, _UNDERSCORE_NAME_GBNF)


@_WHEEL
@_MODEL
def test_installed_parser_rejects_multiline_continuation(_probe_vocab):
    """Regression (TCK-P6-004): rule bodies may not span lines.

    A body continued onto the next line with a leading ``|`` makes the
    parser treat the second line as a fresh rule and error ("expecting name
    at |"). Every rule in envelope.gbnf is therefore a single line.
    """
    vocab, lib = _probe_vocab
    assert not _parses(vocab, lib, _MULTILINE_CONTINUATION_GBNF)


def test_grammar_uses_installed_parser_dialect():
    """Always-on static guard for the installed-parser dialect (no wheel/model).

    CI runs this even where the real-parse probes above skip (wheel absent or
    weights not downloaded). Two invariants, both required by the vendored
    llama.cpp parser and both silently fatal at generate time if violated:

    1. every rule NAME is ``[A-Za-z0-9-]`` — no underscores;
    2. every non-blank content line is a COMPLETE single-line rule — no rule
       body continued onto a following line (the parser ends a rule at the
       newline).

    Comments (a ``#`` starts a comment anywhere) and blank lines are skipped;
    this file never places a ``#`` inside a string literal, so the naive
    split matches the matcher's own comment handling.
    """
    for lineno, raw in enumerate(_GRAMMAR.read_text(encoding="utf-8").splitlines(), 1):
        code = raw.split("#", 1)[0].rstrip()
        if not code.strip():
            continue
        assert "::=" in code, (
            f"envelope.gbnf line {lineno} is a rule-body continuation; the "
            f"installed parser ends a rule at the newline: {raw!r}"
        )
        name = code.split("::=", 1)[0].strip()
        assert "_" not in name, (
            f"envelope.gbnf line {lineno} rule name has an underscore, which "
            f"the installed parser rejects: {name!r}"
        )
        assert _RULE_NAME_RE.fullmatch(name), (
            f"envelope.gbnf line {lineno} has an invalid rule name: {name!r}"
        )
