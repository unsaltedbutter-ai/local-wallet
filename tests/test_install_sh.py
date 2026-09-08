"""Lint and light functional checks for the curl|bash installer (install.sh).

The full install is never run here (it would clone the repo and touch the
network); we verify it is syntactically valid, sourceable, and that the
OS/arch detection behaves on this host.
"""

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
