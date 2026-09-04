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

from pathlib import Path

import pytest

_GRAMMAR = (
    Path(__file__).resolve().parent.parent / "src" / "localwallet" / "agent" / "grammar" / "envelope.gbnf"
)


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
        elif ch.isalnum() or ch == "_":
            j = i
            while j < n and (body[j].isalnum() or body[j] == "_"):
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
        "intent_body",
        "respond",
        "clarify",
        "get_balance",
        "get_history",
        "params_history",
        "limit_int",
        "get_utxos",
        "new_address",
        "params_new_address",
        "branch_digit",
        "create_tx",
        "params_create_tx",
        "recipient_kv",
        "amount_pair",
        "amount_sats_kv",
        "amount_usd_kv",
        "create_tx_tail",
        "fee_target_kv",
        "fee_target",
        "sats_int",
        "usd_num",
        "confirm_tx",
        "sign_tx",
        "params_sign_tx",
        "sign_tx_tail",
        "signer_enum",
        "broadcast_tx",
        "tx_status",
        "hex_txid",
        "node_status",
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
    """Pin the hex_txid rule shape: one class, bounded to exactly 64.

    Lowercase-only ([0-9a-f], no A-F) is the documented contract (ADR-0002
    Phase 3): quoted txids stay verbatim-comparable and URL-safe without
    normalization; the rule text is pinned so a silent widening fails here.
    """
    text = _GRAMMAR.read_text(encoding="utf-8")
    assert "hex_txid ::= [0-9a-f]{64}" in text
