# ADR-0017: Node recommendation policy (advise-only) + node_status interaction

- **Status:** Accepted
- **Date:** 2026-09-03
- **Answers:** PROJECT.md §14 OQ11 — "Node recommendation policy: Bitcoin Core
  vs Knots; Umbrel/Start9 framing; how opinionated the doctor should be."
  Scope: `src/localwallet/node/` (TCK-P4-001 content), the `node_status`
  intent + handler (TCK-P4-003), and the chain-backend switch interaction
  (ADR-0018, TCK-P4-002). Relates to ADR-0016 (localhost node/ loopback
  contract) and ADR-0018 (config-only backend switch).
- **Consensus:** the app *advises only* — it never runs privileged commands
  or auto-configures a node.

## Context

Phase 4 (§12) closes the public-explorer privacy gap by moving chain I/O onto
the user's own node. To get there, the app must (a) *detect* a local
Core/mempool/electrs instance (ADR-0016) and (b) *guide* a user toward running
one. The guidance content — how opinionated it is, which setups to recommend,
and how it relates to the backend switch — is the open question this ADR
settles.

The agent (the LLM) is untrusted input and never touches system state
(PROJECT.md §5). Node setup involves privileged operations (installing
software, opening ports, disk partitioning) that the app must never perform
on the user's behalf. So the doctor is strictly **advise-only**: it surfaces
*content* the user acts on.

## Decision

### 1. Three recommendation tiers, ordered easiest → most manual

The doctor's structured guidance (PROJECT.md §7.7) fixes exactly three
setup options (in `node/doctor.py` `SETUP_OPTIONS`):

1. **Umbrel / Start9 — "easy mode".** A full-node distro on dedicated
   hardware (or a spare machine); runs Bitcoin Core and offers one-click
   apps for an explorer and an indexer. Recommended for newcomers.
2. **Bitcoin Core + prune — "minimal".** Run Core directly with pruning to
   bound disk use; exposes the RPC cookie the app detects automatically.
   Least software, most manual.
3. **Self-hosted mempool / electrs — "explorer + Esplora API".** A
   self-hosted mempool.space instance (or electrs) layered on a Core node
   gives a human-readable explorer and the local Esplora HTTP API the chain
   adapter consumes.

Bitcoin Core is the underlying full node in all tiers; Bitcoin Knots is not
recommended separately (no user-visible benefit for this watch-only app, and
Core is the better-trodden path). The tiers are the *whole* of what the
doctor recommends — the app is deliberately not more opinionated than this.

### 2. Guidance is structured data, agent-narrated, advise-only

- All guidance lives as pure structured content in `node/doctor.py`
  (`SetupOption` + `NodeStateAdvice`) — value-free, copy-ready, no
  addresses/amounts/secrets.
- The `node_status` handler (TCK-P4-003) runs detection
  (`detect_local_nodes`) and the doctor selector (`NodeDoctor.recommend`),
  then returns a dispatcher-owned FACTS dict; the narration prints only
  those facts verbatim. The agent never invents detection results or
  guidance, and never includes cookie contents or credential material.
- `node/` remains advise-only by construction: an AST test pins that it
  never shells out (ADR-0016, `tests/test_node_doctor.py`), and the
  handler never executes commands.

### 3. Recommendation defaults

The `NodeDoctor.recommend` selector maps a detection outcome to one state:

| Detection outcome | Advice |
|---|---|
| nothing reachable | `NONE_FOUND` — "no local node detected; your data is currently served by a public explorer"; next step points to the setup options |
| Core reachable, syncing | `CORE_SYNCING` — report IBD progress, wait for sync |
| Core reachable, auth failed | `CORE_AUTH_ISSUE` — check the RPC cookie / data dir |
| indexer reachable, no Core | `INDEXER_ONLY` — the indexer needs a Core node behind it |
| Core reachable + synced | `CORE_READY` — it can be the local backend; add mempool/electrs for the Esplora API |

There is no auto-selection of a setup tier by the app — the narration offers
all three options for the user to choose from (the "pick a setup option
below" copy), keeping the app from over-committing to one vendor.

### 4. `node_status` and the backend switch are separate knobs

**Detection ("detect") and backend selection ("use") are deliberately
decoupled** (the same separation ADR-0018 records for the config knobs):

- `LOCALWALLET_NODE_DETECTION_ENABLED` gates whether the doctor *probes*;
  a `0` yields a clean "detection disabled" state (no probing, no
  fabricated findings) and the agent says so.
- `LOCALWALLET_CHAIN_BASE_URL` gates which backend the *wallet* actually
  queries (ADR-0018). Setting it is the only way the app stops using the
  public default.
- Detecting a node **never auto-switches** the backend. `node_status` may
  *surface* the doctor's guidance (e.g. "point the app at your local
  instance once the backend is configured"), but the user applies the
  backend selection themselves via config. This keeps detection advisory
  and prevents a surprise flip to a possibly-not-running localhost.

The `node_status` narration and the startup privacy banner both reflect the
backend mode (own node vs public API) derived from the **same** single
selection point (ADR-0018 `ChainConfig.from_settings`), so they can never
disagree with what the client actually queries.

## Alternatives considered

- **Auto-follow the doctor (detected node ⇒ auto-switch).** Rejected: it
  couples detection to selection implicitly, can silently flip the backend
  to a possibly-not-running instance, and over-reaches the advise-only
  contract. ADR-0018 records the same rejection.
- **Recommend more tiers (e.g. Knots, DIY builds).** Rejected: three
  well-understood paths cover the target users (§3) without overwhelming a
  newcomer; the content is structured so a tier can be added later behind
  the same advise-only discipline.
- **Have the model invent setup steps.** Rejected: the model is untrusted
  and must quote guidance verbatim; guidance originates only from
  `doctor.py`'s structured content (R9 / §5.1).

## Consequences

- `node_status` (registry 12) is a read-only intent; its handler performs
  detection (honoring `LOCALWALLET_NODE_DETECTION_ENABLED`) and returns
  doctor FACTS for narration — never a command, never a config change.
- The privacy banner and the node_status narration derive backend mode from
  the single chain-backend selection (ADR-0018).
- Node setup guidance is stable, structured, and tested in `tests/test_node_doctor.py`;
  the `node_status` wiring is covered hermetically in `tests/test_e2e_skeleton.py`
  (mocked transports, indicator flip both directions).
