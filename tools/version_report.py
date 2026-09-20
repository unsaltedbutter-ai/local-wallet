#!/usr/bin/env python3
"""Paste-able version report (TCK-VER-001).

The third access path to the SAME deterministic block as the chat ``/version``
command and the per-launch log lines. Run it and paste the output so the build
can be identified exactly:

    .venv/bin/python tools/version_report.py

It prints the git commit embedded at install, the system-prompt fingerprint
(sha256 + char count — changes with every prompt edit), the resolved model
basename + manifest-pin checksum, the llama.cpp version, the GPU/backend build
flags (does the installed wheel ship Metal/CUDA?), and the store schema + intent
count. Value-free by construction (basenames only, no paths/keys/addresses).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import _ladder_model_truth, version_report_lines


def main() -> None:
    """Print the block, forcing the llama.cpp import so the GPU/backend
    question is answered DEFINITIVELY for this machine (not deferred)."""
    print("\n".join(version_report_lines(model=_ladder_model_truth(), load_llama=True)))


if __name__ == "__main__":
    main()
