"""TCK-HW-007 — natural-language "show it on my coldcard" routing.

USER BUG: after a receive address, "show it to me on my coldcard" answered
with the model's own "I am a hardware wallet-only wallet, so I cannot
display anything directly" — a DEAD END (the device display is by design
not a model intent — HW-005 D3/ADR-0020 — so the utterance reached the
LLM with no route and the LLM improvised the misdirection). The fix is the
deterministic PRE-MODEL intercept :func:`app._run_show_on_device_turn`:
show-on-device phrasings resolve their referent ENGINE-side (session
last-shown / CHAT-001 registry number / store-resolved literal address)
and ride the EXISTING ``/verifyaddress`` handler (:func:`_verify_own_address`)
verbatim — bounds checks, guards and device guidance UNCHANGED.

Pinned contracts (ticket invariants):
* value-free resolution; the model never authors the address/branch/index
  (matched turns never reach the model — ``prompts == []`` pins this);
* device BRAND words (coldcard/jade/…) are matcher vocabulary ONLY — the
  display always binds the CONFIGURED signer (HW-004 discipline);
* an unresolved referent gets the deterministic "which address?" ask —
  NEVER the "hardware wallet-only wallet" misdirection;
* a not-our-address literal gets the EXISTING honest not-shown refusal;
* device-absent/locked guidance is the handler's unchanged family;
* anything the closed matcher cannot fully classify falls through to the
  ordinary pipeline UNCHANGED (never-trap), deny vocabulary included.

Offline: in-memory stores, the fixture zpub, the slice-B fake-device seam
(``tests.test_hw005_slice_b_verify`` imported, not re-implemented).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    _HW_DISPLAY_ASK,
    _VERIFY_NOT_SHOWN,
    ADDRESS_REF_UNKNOWN,
    SendSession,
    _show_on_device_request,
    build_dispatch_table,
)
from localwallet.protocol import IntentName
from localwallet.signer.hwi import DeviceAbsentError
from localwallet.tx.flow import TxFlow
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_hw005_slice_b_verify import (
    ADDRS,
    _FakeDevice,
    _patch_device,
    _world,
)

WORLD = derive_fixture_addresses(8)

#: A second REAL mainnet BIP84 account key (BIP39 test vector seed
#: "abandon … about", m/84'/0'/0') so the wallet-safe replace pin names a
#: genuinely different wallet — never a fake key (TCK-WEB-027 shares it).
ZPUB_B = (
    "zpub6rCLFr1h7Jqbumwox2F3vW12jj4PsVVtCBJEyshncxUdPiWDbWRYwq5uP4TTtxM1"
    "iaKr2xATbESKb87rFHP2nF4Coz84QDbqKUfTUAXkZnV"
)


def _device_ok() -> Callable[[str, str, int, int], str]:
    """The honest device: answers with the wallet's OWN derivation."""
    return lambda _pk, _st, branch, index: WORLD[index] if branch == 0 else "bc1qX"


class Chain:
    """The PRODUCTION ``_run_turn`` chain wired like the live pump
    (store + hwi selection + parsed key in scope), with the fake device at
    the ``HwiUsbSigner`` seam and every model prompt recorded."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        behavior: Any = None,
        receive_first: bool = False,
    ) -> None:
        self.store, self.wd, self.wallet, self.selection = _world()
        _patch_device(
            monkeypatch, behavior if behavior is not None else _device_ok()
        )
        self.prompts: list[str] = []
        self.session = SendSession()
        self.table = build_dispatch_table(
            self.store, self.wallet, self.wd.parsed, client=None,
            scan_fn=lambda: None,
        )
        self.out: list[str] = []
        reply: dict[str, int] = {"n": 0}

        def gen(prompt: str, grammar: str | None) -> str:
            del grammar
            self.prompts.append(prompt)
            # With receive_first the FIRST model turn allocates a receive
            # address (the repro's "give me a receive address" leg);
            # every other turn answers with the plain respond line.
            reply["n"] += 1
            if receive_first and reply["n"] == 1:
                return json.dumps({"v": 0, "intent": "new_address", "params": {}})
            return json.dumps(
                {"v": 0, "intent": "respond", "params": {"text": "model was asked."}}
            )

        self.loop = AgentLoop(gen, self.table)

    def turn(self, line: str) -> list[str]:
        before = len(self.out)
        app._run_turn(
            self.loop, TxFlow(), self.session, line, self.out.append,
            table=self.table, store=self.store,
            signer_selection=self.selection, parsed=self.wd.parsed,
        )
        return self.out[before:]

    @property
    def devices(self) -> list[_FakeDevice]:
        return list(_FakeDevice.instances)

    def display_coords(self) -> list[tuple[int, int]]:
        return [c[-2:] for d in self.devices for c in d.display_calls]


# =========================================================================
# The closed matcher — accept vectors (referent classification only; the
# resolution/dispatch tests below drive the production chain)
# =========================================================================


@pytest.mark.parametrize(
    ("line", "kind", "value"),
    [
        # the USER BUG repro verbatim
        ("show it to me on my coldcard", "pronoun", None),
        ("Show it to me on my ColdCard!", "pronoun", None),
        ("show that address on my hardware wallet", "pronoun", None),
        ("display this on my jade", "pronoun", None),
        ("show my address on the device", "pronoun", None),
        ("can you show it on my coldcard?", "pronoun", None),
        # the closed demonstrative binds a bare own-address noun (MINOR-2)
        ("show the address on my jade", "pronoun", None),
        ("display that address on my coldcard", "pronoun", None),
        # registry number (CHAT-001 # shape and taught "address N" shape)
        ("show #3 on my jade", "number", 3),
        ("show address 3 on my coldcard", "number", 3),
        ("display number 2 on my device", "number", 2),
        # explicit literal (resolved engine-side against the store)
        (f"show {ADDRS[0]} on my hardware wallet", "address", ADDRS[0]),
        (f"Display {ADDRS[1].upper()} on my jade", "address", ADDRS[1]),
    ],
)
def test_matcher_accept(line: str, kind: str, value: object) -> None:
    assert _show_on_device_request(line) == (kind, value)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello",
        "show it",                          # no device topic word
        "show address 2",                    # the get_addresses route (no device)
        "unlock my hardware wallet",         # slice A's verb family, no show verb
        "can you see my coldcard",           # ... same
        "show me my coins",                  # plain listing (no device)
        "show me my coins on my device",     # coin word: the CHAT-009 pipeline's
        "show my balance on my device",      # no address referent candidate
        "show my kyc coins on my jade",      # ... coin word, whatever the label
        "don't show it on my coldcard",      # deny suppresses (slice-C rule)
        "do not display that on my jade",    # ... negation too
        "show it on my phone",               # "phone" is not a device word
        "show #2 and #3 on my jade",         # two numbers: ambiguous, release
        "show #2 on my coldcard and #3 on my jade",  # ... same
        f"show {ADDRS[0]} #3 on my jade",    # mixed forms: ambiguous, release
        "send 5000 sats to bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        "label bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4 as kyc",
        "what is my balance",
        "show me my KYC coins?",             # the CHAT-009 repro stays theirs
        "testnet tb1qw508... show on my device",  # no show verb match alone…
        "show #0 on my jade",                # registry numbers are 1-based
        "show me what address is on my coldcard",  # wh-question: a QUERY (MINOR-2)
        "show me what the address is on my coldcard",  # binder after wh-word: still a QUERY (MINOR-2)
    ],
)
def test_matcher_reject(line: str) -> None:
    assert _show_on_device_request(line) is None


def test_two_literals_ambiguous_release() -> None:
    line = f"show {ADDRS[0]} instead of {ADDRS[1]} on my jade"
    assert _show_on_device_request(line) is None


# =========================================================================
# THE REPRO — receive address, then "show it to me on my coldcard"
# =========================================================================


def test_repro_show_it_after_receive_displays_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's exact bug: after the model narrates a fresh receive
    address, the show-on-device line routes to the display handler with
    the RIGHT coordinates, and the model is consulted EXACTLY once (the
    allocation) — never for the display, so the "hardware wallet-only
    wallet" dead-end is structurally unreachable."""
    c = Chain(monkeypatch, receive_first=True)
    first = c.turn("give me a receive address")
    fresh_index = c.store.get_derivation(c.wallet.id, 0).next_index - 1
    assert any(ADDRS[fresh_index] in ln for ln in first)
    assert c.session.last_shown_own == (ADDRS[fresh_index], 0, fresh_index)
    prompts_after_alloc = len(c.prompts)

    out = c.turn("show it to me on my coldcard")
    assert c.display_coords() == [(0, fresh_index)]  # the right address
    assert any("device says it is showing" in ln for ln in out)
    assert len(c.prompts) == prompts_after_alloc      # NO model turn — deterministic
    assert not any("wallet-only" in ln for ln in out)  # never the misdirection line


def test_show_it_via_receive_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """/receive's un-allocated preview is the session's last-shown address;
    its index (== next_index, branch 0) is exactly what the handler's D2
    bounds allow — the device shows the preview."""
    c = Chain(monkeypatch)
    preview = c.store.get_derivation(c.wallet.id, 0).next_index
    app._print_next_receive_address(
        c.store, c.out.append, session=c.session
    )
    assert c.session.last_shown_own == (WORLD[preview], 0, preview)
    c.turn("show it to me on my coldcard")
    assert c.display_coords() == [(0, preview)]


# =========================================================================
# Explicit literal — store-resolved (the HW-005 D2 path)
# =========================================================================


def test_show_literal_address_resolves_through_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = Chain(monkeypatch)
    c.turn(f"show {ADDRS[0]} on my hardware wallet")
    assert c.display_coords() == [(0, 0)]  # issued row: (branch, index)
    assert c.prompts == []                 # fully deterministic
    assert any("device says it is showing" in ln for ln in c.out)


def test_show_not_our_address_honest_line_never_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    foreign = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP173 test vector
    out = c.turn(f"show {foreign} on my hardware wallet")
    assert out == [_VERIFY_NOT_SHOWN]  # the existing honest line, value-free
    assert c.devices == []             # never prompted for a foreign address
    assert c.prompts == []             # and the model never narrates it either


# =========================================================================
# Registry numbers — "show #3 on my jade"
# =========================================================================


def _issue(c: Chain, n: int) -> None:
    """Allocate n fresh addresses through the PRODUCTION narration path
    (each printing registers it: registry #1..#n, rows (0, 0..n-1))."""
    for _ in range(n):
        envelope = app.Envelope(
            v=0, intent=IntentName.NEW_ADDRESS, params=app.NewAddressParams()
        )
        result = c.table[IntentName.NEW_ADDRESS](envelope)
        app._print_new_address(result, c.out.append, session=c.session)


def test_show_registry_number_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    _issue(c, 2)  # ADDRS[0] (#1 from _world) + #2, #3 land at (0,1), (0,2)
    before = len(c.prompts)
    out = c.turn("show #3 on my jade")
    assert c.display_coords() == [(0, 2)]
    assert any("device says it is showing" in ln for ln in out)
    assert len(c.prompts) == before  # no model turn on the display route


def test_show_registry_number_out_of_range_uses_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    out = c.turn("show #99 on my coldcard")
    assert out == [ADDRESS_REF_UNKNOWN]  # the value-free registry clarify
    assert c.devices == []


def test_show_registry_number_for_preview_resolves_via_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /receive preview is NUMBERED but not yet ALLOCATED (no address
    row): its coordinates come from the session's own last-shown stamp —
    never a guess, never a phantom derivation."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    app._print_next_receive_address(c.store, c.out.append, session=c.session)
    preview = c.store.get_derivation(c.wallet.id, 0).next_index  # == 0 here
    number = c.store.registry_number_for(c.wallet.id, WORLD[preview]).number
    c.turn(f"show #{number} on my jade")
    assert c.display_coords() == [(0, preview)]


# =========================================================================
# Unresolved referent — the deterministic ask, never the misdirection
# =========================================================================


def test_unresolved_it_asks_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing shown yet in this session: the ask (value-free), no device
    touch, NO model turn — the "hardware wallet-only wallet" line is
    structurally unreachable because the model is never asked."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    out = c.turn("show it to me on my coldcard")
    assert out == [_HW_DISPLAY_ASK]
    assert "hardware wallet-only" not in out[0]
    assert c.devices == []
    assert c.prompts == []


def test_registry_number_without_wallet_row_and_no_stamp_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry number whose address has NO store row and is NOT the
    session's stamped preview cannot be located — honest not-shown line,
    device untouched (never an unverifiable display)."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    record = c.store.note_address_shown(c.wallet.id, WORLD[7])  # shown, not issued
    fresh = SendSession()                               # no preview in session
    out: list[str] = []
    consumed = app._run_show_on_device_turn(
        fresh, f"show #{record.number} on my jade", out.append,
        store=c.store, signer_selection=c.selection, parsed=c.wd.parsed,
    )
    assert consumed and out == [_VERIFY_NOT_SHOWN]
    assert c.devices == []


# =========================================================================
# Device guidance UNCHANGED (the reused handler's own family)
# =========================================================================


def test_device_absent_guidance_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent device → the EXISTING hwi guidance line printed verbatim
    through the routed path (zero new device strings)."""
    message = "No device found — plug in and unlock your device, then say 'retry'."
    c = Chain(monkeypatch, behavior=DeviceAbsentError(message))
    _issue(c, 1)
    out = c.turn("show it on my coldcard")
    # The EXISTING hwi guidance family printed verbatim through the routed
    # path (zero new device strings), as friendly narration — never an
    # error page, and the model never asked.
    assert out == [message]
    assert c.prompts == []


def test_brand_words_never_switch_the_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """coldcard / jade / bitbox / generic — every brand routes to the
    CONFIGURED signer: the construction fingerprint is always THIS
    wallet's selection (HW-004 config-authoritative discipline)."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    brands = ("coldcard", "jade", "bitbox", "trezor", "ledger",
              "hardware wallet", "device")
    for brand in brands:
        c.turn(f"show {ADDRS[0]} on my {brand}")
    assert len(c.devices) == len(brands)
    assert c.display_coords() == [(0, 0)] * len(brands)
    for d in c.devices:
        assert d.ctor_args[0] == c.selection.fingerprint_hex


# =========================================================================
# Never-trap — unmatched lines reach the ordinary pipeline UNCHANGED
# =========================================================================


@pytest.mark.parametrize(
    "line",
    [
        "don't show it on my coldcard",      # deny → ordinary gate/model path
        "show me my coins",                  # the plain listing (model route)
        "show address 2",                    # the taught get_addresses route
        "show my balance on my device",      # no address referent candidate
    ],
)
def test_unmatched_lines_reach_the_model(line: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never-trap: an unmatched (or deny-suppressed) show line falls
    through to the ORDINARY pipeline UNCHANGED — and the display device
    seam is never constructed."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    c.prompts.clear()
    out = c.turn(line)
    assert c.prompts
    assert any("model was asked" in ln for ln in out)
    assert c.display_coords() == []


def test_preview_stamp_is_all_the_pronoun_needs_no_registry_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp carries (address, branch, index) directly: a pronoun
    display never consults the registry (the handler bounds-check still
    rules — an index past the derivation state can never be stamped)."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    c.session.last_shown_own = (WORLD[0], 0, 0)
    c.turn("show that address on my hardware wallet")
    assert c.display_coords() == [(0, 0)]


# =========================================================================
# MAJOR-1 fix — the CHAT-007 receive+label turn STAMPS last_shown_own
# =========================================================================


def test_receive_label_turn_then_show_it_displays_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin for the dead ``session`` parameter: the TCK-CHAT-007
    receive-address+label turn (``_run_receive_label_turn``) now passes
    ``session`` through its narration, so the fresh address is stamped as
    the session's last-shown — and the immediately-following "show it to me
    on my coldcard" resolves to THAT address and rides the display handler
    at the right coordinates, deterministically (no model turn)."""
    c = Chain(monkeypatch)
    first = c.turn("I need a receive address labeled 'spearmint'")
    fresh_index = c.store.get_derivation(c.wallet.id, 0).next_index - 1
    assert any(ADDRS[fresh_index] in ln for ln in first)
    assert c.session.last_shown_own == (ADDRS[fresh_index], 0, fresh_index)
    prompts_after_alloc = len(c.prompts)

    out = c.turn("show it to me on my coldcard")
    assert c.display_coords() == [(0, fresh_index)]  # the right address
    assert any("device says it is showing" in ln for ln in out)
    assert len(c.prompts) == prompts_after_alloc     # NO model turn
    assert not any("wallet-only" in ln for ln in out)


# =========================================================================
# MINOR-2 fix — the bare-noun rung needs a binding determiner
# =========================================================================


def test_what_address_query_releases_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"show me WHAT address is on my coldcard" is a QUERY, not a display
    command: the bare-address-noun rung must NOT consume it (a wh-word is
    not a binder), so it falls through to the ordinary pipeline — the model
    is asked, and the device is never touched even with a stamp in scope."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    c.session.last_shown_own = (WORLD[0], 0, 0)  # a stamp EXISTS, still no
    c.prompts.clear()
    out = c.turn("show me what address is on my coldcard")
    assert c.prompts                                  # reached the model
    assert any("model was asked" in ln for ln in out)
    assert c.display_coords() == []                   # never a display


def test_the_address_demonstrative_consumes_as_pronoun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"show THE address on my jade" — the closed demonstrative binds the
    bare noun, so it consumes as a pronoun display of the session's
    last-shown address (deterministic, no model turn)."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    c.session.last_shown_own = (WORLD[0], 0, 0)
    out = c.turn("show the address on my jade")
    assert c.display_coords() == [(0, 0)]
    assert c.prompts == []
    assert any("device says it is showing" in ln for ln in out)


# =========================================================================
# SECURITY LOW-2 fix — the stamp is wallet-safe (re-bind before use)
# =========================================================================


def test_stale_stamp_after_watch_key_replace_gets_the_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-session watch-key replace: the stamp names the OLD wallet's
    address, and the active wallet has since been switched. The pronoun
    path re-binds via the active wallet, so "it" NO LONGER resolves to the
    old stamp — the deterministic ask, the device never touched."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    c.session.last_shown_own = (WORLD[0], 0, 0)  # wallet A's issued address
    wb = c.store.create_wallet(
        "replacement", WalletDescriptor.from_key(ZPUB_B).descriptor
    )
    c.store.set_active_wallet(wb.id)            # A is no longer active
    out = c.turn("show it to me on my coldcard")
    assert out == [_HW_DISPLAY_ASK]             # stale → ask, never A's coords
    assert c.display_coords() == []
    assert c.prompts == []


def test_preview_stamp_after_watch_key_replace_gets_the_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /receive PREVIEW stamp is registry-only (no addresses row), so the
    pronoun re-bind cannot fall back on the row's wallet_id — it must check
    the ACTIVE wallet's registry. After a mid-session watch-key replace the
    preview address is registered to the OLD wallet, so "it" no longer
    resolves: the ask, the device never touched (the MAJOR-1 pin)."""
    _patch_device(monkeypatch, _device_ok())
    c = Chain(monkeypatch)
    app._print_next_receive_address(c.store, c.out.append, session=c.session)
    preview = c.store.get_derivation(c.wallet.id, 0).next_index
    assert c.session.last_shown_own == (WORLD[preview], 0, preview)
    assert c.store.get_by_address(WORLD[preview]) is None  # registry-only
    wb = c.store.create_wallet(
        "replacement", WalletDescriptor.from_key(ZPUB_B).descriptor
    )
    c.store.set_active_wallet(wb.id)            # the preview is now OLD wallet's
    out = c.turn("show it to me on my coldcard")
    assert out == [_HW_DISPLAY_ASK]             # preview no longer resolves → ask
    assert c.display_coords() == []
    assert c.prompts == []
