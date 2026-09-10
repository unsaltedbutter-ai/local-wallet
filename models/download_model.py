#!/usr/bin/env python3
"""Pinned model downloader for local-wallet.

Fetches and verifies the GGUF models listed in ``models/manifest.json``.

NETWORK ACCESS NOTICE
---------------------
This script uses ``urllib.request``. That is intentionally the ONLY place in
the repository where urllib appears, and it is permitted HERE because this is
a **build-time tool** under ``models/``, NOT part of the linted ``src/`` tree.
The network-import lint (``tools/lint_network.py``) only scans
``src/localwallet/``, and the runtime invariant "network I/O only in
``src/localwallet/chain/``" governs the *application*, not build tooling. Do
not copy this pattern into ``src/``.

There is nothing sensitive here: we download public GGUF weights and record
their SHA-256 digests. Nothing is logged beyond file names and byte counts.

TOKEN SUPPORT
-------------
The default sources in ``manifest.json`` are the **ungated** ``unsloth``
mirrors and need NO token. However, the official ``google/gemma-4-*-it-GGUF``
repos are **gated** on Hugging Face: they require license acceptance and a
user token (HTTP 401 without one). To download from those official repos,
the recommended method is to set the ``HF_TOKEN`` environment variable and
run without the flag. Passing ``--hf-token <token>`` on the command line
still works, but it exposes the token via the process table and shell
history, so prefer the environment variable. The value is sent only as an
``Authorization: Bearer <token>`` header and is NEVER printed, logged, or
embedded in any error message.

Redirect behavior: Hugging Face's ``resolve`` endpoint 302-redirects to HF's
own CDN (e.g. ``*.cdn.hf.co``). ``urllib``'s ``HTTPRedirectHandler`` forwards
caller-set headers (only dropping ``Content-length``/``Content-type``) on
redirects, including cross-host ones, so the ``Authorization`` header is sent
to the CDN too. That remains acceptable here for three reasons: (a) the
default manifest sources are ungated and tokenless, so no token is involved;
(b) when a token IS used, the 302 target is HF's own CDN object store for the
exact same object and the CDN fetch is pre-signed, so the token is not needed
there anyway; and (c) the token value is never echoed into logs or error
messages.

Behavior
--------
* Reads ``models/manifest.json``: a JSON list of entries with ``name``, ``url``,
  ``sha256`` (string | null), ``size_bytes`` (string | null).
* Downloads to a ``<name>.part`` file, verifies SHA-256 (streaming), then
  renames into place. Refuses to install on hash mismatch.
* Resume: if ``.part`` already exists we send an HTTP ``Range`` request. If the
  server replies ``206`` we append; if it replies ``200`` the server ignored
  the range and we restart from scratch.
* ``--write-hash``: after a fully verified download, write the computed
  ``sha256`` and ``size_bytes`` back into ``manifest.json``. This is also the
  bootstrap step for entries whose ``sha256`` is ``null`` (first networked
  run).
* ``--check``: verify existing files against the manifest; exit 0 if all
  present and matching, 1 otherwise. Does not download.

Exit codes: 0 = success, 1 = operational failure (mismatch, missing model,
unresolved null hash), 2 = CLI usage error (argparse default).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
CHUNK = 1 << 20  # 1 MiB
HF_TOKEN_ENV = "HF_TOKEN"


# --------------------------------------------------------------------------- #
# manifest helpers
# --------------------------------------------------------------------------- #
def load_manifest() -> list[dict]:
    with MANIFEST.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise SystemExit("manifest.json must be a JSON list of entries")
    return data


def find_entry(entries: list[dict], name: str) -> dict:
    for entry in entries:
        if entry.get("name") == name:
            return entry
    raise SystemExit(f"model '{name}' not found in {MANIFEST.name} "
                     f"(known: {', '.join(e.get('name', '?') for e in entries)})")


def save_manifest(entries: list[dict]) -> None:
    with MANIFEST.open("w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
        fh.write("\n")


def record_hash(entries: list[dict], name: str, sha256: str, size: int) -> None:
    for entry in entries:
        if entry.get("name") == name:
            entry["sha256"] = sha256
            entry["size_bytes"] = str(size)
            return
    raise SystemExit(f"cannot record hash: '{name}' not in manifest")


# --------------------------------------------------------------------------- #
# hashing / verification
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def _open_request(url: str, range_start: int | None, token: str | None = None):
    headers = {}
    if range_start:
        headers["Range"] = f"bytes={range_start}-"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=60)


def _expected_total(resp, existing: int) -> int | None:
    """The full-file byte count from the response headers, or ``None`` when
    the server did not say (resume: ``206`` carries ``Content-Range``'s
    total; a fresh ``200`` carries ``Content-Length``)."""
    try:
        if resp.getcode() == 206:
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                return int(content_range.rsplit("/", 1)[1])
        else:
            length = resp.headers.get("Content-Length")
            if length:
                return existing + int(length)
    except (ValueError, TypeError):
        return None
    return None


def download(url: str, part: Path, dest: Path, token: str | None = None,
             progress=None) -> None:
    """Download ``url`` into ``part`` (resumable), then move to ``dest``.

    ``progress(downloaded_bytes, total_bytes_or_None)`` (TCK-LAUNCH-002
    seam) is called on every chunk when given — byte counts ONLY, no paths;
    the download/verify/install ORDER and the hash contract are unchanged.
    """
    existing = part.stat().st_size if part.exists() else 0

    if existing > 0:
        print(f"resuming from {existing} bytes")
        try:
            resp = _open_request(url, existing, token)
        except urllib.error.HTTPError as exc:
            # 416 Range Not Satisfiable: our .part is >= the whole file.
            # Treat as corrupt and restart cleanly.
            if exc.code == 416:
                print(f"range rejected (416); restarting download for {part.name}")
                part.unlink()
                existing = 0  # clean restart: drop the stale resume size
                resp = _open_request(url, None, token)
                mode = "wb"
            else:
                raise
        else:
            code = resp.getcode()
            if code == 206:
                mode = "ab"
            else:
                # Server ignored our Range header; restart from scratch.
                print(f"server returned {code} (no resume support); restarting")
                part.unlink()
                existing = 0  # clean restart: drop the stale resume size
                mode = "wb"
    else:
        resp = _open_request(url, None, token)
        mode = "wb"

    total = _expected_total(resp, existing)
    written = existing
    with part.open(mode) as fh:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            written += len(chunk)
            if progress is not None:
                progress(written, total)
    resp.close()

    part.replace(dest)
    print(f"downloaded {dest.name} ({dest.stat().st_size} bytes)")


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def cmd_check(entries: list[dict], name: str, out: Path) -> int:
    entry = find_entry(entries, name)
    dest = out / f"{name}.gguf"
    expected = entry.get("sha256")
    size = entry.get("size_bytes")

    if not dest.exists():
        print(f"FAIL: {dest.name} missing", file=sys.stderr)
        return 1
    if expected is None:
        print(f"NOTE: {dest.name} exists but sha256 is null (unpinned). "
              f"Run with --write-hash to pin it.", file=sys.stderr)
        return 1
    if size is not None and dest.stat().st_size != int(size):
        print(f"FAIL: {dest.name} size {dest.stat().st_size} != pinned {size}",
              file=sys.stderr)
        return 1

    actual = sha256_of(dest)
    if actual != expected:
        print(f"FAIL: {dest.name} sha256 mismatch\n  expected {expected}\n"
              f"  actual   {actual}", file=sys.stderr)
        return 1
    print(f"OK: {dest.name} verified")
    return 0


def cmd_install(entries: list[dict], name: str, out: Path,
                write_hash: bool, token: str | None = None,
                json_progress: bool = False) -> int:
    entry = find_entry(entries, name)
    url = entry["url"]
    expected = entry.get("sha256")
    dest = out / f"{name}.gguf"
    part = out / f"{name}.gguf.part"

    def progress(downloaded: int, total: int | None) -> None:
        # TCK-LAUNCH-002: one int-only JSON line per chunk on stdout for the
        # engine's inline progress bar. Byte counts ONLY — never a path,
        # never a username. The app ignores every non-JSON stdout line.
        print(json.dumps({"downloaded": downloaded, "total": total}), flush=True)

    out.mkdir(parents=True, exist_ok=True)
    download(url, part, dest, token,
             progress=progress if json_progress else None)

    actual = sha256_of(dest)
    size = dest.stat().st_size

    if expected is not None and actual != expected:
        print(f"REFUSED: hash mismatch — NOT installing {dest.name}\n"
              f"  expected {expected}\n  actual   {actual}", file=sys.stderr)
        dest.unlink(missing_ok=True)
        return 1

    if expected is None:
        if not write_hash:
            print(f"NOTE: {dest.name} has no pinned sha256 (null). "
                  f"It was downloaded and verified-by-observation only.\n"
                  f"Re-run with --write-hash to record its digest so future "
                  f"downloads are pinned.", file=sys.stderr)
        else:
            print("first-run bootstrap: recording sha256 + size")
            record_hash(entries, name, actual, size)
            save_manifest(entries)
            print(f"pinned {name} -> {actual} ({size} bytes)")

    if write_hash and expected is not None:
        # Re-confirm/refresh the recorded hash after a verified download.
        record_hash(entries, name, actual, size)
        save_manifest(entries)

    print(f"installed {dest.name}")
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pinned model downloader for local-wallet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See models/MODELS.md for usage and the SHA-256 bootstrap step.")
    p.add_argument("--model", required=True,
                   help="model name as listed in manifest.json")
    p.add_argument("--out", type=Path, default=HERE / "bin",
                   help="output directory (default: models/bin)")
    p.add_argument("--write-hash", action="store_true",
                   help="after a fully verified download, write sha256+size "
                        "back into manifest.json (also bootstraps null hashes)")
    p.add_argument("--check", action="store_true",
                   help="verify existing file against the manifest; no download")
    p.add_argument("--json-progress", action="store_true",
                   help="emit one int-only JSON progress line per downloaded "
                        "chunk ({downloaded,total}) on stdout — the seam the "
                        "app's inline model-download progress bar consumes "
                        "(TCK-LAUNCH-002); verification/install order unchanged")
    p.add_argument("--hf-token", default=None,
                   help="Hugging Face token for gated official repos "
                        "(falls back to HF_TOKEN env var). Never logged. "
                        "Ungated unsloth mirrors (the default) need no token. "
                        "Prefer setting the HF_TOKEN env var and omitting "
                        "this flag: a flag value appears in the process table "
                        "and shell history. Avoid shell-expanding $HF_TOKEN "
                        "in the flag (\"--hf-token $HF_TOKEN\" would put the "
                        "literal in history/args) — set the env var instead.")
    return p


def resolve_token(args_token: str | None) -> str | None:
    """Return the effective HF token (CLI arg wins over env) or None.

    The value is used only as an Authorization header and is never printed.
    """
    return args_token if args_token is not None else os.environ.get(HF_TOKEN_ENV)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    entries = load_manifest()

    if args.check:
        return cmd_check(entries, args.model, args.out)

    token = resolve_token(args.hf_token)
    return cmd_install(entries, args.model, args.out, args.write_hash, token,
                       json_progress=args.json_progress)


if __name__ == "__main__":
    raise SystemExit(main())
