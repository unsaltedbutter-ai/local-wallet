"""Node doctor guidance content (Phase 4, TCK-P4-001).

PURE data — the "advise-only" invariant lives here and in
:mod:`localwallet.node.detect`:

- This module contains **no executable logic** beyond describing and
  selecting guidance. It NEVER runs a command, never shells out, and never
  performs privileged operations (PROJECT.md §7.7: the agent **advises only**).
  The AST test in ``tests/test_node_doctor.py`` pins this invariant by
  asserting there is no ``subprocess``/``os.system`` anywhere under ``node/``.
- The content is **structured data** (dataclasses/enums of plain text) that a
  later phase (TCK-P4-003) wires to the ``node_status`` intent so the agent can
  *narrate* it. No addresses, amounts, or secret-bearing paths appear here —
  guidance is value-free and copy-ready for narration.

Recommendation tiers (PROJECT.md §7.7 / OQ11, expanded by ADR-0016):

1. **Umbrel / Start9 — "easy mode".** A full-node distro the user installs on
   dedicated hardware (or a node OS on a spare machine); ships Bitcoin Core
   plus one-click apps (mempool explorer, electrs). Best for non-technical
   users; no manual config.
2. **Bitcoin Core + prune — "minimal".** Run Bitcoin Core directly with
   pruning enabled to bound disk use (``prune`` + ``txindex`` tradeoff
   documented); exposes the RPC cookie the app detects. Least software, most
   manual.
3. **Self-hosted mempool / electrs — "explorer + Esplora API".** A
   self-hosted mempool.space instance (or electrs) layered on a Core node
   provides both a human-readable explorer and the Esplora HTTP API the app's
   chain adapter can consume on localhost (ADR-0003 Phase 4 swap target).

The tiers are ordered from "easiest for a newcomer" to "most manual but most
control". Each tier is a self-contained :class:`SetupOption` so the UI/agent
can render any subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "SETUP_OPTIONS",
    "STATE_ADVICE",
    "NodeDoctor",
    "NodeStateAdvice",
    "NodeStateKind",
    "SetupOption",
    "SetupOptionId",
]


class SetupOptionId(str, Enum):
    """Identifiers for the guided-setup tiers (stable, for narration keys)."""

    UMBREL_START9 = "umbrel_start9"
    CORE_PRUNE = "core_prune"
    MEMPOOL_ESPLORA = "mempool_esplora"


@dataclass(frozen=True)
class SetupOption:
    """One guided-setup tier the agent can narrate to the user.

    All fields are plain, value-free display text — the agent quotes them, it
    never invents or modifies them (PROJECT.md §5.1). ``steps`` are ordered
    human-language instructions; they describe what the USER (or their node
    software) does, never anything this app executes.
    """

    id: SetupOptionId
    title: str
    summary: str
    steps: tuple[str, ...]
    required_skill: str  # e.g. "none", "moderate", "advanced"
    disk_footprint: str  # human description, no exact figures promised


#: The guided-setup tiers, ordered easiest → most manual.
SETUP_OPTIONS: tuple[SetupOption, ...] = (
    SetupOption(
        id=SetupOptionId.UMBREL_START9,
        title="Run a node distro (Umbrel or Start9) — easy mode",
        summary=(
            "Install a full-node operating system on dedicated hardware (or a "
            "spare machine). It runs Bitcoin Core for you and offers one-click "
            "apps for a block explorer and an indexer."
        ),
        steps=(
            "Choose a supported device and flash the distro onto an SSD.",
            (
                "Boot it on your network and follow the on-screen setup to fund and "
                "start Bitcoin Core."
            ),
            (
                "Install the 'mempool' (explorer + API) and/or an indexer app from "
                "its app store."
            ),
            (
                "Note the device's local address — this app will detect it when it "
                "is on the same network."
            ),
        ),
        required_skill="none",
        disk_footprint="dedicated hardware (e.g. an SSD); easiest path, least control",
    ),
    SetupOption(
        id=SetupOptionId.CORE_PRUNE,
        title="Bitcoin Core with pruning — minimal",
        summary=(
            "Run Bitcoin Core directly on this machine, enabling pruning to "
            "keep disk use bounded. This is the smallest-footprint option and "
            "exposes the RPC cookie this app detects automatically."
        ),
        steps=(
            (
                "Install Bitcoin Core and start it on mainnet (the default "
                "network — no extra network flags needed)."
            ),
            "Enable pruning so the chain does not consume your whole disk.",
            (
                "Let initial block download finish — the app will show sync progress "
                "once it detects the node."
            ),
            (
                "This app reads the RPC cookie file for authentication; no password "
                "to enter."
            ),
        ),
        required_skill="moderate",
        disk_footprint="mainnet ~a few GB when pruned; most manual setup",
    ),
    SetupOption(
        id=SetupOptionId.MEMPOOL_ESPLORA,
        title="Self-hosted mempool / electrs — explorer + Esplora API",
        summary=(
            "Layer a self-hosted mempool.space instance (or electrs) on top of "
            "a Bitcoin Core node. You get a human-readable block explorer and "
            "the local Esplora HTTP API this app's chain adapter can use — "
            "closing the public-explorer privacy gap (PROJECT.md §9)."
        ),
        steps=(
            "Start with a synced Bitcoin Core node (see the minimal option).",
            (
                "Run a self-hosted mempool.space instance (or electrs) pointed at "
                "that node's RPC."
            ),
            (
                "Confirm the local API is reachable on this machine (the app probes "
                "the loopback port automatically)."
            ),
            (
                "Point this app at the local instance once backend wiring ships "
                "(TCK-P4-002) — no address ever leaves your machine afterwards."
            ),
        ),
        required_skill="advanced",
        disk_footprint="moderate (index data) on top of a Core node",
    ),
)


class NodeStateKind(str, Enum):
    """States the doctor can advise on, keyed off detection results."""

    NONE_FOUND = "none_found"
    CORE_SYNCING = "core_syncing"
    CORE_READY = "core_ready"
    CORE_AUTH_ISSUE = "core_auth_issue"
    INDEXER_ONLY = "indexer_only"


@dataclass(frozen=True)
class NodeStateAdvice:
    """Human-language guidance for a detected node state (value-free)."""

    state: NodeStateKind
    headline: str
    detail: str
    next_step: str


#: Per-state guidance the agent narrates. Keyed by :class:`NodeStateKind`.
STATE_ADVICE: dict[NodeStateKind, NodeStateAdvice] = {
    NodeStateKind.NONE_FOUND: NodeStateAdvice(
        state=NodeStateKind.NONE_FOUND,
        headline="No local node detected",
        detail=(
            "This machine is not running a node this app could detect. Your "
            "data is currently served by a public explorer."
        ),
        next_step=(
            "Pick a setup option below to move your data on to your own machine "
            "for privacy."
        ),
    ),
    NodeStateKind.CORE_SYNCING: NodeStateAdvice(
        state=NodeStateKind.CORE_SYNCING,
        headline="A Bitcoin Core node is syncing",
        detail=(
            "Bitcoin Core is reachable and currently downloading/verifying the "
            "chain (initial block download). The app can report progress."
        ),
        next_step=(
            "Wait for sync to finish; a self-hosted explorer/API can be layered "
            "on afterwards."
        ),
    ),
    NodeStateKind.CORE_READY: NodeStateAdvice(
        state=NodeStateKind.CORE_READY,
        headline="A Bitcoin Core node is ready",
        detail=(
            "Bitcoin Core is reachable and synced. It can serve as this app's "
            "local backend."
        ),
        next_step=(
            "Add a self-hosted mempool/electrs instance for a block explorer "
            "and the Esplora API the app uses."
        ),
    ),
    NodeStateKind.CORE_AUTH_ISSUE: NodeStateAdvice(
        state=NodeStateKind.CORE_AUTH_ISSUE,
        headline="Bitcoin Core reachable, but authentication failed",
        detail=(
            "A Bitcoin Core RPC endpoint answered, but the RPC cookie did not "
            "authenticate (missing, stale, or wrong data dir)."
        ),
        next_step=(
            "Check that the RPC cookie exists and that this app's cookie path "
            "points at the right data directory."
        ),
    ),
    NodeStateKind.INDEXER_ONLY: NodeStateAdvice(
        state=NodeStateKind.INDEXER_ONLY,
        headline="An indexer/explorer is reachable",
        detail=(
            "A self-hosted mempool or electrs instance is serving on localhost, "
            "but no Bitcoin Core RPC was detected on this machine."
        ),
        next_step=(
            "The indexer needs a Bitcoin Core node behind it; ensure one is "
            "running and synced."
        ),
    ),
}


class NodeDoctor:
    """Advise-only selector mapping a detection outcome to guidance content.

    This class performs NO detection and NO action — it only selects from the
    structured content above, so the agent (later, TCK-P4-003) can narrate it.
    It exists to keep the pure data and the mapping testable independently of
    any network code.
    """

    def recommend(self, *, any_core_reachable: bool, core_synced: bool,
                  core_auth_issue: bool, indexer_reachable: bool) -> NodeStateAdvice:
        """Return the guidance for a detection outcome.

        Args are booleans summarizing a :class:`LocalNodeReport` (see
        :func:`localwallet.node.detect.detect_local_nodes`). Pure selection;
        never raises.
        """
        if not any_core_reachable and not indexer_reachable:
            return STATE_ADVICE[NodeStateKind.NONE_FOUND]
        if any_core_reachable and core_auth_issue:
            return STATE_ADVICE[NodeStateKind.CORE_AUTH_ISSUE]
        if indexer_reachable and not any_core_reachable:
            return STATE_ADVICE[NodeStateKind.INDEXER_ONLY]
        if any_core_reachable and not core_synced:
            return STATE_ADVICE[NodeStateKind.CORE_SYNCING]
        return STATE_ADVICE[NodeStateKind.CORE_READY]


def setup_option_by_id(option_id: SetupOptionId) -> SetupOption | None:
    """Return the setup option with ``option_id``, or ``None`` if unknown."""
    for option in SETUP_OPTIONS:
        if option.id is option_id:
            return option
    return None
