"""TCK-PUBLICBCAST-001: public-broadcast fallback (mempool.space POST /api/tx).

When the user's OWN node refuses a broadcast and the engine can POSITIVELY
classify the refusal as fee-floor-shaped, the app emits ONE deterministic
offer line; the SAME signed transaction can then be broadcast via the
public operator — but ONLY through the user's own ConfirmGate-classified
affirmative read from their raw utterance BEFORE the model (an LLM "yes"
is structurally inert; the model has no intent, no envelope, no seam).
The chain half is a single-attempt POST whose returned txid is bound to
sha256d(tx_hex) per TCK-SEC-004 (mismatch = hard stop). Status answers
after a public broadcast say plainly that the user's own node may not
have seen the transaction yet (gossip lag) — never "try again forever".

Pins (the ticket's done-when matrix):
  A. the publicinfo POST (endpoint, single attempt, txid bind, value-free)
  B. offer gating (classes x reason-family matrix; fail-closed to NO offer)
  C. consent shape (utterance-gated dispatch; model authorization refused;
     never-trap close; one offer per signed record)
  D. the public send itself (same retained bytes, SIGNED-state honesty)
  E. status-lag honesty + value-free surfaces everywhere.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import httpx
import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.chain import ChainError
from localwallet.chain.esplora import (
    HTTP_STATUS,
    RPC_ERROR,
    SERVER_REJECTED,
    TXID_BIND_MISMATCH,
)
from localwallet.chain.publicinfo import PublicInfoClient
from localwallet.protocol import IntentName, validate_payload
from localwallet.tx.flow import TxFlowStatus
from tests.test_chain_esplora import BROADCAST_TXID, TX_HEX, ScriptedServer
from tests.test_tx_flow import make_flow

TXID_OK = BROADCAST_TXID  # the embit sha256d bind of TX_HEX (real tx fixture)

FEE_FLOOR_REJECTION = ChainError(
    "broadcast request rejected by the server",
    failure_class=RPC_ERROR,
    exc_name="RPCError",
    rpc_code=-25,
)

# TCK-PUBLICBCAST-004: the gate's known policy-code set is {-25, -26}. -26
# (Core's RPC_VERIFY_REJECTED) is where a genuine "min relay fee not met"
# refusal classically lands; -25 (RPC_VERIFY_ERROR) is the generic wrapper.
# The user's real broadcast failure carried a POSITIVE ``code=2`` on an
# rpc-error envelope — a code OUTSIDE the known set, so the belt-and-braces
# class gate stays closed on it. The floor proof, not the code, is the real
# discriminator, so the offer never arms on an unproven floor for ANY code.
# Deviation record: code=2 is deliberately NOT added to the accepted set (an
# arbitrary positive code is not a named Core policy constant). If the user's
# repro was genuinely fee-too-low the offer arms only once their node reports
# -25/-26 — the user should re-test per MANUAL-WORK MW-9.
FEE_FLOOR_REJECTION_VERIFY = ChainError(
    "transaction rejected",
    failure_class=RPC_ERROR,
    exc_name="RPCError",
    rpc_code=-26,
)
POSITIVE_CODE_REJECTION = ChainError(
    "broadcast request rejected by the server",
    failure_class=RPC_ERROR,
    exc_name="RPCError",
    rpc_code=2,
)


# --------------------------------------------------------------------- A. chain


def _public_client(server: ScriptedServer) -> PublicInfoClient:
    return PublicInfoClient(
        base_url="https://mempool.space/api",
        transport=httpx.MockTransport(server.handler),
    )


def test_publicinfo_broadcast_posts_api_tx_once() -> None:
    """The ONLY write publicinfo ever does: POST {base}/tx (the /tx/push
    form's endpoint), body = the signed hex verbatim, single attempt."""
    server = ScriptedServer(httpx.Response(200, text=TXID_OK))
    client = _public_client(server)
    assert client.broadcast_tx(TX_HEX) == TXID_OK
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/tx"
    assert request.content.decode("ascii") == TX_HEX


def test_publicinfo_broadcast_binds_txid_to_sha256d() -> None:
    """A well-formed FOREIGN txid is a hard stop (TCK-SEC-004 bind,
    inherited from the Esplora client verbatim) — the caller never records
    a txid that is not this transaction's."""
    foreign = "cd" * 32
    server = ScriptedServer(httpx.Response(200, text=foreign))
    client = _public_client(server)
    with pytest.raises(ChainError) as excinfo:
        client.broadcast_tx(TX_HEX)
    assert excinfo.value.failure_class == TXID_BIND_MISMATCH
    assert TXID_OK not in str(excinfo.value) and foreign not in str(excinfo.value)
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("response", "class_name"),
    [
        (httpx.Response(400, text="min relay fee not met"), SERVER_REJECTED),
        (httpx.Response(429), SERVER_REJECTED),
        (httpx.Response(500), SERVER_REJECTED),
    ],
)
def test_publicinfo_broadcast_single_attempt_never_retries(
    response: httpx.Response, class_name: str
) -> None:
    """Double-broadcast discipline: EVERY failure of the POST is ONE
    attempt, value-free (server text never echoed), labeled as the DIAG-005
    refusal class."""
    server = ScriptedServer(response)
    client = _public_client(server)
    with pytest.raises(ChainError) as excinfo:
        client.broadcast_tx(TX_HEX)
    assert excinfo.value.failure_class == class_name
    assert len(server.requests) == 1
    assert TX_HEX not in str(excinfo.value)
    assert "min relay fee not met" not in str(excinfo.value)


# ------------------------------------------------------- the handler harness


class _Node:
    """Wallet-backend test double: scripted broadcast failure, optional
    min-relay-floor capability (``min_relay_centisat_vb`` — bitcoind has
    it) and, TCK-PUBLICBCAST-001 rider, the electrum-shaped gate-only
    seam (``gate=True, capable=False``: ``broadcast_gate_relay_centisat_vb``
    answering from the handshake-retained relayfee, None when the server
    announced nothing usable). A get_tx_status for the status-lag pins."""

    def __init__(
        self,
        *,
        exc: Exception | None = None,
        floor: Any = 100,
        capable: bool = True,
        gate: bool = False,
        ret_txid: str = TXID_OK,
    ) -> None:
        self.exc = exc
        self.floor = floor
        self.broadcasts: list[str] = []
        self.ret_txid = ret_txid
        if capable:
            self.min_relay_centisat_vb = self._floor
        if gate:
            self.broadcast_gate_relay_centisat_vb = self._floor

    def _floor(self) -> Any:
        if isinstance(self.floor, Exception):
            raise self.floor
        return self.floor

    def broadcast_tx(self, tx_hex: str) -> str:
        self.broadcasts.append(tx_hex)
        if self.exc is not None:
            raise self.exc
        return self.ret_txid

    def get_tx_status(self, txid: str) -> Any:  # duck-typed seam
        # TCK-PUBLICBCAST-002 review: the not-found is the typed class
        # PLUS the explicit numeric 404 field — exactly what the real
        # immediate-4xx raise sites carry; the lag detection keys on that
        # evidence, never on dialect text.
        raise ChainError(
            "tx-status request failed: status 404",
            failure_class=HTTP_STATUS,
            http_status=404,
        )


class _Public:
    """PublicInfoClient-shaped double recording every consented POST."""

    def __init__(self, *, exc: Exception | None = None, txid: str = TXID_OK) -> None:
        self.exc = exc
        self.txid = txid
        self.broadcasts: list[str] = []

    def broadcast_tx(self, tx_hex: str) -> str:
        self.broadcasts.append(tx_hex)
        if self.exc is not None:
            raise self.exc
        return self.txid


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    """One SIGNED flow (canonical 50_000-sat staging at an overridable fee
    rate) + the broadcast handler over the fakes (diag003's pattern; the
    hex-extraction seam is pinned, this ticket is about WHO the bytes are
    SENT to and WHETHER the user said so)."""
    monkeypatch.setattr(app, "_extract_signed_tx_hex", lambda _b64: TX_HEX)
    from tests.test_e2e_skeleton import _fixture_parsed

    store = app.Store(None)
    wallet = store.create_wallet("default", "desc")
    store.update_derivation(wallet.id, 1, next_index=1)

    def build(
        *,
        fee_rate: int = 50,
        node: _Node | None = None,
        public: _Public | None = None,
    ):
        flow, _ticks = make_flow()
        signed = _drive_to_signed_at_fee(flow, fee_rate)
        session = app.SendSession()
        handler = app._make_broadcast_tx_handler(
            flow,
            node if node is not None else _Node(exc=ChainError(
                "broadcast request rejected by the server",
                failure_class=RPC_ERROR,
                rpc_code=-25,
            )),
            store,
            wallet.id,
            _fixture_parsed(),
            session=session,
            public_info=public,
        )
        return flow, session, handler, signed

    yield build
    store.close()


def _drive_to_signed_at_fee(flow, fee_rate: int):
    """Stage (fee rate overridable) → confirm → sign, WITHOUT the shared
    drive_to_signed helper's own re-stage (that would hit 'already
    pending' on top of our custom stage)."""
    from tests.test_tx_flow import stage

    pending = stage(flow, fee_rate_centisat_vb=fee_rate)
    from localwallet.tx.flow import GateDecision

    flow.confirm(pending.tx_ref, gate_decision=GateDecision.CONFIRM, at=pending.created_at + 1)
    return flow.mark_signed(pending.tx_ref, "cHNidP8BAFICAAAAAane-signed-payload")


def _env(params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": "broadcast_tx", "params": params}))


# ------------------------------------------------------------------ B. gating


@pytest.mark.parametrize(
    ("exc", "floor", "capable", "fee_rate", "expected"),
    [
        # fee-floor-shaped: policy refusal + node's live floor above the tx.
        (FEE_FLOOR_REJECTION, 100, True, 50, True),
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            100, True, 50, True,
        ),
        # TCK-PUBLICBCAST-004: -26 (Core's real fee-too-low / VERIFY_REJECTED
        # code) joins the known set — arms when the floor is proven.
        (FEE_FLOOR_REJECTION_VERIFY, 100, True, 50, True),
        # at/above the node's advertised floor: the floor cannot explain the
        # refusal — mempool-conflict-shaped → FAIL-CLOSED, no offer. Proven
        # for BOTH accepted codes (-25, -26): the class gate is belt-and-braces,
        # the floor proof is the discriminator.
        (FEE_FLOOR_REJECTION, 100, True, 100, False),
        (FEE_FLOOR_REJECTION, 100, True, 200, False),
        (FEE_FLOOR_REJECTION_VERIFY, 100, True, 100, False),
        (FEE_FLOOR_REJECTION_VERIFY, 100, True, 200, False),
        # other rpc codes / code-less rpc-error: outside the known {-25,-26}
        # set → unclear → no offer. Includes the user's observed positive
        # code=2 (TCK-PUBLICBCAST-004) — still non-arming even with a proven
        # floor, because an arbitrary positive code is not a named policy code.
        (
            ChainError("nope", failure_class=RPC_ERROR, rpc_code=-27),
            100, True, 50, False,
        ),
        (POSITIVE_CODE_REJECTION, 100, True, 50, False),
        (ChainError("nope", failure_class=RPC_ERROR), 100, True, 50, False),
        # integrity + transport classes are not refusals at all.
        (
            ChainError("x", failure_class=TXID_BIND_MISMATCH),
            100, True, 50, False,
        ),
        (ChainError("broadcast failed: network error (ConnectError)"), 100, True, 50, False),
        # honestly-absent capability (electrum): what it enforces is
        # unknowable → unclear → NO OFFER.
        (FEE_FLOOR_REJECTION, None, False, 50, False),
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            None, False, 50, False,
        ),
        # failed / malformed floor answers are "unclear" (fail closed).
        (FEE_FLOOR_REJECTION, TimeoutError("node silent"), True, 50, False),
        (FEE_FLOOR_REJECTION, "100", True, 50, False),
        (FEE_FLOOR_REJECTION, None, True, 50, False),
    ],
)
def test_offer_arms_only_on_fee_floor_shaped_refusal(
    world, exc, floor, capable, fee_rate, expected
) -> None:
    flow, session, handler, signed = world(
        fee_rate=fee_rate,
        node=_Node(exc=exc, floor=floor, capable=capable),
        public=_Public(),
    )
    result = handler(_env({"tx_ref": signed.tx_ref}))
    assert result["error"] == "broadcast_failed"
    assert (result.get("public_bcast_offer") is True) is expected
    assert (session.public_offer_txref == signed.tx_ref) is expected
    assert flow.state is TxFlowStatus.SIGNED  # kept-for-retry, always


@pytest.mark.parametrize(
    ("rpc_code", "fee_rate", "expected"),
    [
        # TCK-PUBLICBCAST-004 direct class-gate pin: BOTH known policy codes
        # arm when the floor is proven (-25 generic wrapper, -26 the classic
        # fee-too-low reject the user's node actually uses).
        (-25, 50, True),
        (-26, 50, True),
        # Never arm when the floor is NOT proven, for ANY code (the proof is
        # the discriminator; the class gate is only belt-and-braces).
        (-25, 100, False),
        (-26, 100, False),
        (-26, 200, False),
        # The user's observed positive code=2: outside the known set, so it
        # stays non-arming even with a proven low fee (a positive code is not
        # a named Core policy constant; see MANUAL-WORK MW-9 re-test note).
        (2, 50, False),
        # Non-numeric / absent rpc_code behavior is unchanged (not a code).
        (None, 50, False),
    ],
)
def test_fee_floor_shaped_class_gate_accepts_the_pair(rpc_code, fee_rate, expected) -> None:
    """_fee_floor_shaped's class gate now accepts rpc_code in {-25, -26}
    (TCK-PUBLICBCAST-004), while the live-floor FAMILY proof stays mandatory —
    a known code with an unproven floor still returns False."""
    exc = ChainError(
        "rejected", failure_class=RPC_ERROR, exc_name="RPCError", rpc_code=rpc_code
    )
    client = _Node(floor=100, capable=True)  # node enforces 100 centisat/vB
    assert app._fee_floor_shaped(exc, client, fee_rate) is expected


def test_fee_floor_shaped_pair_still_needs_capability() -> None:
    """The -26 code alone never arms on a backend that cannot state its floor
    (honest capability absence → unclear → no offer), exactly as -25 already."""
    exc = ChainError(
        "rejected", failure_class=RPC_ERROR, exc_name="RPCError", rpc_code=-26
    )
    assert app._fee_floor_shaped(exc, _Node(capable=False), 50) is False


def test_offer_unavailable_without_public_client(world) -> None:
    """No public transport wired → the offer itself is unavailable (the app
    never dangles a consent it cannot fulfil)."""
    _flow, session, handler, signed = world(public=None)
    result = handler(_env({"tx_ref": signed.tx_ref}))
    assert "public_bcast_offer" not in result
    assert session.public_offer_txref is None


def test_offer_arms_once_per_signed_record(world) -> None:
    """ONE offer line, ever, per signed transaction: a retried broadcast
    that fails again does not re-ask."""
    _flow, session, handler, signed = world(public=_Public())
    assert handler(_env({"tx_ref": signed.tx_ref})).get("public_bcast_offer") is True
    session.public_offer_txref = None  # the user derailed instead of answering
    second = handler(_env({"tx_ref": signed.tx_ref}))
    assert "public_bcast_offer" not in second
    assert session.public_offer_shown == signed.tx_ref


def test_success_arms_no_offer(world) -> None:
    _flow, session, handler, signed = world(node=_Node(), public=_Public())
    result = handler(_env({"tx_ref": signed.tx_ref}))
    assert result["status"] == "broadcast"
    assert "public_bcast_offer" not in result and session.public_offer_txref is None


# ------------------------------------------- B2. the electrum relayfee branch
# (TCK-PUBLICBCAST-001 rider): rejection-as-answer + the handshake-retained
# server.features relayfee. The bitcoind matrix above stays untouched; the
# electrum backend answers the floor through the gate-only seam instead of
# the estimator's capability name (see tests/test_chain_electrum.py).


@pytest.mark.parametrize(
    ("exc", "floor", "fee_rate", "expected"),
    [
        # THE RIDER: server-rejected + announced relayfee (100 = 1 sat/vB)
        # + the tx's own retained rate under the floor → the feature is
        # ALIVE for an electrum user (was structurally dead: no capability).
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            100, 50, True,
        ),
        # family gate UNCHANGED: at/above the announced floor the floor
        # cannot explain the refusal (mempool-conflict-shaped) → no offer.
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            100, 100, False,
        ),
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            100, 200, False,
        ),
        # relayfee 0/absent/malformed answers None chain-side → unclear →
        # NO OFFER (the fail-closed rung the whole gate is built on).
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            None, 50, False,
        ),
        # a non-int floor answer is equally "unclear".
        (
            ChainError("broadcast refused", failure_class=SERVER_REJECTED),
            "100", 50, False,
        ),
        # and a NON-refusal class still has no offer from the gate seam.
        (ChainError("boom"), 100, 50, False),
    ],
)
def test_electrum_relayfee_branch_of_the_gate(
    world, exc, floor, fee_rate, expected
) -> None:
    """The electrum-shaped node (NO min_relay_centisat_vb — that name is
    the estimator's and stays absent here) arms the offer ONLY via the
    gate seam, and only under the unchanged class + family gates."""
    flow, session, handler, signed = world(
        fee_rate=fee_rate,
        node=_Node(exc=exc, floor=floor, capable=False, gate=True),
        public=_Public(),
    )
    result = handler(_env({"tx_ref": signed.tx_ref}))
    assert result["error"] == "broadcast_failed"
    assert (result.get("public_bcast_offer") is True) is expected
    assert (session.public_offer_txref == signed.tx_ref) is expected
    assert flow.state is TxFlowStatus.SIGNED


# ----------------------------------------------------------------- C/D. flow


class _FakeGen:
    """Records prompts; answers with a canned respond envelope."""

    def __init__(self, reply: str | None = None) -> None:
        self.prompts: list[str] = []
        self.calls = 0
        self.reply = reply or json.dumps(
            {"v": 0, "intent": "respond", "params": {"text": "ok"}}
        )

    def __call__(self, prompt: str, grammar_text):
        self.calls += 1
        self.prompts.append(prompt)
        return self.reply


def _harness(
    store,
    wallet,
    *,
    fee_rate: int = 50,
    node: _Node | None = None,
    public: _Public | None = None,
):
    """Build the flow + a production dispatch table over the fakes (real
    handlers — the intercept chain under test reaches the SAME broadcast
    handler a model envelope would)."""
    flow, _ticks = make_flow()
    signed = _drive_to_signed_at_fee(flow, fee_rate)
    session = app.SendSession()
    node = node if node is not None else _Node(exc=FEE_FLOOR_REJECTION)
    public = public if public is not None else _Public()
    table = app.build_dispatch_table(
        store,
        wallet,
        _fixture_parsed(),
        node,
        lambda: None,
        flow=flow,
        session=session,
        node_detect_fn=lambda: None,
        public_info=public,
    )
    return flow, session, signed, node, public, table


def _fixture_parsed():
    from tests.test_e2e_skeleton import _fixture_parsed as fp

    return fp()


@pytest.fixture()
def live(monkeypatch: pytest.MonkeyPatch):
    """A store + wallet with the broadcast-failure world standing by."""
    monkeypatch.setattr(app, "_extract_signed_tx_hex", lambda _b64: TX_HEX)
    store = app.Store(None)
    wallet = store.create_wallet("default", "desc")
    store.update_derivation(wallet.id, 1, next_index=1)
    yield store, wallet
    store.close()


def test_offline_offer_prints_one_deterministic_value_free_line(world) -> None:
    """The renderer emits the failure line + EXACTLY the pinned offer
    sentence — value-free (no reason text, no txid, no hex, no amount) —
    and nothing else."""
    _flow, session, handler, signed = world(public=_Public())
    result = handler(_env({"tx_ref": signed.tx_ref}))
    outputs: list[str] = []
    app._print_broadcast_tx(result, outputs.append, session=session)
    assert outputs[-1] == app._PUBLIC_BCAST_OFFER
    joined = "\n".join(outputs)
    assert TX_HEX not in joined and TXID_OK not in joined and "100" not in joined
    assert outputs[-2].startswith("Broadcast failed")
    assert outputs[-2].endswith("say 'broadcast' to retry.")


def test_no_offer_line_without_marker(world) -> None:
    _flow, session, handler, signed = world(
        node=_Node(exc=FEE_FLOOR_REJECTION, capable=False), public=_Public()
    )
    result = handler(_env({"tx_ref": signed.tx_ref}))
    outputs: list[str] = []
    app._print_broadcast_tx(result, outputs.append, session=session)
    assert outputs == [
        (
            "Broadcast failed (broadcast request rejected by the server) — "
            "the signed transaction is kept; say 'broadcast' to retry."
        )
    ]


def test_consent_yes_broadcasts_the_same_tx_publicly(live) -> None:
    """The affirmative goes through the deterministic ConfirmGate on the
    user's RAW utterance before the model: the SAME retained signed bytes
    POST once to the public client; the node is not touched again; the
    flow takes the unchanged SIGNED→BROADCAST transition; the honest
    status-lag companion prints; the model never sees the turn."""
    store, wallet = live
    flow, session, signed, node, public, table = _harness(store, wallet)
    outputs: list[str] = []
    fake = _FakeGen()
    loop = AgentLoop(fake, table)
    # 1. Failed node broadcast arms the offer (production handler).
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    assert session.public_offer_txref == signed.tx_ref
    node.broadcasts.clear()
    # 2. The user says yes.
    app._run_turn(
        loop, flow, session, "yes", outputs.append, table=table, store=store,
    )
    assert node.broadcasts == []          # own node NOT re-asked
    assert public.broadcasts == [TX_HEX]  # SAME signed hex, verbatim
    assert flow.state is TxFlowStatus.BROADCAST
    assert flow.txid == TXID_OK
    assert session.public_bcast_txid == TXID_OK
    assert session.public_offer_txref is None and session.public_bcast_once is False
    assert fake.calls == 0                # pre-model intercept consumed it
    joined = "\n".join(outputs)
    assert f"Sent! txid {TXID_OK}" in joined
    assert app._PUBLIC_BCAST_SENT in joined
    assert "mempool.space" in joined
    store.close()



@pytest.mark.parametrize("word", ["yes", "y", "confirm", "send it", "do it", "yes please"])
def test_consent_accepts_the_confirm_whitelist(live, word: str) -> None:
    """The exact utterance-shape gate treatment as confirm — the same
    ConfirmGate phrases authorize, nothing else does."""
    store, wallet = live
    flow, session, signed, _node, public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, word, lambda _s: None,
        table=table, store=store,
    )
    assert public.broadcasts == [TX_HEX], word
    assert flow.state is TxFlowStatus.BROADCAST
    store.close()


@pytest.mark.parametrize(
    ("word", "needle"),
    [
        ("no", app._PUBLIC_BCAST_DENIED),
        ("cancel", app._PUBLIC_BCAST_DENIED),
        ("yes no", app._PUBLIC_BCAST_AMBIGUOUS),
    ],
)
def test_consent_refusal_retires_offer_nothing_sent(live, word, needle) -> None:
    store, wallet = live
    flow, session, signed, node, public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    node.broadcasts.clear()
    outputs: list[str] = []
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, word, outputs.append,
        table=table, store=store,
    )
    assert public.broadcasts == [] and node.broadcasts == []
    assert flow.state is TxFlowStatus.SIGNED  # kept-for-retry
    assert session.public_offer_txref is None
    assert needle in "\n".join(outputs)
    store.close()


def test_derailing_utterance_closes_offer_then_falls_through(live) -> None:
    """Never-trap: any non-decision word closes the offer (a LATER stray
    "yes" can never collect it) and flows through to the ordinary
    pipeline; nothing is sent publicly."""
    store, wallet = live
    flow, session, signed, _node, public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    fake = _FakeGen()
    outputs: list[str] = []
    app._run_turn(
        AgentLoop(fake, table), flow, session, "what is my balance?",
        outputs.append, table=table, store=store,
    )
    assert session.public_offer_txref is None
    assert public.broadcasts == []
    assert fake.calls == 1  # the derailment went to the model as ordinary chat
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", outputs.append,
        table=table, store=store,
    )
    assert public.broadcasts == []  # the stray later "yes" finds nothing armed
    store.close()


def test_model_cannot_authorize_or_trigger_the_public_send(live) -> None:
    """DISPATCHER-OWNED: a model envelope (even a broadcast_tx quoting the
    real signed tx_ref — the ordinary retry) hits the handler WITHOUT the
    one-shot → it can only ever re-ask the user's own node. The public
    leg is unreachable from any envelope."""
    store, wallet = live
    flow, session, signed, node, public, table = _harness(store, wallet)
    # Arm the offer first (a real fee-floor failure).
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    node.broadcasts.clear()
    fake = _FakeGen(
        reply=json.dumps(
            {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": signed.tx_ref}}
        )
    )
    outputs: list[str] = []
    # The user says something NON-decisional ("please check again slowly" —
    # unknown tokens) → the offer closes; the MODEL then emits broadcast_tx.
    # (Model mode: the envelope goes through handle_raw inside loop.run.)
    app._run_turn(
        AgentLoop(fake, table), flow, session, "check again slowly please",
        outputs.append, table=table, store=store,
    )
    assert public.broadcasts == []           # the model NEVER reaches the public leg
    assert node.broadcasts == [TX_HEX]       # its only power: retry the own node
    assert session.public_bcast_once is False
    store.close()


def test_public_send_failure_keeps_signed_and_does_not_rearm(live) -> None:
    """Consent given, public POST fails → ordinary broadcast_failed (the
    DIAG-003 debug line included), flow stays SIGNED, the offer is NOT
    re-armed (one offer per record; a failed public attempt can never
    farm a second consent)."""
    store, wallet = live
    public = _Public(exc=ChainError(
        "broadcast request rejected by the server",
        failure_class=SERVER_REJECTED,
    ))
    flow, session, signed, _node, _p, table = _harness(store, wallet, public=public)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    outputs: list[str] = []
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", outputs.append,
        table=table, store=store,
    )
    assert public.broadcasts == [TX_HEX]
    assert flow.state is TxFlowStatus.SIGNED
    assert session.public_offer_txref is None
    assert app._PUBLIC_BCAST_OFFER not in "\n".join(outputs)
    joined = "\n".join(outputs)
    assert "Broadcast failed" in joined and "say 'broadcast' to retry." in joined
    store.close()


def test_one_shot_without_public_client_refuses_value_free(live) -> None:
    """Fail-closed seam: a consented dispatch with no public transport must
    NEVER fall back to the own node (the user said yes to PUBLIC); the
    refusal carries nothing but the kind."""
    store, wallet = live
    flow, session, signed, node, _public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX] = app._make_broadcast_tx_handler(
        flow, node, store, wallet.id, _fixture_parsed(),
        session=session, public_info=None,
    )
    session.public_bcast_once = True
    result = table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    assert result["error"] == "broadcast_refused"
    assert node.broadcasts == []
    assert flow.state is TxFlowStatus.SIGNED
    assert session.public_bcast_once is False  # consumed at send time
    store.close()


def test_public_success_records_history_like_a_node_broadcast(live) -> None:
    """The state semantics stay honest: the public broadcast reuses the
    SAME recording (BROADCAST terminal + the outbound history row), so
    get_history shows the tx immediately; only the narration adds the lag
    companion."""
    store, wallet = live
    flow, session, signed, _node, _public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", lambda _s: None,
        table=table, store=store,
    )
    rows = store.get_txs_for_wallet(wallet.id)
    assert [r.txid for r in rows] == [TXID_OK]
    assert rows[0].direction == "out" and rows[0].height is None
    store.close()


# ---------------------------------------------------------------- E. status


def test_status_after_public_broadcast_says_gossip_lag(live) -> None:
    """Condition 4: the user's own node has NOT seen the publicly-broadcast
    tx — the answer names the mechanism and its bound (the watch poll
    picks it up), never the indexing hedge, never "try again forever".
    The quoted txid rides verbatim."""
    store, wallet = live
    flow, session, signed, _node, public, table = _harness(store, wallet)
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", lambda _s: None,
        table=table, store=store,
    )
    assert flow.txid == public.txid
    tx_status = table[IntentName.TX_STATUS]
    result = tx_status(
        validate_payload(
            json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": public.txid}})
        )
    )
    assert result["error"] == "unknown_tx"
    assert result["public_lag"] is True
    assert result["detail"] == app._PUBLIC_BCAST_STATUS_LAG
    outputs: list[str] = []
    app._print_tx_status(result, outputs.append)
    assert outputs == [app._PUBLIC_BCAST_STATUS_LAG]
    assert "forever" not in outputs[0]
    store.close()


def test_status_lag_honesty_does_not_move_other_answers(live) -> None:
    """A non-public just-broadcast 404 keeps the PRE-TICKET bytes (the
    eventual-consistency hedge), and the offer/consent lines never appear
    in a status answer."""
    store, wallet = live
    _flow, session, signed, node, _public, table = _harness(store, wallet)
    # Ordinary SUCCESSFUL node broadcast, then the node forgets the tx.
    node.exc = None
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    assert session.public_bcast_txid is None
    result = table[IntentName.TX_STATUS](
        validate_payload(
            json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": TXID_OK}})
        )
    )
    assert result["error"] == "unknown_tx" and "public_lag" not in result
    outputs: list[str] = []
    app._print_tx_status(result, outputs.append)
    assert outputs == [
        (
            "Transaction not found on the chain yet — it may not be indexed; "
            "try again in a moment."
        )
    ]
    store.close()


def test_public_bcast_slot_cleared_by_a_later_different_broadcast(live) -> None:
    """TCK-PUBLICBCAST-002 (a): after a PUBLIC broadcast of tx A, a later
    NON-public (own-node) broadcast of a DIFFERENT tx B clears the stale
    public slot — the flow has left the public-broadcast context, so no
    later status answer can read the old txid as gossip lag."""
    store, wallet = live
    flow, session, signed, node, _public, table = _harness(store, wallet)
    # Public broadcast of tx A.
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", lambda _s: None,
        table=table, store=store,
    )
    assert flow.txid == TXID_OK and session.public_bcast_txid == TXID_OK
    # A NEW transaction B, broadcast through the OWN node (not public).
    flow.reset()
    signed_b = _drive_to_signed_at_fee(flow, 50)
    txid_b = "bb" * 32
    node.exc = None          # the own node accepts B now
    node.ret_txid = txid_b   # B is a DIFFERENT txid than A
    result = table[IntentName.BROADCAST_TX](_env({"tx_ref": signed_b.tx_ref}))
    assert result["status"] == "broadcast"
    assert flow.txid == txid_b
    # The stale public slot is gone — no later status answer can read it.
    assert session.public_bcast_txid is None
    # And a status query for the OLD publicly-broadcast A no longer claims lag.
    status_a = table[IntentName.TX_STATUS](
        validate_payload(
            json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": TXID_OK}})
        )
    )
    assert "public_lag" not in status_a
    store.close()


def test_lag_detection_keys_on_class_not_dialect_text(live) -> None:
    """TCK-PUBLICBCAST-002 (b): the status-lag detection fires on the
    explicit not-found EVIDENCE (HTTP_STATUS class + the 404 status field),
    never on the error-message dialect text — a wording change upstream, a
    FOREIGN class, or the class WITHOUT the explicit field (a retry-
    exhausted 429/5xx) can neither break nor fake the lag claim."""
    store, wallet = live
    flow, session, signed, node, _public, table = _harness(store, wallet)
    # Public broadcast A so the lag branch is reachable for TXID_OK.
    table[IntentName.BROADCAST_TX](_env({"tx_ref": signed.tx_ref}))
    app._run_turn(
        AgentLoop(_FakeGen(), table), flow, session, "yes", lambda _s: None,
        table=table, store=store,
    )
    assert session.public_bcast_txid == TXID_OK and flow.txid == TXID_OK
    tx_status = table[IntentName.TX_STATUS]
    status_env = validate_payload(
        json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": TXID_OK}})
    )
    # (i) not-found as the TYPED class + the explicit 404 status field,
    # with a DIFFERENT dialect string: fires.
    node.get_tx_status = lambda txid: (_ for _ in ()).throw(
        ChainError(
            "the server answered that it has no record of that transaction",
            failure_class=HTTP_STATUS,
            http_status=404,
        )
    )
    result = tx_status(status_env)
    assert result["public_lag"] is True
    assert result["detail"] == app._PUBLIC_BCAST_STATUS_LAG
    # (ii) the "status 404" TEXT on a NON-not-found class does NOT fire —
    # fail-closed, dialect text is inert. (SERVER_REJECTED is Electrum's
    # BROADCAST-refusal class; its tx-status refusal class is RPC_ERROR —
    # either way a foreign class carries no not-found evidence.)
    node.get_tx_status = lambda txid: (_ for _ in ()).throw(
        ChainError("tx-status failed: status 404", failure_class=SERVER_REJECTED)
    )
    result = tx_status(status_env)
    assert "public_lag" not in result
    assert result["error"] == "chain_unavailable"
    # (iii) the HTTP_STATUS class WITHOUT the explicit 404 field — the
    # retry-EXHAUSTED 5xx shape (an unreachable/flaky backend) — does NOT
    # fire: a transient failure is never claimed as structural gossip lag.
    node.get_tx_status = lambda txid: (_ for _ in ()).throw(
        ChainError(
            "tx-status request failed after 3 retries: status 502",
            failure_class=HTTP_STATUS,
            exc_name="HTTPStatus",
        )
    )
    result = tx_status(status_env)
    assert "public_lag" not in result
    assert result["error"] == "chain_unavailable"
    store.close()


def test_all_publicbcast_copy_is_value_free() -> None:
    """The invariant for every sentence this ticket ships: no amount/
    address/txid placeholders, no server text — the only name is the
    service the user is consenting to."""
    sentences = [
        app._PUBLIC_BCAST_OFFER,
        app._PUBLIC_BCAST_DENIED,
        app._PUBLIC_BCAST_AMBIGUOUS,
        app._PUBLIC_BCAST_SENT,
        app._PUBLIC_BCAST_STATUS_LAG,
    ]
    for sentence in sentences:
        assert "{" not in sentence and "}" not in sentence
        assert TX_HEX not in sentence and TXID_OK not in sentence
        assert not any(ch.isdigit() for ch in sentence.replace("404", ""))
