"""TCK-WEB-022 (engine half): the code-owned vetted public-Electrum chips.

The settings pane renders "suggested servers" as click-to-FILL chips, so the
list must be ENGINE truth on the typed ``/state`` door — the client never
invents a server and never echoes what the user typed. Pinned here:

1. the additive ``suggested_servers`` field rides a REAL engine pump's
   ``state/1`` snapshot as an array of ``{url, label}`` objects, verbatim
   (the label is display text; the url is the consent target's URL),
2. an empty/absent list OMITS the key — never an empty array,
3. the field is a defensive COPY: a consumer cannot poison the module
   constant through a snapshot,
4. NOTHING else in the snapshot changes (the key set grows by exactly one),
5. the vetted list is exactly what the 2026-09-11 probe verified — one
   ``ssl://`` mainnet entry (``config.PUBLIC_ELECTRUM_URL``, the DESCOPE-M3A
   "use public" consent target); the three rejected candidates (bitaroo's
   TLS-verification failure, bluewallet's PLAINTEXT port, getlynx's failed
   mainnet check) are absent, and an unverified host never sneaks in.

The web half (static chips + consent copy) is the sibling ticket task; this
file touches no static asset.
"""

from __future__ import annotations

from typing import Any

from localwallet import app
from localwallet.config import PUBLIC_ELECTRUM_URL, Settings

_VETTED_URL = "ssl://electrum.blockstream.info:50002"
_VETTED_LABEL = "Blockstream public electrum"


def _state_snap(settings: Settings | None = None) -> dict[str, object]:
    """One typed ``/state`` read through a REAL engine pump — the field is
    supplied at the PUMP call site (the WEB-010/023/027 source rule), so the
    builder is never the production path under test."""
    events: list[Any] = []
    table = {app.IntentName.RESPOND: app._respond_handler}

    def bootstrap() -> app.EngineContext:
        return app.EngineContext(
            loop=app.AgentLoop(app.stub_generate, table),
            flow=app.TxFlow(),
            session=app.SendSession(),
            table=table,
            settings=settings,
        )

    handle = app.start_engine(bootstrap, events.append)
    try:
        snap = handle.request_state(5.0)
    finally:
        handle.shutdown()
        assert handle.thread is not None
        handle.thread.join(10)
    assert snap is not None
    return snap


def test_pump_snapshot_carries_the_vetted_chips_verbatim() -> None:
    """Done-when 1: shape + verbatim values, through the real pump — and for
    every backend state (the list is code-owned, so it does not depend on
    settings; the awaiting first-run pane needs the chip too)."""
    for settings in (
        None,
        Settings(chain_base_url=""),  # awaiting first run
        Settings(chain_base_url=PUBLIC_ELECTRUM_URL),
    ):
        snap = _state_snap(settings)
        assert snap["schema"] == "state/1"
        chips = snap["suggested_servers"]
        assert isinstance(chips, list)
        assert all(sorted(chip) == ["label", "url"] for chip in chips)
        assert {"url": _VETTED_URL, "label": _VETTED_LABEL} in chips


def test_builder_omits_the_field_when_the_list_is_empty_or_absent() -> None:
    """Done-when 2: the ``wallet_fingerprint``/``backend_host`` absent rule —
    an empty (or unpassed) vetted list means "no chips", shipped as an
    OMITTED key, never ``[]`` for the client to mis-render."""
    flow, session = app.TxFlow(), app.SendSession()
    assert "suggested_servers" not in app.build_state_snapshot(flow, session, None)
    assert "suggested_servers" not in app.build_state_snapshot(
        flow, session, None, suggested_servers=[]
    )
    assert "suggested_servers" not in app.build_state_snapshot(
        flow, session, None, suggested_servers=None
    )


def test_snapshot_chips_are_a_copy_of_the_module_constant() -> None:
    """Done-when 3: the builder shallow-copies each entry into a fresh list,
    so no snapshot consumer (or bug) can mutate :data:`SUGGESTED_ELECTRUM_SERVERS`
    for the rest of the process."""
    snap = _state_snap()
    chips = snap["suggested_servers"]
    assert chips is not app.SUGGESTED_ELECTRUM_SERVERS
    assert chips == list(app.SUGGESTED_ELECTRUM_SERVERS)
    chips[0]["url"] = "ssl://evil.invalid:1"  # poison the snapshot
    chips[0]["label"] = "poisoned"
    assert next(iter(app.SUGGESTED_ELECTRUM_SERVERS)) == {
        "url": _VETTED_URL,
        "label": _VETTED_LABEL,
    }
    assert _state_snap()["suggested_servers"] == [
        {"url": _VETTED_URL, "label": _VETTED_LABEL}
    ]


def test_the_field_is_the_only_snapshot_change() -> None:
    """Done-when 4: additive under the UNCHANGED ``state/1`` tag — the same
    builder call with and without the list differs in exactly one key."""
    args = (app.TxFlow(), app.SendSession(), None)
    bare = app.build_state_snapshot(*args)
    with_chips = app.build_state_snapshot(*args, suggested_servers=app.SUGGESTED_ELECTRUM_SERVERS)
    assert set(with_chips) - set(bare) == {"suggested_servers"}
    assert {k: v for k, v in with_chips.items() if k != "suggested_servers"} == bare
    assert with_chips["schema"] == "state/1" == bare["schema"]


def test_vetted_list_is_exactly_the_probed_entry() -> None:
    """Done-when 5: the 2026-09-11 probe shipped ONE verified server. Every
    entry is ssl:// (the adapter's only Electrum scheme), the URL is the
    consent target's own constant (single source — never a second literal),
    the label is value-free display text, and the rejected/unverified hosts
    are absent by name."""
    chips = list(app.SUGGESTED_ELECTRUM_SERVERS)
    assert len(chips) == 1
    for chip in chips:
        assert chip["url"] == PUBLIC_ELECTRUM_URL
        assert chip["url"].startswith("ssl://")
        assert chip["label"].strip() == chip["label"] and chip["label"]
        # value-free label: display words only, no port/amount/address shape
        assert ":" not in chip["label"] and not any(c.isdigit() for c in chip["label"])
    urls = {chip["url"] for chip in chips}
    for omitted in (
        "bitaroo",  # TLS verification FAILED on the probe
        "bluewallet",  # PLAINTEXT tcp — the ssl-only adapter cannot use it
        "getlynx",  # MAINNET CHECK FAILED — unresolved (DESCOPE-M3B)
        "testnet",
        "mempool.space",  # public-info source, not a wallet backend (M3A)
    ):
        assert not any(omitted in url for url in urls), omitted
