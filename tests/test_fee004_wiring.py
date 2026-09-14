"""TCK-FEE-004 code-review fix: the PRODUCTION wiring of the min-relay floor.

The MAJOR the review caught: the shared estimator is built over the public
fee source (mempool.space), so the bitcoind backend's ``min_relay_centisat_
vb`` capability was unreachable in production — the ticket's core intent
("the floor OUR NODE defines") never fired on the main path, and the
explicit-rate seam floored at a CONGESTION estimate (minimumFee) instead
of the relay floor. These pins drive the REAL :func:`app._wire`:

* a bitcoind-shaped wallet backend → its floor answers the explicit clamp
  (even a LOWER one than the source's congestion figure);
* an electrum-shaped backend (capability honestly absent) → the assumed
  1 sat/vB, and ZERO chain calls on the public source;
* a hot-swap re-points the shared estimator's floor at the new backend
  without rebuilding it (one estimator, one cache — the FLOOR moves).

All hermetic: the wallet "backends" are capability doubles, the public
source is an httpx.MockTransport, AUTO_SCAN=0 (nothing runs at boot).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from localwallet import app
from localwallet.chain.publicinfo import PublicInfoClient
from localwallet.config import Settings
from localwallet.store import Store
from localwallet.wallet import WalletDescriptor
from tests.test_e2e_skeleton import TEST_GAP, ZPUB

#: A CONGESTED recommended payload: minimumFee 3 sat/vB. Under the broken
#: wiring this was the explicit clamp's floor; the fix forbids it there.
CONGESTED_RECOMMENDED = {
    "fastestFee": 8,
    "halfHourFee": 5,
    "hourFee": 4,
    "economyFee": 2,
    "minimumFee": 3,
}


class _BitcoindLike:
    """The capability shape BitcoindClient presents: the estimator's
    relay_floor_client seam duck-types on ``min_relay_centisat_vb`` alone."""

    supports_price = False

    def __init__(self, floor_centisat_vb: int) -> None:
        self.floor = floor_centisat_vb
        self.calls = 0

    def min_relay_centisat_vb(self) -> int:
        self.calls += 1
        return self.floor

    def close(self) -> None:  # pragma: no cover — swap-in cleanup only
        pass


class _ElectrumLike:
    """The honest absence: a wallet backend with NO floor capability."""

    supports_price = False

    def close(self) -> None:  # pragma: no cover
        pass


def _public_info(recorded: list[httpx.Request]) -> PublicInfoClient:
    return PublicInfoClient(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: (
                recorded.append(request),
                httpx.Response(200, json=CONGESTED_RECOMMENDED),
            )[1]
        ),
    )


def _wire_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wallet_client: Any,
    public_info: PublicInfoClient,
) -> Any:
    """The REAL ``app._wire`` with both chain seams injected (the
    test_scan_worker._wiring_for_test shape): resolved env rung,
    AUTO_SCAN=0, watch off — nothing touches the chain at boot."""
    wd = WalletDescriptor.from_key(ZPUB)
    seed = Store(tmp_path / "fee004.db")
    seed.set_setting("gap_limit", str(TEST_GAP))
    seed.close()
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "fee004.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv(app.CHAIN_BASE_URL_ENV_VAR, "bitcoind://mock.test:8332")
    monkeypatch.setenv(app.WATCH_INTERVAL_ENV_VAR, "0")
    monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.setattr(app, "_build_chain_client", lambda _s, _a: wallet_client)
    monkeypatch.setattr(app, "_public_info_client", lambda _s: public_info)
    return app._wire(
        parsed=wd.parsed,
        descriptor=wd,
        signer_selection=app.SignerSelection(
            kind=app.SIGNER_KIND_FILE,
            dir_path=tmp_path / "signer",
            fingerprint_hex=wd.parsed.hd_key.my_fingerprint.hex(),
        ),
        settings=Settings.from_env(),
        env_gap=None,
        rescan=False,
        flow=None,
        generate=app.stub_generate,
        node_detect_fn=None,
        output_fn=lambda _line: None,
    )


def test_wire_bitcoind_backend_node_floor_reaches_the_explicit_clamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE production pin (the review MAJOR): estimator over the public
    source + bitcoind backend → the NODE's floor (2.5 sat/vB) answers the
    explicit seam — not the congestion minimumFee 3 — for at most ONE
    getmempoolinfo-shaped query and ZERO public-source calls."""
    recorded: list[httpx.Request] = []
    node = _BitcoindLike(250)
    wiring = _wire_with(tmp_path, monkeypatch, node, _public_info(recorded))
    try:
        assert wiring.fee_estimator is not None
        assert wiring.fee_estimator.clamp_to_min_relay_floor(100) == (250, True)
        assert wiring.fee_estimator.clamp_to_min_relay_floor(250) == (250, False)
        assert node.calls == 1  # TTL-cached, bounded
        assert recorded == []  # the fee source was never consulted
    finally:
        wiring.worker.stop()
        wiring.store.close()
        _public_info_client_close_quietly(wiring)


def _public_info_client_close_quietly(wiring: Any) -> None:
    if wiring.public_info is not None:
        wiring.public_info.close()


def test_wire_electrum_backend_floor_is_the_assumed_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flip the backend to electrum-shaped (capability honestly absent):
    the explicit floor is the assumed 1 sat/vB — the congested source's
    minimumFee 3 does NOT lift an explicit 1 — and the seam costs ZERO
    chain calls on any source (the restored FEE-002 property)."""
    recorded: list[httpx.Request] = []
    wiring = _wire_with(
        tmp_path, monkeypatch, _ElectrumLike(), _public_info(recorded)
    )
    try:
        assert wiring.fee_estimator is not None
        assert wiring.fee_estimator.clamp_to_min_relay_floor(100) == (100, False)
        assert wiring.fee_estimator.clamp_to_min_relay_floor(99) == (100, True)
        assert recorded == []
    finally:
        wiring.worker.stop()
        wiring.store.close()
        _public_info_client_close_quietly(wiring)


def test_hot_swap_repoints_the_shared_estimators_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-run path the ticket's intent lives on: boot UNRESOLVED-
    onto-electrum (assumed floor), then the user saves a bitcoind URL —
    the hot-swap rebind keeps the SAME estimator instance (one cache, one
    TTL, bids keep riding the public source) and moves ONLY the floor:
    the new node's 2.5 sat/vB now answers the explicit clamp."""
    recorded: list[httpx.Request] = []
    wiring = _wire_with(
        tmp_path, monkeypatch, _ElectrumLike(), _public_info(recorded)
    )
    try:
        old_estimator = wiring.fee_estimator
        assert old_estimator is not None
        assert old_estimator.clamp_to_min_relay_floor(100) == (100, False)
        node = _BitcoindLike(250)
        flow = app.ChainBackendFlow(wiring, lambda url: url)
        wiring.table = {}  # the test_descope_m3a rebind shape
        flow._rebind_handlers(node)
        assert wiring.fee_estimator is old_estimator  # ONE shared instance
        assert old_estimator.clamp_to_min_relay_floor(100) == (250, True)
        assert node.calls == 1
        assert recorded == []  # still: zero public-source calls on the seam
    finally:
        wiring.worker.stop()
        wiring.store.close()
        _public_info_client_close_quietly(wiring)
