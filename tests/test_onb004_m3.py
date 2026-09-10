"""TCK-ONB-004 M3 — auto-detect + credentials UX (the final ONB-004 milestone).

Pinned here (docs/onb-004-backend-adapters-plan.md §3; ADR-0018 M3 amendment):

* **the credential store rung** — typed writers/readers for
  ``backend_auth_user`` / ``backend_auth_pass`` / ``backend_auth_none``:
  fail-closed shape rules (length / ASCII / no whitespace / no control
  characters — the values are joined into an ``Authorization`` header),
  the ``""``-clears convention, value-free refusals;
* **the never-echo settings surface** — the three keys ride GET/POST
  /settings as SECRET entries: ``configured`` is the whole story a read
  may tell, the value never appears in any reply (nor in any refusal),
  and clearing is the empty write;
* **the resolver interplay** (documented rule): checkbox SET → omit auth
  entirely; UNSET + filled pair → basic auth; UNSET + empty/half → the
  M2 default ladder (URL userinfo rung, cookie file, no auth);
* **creds ride the probe AND the built client** — the settings-apply
  path resolves the stored overlay at call time;
* **the stored ``bitcoind://`` rung** — the store accepts
  ``bitcoind://host[:port]`` WITHOUT userinfo (``@`` refused; logins ride
  the dedicated keys) while the env/config-file rungs keep M2's userinfo
  allowance (``ChainConfig`` — pinned by the existing suites);
* **the /setup credentials step** — one bounded offer after a failed
  auth-capable validation; a re-probe that fails RESTORES the prior
  credential record (a failed attempt can never strand the working
  backend's login); a save keeps the pair; nothing on the channel echoes
  the typed value;
* **the copy pass 2 §2h rewords** (rows 64/66/68/72/73 — applied verbatim
  except the 73 amendment noted in the source).

Everything hermetic: real stores in tmp dirs, loopback fixtures (their own
files), monkeypatched construction seams — NO live network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

import pytest

from localwallet import app
from localwallet.app import (
    BACKEND_PROBE_FAIL,
    MODEL_CARD_QUESTION,
    MODEL_DL_DONE,
    MODEL_DL_STARTED,
    MODEL_INTEGRITY_WARNING,
    SETTINGS_SCHEMA,
    _backend_auth,
    _BackendAuth,
    _build_chain_client,
)
from localwallet.chain import BitcoindClient
from localwallet.chain import bitcoind as bitcoind_module
from localwallet.config import Settings
from localwallet.store import Store, StoreError
from localwallet.ui import onboarding as ob

PASSWORD = "s3cr3t-rpc-pass"
USER = "rpcuser"


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        app.GAP_LIMIT_ENV_VAR,
        app.CHAIN_BASE_URL_ENV_VAR,
        "LOCALWALLET_RPC_COOKIE_PATH",
        "LOCALWALLET_TLS_VERIFY",
    ):
        monkeypatch.delenv(var, raising=False)


# ------------------------------------------------------------ store rung


class TestStoreCredentialWriters:
    def test_roundtrip_overwrite_and_clear(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "creds.db")
        try:
            assert store.get_backend_auth_user() is None
            assert store.get_backend_auth_pass() is None
            assert store.get_backend_auth_none() is False
            store.set_backend_auth_user(USER)
            store.set_backend_auth_pass(PASSWORD)
            assert store.get_backend_auth_user() == USER
            assert store.get_backend_auth_pass() == PASSWORD
            store.set_backend_auth_user("")  # the ""-clears convention
            store.set_backend_auth_pass("")
            assert store.get_backend_auth_user() is None
            assert store.get_backend_auth_pass() is None
            store.set_backend_auth_none(True)
            assert store.get_backend_auth_none() is True
            store.set_backend_auth_none(False)
            assert store.get_backend_auth_none() is False
        finally:
            store.close()

    def test_persists_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.db"
        with Store(path) as store:
            store.set_backend_auth_user(USER)
            store.set_backend_auth_pass(PASSWORD)
        with Store(path) as store:
            assert store.get_backend_auth_user() == USER
            assert store.get_backend_auth_pass() == PASSWORD

    @pytest.mark.parametrize(
        "bad",
        [
            "has space",  # whitespace anywhere (Authorization-header value)
            "tab\there",
            "crlf\r\ninject",
            "nonascii\u00e9",
            "x" * 257,  # over the shape cap
        ],
    )
    def test_shape_rules_fail_closed_and_value_free(
        self, tmp_path: Path, bad: str
    ) -> None:
        store = Store(tmp_path / "creds.db")
        try:
            for writer in (store.set_backend_auth_user, store.set_backend_auth_pass):
                with pytest.raises(StoreError) as excinfo:
                    writer(bad)
                assert bad not in str(excinfo.value)  # never echoes the secret
                assert store.get_backend_auth_user() is None  # nothing landed
        finally:
            store.close()

    def test_none_flag_type_is_strict(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "creds.db")
        try:
            with pytest.raises(StoreError):
                store.set_backend_auth_none("yes")  # type: ignore[arg-type]
        finally:
            store.close()


class TestStoreBitcoindRung:
    """M3 opened the STORED rung to ``bitcoind://`` — WITHOUT userinfo (the
    env/config-file rungs keep M2's allowance, pinned in the ChainConfig
    suites); ``@`` stays refused for every scheme written here."""

    @pytest.mark.parametrize(
        "good",
        [
            "bitcoind://127.0.0.1:8332",
            "bitcoind://node.local",
            "bitcoind://[::1]:8332",  # bracketed IPv6 host parses
        ],
    )
    def test_accepts_host_port_shapes(self, tmp_path: Path, good: str) -> None:
        store = Store(tmp_path / "burl.db")
        try:
            store.set_chain_base_url(f"  {good}  ")  # strip-on-write, unchanged
            assert store.get_chain_base_url() == good
        finally:
            store.close()

    @pytest.mark.parametrize(
        "bad",
        [
            "bitcoind://",  # no host
            "bitcoind://host:port",  # non-numeric port
            "bitcoind://host:99999",  # out of range
            "bitcoind://host/rpc",  # path (RPC surface is the POST root)
            "bitcoind://?q",
            "bitcoind://user:pw@host",  # userinfo — the STORED rung refuses
            "bitcoind://user@host",  # half a pair too
            "bitcoind://ho st",
        ],
    )
    def test_refusals_are_fail_closed_and_value_free(
        self, tmp_path: Path, bad: str
    ) -> None:
        store = Store(tmp_path / "burl.db")
        try:
            with pytest.raises(StoreError) as excinfo:
                store.set_chain_base_url(bad)
            assert bad not in str(excinfo.value)
            assert "user" not in str(excinfo.value)  # the userinfo refusal
            assert store.get_chain_base_url() is None  # nothing landed
        finally:
            store.close()


# ------------------------------------------------------------ resolver


class TestBackendAuthResolver:
    def _store(self, tmp_path: Path) -> Store:
        return Store(tmp_path / "auth.db")

    def test_checkbox_wins_over_a_stored_pair(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.set_backend_auth_none(True)
            store.set_backend_auth_user(USER)  # stale pair the box overrides
            store.set_backend_auth_pass(PASSWORD)
            assert _backend_auth(store) == _BackendAuth(omit=True)
        finally:
            store.close()

    def test_filled_pair_is_basic_auth(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.set_backend_auth_user(USER)
            store.set_backend_auth_pass(PASSWORD)
            assert _backend_auth(store) == _BackendAuth(user=USER, password=PASSWORD)
        finally:
            store.close()

    @pytest.mark.parametrize("half", ["user", "pass"])
    def test_half_pair_falls_to_the_cookie_default(
        self, tmp_path: Path, half: str
    ) -> None:
        # Documented interplay: UNchecked + empty/half = the M2 default
        # ladder (URL userinfo, then the cookie file, then no auth).
        store = self._store(tmp_path)
        try:
            if half == "user":
                store.set_backend_auth_user(USER)
            else:
                store.set_backend_auth_pass(PASSWORD)
            assert _backend_auth(store) is None
        finally:
            store.close()

    def test_empty_store_resolves_none(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            assert _backend_auth(store) is None
        finally:
            store.close()


class TestClientBuildOverlay:
    def _capture_build(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

        monkeypatch.setattr(app, "BitcoindClient", _Fake)
        return seen

    def test_bitcoind_kind_threads_the_pair(
        self, monkeypatch: pytest.MonkeyPatch, env_clean: None
    ) -> None:
        seen = self._capture_build(monkeypatch)
        settings = Settings(chain_base_url="bitcoind://h:8332")
        _build_chain_client(settings, _BackendAuth(user=USER, password=PASSWORD))
        assert seen["rpc_user"] == USER
        assert seen["rpc_password"] == PASSWORD
        assert seen.get("no_credentials") is not True

    def test_omit_flag_threads_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch, env_clean: None
    ) -> None:
        seen = self._capture_build(monkeypatch)
        settings = Settings(chain_base_url="bitcoind://h:8332")
        _build_chain_client(settings, _BackendAuth(omit=True))
        assert seen["no_credentials"] is True
        assert "rpc_user" not in seen

    def test_no_overlay_keeps_the_m2_default_ladder(
        self, monkeypatch: pytest.MonkeyPatch, env_clean: None
    ) -> None:
        seen = self._capture_build(monkeypatch)
        settings = Settings(chain_base_url="bitcoind://h:8332")
        _build_chain_client(settings, None)
        assert "rpc_user" not in seen
        assert "no_credentials" not in seen
        assert seen["base_url"] == "bitcoind://h:8332"

    def test_http_and_ssl_kinds_ignore_credentials(
        self, monkeypatch: pytest.MonkeyPatch, env_clean: None
    ) -> None:
        # Creds are inert for non-Core kinds (plan OQ-5): the Esplora and
        # Electrum constructors never even see them.
        built: list[tuple[str, dict[str, Any]]] = []

        monkeypatch.setattr(
            app,
            "EsploraClient",
            lambda **kw: built.append(("esplora", kw)),
        )
        monkeypatch.setattr(
            app,
            "ElectrumClient",
            lambda **kw: built.append(("electrum", kw)),
        )
        auth = _BackendAuth(user=USER, password=PASSWORD)
        _build_chain_client(Settings(chain_base_url="https://x.example/api"), auth)
        _build_chain_client(Settings(chain_base_url="ssl://x:50002"), auth)
        assert [kind for kind, _ in built] == ["esplora", "electrum"]
        for _, kwargs in built:
            assert "rpc_user" not in kwargs and "no_credentials" not in kwargs


class TestHotSwapApplyCarriesCreds:
    """The Apply path resolves the stored overlay at CALL time: creds saved
    moments before an Apply ride the probe (covered against the loopback
    fixtures in test_chain_bitcoind) AND the built client."""

    def test_apply_passes_the_resolved_overlay_to_the_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_clean: None,
    ) -> None:
        from tests.test_backend_hotswap import (
            _drain,
            _FakeChain,
            _mk_wiring,
        )

        wiring, commands = _mk_wiring(tmp_path, monkeypatch)
        try:
            wiring.store.set_backend_auth_user(USER)
            wiring.store.set_backend_auth_pass(PASSWORD)
            seen: dict[str, Any] = {}

            def fake_build(settings: Any, auth: Any = None) -> Any:
                seen["auth"] = auth
                return _FakeChain(settings.chain_base_url)

            monkeypatch.setattr(app, "_build_chain_client", fake_build)
            flow = app.ChainBackendFlow(wiring, lambda url: url)
            error, fields = flow.apply("bitcoind://node.local:8332")
            assert error is None and fields["swapped"] is True
            assert seen["auth"] == _BackendAuth(user=USER, password=PASSWORD)
            assert wiring.store.get_chain_base_url() == "bitcoind://node.local:8332"
            _drain(wiring, commands)
        finally:
            wiring.store.close()


# ------------------------------------------------- never-echo settings API


class TestSettingsSurfaceSecrets:
    def _entries(self, store: Store) -> dict[str, dict[str, Any]]:
        result = app.handle_settings_request(store, None, None)
        assert result["schema"] == SETTINGS_SCHEMA and result["status"] == "ok"
        return {e["key"]: e for e in result["settings"]}  # type: ignore[index]

    def test_write_applies_and_replies_configured_only(
        self, tmp_path: Path
    ) -> None:
        store = Store(tmp_path / "s.db")
        try:
            reply = app.handle_settings_request(store, "backend_auth_user", USER)
            assert reply["status"] == "applied"
            entry = reply["settings"][0]  # type: ignore[index]
            assert entry["key"] == "backend_auth_user"
            assert entry["value"] is None and entry["configured"] is True
            assert USER not in json.dumps(reply)  # the applied reply never echoes

            reply = app.handle_settings_request(store, "backend_auth_pass", PASSWORD)
            assert reply["status"] == "applied"
            assert PASSWORD not in json.dumps(reply)

            entries = self._entries(store)
            assert entries["backend_auth_user"]["configured"] is True
            assert entries["backend_auth_pass"]["configured"] is True
            assert entries["backend_auth_pass"]["value"] is None
            assert PASSWORD not in json.dumps(entries)

            # Single-key read (?key= backend) answers the same shape:
            single = app.handle_settings_request(store, "backend_auth_pass", None)
            assert single["settings"][0]["value"] is None  # type: ignore[index]
            assert PASSWORD not in json.dumps(single)
        finally:
            store.close()

    def test_clear_writes(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "s.db")
        try:
            store.set_backend_auth_user(USER)
            store.set_backend_auth_pass(PASSWORD)
            store.set_backend_auth_none(True)
            for key in (
                "backend_auth_user",
                "backend_auth_pass",
                "backend_auth_none",
            ):
                reply = app.handle_settings_request(store, key, "")
                assert reply["status"] == "applied", key
            entries = self._entries(store)
            assert entries["backend_auth_user"]["configured"] is False
            assert entries["backend_auth_pass"]["configured"] is False
            assert entries["backend_auth_none"]["configured"] is False
        finally:
            store.close()

    def test_none_flag_closed_vocabulary(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "s.db")
        try:
            assert (
                app.handle_settings_request(store, "backend_auth_none", "1")[
                    "status"
                ]
                == "applied"
            )
            assert store.get_backend_auth_none() is True
            reply = app.handle_settings_request(store, "backend_auth_none", "0")
            assert reply["status"] == "rejected"
            assert "must be 1 or empty" in str(reply["error"])
            assert store.get_backend_auth_none() is True  # refusal touched nothing
        finally:
            store.close()

    def test_shape_refusal_is_rejected_value_free(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "s.db")
        try:
            reply = app.handle_settings_request(
                store, "backend_auth_pass", "bad password"
            )
            assert reply["status"] == "rejected"
            assert "bad password" not in json.dumps(reply)
        finally:
            store.close()


# ------------------------------------------------------- bitcoind.py units


class TestCookieBoundedRead:
    """The M2 review LOW, closed at M3: the cap is enforced AT READ TIME."""

    def test_oversized_cookie_never_slurped_and_never_sent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_clean: None,
    ) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_bytes(b"__cookie__:" + b"x" * 5_000_000)
        asked: list[int] = []
        real_open = Path.open

        class _Counting:
            def __init__(self, handle: Any) -> None:
                self._handle = handle

            def read(self, size: int = -1) -> bytes:
                asked.append(size)
                return self._handle.read(size)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *exc: object) -> None:
                self._handle.close()

        def spy_open(self: Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
            handle = real_open(self, mode, *args, **kw)
            return _Counting(handle) if self == cookie else handle

        monkeypatch.setattr(Path, "open", spy_open)
        client = BitcoindClient(
            base_url="bitcoind://127.0.0.1:1",
            timeout_s=0.2,
            max_retries=0,
            rpc_cookie_path=str(cookie),
        )
        assert client._credential_header() is None  # oversized → no header
        assert asked == [bitcoind_module._MAX_COOKIE_BYTES + 1]  # capped read

    def test_valid_cookie_still_authenticates(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:pw")
        client = BitcoindClient(
            base_url="bitcoind://127.0.0.1:1",
            timeout_s=0.2,
            max_retries=0,
            rpc_cookie_path=str(cookie),
        )
        header = client._credential_header()
        assert header is not None and header.startswith("Basic ")
        # The secret rides ONLY base64-wrapped, never as plaintext bytes:
        assert "__cookie__:pw" not in header

    def test_no_credentials_flag_skips_an_existing_cookie(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:pw")
        client = BitcoindClient(
            base_url="bitcoind://127.0.0.1:1",
            timeout_s=0.2,
            max_retries=0,
            rpc_cookie_path=str(cookie),
            no_credentials=True,
        )
        assert client._credential_header() is None

    def test_contradictory_construction_refused(self, env_clean: None) -> None:
        with pytest.raises(ValueError):
            BitcoindClient(
                base_url="bitcoind://127.0.0.1:1", no_credentials=True, rpc_user="u"
            )  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            BitcoindClient(
                base_url="bitcoind://u:p@127.0.0.1:1", no_credentials=True
            )


# ------------------------------------------------------- /setup cred step


class TestSetupCredentials:
    """The /setup credentials step against a REAL store; the injected probe
    resolves credentials the way the production closure does (``_backend_auth``
    at call time), so the whole ride — write candidate, re-probe, restore on
    failure, keep on save — runs through the shipping code paths."""

    HTTP = "http://127.0.0.1:8332"
    CANONICAL = "bitcoind://127.0.0.1:8332"

    def _flow(
        self,
        store: Store,
        probe: Any,
        *,
        saved: list[str] | None = None,
    ) -> ob.OnboardingFlow:
        return ob.OnboardingFlow(
            store=store,
            check_backend=probe,
            armed=True,
            backend_saved=(
                None
                if saved is None
                else (lambda url: saved.append(url) or "swapped")
            ),
        )

    @staticmethod
    def _run(flow: ob.OnboardingFlow, lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            flow.handle_line(line, out.append)
        return out

    @staticmethod
    def _credentialed_probe(store: Store, calls: list[str]) -> Any:
        """A stand-in Core node that demands exactly our fixture pair:
        answers in Core shape (→ the canonical rewrite) only once the
        store holds the login; refuses everyone else."""

        def probe(url: str) -> str | None:
            calls.append(url)
            auth = _backend_auth(store)
            if url == TestSetupCredentials.HTTP and auth is not None:
                if auth.omit:
                    return TestSetupCredentials.CANONICAL  # open node, honest omit
                if auth.user == USER and auth.password == PASSWORD:
                    return TestSetupCredentials.CANONICAL
            return None

        return probe

    def test_failed_https_and_ssl_never_ask_for_a_login(self, tmp_path: Path) -> None:
        for url in ("https://x.example/api", "ssl://x.example:50002"):
            store = Store(tmp_path / f"ob{url[:5]}.db")
            try:
                flow = self._flow(store, lambda _u: None)
                out = self._run(flow, [url])
                assert ob.VALIDATION_FAIL in out
                assert ob.URL_CRED_ASK not in out  # creds would be inert there
                assert flow._state is not ob._AskState.CRED_ASK
            finally:
                store.close()

    def test_http_failure_offers_one_bounded_login_step(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        try:
            flow = self._flow(store, lambda _u: None)
            out = self._run(flow, [self.HTTP])
            assert ob.VALIDATION_FAIL in out and ob.URL_CRED_ASK in out
            assert flow._state is ob._AskState.CRED_ASK
        finally:
            store.close()

    def test_bitcoind_failure_also_offers_the_step(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        try:
            flow = self._flow(store, lambda _u: None)
            out = self._run(flow, ["bitcoind://node.local:8332"])
            assert ob.URL_CRED_ASK in out  # explicit Core: same one bounded offer
        finally:
            store.close()

    def test_pair_answer_reprobes_saves_and_keeps_the_creds(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        saved: list[str] = []
        try:
            flow = self._flow(
                store, self._credentialed_probe(store, calls), saved=saved
            )
            out = self._run(flow, [self.HTTP, f"{USER}:{PASSWORD}"])
            assert calls == [self.HTTP, self.HTTP]  # same candidate, now credentialed
            assert store.get_backend_auth_user() == USER
            assert store.get_backend_auth_pass() == PASSWORD
            assert store.get_chain_base_url() == self.CANONICAL  # rewrite saved
            assert saved == [self.CANONICAL]  # the swap hook rode the canonical URL
            assert ob.CONFIRMED in out and ob.SWITCHING_NOW in out
            assert flow._state is ob._AskState.DONE
            # Value-free: the typed credential never rides any output line.
            assert all(PASSWORD not in line for line in out)
            assert all(USER not in line for line in out)
        finally:
            store.close()

    def test_second_failure_restores_the_prior_credential_record(
        self, tmp_path: Path
    ) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        try:
            store.set_backend_auth_user("old-user")  # a WORKING backend's login
            store.set_backend_auth_pass("old-pass")
            flow = self._flow(store, self._credentialed_probe(store, calls))
            out = self._run(flow, [self.HTTP, "new-user:" + PASSWORD + "x"])
            assert ob.VALIDATION_FAIL in out
            # The failed attempt rolled back — the old pair still stands:
            assert store.get_backend_auth_user() == "old-user"
            assert store.get_backend_auth_pass() == "old-pass"
            assert store.get_chain_base_url() is None
            assert flow._state is ob._AskState.URL_ASK
            joined = " ".join(out)
            assert "old-user" not in joined and "old-pass" not in joined
            assert PASSWORD + "x" not in joined
        finally:
            store.close()

    def test_none_answer_records_explicit_no_auth(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        try:
            store.set_backend_auth_user("old-user")  # stale pair must clear
            store.set_backend_auth_pass("old-pass")
            flow = self._flow(store, self._credentialed_probe(store, calls))
            # The probe's omit branch answers CANONICAL for auth.omit.
            out = self._run(flow, [self.HTTP, "none"])
            assert store.get_backend_auth_none() is True
            assert store.get_backend_auth_user() is None
            assert store.get_backend_auth_pass() is None
            assert store.get_chain_base_url() == self.CANONICAL
            assert ob.CONFIRMED in out
        finally:
            store.close()

    def test_back_returns_to_the_address_step(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        try:
            flow = self._flow(store, lambda u: calls.append(u) or None)
            out = self._run(flow, [self.HTTP, "back"])
            assert out[-1] == ob.URL_PROMPT
            assert flow._state is ob._AskState.URL_ASK
            assert calls == [self.HTTP]  # "back" re-probes nothing
        finally:
            store.close()

    def test_shape_refused_pair_reprompts_without_a_write(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        try:
            flow = self._flow(store, lambda _u: calls.append(_u) or None)
            out = self._run(flow, [self.HTTP, "bad user:pw"])
            assert out[-1] == ob.URL_CRED_REJECTED
            assert store.get_backend_auth_user() is None
            assert calls == [self.HTTP]  # refused BEFORE any re-probe
            assert "bad" not in " ".join(out)
            assert flow._state is ob._AskState.CRED_ASK  # the step stays open
        finally:
            store.close()

    def test_half_pair_and_junk_reprompt_the_closed_step(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        try:
            flow = self._flow(store, lambda _u: None)
            out = self._run(flow, [self.HTTP, "user:", "just prose"])
            # initial offer + one re-prompt per unrecognized line, never a guess
            assert out.count(ob.URL_CRED_ASK) == 3
            assert store.get_backend_auth_user() is None
        finally:
            store.close()

    def test_corrected_address_welcomed_mid_step(self, tmp_path: Path) -> None:
        good = "http://127.0.0.1:3006/api"
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []

        def probe(url: str) -> str | None:
            calls.append(url)
            return url if url == good else None

        try:
            flow = self._flow(store, probe)
            self._run(flow, [self.HTTP, good])
            assert store.get_chain_base_url() == good
            assert calls == [self.HTTP, good]
        finally:
            store.close()

    def test_bitcoind_scheme_is_a_first_class_candidate(self, tmp_path: Path) -> None:
        url = "bitcoind://node.local:8332"
        store = Store(tmp_path / "ob.db")
        try:
            flow = self._flow(store, lambda u: u)
            out = self._run(flow, [url])
            assert ob.CONFIRMED in out and ob.VALIDATION_FAIL not in out
            assert store.get_chain_base_url() == url
        finally:
            store.close()

    def test_userinfo_bitcoind_url_is_refused_value_free(self, tmp_path: Path) -> None:
        # The probe (identity here) tolerates the shape; the STORE writer
        # is the fail-closed door — and its refusal never echoes the creds.
        store = Store(tmp_path / "ob.db")
        try:
            flow = self._flow(store, lambda u: u)
            out = self._run(flow, ["bitcoind://u:p@node.local:8332"])
            assert ob.VALIDATION_FAIL in out
            assert store.get_chain_base_url() is None
            assert "u:p" not in " ".join(out)
        finally:
            store.close()

    def test_foreign_scheme_still_refused_without_probe(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "ob.db")
        calls: list[str] = []
        try:
            flow = self._flow(store, lambda u: calls.append(u) or None)
            out = self._run(flow, ["ftp://x.example"])
            assert out == [ob.NON_ESPLORA_URL]
            assert calls == []
        finally:
            store.close()


# ------------------------------------------------------------- rewords


class TestRewords2h:
    """docs/ux-web-copy-2.md §2h rows 64/66/68/72/73 applied verbatim (73:
    the designer's gloss plus the Core clause M2 added — see the constant's
    comment). Literal pins so a later silent drift fails here."""

    def test_row_64_model_card_question(self) -> None:
        assert MODEL_CARD_QUESTION == (
            "The AI model hasn't been downloaded yet. Want to download it now?"
        )

    def test_row_66_download_started(self) -> None:
        assert MODEL_DL_STARTED == (
            "Downloading the model now — it will be checked against its "
            "official fingerprint before it is installed. Progress shows here."
        )
        assert "pinned hash" not in MODEL_DL_STARTED

    def test_row_68_download_done(self) -> None:
        assert MODEL_DL_DONE == (
            "Downloaded and verified. The model takes over the next time you "
            "start the app — this session keeps running without it."
        )
        assert "NEXT" not in MODEL_DL_DONE  # ALL-CAPS emphasis retired

    def test_row_72_integrity_warning(self) -> None:
        assert MODEL_INTEGRITY_WARNING == (
            "The model file failed its integrity check — it may be "
            "corrupted. Run /download to fetch a fresh copy."
        )
        assert "recommended" not in MODEL_INTEGRITY_WARNING

    def test_row_73_backend_probe_fail_gloss(self) -> None:
        # Designer gloss "(mempool.space-style)" for Esplora, the "or"
        # joining, and the value-free tail kept verbatim; the Core-RPC
        # clause STAYS (the probe really does accept bitcoind:// since M2).
        assert "Esplora (mempool.space-style) http(s)" in BACKEND_PROBE_FAIL
        assert "Bitcoin Core RPC (bitcoind://)" in BACKEND_PROBE_FAIL
        assert "nothing was saved and the current backend stays in service" in (
            BACKEND_PROBE_FAIL
        )
        assert "unreachable" in BACKEND_PROBE_FAIL
