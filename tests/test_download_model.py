"""TCK-LAUNCH-002: the --json-progress seam of models/download_model.py.

The seam is additive: a progress callback threaded through ``download()``
that observes byte counts ONLY. These tests pin the seam's shape AND that
the verification contract is untouched (no progress flag changes the
hash-check or the refuse-to-install path). No network: ``_open_request``
and the digest are driven with fakes / tmp files.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "models" / "download_model.py"


def _load():
    spec = importlib.util.spec_from_file_location("download_model", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DM = _load()


class _Resp:
    """Minimal urllib-response stand-in: getcode/headers/read/close."""

    def __init__(self, code: int, headers: dict[str, str], body: bytes) -> None:
        self._code = code
        self.headers = headers
        self._chunks = [body[i : i + 64] for i in range(0, len(body), 64)] + [b""]

    def getcode(self) -> int:
        return self._code

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        pass


def test_expected_total_from_status_headers() -> None:
    assert DM._expected_total(_Resp(200, {"Content-Length": "2048"}, b""), 0) == 2048
    assert DM._expected_total(_Resp(200, {"Content-Length": "1024"}, b""), 1024) == 2048
    assert (
        DM._expected_total(_Resp(206, {"Content-Range": "bytes 1024-2047/3072"}, b""), 0)
        == 3072
    )
    assert DM._expected_total(_Resp(200, {}, b""), 0) is None
    assert DM._expected_total(_Resp(200, {"Content-Length": "junk"}, b""), 0) is None


def test_download_reports_int_progress_without_paths(tmp_path, monkeypatch) -> None:
    body = b"x" * 200
    monkeypatch.setattr(
        DM, "_open_request", lambda url, start, token=None: _Resp(200, {"Content-Length": str(len(body))}, body)
    )
    ticks: list[tuple[int, Any]] = []
    dest = tmp_path / "m.gguf"
    DM.download("https://example/m.gguf", tmp_path / "m.gguf.part", dest,
                progress=lambda d, t: ticks.append((d, t)))
    assert dest.is_file() and dest.read_bytes() == body
    assert [d for d, _ in ticks][-1] == 200
    assert all(t == 200 for _, t in ticks)  # total known from the header
    # the payload the app will JSON-ify carries no path material by shape:
    assert all(isinstance(d, int) and (t is None or isinstance(t, int)) for d, t in ticks)


def test_progress_seam_is_opt_in_default_off(tmp_path, monkeypatch) -> None:
    """Without a callback the download behaves exactly as before (no
    print, no behavior change) — the app passes --json-progress; a plain
    CLI run never sees JSON lines unless asked."""
    body = b"y" * 130
    monkeypatch.setattr(
        DM, "_open_request", lambda url, start, token=None: _Resp(200, {"Content-Length": str(len(body))}, body)
    )
    DM.download("https://example/m.gguf", tmp_path / "m.gguf.part", tmp_path / "m.gguf")
    assert (tmp_path / "m.gguf").read_bytes() == body


def test_restart_after_416_reports_full_not_inflated_progress(tmp_path, monkeypatch) -> None:
    """A 416 / ignored-Range restart must NOT carry the stale .part size
    forward: progress restarts from 0, never inflated past the real bytes."""
    body = b"z" * 100
    part = tmp_path / "m.gguf.part"
    part.write_bytes(b"0" * 50)  # stale partial file

    def fake_open(url, start, token=None):
        if start is not None:  # first (resume) attempt is rejected
            raise urllib.error.HTTPError(url, 416, "Range Not Satisfiable", None, None)
        return _Resp(200, {"Content-Length": str(len(body))}, body)

    monkeypatch.setattr(DM, "_open_request", fake_open)
    ticks: list[tuple[int, Any]] = []
    DM.download("https://example/m.gguf", part, tmp_path / "m.gguf",
                progress=lambda d, t: ticks.append((d, t)))
    assert (tmp_path / "m.gguf").read_bytes() == body
    # the stale 50 bytes never inflate the count: last tick == real size
    assert [d for d, _ in ticks][-1] == len(body)
    assert all(t == len(body) for _, t in ticks)
    assert all(d <= len(body) for d, _ in ticks)



def test_json_progress_flag_exists_on_the_parser() -> None:
    args = DM.build_parser().parse_args(["--model", "m", "--json-progress"])
    assert args.json_progress is True
    assert DM.build_parser().parse_args(["--model", "m"]).json_progress is False


def test_install_still_refuses_on_hash_mismatch(tmp_path, monkeypatch, capsys) -> None:
    """VERIFICATION UNTOUCHED: the --json-progress path installs nothing on
    a bad digest (the same cmd_install the engine spawns)."""
    body = b"GGUF-not-the-pinned-bytes"
    monkeypatch.setattr(
        DM, "_open_request", lambda url, start, token=None: _Resp(200, {"Content-Length": str(len(body))}, body)
    )
    out = tmp_path / "bin"
    rc = DM.cmd_install(
        [{"name": "m", "url": "https://example/m.gguf", "sha256": "0" * 64,
          "size_bytes": str(len(body))}],
        "m", out, write_hash=False, json_progress=True,
    )
    assert rc == 1
    assert not (out / "m.gguf").exists()  # refuse-to-install stands
    captured = capsys.readouterr().out
    # progress lines are parseable int-only JSON documents:
    ticks = [json.loads(line) for line in captured.splitlines() if line.startswith("{")]
    assert ticks and all(set(t) == {"downloaded", "total"} for t in ticks)
    assert ticks[-1]["downloaded"] == len(body)
