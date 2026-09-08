"""Localhost web UI (TCK-WEB-002, ADR-0024): opt-in browser front-end.

The server listens on loopback only — this package is the
``WEB_SERVER_DIRS`` exception in ``tools/lint_network.py`` (INBOUND
loopback, ADR-0024 §10); outbound network I/O stays in ``chain/``.
"""

from localwallet.ui.web.server import WebServer, serve_web

__all__ = ["WebServer", "serve_web"]
