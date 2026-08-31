"""CLI entry point for local-wallet (thin wrapper).

Per PROJECT.md §15 the ``ui`` layer owns user-facing surfaces; all
application wiring (dispatch table, agent loop, REPL, banner) lives in
:mod:`localwallet.app`. This module only adapts the console entry point
and adds nothing else — keep it thin.
"""

from __future__ import annotations

from collections.abc import Sequence

from localwallet.app import main as _app_main

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point; delegates to :func:`localwallet.app.main`."""
    return _app_main(argv)


if __name__ == "__main__":  # pragma: no cover — manual invocation only
    raise SystemExit(main())
