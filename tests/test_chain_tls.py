"""TCK-BACKEND-001 — self-hosted https backends with private / self-signed certs.

Diagnosis (Start9, 2026-09): a self-hosted Esplora backend serving a
SELF-SIGNED TLS certificate makes httpx's default ``verify=True`` abort the
handshake; httpx surfaces the failed cert verification as ``ConnectError``
(``[SSL: CERTIFICATE_VERIFY_FAILED] ... self-signed certificate``), which the
Esplora retry wrapper then reports as exactly the user's line —
``tip-height request failed after N retries: network error (ConnectError)``.
(mDNS ``.local`` resolution is NOT the cause: httpx resolves the host via
``getaddrinfo`` before TLS, so a resolution failure would never reach a cert
error; the reachability the user confirmed with ping/browser proves DNS is
fine. The failure is purely transport AUTHENTICATION.)

These tests pin, offline and deterministically, against a real local
self-signed TLS server (stdlib ``ssl`` + ``http.server``):

* verify=True (the fail-closed default) → ``ConnectError`` → ``ChainError``
  carrying the user's exact ``(ConnectError)`` surface;
* ``LOCALWALLET_TLS_VERIFY=0`` (the ladder's disable rung) → the request
  SUCCEEDS, through the SAME construction path as ``chain_base_url``;
* the env > config-file > default ladder types/parses strictly (malformed =
  value-free startup refusal);
* the honest startup warning is emitted IFF verification is disabled.

No public network is touched: the "backend" is a loopback test server.
"""

from __future__ import annotations

import http.server
import json
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest

from localwallet import app as app_module
from localwallet.app import AUTO_SCAN_ENV_VAR, TLS_UNVERIFIED_WARNING, run
from localwallet.chain import (
    MAINNET_GENESIS_HASH,
    ChainConfig,
    ChainError,
    EsploraClient,
    check_backend,
)
from localwallet.config import Settings
from tests.test_e2e_skeleton import ZPUB

# Fixed self-signed cert/KEY for CN=127.0.0.1 (SAN IP:127.0.0.1, DNS:localhost),
# not-valid-before 2026-09-07, expires 2126-08-15. Test-only key material for a
# throwaway loopback server — it authenticates NOTHING real and is not a secret
# in any sense PROJECT.md protects (no wallet, no xpub, no address).
_SELF_SIGNED_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAzNzt93IiPEygXivk2V9ErREn6BePubMNfLSOqlbi2LtDR6oi
hVGi8sYBaqCTPatzqeGOsw8CHg04y4YLjmLGCU+j4jSrs9Sx7dBz91xHciEutpRN
MYuzxuRMcx83v1hfm/FCXsn0DvrXq0dMvg4lVlA5eHfgU/bgRw4ve73v3lKbtIUI
vjXTDto1Mr6Hjutt6geYQ1OCpWLpnmMOvtIARgezWnSdQHMRhlD/yE7y+OQ31JO9
NIUT4F4JaQ/24cp0IJwsMoDjaOvXa5+6xzS8GT5YqBO9MRmXus8tN8DUnwkUZy78
X/VtmBo7FU3THWQIkU6DtCKYTS6O7uImmtrnMwIDAQABAoIBAANre4md+Ohm/EQX
P7Fi/rchTFp6inq2a3Km2vjVt+D+I0me8UuVldyNUF8SrI5NoJMJH7uLjaCHZpNZ
mKrsU+z+d0DvaygYy8UP6YrgmkAPML+Kpm9gAHxA7O3rt/2OGlkn6mHBcCwhC4eh
m3CXCnaWs+w8I8w9sjvnBq/UofpceElrWFRb7rhryKr0ga0FL8XQmCZEDxiO8o+S
8UPi+Lv4OpW27MfFsv7lmCOoe6d+SRC6xhKJHeLA8TMuIUZ6WdmcEFKu2H0tb2CA
tLjgznzABtKXmBaHpL4wMCCl5xOekY0bq2AixPpZ9Y3UqTSbqmJK2KT347rciXfn
p8J1B10CgYEA7QeY9wKy927U9FtWWeBfKd0TLCLxBu3YZzo79lWU+cKHsM9t8EAo
HfddxjggEQO1DpVlv8eZrO4c4SbF8MQQv8HPDO9ZYr4erm/h7b1xOYnXdqUsMNOC
My3znWvG4cQ02qFmX13ZRFvxtNbEjKJrC8kMNjhH29717Ju3Y5z/ER8CgYEA3UJI
UJs6gNBo9jn26Ll9/GKK9e+2IV/oMHLK1kLisqm+Y3JMVHce+bCl32xcYGBCcb5D
BpiP+4SC68/5kBodn1HhpM8NdSIwSKxZsv2A3DrfC+RvKWchjPo2bTogh2X/43Ts
2vwh8ASZTTjAxVROu8G1M7Onl7Zcv5SKRZMKw20CgYB2PAr2dBc3y8ZYWdNaI8z0
gf2VT5yxWyVOYMMWXpxgdcPf06jAZhBc2k6hmM+ODS5cpvNJVdR3aZNoUEH+lp7Q
OGoCxsXstm9xjgfB4nS/Qd4DpeLEPE0/IFXcGa3sYkYHJOl++r5tFfwcu+DxUfdZ
uqDnzu0xZSeBLi+tddvZ+wKBgBvV++0QKmMMVTgtALA0rfHzn9HjD4HRZA+8UWJ1
VbnuewJd3dZ+igoVvDiIlHKXiaRvsFUDGpIlEKeEKbyEXJevoHiwh9vlqjdqX3qS
RATw7yC643VNAT6QOAqz1mXSYkgGbMn8EHT2zyaU7kOlIKakbxyLDJmcmryLfn3U
SvVdAoGBALryJ01TKr3u9J+izEn8lwH9dDi4+ixYGhHrd87vVZVBdZ8ZZWjhDMXS
LwiJGE9UpreyiC1OGbqNNIwm/A89SGk0MxAEXmTxyHwsgP3qanEdPYfyN0EheJsZ
AT6tkbNxker0wtOsQYGQj0X/C2xNzbtJIMqVqtULkzrtJr4BEHjq
-----END RSA PRIVATE KEY-----"""

_SELF_SIGNED_CERT = """-----BEGIN CERTIFICATE-----
MIIC0zCCAbugAwIBAgIBATANBgkqhkiG9w0BAQsFADAcMRowGAYDVQQDDBFsb2Nh
bC13YWxsZXQgdGVzdDAgFw0yNjA5MDcwMDAwMDBaGA8yMTI2MDgxNTAwMDAwMFow
HDEaMBgGA1UEAwwRbG9jYWwtd2FsbGV0IHRlc3QwggEiMA0GCSqGSIb3DQEBAQUA
A4IBDwAwggEKAoIBAQDM3O33ciI8TKBeK+TZX0StESfoF4+5sw18tI6qVuLYu0NH
qiKFUaLyxgFqoJM9q3Op4Y6zDwIeDTjLhguOYsYJT6PiNKuz1LHt0HP3XEdyIS62
lE0xi7PG5ExzHze/WF+b8UJeyfQO+terR0y+DiVWUDl4d+BT9uBHDi97ve/eUpu0
hQi+NdMO2jUyvoeO623qB5hDU4KlYumeYw6+0gBGB7NadJ1AcxGGUP/ITvL45DfU
k700hRPgXglpD/bhynQgnCwygONo69drn7rHNLwZPlioE70xGZe6zy03wNSfCRRn
Lvxf9W2YGjsVTdMdZAiRToO0IphNLo7u4iaa2uczAgMBAAGjHjAcMBoGA1UdEQQT
MBGHBH8AAAGCCWxvY2FsaG9zdDANBgkqhkiG9w0BAQsFAAOCAQEAMVmRD2DD9nXy
l2EfFRNk5K26fir6YnflGeWmaxhPB6fmk6V6L481gLM+ayZjJvQxzkkVWhNcNnCW
D5WyFj3wp8wvlXON0ldsuGn4gYI6H1lH/fSzIhu9CQd3GBbuKUL3T3Vh0Qe+DvO3
AefMnR8L5Mc1Oz56zSc4NRh1NZ/5RJHDxnIabPCy7dabVcApx4lyVN8e8qokRKxM
SIh9IJmPS2vCKOckG5kuu+Z2KtfBbd7I4RtMLeKNkqhJ57YqmqgwykVuy0/Ih6jj
C/1+9sJqQ+cPRhrvREEl66Hz6fiQo9hPK6E3JmS+RK9LgAW/nvGfsqTtkIzl9ZM4
kcqSHUyiDA==
-----END CERTIFICATE-----"""

_TIP_HEIGHT = 800_000


@pytest.fixture(autouse=True)
def _no_real_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hermetic: EsploraClient/run() call ``Settings.from_env()`` with the
    DEFAULT path — point the file rung at a nonexistent tmp file so a real
    ``~/.localwallet/config.json`` on the test host can never flip
    ``tls_verify`` underneath these tests."""
    monkeypatch.setattr(
        "localwallet.config.CONFIG_FILE_PATH", tmp_path / "host-config-absent.json"
    )


class _EsploraHandler(http.server.BaseHTTPRequestHandler):
    """Minimal Esplora shape over TLS: tip height + mainnet genesis block."""

    def do_GET(self) -> None:
        if self.path == "/blocks/tip":
            body = str(_TIP_HEIGHT).encode("ascii")
        elif self.path == "/blocks/0":
            body = json.dumps([{"id": MAINNET_GENESIS_HASH, "height": 0}]).encode()
        else:
            body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:  # silence the test server
        return


@pytest.fixture()
def self_signed_url(tmp_path: Path) -> str:
    """Loopback https server speaking Esplora shape with a SELF-SIGNED cert."""
    key = tmp_path / "key.pem"
    crt = tmp_path / "cert.pem"
    key.write_text(_SELF_SIGNED_KEY)
    crt.write_text(_SELF_SIGNED_CERT)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EsploraHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# STEP 1 diagnosis — self-signed cert => ConnectError under the default.
# ---------------------------------------------------------------------------


def test_selfsigned_default_verify_raises_connect_error(
    self_signed_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repro: the fail-closed default REFUSES the self-signed backend and the
    failure surfaces as ``(ConnectError)`` — the user's exact error class."""
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    with EsploraClient(
        base_url=self_signed_url, timeout_s=5.0, max_retries=0
    ) as client:
        with pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        err = excinfo.value
        # The user-visible surface names the transport class verbatim — the
        # retry wrapper converts the httpx failure value-free, and the
        # exhausted-retry ChainError carries the class name in its message
        # (``from exc`` is not kept on the exhaustion surface; the message
        # IS the contract here).
        assert "network error (ConnectError)" in str(err)


def test_selfsigned_env_disable_succeeds(
    self_signed_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix (client path): LOCALWALLET_TLS_VERIFY=0 rides the SAME construction
    path as chain_base_url and the very same request now succeeds."""
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    with EsploraClient(
        base_url=self_signed_url, timeout_s=5.0, max_retries=0
    ) as client:
        assert client.get_tip_height() == _TIP_HEIGHT


def test_check_backend_selfsigned_ladder(
    self_signed_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The onboarding probe shares the ladder: default verify fails closed,
    the disable rung reaches the self-signed mainnet backend."""
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    assert check_backend(self_signed_url, timeout_s=5.0, max_retries=0) is False
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    assert check_backend(self_signed_url, timeout_s=5.0, max_retries=0) is True


# ---------------------------------------------------------------------------
# STEP 2 — the ladder: env > config file > fail-closed default, typed strict.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("yes", True),
    ],
)
def test_env_boolean_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", raw)
    got = Settings.from_env(config_path=tmp_path / "absent.json")
    assert got.tls_verify is expected


@pytest.mark.parametrize("raw", ["maybe", "2", "off", "on"])
def test_env_malformed_refuses_value_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", raw)
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env(config_path=tmp_path / "absent.json")
    message = str(excinfo.value)
    assert "tls_verify" in message
    assert raw not in message  # value-free


def test_default_is_fail_closed_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    got = Settings.from_env(config_path=tmp_path / "absent.json")
    assert got.tls_verify is True


def test_config_file_boolean_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tls_verify": False}), encoding="utf-8")
    assert Settings.from_env(config_path=path).tls_verify is False


def test_env_beats_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tls_verify": True}), encoding="utf-8")
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    assert Settings.from_env(config_path=path).tls_verify is False


@pytest.mark.parametrize("value", ['"false"', "0", "1", "null", "[true]"])
def test_config_file_wrong_type_refuses_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tls_verify": json.loads(value)}), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env(config_path=path)
    assert "tls_verify must be a boolean" in str(excinfo.value)


def test_chain_config_carries_flag_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    # TCK-DESCOPE-M3A: a WALLET selection needs a named backend (no public
    # default) — the knob resolution is otherwise untouched.
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "ssl://h:50002")
    settings = Settings.from_env(config_path=tmp_path / "absent.json")
    config = ChainConfig.from_settings(settings)
    assert config.tls_verify is False
    assert config.base_url == "ssl://h:50002"


def test_chain_config_default_and_strict_type() -> None:
    assert ChainConfig("https://h/api", 10.0, 3).tls_verify is True
    with pytest.raises(ValueError):
        ChainConfig("https://h/api", 10.0, 3, tls_verify="false")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# STEP 2 — honest startup warning, iff disabled.
# ---------------------------------------------------------------------------


def _run_with_mock_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Drive the real ``run()`` startup banners with a mocked chain client and
    no scan/watch/model traffic; return the captured output lines."""
    for var in (
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    store_path = tmp_path / "warn.db"
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.setenv("LOCALWALLET_NODE_DETECTION_ENABLED", "0")
    # TCK-DESCOPE-M3A: the app builds the wallet client and the decoupled
    # public-info fetcher through two named seams — inject the null client
    # at both (a resolved backend keeps the headless launch off the new
    # refuse-the-scan path, which is not what these TLS pins test).
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "ssl://tls.test:50002")
    monkeypatch.setattr(
        app_module, "_build_chain_client", lambda *_a, **_k: _NullClient()
    )
    monkeypatch.setattr(
        app_module, "_public_info_client", lambda *_a, **_k: _NullClient()
    )

    outputs: list[str] = []
    code = run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _prompt: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    return outputs


class _NullClient:
    """Construct-and-close seam: _wire shares this into fee/price/worker but
    nothing calls it (scan + watch + model are all switched off above)."""

    def close(self) -> None:
        return


def test_no_warning_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outputs = _run_with_mock_client(monkeypatch, tmp_path)
    assert not any("TLS" in line or "verification" in line.lower() for line in outputs)


def test_warning_emitted_once_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    outputs = _run_with_mock_client(monkeypatch, tmp_path)
    lines = [line for line in outputs if line == TLS_UNVERIFIED_WARNING]
    assert len(lines) == 1
    # Honest, value-free wording: names the risk, not any host.
    text = lines[0]
    assert "DISABLED" in text and "addresses" in text and "tamper" in text
    assert "127.0.0.1" not in text
