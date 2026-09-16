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
    # ---- TCK-FEE-002: explicit fee_rate_sat_vb tail (fee_target's exclusive sibling)
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_rate_sat_vb":1}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_rate_sat_vb":10000}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_usd":10.5,"fee_rate_sat_vb":7}}',
    # grammar-legal rate the schema later bounds: 0 (below the floor) and a
    # 5-digit 99999 (above MAX_FEE_RATE_SAT_VB=10000) — grammar admits,
    # schema rejects (same loose-grammar/tight-schema split as limit/sats)
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_rate_sat_vb":0}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":546,"fee_rate_sat_vb":99999}}',
    # whitespace around the new tail
    '{\n "v" : 0 ,\n "intent" : "create_tx" ,\n "params" : { "recipient" : "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx" , "amount_sats" : 546 , "fee_rate_sat_vb" : 5 }\n}',
    # ---- TCK-TX-SELF-001: self_transfer drift pins (mode↔key coupling)
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":4}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":20,"fee_target":"fast"}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":546}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":2100000000000000,"fee_target":"slow"}}',
    # whitespace around every token of the new branch
    '{\n "v" : 0 ,\n "intent" : "self_transfer" ,\n "params" : { "mode" : "split" , "parts" : 2 }\n}',
    # grammar-legal values the schema later bounds: parts 1/99 (outside
    # 2..20) and below_size 0 (below the 546 floor) and "0" admitted
    # (schema rejects) — same loose-grammar/tight-schema split as limit
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":1}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":99}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":0}}',
    # ---- TCK-CPFP-001: self_transfer cpfp-mode drift pins (additive mode:
    # bare, +merge_coin (both literals), +fee_target, +both; strict order
    # mode < merge_coin < fee_target, the bump_fee optional-tail idiom)
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp"}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":true}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":false,"fee_target":"fast"}}',
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","fee_target":"slow"}}',
    # whitespace around every token of the new branch
    '{\n "v" : 0 ,\n "intent" : "self_transfer" ,\n "params" : { "mode" : "cpfp" , "merge_coin" : true , "fee_target" : "medium" }\n}',
    # ---- TCK-RBF-003: bump_fee drift pins (target + optional tail)
    '{"v":0,"intent":"bump_fee","params":{"target":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"pending-3f2a9c"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","funding_ref":"3"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_target":"fast"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_target":"medium"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_target":"slow"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_rate_sat_vb":5}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","funding_ref":"3","fee_target":"fast"}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","funding_ref":"3","fee_rate_sat_vb":10000}}',
    # whitespace around every token of the new branch
    '{\n "v" : 0 ,\n "intent" : "bump_fee" ,\n "params" : { "target" : "abc" , "funding_ref" : "3" , "fee_target" : "slow" }\n}',
    # grammar-legal rate the schema later bounds: 0 and 99999 (loose/tight split)
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_rate_sat_vb":0}}',
    '{"v":0,"intent":"bump_fee","params":{"target":"' + "a" * 64 + '","fee_rate_sat_vb":99999}}',
    # ---- TCK-CHAT-001: get_addresses + the additive address_number key
    '{"v":0,"intent":"get_addresses","params":{}}',
    '{"v":0,"intent":"get_addresses","params":{"address_number":3}}',
    '{"v":0,"intent":"get_balance","params":{"address_number":1}}',
    '{"v":0,"intent":"get_utxos","params":{"address_number":9999999}}',
    # whitespace around every token of the new branches
    '{\n "v" : 0 ,\n "intent" : "get_addresses" ,\n "params" : { "address_number" : 7 }\n}',
    # grammar-legal values the schema/handler later bound: 10000000 (8-digit
    # syntactic cap rejects — asserted in REJECT below); the registry
    # bound-check is engine-side, the grammar only carries the shape
    # ---- TCK-CHAT-005: money-filter drift pins (additive optional keys on
    # get_history/get_utxos; strict order limit/address_number < direction
    # < since < label_set < label_mode; relative since ONLY)
    '{"v":0,"intent":"get_history","params":{"direction":"in"}}',
    '{"v":0,"intent":"get_history","params":{"direction":"out"}}',
    '{"v":0,"intent":"get_history","params":{"since":{"days":14}}}',
    '{"v":0,"intent":"get_history","params":{"since":{"weeks":2}}}',
    '{"v":0,"intent":"get_history","params":{"since":{"months":1}}}',
    '{"v":0,"intent":"get_history","params":{"label_set":["kyc"]}}',
    '{"v":0,"intent":"get_history","params":{"label_set":["kyc","Spearmint","a b"]}}',
    '{"v":0,"intent":"get_history","params":{"label_set":["kyc"],"label_mode":"include"}}',
    '{"v":0,"intent":"get_history","params":{"limit":5,"direction":"in","since":{"days":7},"label_set":["a"],"label_mode":"exclude"}}',
    '{"v":0,"intent":"get_utxos","params":{"label_set":["kyc"]}}',
    '{"v":0,"intent":"get_utxos","params":{"since":{"weeks":2},"label_set":["kyc"],"label_mode":"exclude"}}',
    '{"v":0,"intent":"get_utxos","params":{"address_number":3,"direction":"in","since":{"days":30},"label_set":["kyc"]}}',
    # label_mode WITHOUT label_set is grammatical (omission is just leaving
    # keys out) — the SCHEMA pairing rule is what refuses the carrier
    '{"v":0,"intent":"get_history","params":{"label_mode":"exclude"}}',
    # whitespace/newlines around every token of the new chain, incl. the array
    '{\n "v" : 0 ,\n "intent" : "get_history" ,\n "params" : { "direction" : "in" , "since" : { "weeks" : 2 } , "label_set" : [ "kyc" , "mine" ] }\n}',
    # grammar-legal values the schema later bounds (loose/tight split): days
    # 9999 > 3660, weeks 523 > 522, months 999 > 120
    '{"v":0,"intent":"get_history","params":{"since":{"days":9999}}}',
    '{"v":0,"intent":"get_history","params":{"since":{"weeks":523}}}',
    '{"v":0,"intent":"get_utxos","params":{"since":{"months":999}}}',
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
    "get_history since as a bare integer (windows are relative OBJECTS; absolute time is unrepresentable)",
    '{"v":0,"intent":"get_history","params":{"limit":5,"since":1}}',
    "get_history since a bare timestamp integer (the model NEVER carries absolute time)",
    '{"v":0,"intent":"get_history","params":{"since":1710000000}}',
    "get_history since empty object (exactly one unit required)",
    '{"v":0,"intent":"get_history","params":{"since":{}}}',
    "get_history since two units (whole-object alternation admits one)",
    '{"v":0,"intent":"get_history","params":{"since":{"days":1,"weeks":1}}}',
    "get_history since unknown unit key",
    '{"v":0,"intent":"get_history","params":{"since":{"hours":3}}}',
    "get_history since days 0 (period-int [1-9] start)",
    '{"v":0,"intent":"get_history","params":{"since":{"days":0}}}',
    "get_history since days leading zero",
    '{"v":0,"intent":"get_history","params":{"since":{"days":014}}}',
    "get_history since days 5 digits (4-digit syntactic cap; schema cap is semantic)",
    '{"v":0,"intent":"get_history","params":{"since":{"days":10000}}}',
    "get_history direction unknown literal (closed enum in|out)",
    '{"v":0,"intent":"get_history","params":{"direction":"both"}}',
    "get_history direction case-sensitive literal",
    '{"v":0,"intent":"get_history","params":{"direction":"IN"}}',
    "get_history direction null (omission is leaving the key out)",
    '{"v":0,"intent":"get_history","params":{"direction":null}}',
    "get_history label_set empty array (1+ element required)",
    '{"v":0,"intent":"get_history","params":{"label_set":[]}}',
    "get_history label_set bare string (array only)",
    '{"v":0,"intent":"get_history","params":{"label_set":"kyc"}}',
    "get_history label_set non-string element",
    '{"v":0,"intent":"get_history","params":{"label_set":[1]}}',
    "get_history label_mode unknown literal (closed enum include|exclude)",
    '{"v":0,"intent":"get_history","params":{"label_set":["a"],"label_mode":"notin"}}',
    "get_history label_mode BEFORE label_set (strict chain order)",
    '{"v":0,"intent":"get_history","params":{"label_mode":"exclude","label_set":["a"]}}',
    "get_history direction before limit (strict chain order)",
    '{"v":0,"intent":"get_history","params":{"direction":"in","limit":5}}',
    "get_history since before direction (strict chain order)",
    '{"v":0,"intent":"get_history","params":{"since":{"days":7},"direction":"in"}}',
    "get_history duplicate direction key (tail chain admits one)",
    '{"v":0,"intent":"get_history","params":{"direction":"in","direction":"out"}}',
    "get_history label_set null (omission is leaving the key out)",
    '{"v":0,"intent":"get_history","params":{"label_set":null}}',
    "get_utxos filters BEFORE address_number (strict chain order)",
    '{"v":0,"intent":"get_utxos","params":{"direction":"in","address_number":3}}',
    "get_balance with a filter key (the widen rode history/utxos ONLY)",
    '{"v":0,"intent":"get_balance","params":{"direction":"in"}}',
    "get_addresses with a label key (filters ride history/utxos only)",
    '{"v":0,"intent":"get_addresses","params":{"label_set":["kyc"]}}',
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
    # ---- TCK-FEE-002: fee-rate tail drift pins
    "create_tx BOTH fee_target and fee_rate_sat_vb (exclusive tail alternation; schema re-checks)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_target":"fast","fee_rate_sat_vb":5}}',
    "create_tx BOTH fee knobs, rate first (order recipient < amount < single fee knob)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":5,"fee_target":"fast"}}',
    "create_tx fee_rate_sat_vb before the amount (strict key order)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","fee_rate_sat_vb":5,"amount_sats":1000}}',
    "create_tx fee_rate_sat_vb 6 digits (5-digit syntactic cap; schema ceiling is semantic)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":100000}}',
    "create_tx fee_rate_sat_vb decimal (integer sat/vB only)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":1.5}}',
    "create_tx fee_rate_sat_vb exponent notation (conservative grammar narrowing)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":1e2}}',
    "create_tx fee_rate_sat_vb negative (no sign in the number rules)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":-5}}',
    "create_tx fee_rate_sat_vb leading zero",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":05}}',
    "create_tx fee_rate_sat_vb null (omission is expressed by leaving the key out)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":null}}',
    "create_tx fee_rate_sat_vb quoted string",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","amount_sats":1000,"fee_rate_sat_vb":"5"}}',
    "create_tx fee_rate_sat_vb with neither amount (tail requires an amount first)",
    '{"v":0,"intent":"create_tx","params":{"recipient":"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx","fee_rate_sat_vb":5}}',
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
    # ---- TCK-TX-SELF-001: self_transfer drift pins (money-invention guard)
    "self_transfer split missing parts (mode↔key coupling makes it impossible)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split"}}',
    "self_transfer consolidate missing below_size_sats",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate"}}',
    "self_transfer split with the consolidate key (whole-params alternation)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","below_size_sats":1000}}',
    "self_transfer consolidate with the split key",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","parts":4}}',
    "self_transfer both number keys on split",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":4,"below_size_sats":1000}}',
    "self_transfer unknown mode literal (closed enum)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"merge","parts":4}}',
    "self_transfer case-sensitive mode literal",
    '{"v":0,"intent":"self_transfer","params":{"mode":"Split","parts":4}}',
    "self_transfer mode missing entirely",
    '{"v":0,"intent":"self_transfer","params":{"parts":4}}',
    "self_transfer empty params",
    '{"v":0,"intent":"self_transfer","params":{}}',
    "self_transfer MODELS A RECIPIENT ADDRESS — unrepresentable by design",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":2,"recipient":"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}}',
    "self_transfer MODELS AN AMOUNT — unrepresentable by design",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":1000,"amount_sats":500}}',
    "self_transfer MODELS AN OUTPOINT — unrepresentable by design",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":2,"txid":"' + "a" * 64 + '"}}',
    "self_transfer fee_rate_sat_vb not offered on this intent (fee_target only)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":4,"fee_rate_sat_vb":5}}',
    "self_transfer fee_target before the mode key (strict key order)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","fee_target":"fast","parts":4}}',
    "self_transfer parts before mode (strict key order)",
    '{"v":0,"intent":"self_transfer","params":{"parts":4,"mode":"split"}}',
    "self_transfer below_size_sats before mode (strict key order)",
    '{"v":0,"intent":"self_transfer","params":{"below_size_sats":1000,"mode":"consolidate"}}',
    "self_transfer unknown fee_target literal",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":4,"fee_target":"urgent"}}',
    "self_transfer parts-int rejects lone 0 ([1-9] [0-9]? idiom)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":0}}',
    "self_transfer parts 100 (2-digit syntactic cap; schema bound is semantic)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":100}}',
    "self_transfer parts leading zero",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":04}}',
    "self_transfer parts negative (no sign in the number rules)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":-2}}',
    "self_transfer below_size_sats exponent (decimal-only number idiom)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":1e3}}',
    "self_transfer below_size_sats string (must be an integer)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":"1000"}}',
    # ---- TCK-CPFP-001: cpfp-mode drift pins (additive mode, closed keys)
    "self_transfer cpfp with the split key (whole-params alternation)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","parts":4}}',
    "self_transfer cpfp with the consolidate key",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","below_size_sats":1000}}',
    "self_transfer merge_coin on split (only the cpfp branch owns it)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"split","parts":4,"merge_coin":true}}',
    "self_transfer merge_coin on consolidate",
    '{"v":0,"intent":"self_transfer","params":{"mode":"consolidate","below_size_sats":1000,"merge_coin":false}}',
    "self_transfer merge_coin null (omission is leaving the key out)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":null}}',
    "self_transfer merge_coin integer 1 (only the JSON bool literals)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":1}}',
    "self_transfer merge_coin string",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":"true"}}',
    "self_transfer merge_coin before mode (strict key order)",
    '{"v":0,"intent":"self_transfer","params":{"merge_coin":true,"mode":"cpfp"}}',
    "self_transfer fee_target before merge_coin (mode < merge_coin < fee_target)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","fee_target":"fast","merge_coin":true}}',
    "self_transfer fee_rate_sat_vb on cpfp (self_transfer tail is fee_target only)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","fee_rate_sat_vb":5}}',
    "self_transfer duplicate merge_coin key (tail chain admits one)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","merge_coin":true,"merge_coin":false}}',
    "self_transfer cpfp with a bump_fee key (intent->params coupling)",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","target":"abc"}}',
    "self_transfer cpfp MODELS AN OUTPOINT — unrepresentable by design",
    '{"v":0,"intent":"self_transfer","params":{"mode":"cpfp","outpoint":"' + "a" * 64 + ':0"}}',
    # ---- TCK-RBF-003: bump_fee drift pins (target + optional tail)
    "bump_fee empty params (target is required)",
    '{"v":0,"intent":"bump_fee","params":{}}',
    "bump_fee target missing (target is the required first key)",
    '{"v":0,"intent":"bump_fee","params":{"funding_ref":"3"}}',
    "bump_fee wrong key order (funding_ref before target)",
    '{"v":0,"intent":"bump_fee","params":{"funding_ref":"3","target":"abc"}}',
    "bump_fee fee_target before target (strict key order)",
    '{"v":0,"intent":"bump_fee","params":{"fee_target":"fast","target":"abc"}}',
    "bump_fee BOTH fee knobs (exclusive tail alternation; schema re-checks)",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","fee_target":"fast","fee_rate_sat_vb":5}}',
    "bump_fee rate before funding_ref (strict key order)",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","fee_rate_sat_vb":5,"funding_ref":"3"}}',
    "bump_fee unknown fee_target literal",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","fee_target":"urgent"}}',
    "bump_fee fee_rate_sat_vb decimal (integer sat/vB only)",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","fee_rate_sat_vb":1.5}}',
    "bump_fee fee_rate_sat_vb 6 digits (5-digit syntactic cap)",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","fee_rate_sat_vb":100000}}',
    "bump_fee params extra key (closed world)",
    '{"v":0,"intent":"bump_fee","params":{"target":"abc","memo":"x"}}',
    "bump_fee with tx_ref key (intent->params coupling)",
    '{"v":0,"intent":"bump_fee","params":{"tx_ref":"abc"}}',
    "bump_fee target is a JSON integer (must be a string)",
    '{"v":0,"intent":"bump_fee","params":{"target":64}}',
    # ---- TCK-CHAT-001: address_number drift pins (number-only carrier)
    "get_balance address_number 0 (grammar [1-9] start; the schema floor rejects too)",
    '{"v":0,"intent":"get_balance","params":{"address_number":0}}',
    "get_balance address_number 8 digits (7-digit syntactic cap)",
    '{"v":0,"intent":"get_balance","params":{"address_number":10000000}}',
    "get_balance address_number negative (no sign in the number rules)",
    '{"v":0,"intent":"get_balance","params":{"address_number":-3}}',
    "get_balance address_number leading zero",
    '{"v":0,"intent":"get_balance","params":{"address_number":03}}',
    "get_balance address_number null (omission is leaving the key out)",
    '{"v":0,"intent":"get_balance","params":{"address_number":null}}',
    "get_balance address_number as a string",
    '{"v":0,"intent":"get_balance","params":{"address_number":"3"}}',
    "get_balance address_number as a bool",
    '{"v":0,"intent":"get_balance","params":{"address_number":true}}',
    "get_utxos BOTH limit and address_number (closed whole-object alternation)",
    '{"v":0,"intent":"get_utxos","params":{"limit":5,"address_number":3}}',
    "get_utxos address_number BEFORE nothing is fine but a second key is not",
    '{"v":0,"intent":"get_utxos","params":{"address_number":3,"address":"bc1q"}}',
    "get_addresses with an address key — the model cannot CARRY an address",
    '{"v":0,"intent":"get_addresses","params":{"address":"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}}',
    "get_addresses with a recipient key (intent->params coupling)",
    '{"v":0,"intent":"get_addresses","params":{"recipient":"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}}',
    "get_addresses params extra key (closed world)",
    '{"v":0,"intent":"get_addresses","params":{"verbose":true}}',
    "get_addresses address_number with a txid-looking value (int only)",
    '{"v":0,"intent":"get_addresses","params":{"address_number":"' + "a" * 64 + '"}}',
    "get_addresses with a bump_fee key (intent->params coupling)",
    '{"v":0,"intent":"get_addresses","params":{"target":"abc"}}',
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
        "params-balance",
        "address-number-kv",
        "address-number-int",
        "get-history",
        "params-history",
        "limit-int",
        "get-utxos",
        "params-utxos",
        "get-addresses",
        "params-get-addresses",
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
        "fee-rate-kv",
        "fee-rate-int",
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
        "self-transfer",
        "params-self-transfer",
        "split-params",
        "consolidate-params",
        "mode-split",
        "mode-consolidate",
        "parts-kv",
        "parts-int",
        "below-size-kv",
        "self-transfer-tail",
        "cpfp-params",
        "mode-cpfp",
        "cpfp-tail",
        "cpfp-tail-after-merge",
        "merge-coin-kv",
        "bool-lit",
        "bump-fee",
        "params-bump-fee",
        "bump-fee-tail",
        "bump-fee-tail-after-ref",
        "funding-ref-kv",
        "bump-fee-fee-kv",
        # TCK-CHAT-005: money-filter chain (shared by get_history/get_utxos)
        "filter-tail-all",
        "filter-tail-after-direction",
        "filter-tail-after-since",
        "filter-tail-after-label-set",
        "direction-kv",
        "direction-enum",
        "since-kv",
        "since-value",
        "since-days-kv",
        "since-weeks-kv",
        "since-months-kv",
        "period-int",
        "label-set-kv",
        "label-set-value",
        "label-mode-kv",
        "label-mode-enum",
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
        "self_transfer",
        "bump_fee",
        "get_addresses",
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


# ------------------------------------------------------------------ field-order pin
#
# TCK-CHAT-005B (b): the grammar enforces STRICT key order per intent's
# params (envelope.gbnf header + each branch rule; cross-checked by the
# ACCEPT/REJECT drift pins above), and the pydantic schema serializes params
# in declaration/MRO order. These must agree, or a future field added to one
# side silently desyncs the two (the grammar would accept keys in one order
# while ``model_dump`` re-emits them in another — benign while the model is
# the only producer, latent for any consumer that round-trips dumps). This
# pin asserts the keys present in a ``model_dump`` keep the grammar's
# relative order for EVERY intent, so a field addition on either side fails
# the suite if it desyncs them.

#: The GRAMMAR's strict param key set AND order per intent (envelope.gbnf
#: branch rules). ``create_tx``/``self_transfer``/``bump_fee`` list BOTH
#: alternatives of their alternation slots. The order check below is a
#: subsequence; the set check (``set(model fields) == set(table)``) is a
#: hard pin on the admitted key SET — so a future field added on either side
#: fails the suite even if it happens to slot into the existing order.
_GRAMMAR_PARAM_ORDER: dict[str, tuple[str, ...]] = {
    "respond": ("text",),
    "clarify": ("question",),
    "get_balance": ("address_number",),
    "get_history": ("limit", "direction", "since", "label_set", "label_mode"),
    "get_utxos": (
        "address_number",
        "direction",
        "since",
        "label_set",
        "label_mode",
    ),
    "get_addresses": ("address_number",),
    "new_address": ("branch",),
    "create_tx": ("recipient", "amount_sats", "amount_usd", "fee_target", "fee_rate_sat_vb"),
    "sign_tx": ("tx_ref", "signer"),
    "broadcast_tx": ("tx_ref",),
    "tx_status": ("txid",),
    "node_status": (),
    "self_transfer": ("mode", "parts", "below_size_sats", "merge_coin", "fee_target"),
    "bump_fee": ("target", "funding_ref", "fee_target", "fee_rate_sat_vb"),
    "confirm_tx": ("tx_ref",),
}

# RESOLVED by TCK-CHAT-005B adjudication (schema reordered to match the
# grammar): both ``get_history`` and ``get_utxos`` now dump in grammar order,
# so the strict-xfail desync markers from this ticket's (never-committed)
# intermediate state were removed and the pin is strict for all 15 intents.



def _sample_params(intent: str) -> object:
    from localwallet.protocol.envelope import (
        BroadcastTxParams,
        BumpFeeParams,
        ClarifyParams,
        ConfirmTxParams,
        CreateTxParams,
        GetAddressesParams,
        GetBalanceParams,
        GetHistoryParams,
        GetUtxosParams,
        NewAddressParams,
        NodeStatusParams,
        RespondParams,
        SelfTransferParams,
        SignTxParams,
        SincePeriod,
        TxStatusParams,
    )
    _BC1 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    return {
        "respond": RespondParams(text="hi"),
        "clarify": ClarifyParams(question="q"),
        "get_balance": GetBalanceParams(address_number=3),
        "get_history": GetHistoryParams(
            limit=5,
            direction="in",
            since=SincePeriod(days=7),
            label_set=["a"],
            label_mode="exclude",
        ),
        "get_utxos": GetUtxosParams(
            address_number=3, direction="in", since=SincePeriod(days=7), label_set=["a"]
        ),
        "get_addresses": GetAddressesParams(address_number=3),
        "new_address": NewAddressParams(branch=1),
        "create_tx": CreateTxParams(
            recipient=_BC1, amount_sats=1000, fee_target="fast"
        ),
        "sign_tx": SignTxParams(tx_ref="abc", signer="file"),
        "broadcast_tx": BroadcastTxParams(tx_ref="abc"),
        "tx_status": TxStatusParams(txid="a" * 64),
        "node_status": NodeStatusParams(),
        "self_transfer": SelfTransferParams(mode="split", parts=4, fee_target="fast"),
        "bump_fee": BumpFeeParams(target="abc", funding_ref="3", fee_target="fast"),
        "confirm_tx": ConfirmTxParams(tx_ref="abc"),
    }[intent]


def _params_is_subsequence(dump_keys: list[str], grammar_order: tuple[str, ...]) -> bool:
    """True iff the dumped keys keep the grammar's relative order."""
    it = iter(grammar_order)
    for key in dump_keys:
        try:
            while next(it) != key:
                pass
        except StopIteration:
            return False
    return True


@pytest.mark.parametrize("intent", sorted(_GRAMMAR_PARAM_ORDER))
def test_params_model_dump_order_matches_grammar(intent: str) -> None:
    params = _sample_params(intent)
    dump_keys = list(params.model_dump().keys())
    assert _params_is_subsequence(dump_keys, _GRAMMAR_PARAM_ORDER[intent]), (
        f"{intent} model_dump order {dump_keys} desyncs from the grammar's "
        f"param order {list(_GRAMMAR_PARAM_ORDER[intent])}"
    )
    # The grammar's admitted key SET for this intent's params must exactly
    # match the model's declared fields — a future field added to either side
    # (grammar slot or schema) fails here even if it slots into the existing
    # order, which the subsequence check above cannot catch.
    assert set(type(params).model_fields) == set(_GRAMMAR_PARAM_ORDER[intent]), (
        f"{intent} model fields {sorted(type(params).model_fields)} != grammar "
        f"param slots {sorted(_GRAMMAR_PARAM_ORDER[intent])}"
    )
