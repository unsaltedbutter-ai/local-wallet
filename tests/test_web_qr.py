"""TCK-QR-001: receive-address QR (GET /qr + client viewer).

Server half — a token-gated, STATELESS offline encoder:
* only checksum-valid MAINNET segwit addresses (the client's exact lowercase
  shape rule AND embit's BIP-173/350 structure) get a standalone
  ``image/svg+xml`` QR (xmlns present — it must load as an <img> src);
* everything else (uppercase, testnet, txid, bad checksum, v1 with a
  non-32-byte program, missing/duplicate ``value``) is a value-free 400 —
  the submitted string never rides back;
* the gate order holds: Host allowlist first, then token, then validation;
* the CSP delta is exactly one scheme: ``img-src 'self' blob:`` on every
  response (the blob exists because <img> cannot carry X-Auth-Token).

Client half — static pins on the shipped app.js/index.html (browser-free,
same technique as test_web_render_contract): the viewer dialog markup, the
one ``fetch("/qr?value="`` with the auth header, the blob: object-URL
lifecycle (create + revoke), toggle/Escape close wiring via addEventListener
(no inline handlers — also covered by the render-contract sink scan), and
the copy-text exclusion so the "QR" button words never leak into a copied
message.
"""

from __future__ import annotations

import http.client
import re
from pathlib import Path
from typing import Any

import pytest

from localwallet.ui.web.server import serve_web
from tests.test_web_server import _bootstrap

REPO_ROOT = Path(__file__).resolve().parent.parent
_STATIC = REPO_ROOT / "src" / "localwallet" / "ui" / "web" / "static"

# Canonical vectors: BIP-173 (v0 P2WPKH), BIP-173 (v0 P2WSH), BIP-350 (v1).
P2WPKH = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
P2WSH = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
TAPROOT = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"


@pytest.fixture
def serve(tmp_path: Path) -> Any:
    servers: list[Any] = []

    def _serve(**options: Any) -> Any:
        options.setdefault("static_dir", tmp_path / "static")
        server = serve_web(_bootstrap, **options)
        servers.append(server)
        return server

    yield _serve
    for server in servers:
        server.stop()


def _get(server: Any, path: str, *, token: str | None = None, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=10)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["X-Auth-Token"] = token
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    got = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, got, body


# --------------------------------------------------------------- server: accept

@pytest.mark.parametrize("address", [P2WPKH, P2WSH, TAPROOT], ids=["v0-p2wpkh", "v0-p2wsh", "v1-taproot"])
def test_mainnet_segwit_addresses_encode(serve: Any, address: str) -> None:
    server = serve()
    status, headers, body = _get(server, f"/qr?value={address}", token=server.token)
    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    # A STANDALONE SVG doc: xmlns is what makes it loadable as <img> src.
    assert b"<svg" in body and b"xmlns=\"http://www.w3.org/2000/svg\"" in body
    assert b"<?xml" in body
    # no-cache like every response (an address may rotate) + the CSP rides it.
    assert headers["cache-control"] == "no-store"
    assert "img-src 'self' blob:" in headers["content-security-policy"]
    assert server.token.encode() not in body


# --------------------------------------------------------------- server: reject

@pytest.mark.parametrize("bad", [
    P2WPKH.upper(),                       # uppercase: client shape rule is lowercase
    "tb1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",  # testnet
    "bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y",  # BIP-350 bad-length v1
    "f" * 64,                             # txid
    "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdx",  # valid shape, broken checksum
    "bc1qzzzzzzzzz",                      # shape-valid fragment, not an address
    "3FZbgi29cpjq2GjdwV8ETHuJs6BtX9Nwp2",  # legacy base58
    "javascript:alert(1)",                # scheme junk
    "",                                   # empty value
])
def test_everything_else_is_a_value_free_400(serve: Any, bad: str) -> None:
    server = serve()
    status, _headers, body = _get(server, "/qr?value=" + bad, token=server.token)
    assert status == 400
    assert body == b'{"error": "not a mainnet bech32 address"}'
    # value-free: NOTHING of the submitted string rides back.
    for fragment in (b"bc1", b"tb1", b"3FZ", b"alert", bad[:8].encode() if bad else b"\xff"):
        assert fragment not in body


def test_missing_and_duplicate_value_are_400(serve: Any) -> None:
    server = serve()
    assert _get(server, "/qr", token=server.token)[0] == 400
    dup = f"/qr?value={P2WPKH}&value={P2WPKH}"
    assert _get(server, dup, token=server.token)[0] == 400


def test_qr_is_token_gated_and_host_checked_first(serve: Any) -> None:
    server = serve()
    # No token → 401, and the error body carries neither token nor address.
    status, _h, body = _get(server, f"/qr?value={P2WPKH}")
    assert status == 401 and server.token.encode() not in body and b"bc1" not in body
    # DNS-rebinding Host is refused BEFORE the token (gate order preserved).
    status, _h, body = _get(
        server, f"/qr?value={P2WPKH}", host="evil.example", token=server.token
    )
    assert status == 400 and b"evil" not in body


def test_csp_delta_is_exactly_one_scheme(serve: Any) -> None:
    """index + /qr + JSON errors all carry the same policy; the only QR-related
    change is ``blob:`` on img-src — nothing else loosened (no data:, no
    unsafe-*, no remote origins)."""
    server = serve()
    _status, headers, _body = _get(server, "/qr", token=server.token)
    csp = headers["content-security-policy"]
    assert "img-src 'self' blob:;" in csp
    assert "data:" not in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert csp.startswith("default-src 'none';")


def test_served_index_allows_the_same_blob_imgs(tmp_path: Any) -> None:
    server = serve_web(_bootstrap, static_dir=_STATIC)
    server.serve()
    try:
        status, headers, _body = _get(server, "/", token=server.token)
    finally:
        server.stop()
    assert status == 200
    assert "img-src 'self' blob:;" in headers["content-security-policy"]


# ------------------------------------------------------------- client static pins

def test_client_fetches_qr_with_token_and_renders_via_blob() -> None:
    code = _STATIC.joinpath("app.js").read_text(encoding="utf-8")
    # Exactly ONE /qr fetch, always with the auth header + encodeURIComponent.
    assert code.count('"/qr?value="') == 1
    assert 'encodeURIComponent(address)' in code
    assert "URL.createObjectURL(blob)" in code
    assert "URL.revokeObjectURL" in code  # the blob is revoked on close/replace
    # alt carries the ticket's title string plus the verbatim address; the
    # caption is textContent (never a markup sink — the render-contract scan
    # bans innerHTML tree-wide).
    assert "LABELS.qrTitle" in code
    assert "qrCaptionEl.textContent = address;" in code
    # Close paths: Close button, backdrop click, Escape, and toggle-off —
    # all addEventListener (no inline handlers; the \bon[a-z]+= scans in
    # test_web_render_contract cover markup and JS alike).
    assert 'qrCloseEl.addEventListener("click", closeQr)' in code
    assert 'event.key === "Escape"' in code
    assert "function qrButton" in code
    # The copy-text exclusion: a copied message never gains the button words.
    assert 'node.classList.contains("qr-btn")' in code
    html = _STATIC.joinpath("index.html").read_text(encoding="utf-8")
    assert 'id="qr-viewer"' in html
    assert 'role="dialog"' in html
    assert re.search(r"<img[^>]*id=\"qr-img\"", html)
