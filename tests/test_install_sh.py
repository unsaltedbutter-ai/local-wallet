"""Lint and light functional checks for the curl|bash installer (install.sh).

The full install is never run here (it would clone the repo and touch the
network); we verify it is syntactically valid, sourceable, and that the
OS/arch detection behaves on this host.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"


def _run(*args, **kw):
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, check=False, **kw
    )


def test_install_sh_syntax():
    res = _run("bash", "-n", INSTALL_SH)
    assert res.returncode == 0, res.stderr


def test_install_sh_detect_os_arch():
    res = _run("bash", "-c", 'source "$1"; detect_os_arch', "sh", INSTALL_SH)
    assert res.returncode == 0, res.stderr
    pair = res.stdout.strip()
    assert pair in {
        "macos/arm64",
        "macos/x86_64",
        "linux/arm64",
        "linux/x86_64",
    }, f"detect_os_arch returned unsupported pair: {pair!r}"


def _resolve(dirpath, env=None):
    return _run(
        "bash",
        "-c",
        'source "$1"; cd "$2"; resolve_repo; printf "%s|%s" "$REPO" "$MODE"',
        "sh",
        INSTALL_SH,
        str(dirpath),
        env=env,
    )


def test_install_sh_resolve_inplace(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "src" / "localwallet" / "__init__.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "localwallet" / "__init__.py").touch()
    res = _resolve(tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"{tmp_path}|inplace"


def test_install_sh_resolve_neutral_dir_clone(tmp_path):
    target = tmp_path / "managed"
    res = _resolve(tmp_path, env={**os.environ, "INSTALL_ROOT": str(target)})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"{target}|clone"


def test_install_sh_resolve_foreign_pyproject_refuses(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    res = _resolve(tmp_path)
    assert res.returncode == 2, (res.returncode, res.stdout, res.stderr)
    assert "not local-wallet" in res.stderr


# --------------------------------------------------------------------------- #
# model_state / default_model (TCK-DIST-004)
# --------------------------------------------------------------------------- #

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _setup_model_repo(tmp_path, manifest_entries, files):
    """Lay out a fake repo ($REPO) with models/ + a venv-python shim so
    model_state can run download_model.py --check against temp artifacts."""
    (tmp_path / "models" / "bin").mkdir(parents=True)
    shutil.copy(REPO / "models" / "download_model.py",
                tmp_path / "models" / "download_model.py")
    (tmp_path / "models" / "manifest.json").write_text(
        json.dumps(manifest_entries), encoding="utf-8")
    venvpy = tmp_path / ".venv" / "bin" / "python"
    venvpy.parent.mkdir(parents=True)
    venvpy.write_text("#!/usr/bin/env bash\nexec python3 \"$@\"\n", encoding="utf-8")
    venvpy.chmod(0o755)
    for rel, data in files.items():
        path = tmp_path / "models" / "bin" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _model_state(tmp_path, name):
    return _run(
        "bash",
        "-c",
        'source "$1"; REPO="$2"; printf "%s" "$(model_state "$3")"',
        "sh", INSTALL_SH, str(tmp_path), name,
    )


def test_model_state_absent(tmp_path):
    _setup_model_repo(tmp_path, [], {})
    res = _model_state(tmp_path, "gemma-4-E2B-it-Q4_K_M")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "absent"


def test_model_state_partial(tmp_path):
    _setup_model_repo(tmp_path, [], {"gemma-4-E2B-it-Q4_K_M.gguf.part": b"x"})
    res = _model_state(tmp_path, "gemma-4-E2B-it-Q4_K_M")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "partial"


def test_model_state_present(tmp_path):
    data = b"weights"
    entries = [{
        "name": "gemma-4-E2B-it-Q4_K_M",
        "url": "https://example.invalid/x.gguf",
        "sha256": _sha256(data),
        "size_bytes": str(len(data)),
    }]
    _setup_model_repo(tmp_path, entries, {"gemma-4-E2B-it-Q4_K_M.gguf": data})
    res = _model_state(tmp_path, "gemma-4-E2B-it-Q4_K_M")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "present"


def test_model_state_corrupt(tmp_path):
    data = b"weights"
    entries = [{
        "name": "gemma-4-E2B-it-Q4_K_M",
        "url": "https://example.invalid/x.gguf",
        "sha256": _sha256(b"other"),
        "size_bytes": str(len(data)),
    }]
    _setup_model_repo(tmp_path, entries, {"gemma-4-E2B-it-Q4_K_M.gguf": data})
    res = _model_state(tmp_path, "gemma-4-E2B-it-Q4_K_M")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "corrupt"


def _default_model(tmp_path):
    venvpy = tmp_path / ".venv" / "bin" / "python"
    venvpy.parent.mkdir(parents=True)
    venvpy.write_text("#!/usr/bin/env bash\nexec python3 \"$@\"\n", encoding="utf-8")
    venvpy.chmod(0o755)
    return _run(
        "bash",
        "-c",
        'source "$1"; REPO="$2"; printf "%s" "$(default_model)"',
        "sh", INSTALL_SH, str(tmp_path),
    )


def test_default_model_uses_default_entry(tmp_path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "manifest.json").write_text(json.dumps([
        {"name": "other", "default": False},
        {"name": "picked", "default": True},
    ]), encoding="utf-8")
    res = _default_model(tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "picked"


def test_default_model_falls_back_to_first(tmp_path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "manifest.json").write_text(json.dumps([
        {"name": "first"},
        {"name": "second"},
    ]), encoding="utf-8")
    res = _default_model(tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "first"
