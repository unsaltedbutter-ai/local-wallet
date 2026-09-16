"""Hostname→IP TRUST RESOLUTION seam (TCK-WEB-030, USER DIRECTION 2026-09-15).

The one place the app may learn whether a configured chain backend HOSTNAME
(normal DNS or ``.local``/mDNS — both ride the OS resolver via stdlib
:func:`socket.getaddrinfo`) sits on the user's LAN. The private-range set
is the SAME enum TCK-WEB-023 used for literal-IP classification — it lives
HERE (next to the only resolver) so the literal answer and the resolved
answer can never drift; ``app`` answers through these two functions.

The result is a CLOSED tri-state boolean, never an address:

* ``True``  — every resolved answer is in the private green set (own node,
  private network ⇒ GREEN badge surface);
* ``False`` — any answer is public/unclassifiable ⇒ the yellow trust-hedge
  branch (a mixed A record fails closed: one public route is enough for the
  queries to leave the LAN);
* ``None``  — resolution FAILED (NXDOMAIN, timeout, any resolver error) —
  the caller falls back to its previous no-DNS classification.

Value-free by construction: never raises, never logs, never returns an IP
or anything else about the answer. Network I/O lives here only because
``chain/`` is the lint-sanctioned networked module (tools/lint_network.py).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final

__all__ = [
    "PRIVATE_GREEN_NETS_V4",
    "ip_in_private_green_range",
    "is_ip_literal",
    "resolves_to_private",
]

#: The IPv4 ranges that classify a backend address as PRIVATE/green
#: (TCK-WEB-023 user direction, adopted verbatim by TCK-WEB-030 for RESOLVED
#: addresses too: 10/8, 172.16/12, 192.168/16, plus the whole 127/8 loopback
#: block — 127.0.0.2 IS this machine as much as 127.0.0.1). DELIBERATELY NOT
#: ``ipaddress``'s ``is_private``: it also counts 169.254/16 link-local (and
#: the IPv6 link-local/ULA blocks) private — the council ruled link-local
#: NEVER green (auto-assigned, any device on the segment may hold it), and
#: CGNAT 100.64/10 is excluded the same way (a carrier-shared address is not
#: a network you run). IPv6 gets no private branch beyond IPv4-mapped
#: literals.
PRIVATE_GREEN_NETS_V4: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)


def ip_in_private_green_range(text: str) -> bool:
    """Whether ``text`` is a LITERAL IP inside :data:`PRIVATE_GREEN_NETS_V4`
    — PURE TEXT PARSING, no DNS, no resolver. The ``::ffff:10.x`` IPv4-mapped
    spelling counts; CGNAT/link-local/IPv6/leading-zero junk do not;
    anything :mod:`ipaddress` refuses to parse (i.e. every hostname) →
    ``False``. Never raises; value-free."""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.version == 4 and any(addr in net for net in PRIVATE_GREEN_NETS_V4)


def is_ip_literal(text: str) -> bool:
    """Whether ``text`` IS an address (v4/v6, any range) — the caller's
    "a name, not a number" gate: literals are classified from the text and
    must never be handed to a resolver."""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def resolves_to_private(host: str) -> bool | None:
    """Tri-state LAN answer for ``host`` (see the module docstring): resolve
    via the OS resolver (``getaddrinfo`` answers normal DNS AND ``.local``
    mDNS, plus /etc/hosts) and check EVERY answer against
    :data:`PRIVATE_GREEN_NETS_V4`.

    * A LITERAL IP input is answered from the text without touching the
      resolver (WEB-023's fail-safe classification, unchanged).
    * Mixed private/public answers → ``False`` (fail closed).
    * Resolver errors of ANY kind (gaierror/timeout/OSError on the
      host bytes) or an empty answer set → ``None`` — the caller keeps its
      previous classification. No timeout knob exists on
      :func:`socket.getaddrinfo`; the caller bounds the stall with its own
      cache (ponytail: OS resolver timeout is the ceiling; the app's
      per-host TTL means one attempt per host per window).
    """
    if is_ip_literal(host):
        return ip_in_private_green_range(host)  # literal: never the resolver
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return None
    answers = {info[4][0] for info in infos}
    if not answers:
        return None
    return all(ip_in_private_green_range(answer) for answer in answers)
