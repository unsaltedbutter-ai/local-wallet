"""Node subsystem: node doctor — detect, guide, health (Phase 4, TCK-P4-001).

This package detects a user's OWN local Bitcoin Core / mempool / electrs
instances on localhost, reports health/IBD sync state, and provides structured
"advise-only" setup guidance the agent can later narrate (PROJECT.md §7.7).

DESIGN DECISION — scoped network exception for ``node/`` (ADR-0016):

The project invariant (AGENTS.md, PROJECT.md §5.6) states ``chain/`` is the
only module with network access. Phase 4 exists to move chain I/O onto the
user's own machine, which requires probing a localhost daemon. Localhost node
probing is therefore a *privacy upgrade, not a leak*: every request in this
package targets ``127.0.0.1``/``localhost`` and nothing leaves the machine.

We extend ``tools/lint_network.py`` with a directory-scoped exception for
``src/localwallet/node/**`` (mechanism: ``NODE_NETWORK_DIRS``, sibling to the
ADR-0007 ``AGENT_LLM_TRANSPORT_FILES`` file list; pinned by tests in
``tests/test_lint_network.py``). Rationale and alternatives are recorded in
``docs/adr/0016-localhost-node-io.md``.

Loopback-only is enforced, not assumed: the detector parses the host of any
*configured* URL (e.g. ``LOCALWALLET_LOCAL_MEMPOOL_URL``) and only probes it
when the host is ``127.0.0.1``/``localhost``/``::1``. A configured URL pointing
at a public or LAN host — or a malformed URL — is never contacted and resolves
to the clean ``OFFLINE`` state. node/ probing is loopback-only by contract;
remote/self-hosted LAN reach is TCK-P4-002's backend-switch concern and must
not be smuggled through node/.

Advise-only invariant: this package only detects and informs. It never
executes commands or runs privileged operations. An AST test
(``tests/test_node_doctor.py``) pins the absence of ``subprocess``/``os.system``
across ``node/``.
"""

from localwallet.node.detect import (
    BitcoinCoreDetector,
    CoreHealth,
    CoreRpcProbe,
    LocalNodeReport,
    NodeKind,
    NodeStatus,
    detect_local_nodes,
)
from localwallet.node.doctor import (
    NodeDoctor,
    NodeStateAdvice,
    NodeStateKind,
    SetupOption,
    SetupOptionId,
    setup_option_by_id,
)

__all__ = [
    "BitcoinCoreDetector",
    "CoreHealth",
    "CoreRpcProbe",
    "LocalNodeReport",
    "NodeDoctor",
    "NodeKind",
    "NodeStateAdvice",
    "NodeStateKind",
    "NodeStatus",
    "SetupOption",
    "SetupOptionId",
    "detect_local_nodes",
    "setup_option_by_id",
]
