# ADR-0016: Localhost node I/O — scoped network exception for `node/`

- **Status:** Accepted
- **Date:** 2026-09-03
- **Decides:** Whether the node doctor (PROJECT.md §7.7) may perform network
  I/O on localhost despite the "one network module" invariant (PROJECT.md §5.6,
  AGENTS.md). Scope: `src/localwallet/node/**` (TCK-P4-001); the scoped
  exception in `tools/lint_network.py` (`NODE_NETWORK_DIRS`); relates to
  ADR-0003 (Phase 4 backend swap) and OQ11 (node recommendation policy).
  **Note:** the node-recommendations ADR planned for TCK-P4-003 was renumbered
  to ADR-0017 to keep this one chronologically ordered.
- **Consensus:** localhost probing is the Phase 4 privacy upgrade, not a leak.

## Context

The core invariant is that `chain/` is the only module with network access,
so a reviewer can "grep exactly what crosses the wire" (PROJECT.md §5.6).
Phase 4's entire purpose is to *close* the public-explorer privacy gap by
moving address queries onto the user's own node (§9, §12). To do that, the app
must first *detect* a local Bitcoin Core / mempool / electrs instance — which
requires probing loopback ports and reading the RPC cookie. That is network
I/O performed outside `chain/`.

Naïvely enforcing the invariant would forbid the node doctor from probing
anything, which would make Phase 4 undeliverable. But exempting `node/`
wholesale creates a hole in the "one networked module" guarantee. The question
is how to grant a *scoped* exception without turning the privacy/security
surface back into a free-for-all.

## Decision

1. **`node/` gets a directory-scoped network exception.** Extend
   `tools/lint_network.py` with `NODE_NETWORK_DIRS = ("node",)` (directories
   relative to the lint root), using the same mechanism as the ADR-0007
   `AGENT_LLM_TRANSPORT_FILES` file list. Tests pin the exact list
   (``tests/test_lint_network.py``) so the exception cannot silently grow.

2. **Localhost-only is a hard, enforced contract.** The exception is for
   probing the user's *own* daemon on `127.0.0.1`/`localhost`. Detection code
   builds every URL from a loopback constant; the lint cannot (cheaply) prove
   "loopback", so the guarantee is additionally pinned by tests asserting every
   probed host is loopback and by a code-review rule (ADR text + module
   docstring). Any future need to contact a remote host stays in `chain/` —
   this exception does not move public-network calls. Enforcement is
   operational, not just asserted: before probing a *configured* URL (e.g.
   `LOCALWALLET_LOCAL_MEMPOOL_URL`), the detector parses its host and only
   probes when it is `127.0.0.1`/`localhost`/`::1`. A configured URL pointing at
   a public or LAN host — or a malformed URL — is never contacted and resolves
   to the clean `offline` state. node/ probing is loopback-only by contract;
   remote/self-hosted LAN reach is TCK-P4-002's backend-switch concern and must
   not be smuggled through node/.

3. **`node/` remains advise-only.** Network access does not imply privilege.
   The package only *detects and informs*; it never executes commands. An AST
   test (`tests/test_node_doctor.py`, same approach as the network lint)
   asserts there is no `subprocess` import or `os.system` call anywhere under
   `node/`.

4. **Fail-closed state machine.** Every probe resolves to a clean
   `NodeStatus` (`reachable` / `offline` / `auth_failed` / `malformed`);
   an unreachable instance lands in `offline` and never crashes the app. RPC
   authentication uses the cookie file (canonical Core auth); the cookie
   *content* is a secret and is never logged or echoed (PROJECT.md §7.8).

## Rationale

- **Privacy upgrade, not a leak.** The invariant exists so a reviewer can see
  what leaves the machine. A request to `127.0.0.1:48332` never leaves the
  machine; it is the mechanism that *stops* address queries from leaving
  (ADR-0003's Phase 4 swap). Treating it as equivalent to a public-API call
  would forbid the very feature Phase 4 exists to ship.
- **Directory scope matches the package boundary.** `node/` is a cohesive,
  small, newly-created subsystem (detect + doctor). Scoping to the directory
  keeps the exception legible and lets the whole subsystem use `httpx`
  consistently, while every *other* package stays banned.
- **Advise-only is orthogonal to I/O.** Granting network access does not grant
  the ability to run privileged commands; the two are enforced separately (the
  AST test keeps `node/` from ever shelling out).

## Alternatives considered

- **Route detection through `chain/`** (keep the exception in the one networked
  module). Rejected: `chain/` is the *chain data* adapter; node-doctor logic
  (cookie resolution, health parsing, guidance) is a distinct concern owned by
  `node/` per the layout (§15), and forcing it into `chain/` would blur the
  module's single responsibility.
- **A file-scoped exception (like `remote_runtime.py`)** listing each networked
  file. Rejected for now as over-engineering: `node/` is one cohesive new
  package with at most a couple of networked files, and the directory scope is
  the natural boundary. If `node/` grows unrelated networked modules later, a
  file-level list is the fallback (amend via a new ADR).
- **No exception (enforce the invariant literally).** Rejected: Phase 4 would
  be undeliverable and the privacy promise (§9) could never be closed.

## Consequences

- `tools/lint_network.py` now exempts `node/**` in addition to `chain/` and the
  ADR-0007 bridge; its error message names both exceptions.
- `src/localwallet/node/**` may import `httpx` (loopback-only); every other
  package remains banned.
- `tests/test_lint_network.py` pins `NODE_NETWORK_DIRS == ("node",)` and that a
  *sibling file* named `node.py` is still flagged (the exception is a
  directory, not a name prefix).
- `tests/test_node_doctor.py` pins the advise-only (no subprocess/os.system)
  invariant across `node/`.
- The node-recommendations ADR (OQ11) moves to ADR-0017 (TCK-P4-003).
