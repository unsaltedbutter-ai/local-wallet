"""First-run onboarding conversation (TCK-ONB-003, ADR-0023).

Deterministic, CODE-OWNED terminal conversation — the model never sees any
of it and the user's onboarding lines never reach the model (the same
pinned channel as ``/label``, ADR-0020): greetings, asks, and replies are
static constants plus progress figures quoted VERBATIM from tool output
(node doctor facts). Zero prompt change ⇒ zero eval-fixture change.

Pieces (the CLI transport owns all of them; the web transport never
constructs an :class:`OnboardingFlow` — requirement: no onboarding surface
in the browser, only :data:`WEB_SETUP_HINT`):

- :data:`GREETING` + :func:`ask_watch_key` — step 1 (skipped when the key
  arrives via ``--zpub``/env). Seed-word-shaped input is refused with
  guidance (watch-only invariant, AGENTS.md) and re-asked; private/testnet
  keys are refused by the same gated parser the startup path uses
  (value-free).
- :class:`OnboardingFlow` — the step-2 node ask (copy (a)) offered while
  the startup scan runs, kept OPEN across ordinary chat turns: a URL line
  or ``2`` enters step 5 (URL prompt (b), validation, confirmation (d) /
  failure copy (c) with doctor guidance and an explicit public choice),
  ``1``/skip answers with (e), "what's a node?" reads (f). Anything else
  falls through to the model untouched. Steps 3/4 narration rides
  :meth:`opening_lines` / :meth:`emit_load_complete` (non-blocking variant,
  ADR-0022 / TCK-SCAN-003 landed).
- Validation is the ADR-0023 decision-5 gate: the ``chain/`` probe
  (Esplora shape + mainnet genesis, ADR-0021) plus — loopback URLs only,
  per the ADR-0016 contract — the node doctor's IBD facts; a syncing node
  is refused with its progress quoted from tool output. A failed URL is
  NEVER saved and never silently falls back; on success the store's typed
  writer (:meth:`Store.set_chain_base_url`) is the only writer (ONB-002),
  and the switch takes effect on the NEXT launch (ADR-0018 config-only
  semantics — the live chain client is built once per session).

All copy is value-free: no address, amount, xpub, or echoed URL in any
string, ever (AGENTS.md).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Final

from localwallet.agent.session import redact_transcript
from localwallet.node import LocalNodeReport, NodeStatus
from localwallet.node.doctor import NodeDoctor, NodeStateKind
from localwallet.store import Store, StoreError
from localwallet.wallet.descriptor import WatchKeyError, parse_wallet_key

__all__ = [
    "GREETING",
    "LOAD_COMPLETE",
    "LOAD_NARRATION",
    "NODE_ASK",
    "URL_PROMPT",
    "WEB_SETUP_HINT",
    "OnboardingFlow",
    "ask_watch_key",
]

# ---------------------------------------------------------------------------
# §9-signed copy (ADR-0023). One named constant per draft block; the ADR
# paragraph breaks are preserved, prose is single-line (terminal wraps).
# NOTE: the repo's copy constants live HERE, not in ui/cli.py (the ADR
# §9 sentence): cli.py is a thin entry wrapper that imports app.py, so
# app.py cannot import strings back from it (cycle). PROJECT.md §15 keeps
# the strings in the ui layer regardless.
# ---------------------------------------------------------------------------

#: Step 1 — greeting + key ask (the seed-words clarification is
#: load-bearing per the ADR: it teaches the refusal before the mistake).
GREETING: Final[str] = (
    "Hi, nice to meet you. I'm local wallet, your personal AI bitcoin "
    "wallet. Our conversation stays private — the AI runs right here on "
    "your machine. To get started, can you give me the xpub or zpub of "
    "the wallet you want to work with? These are not your seed words — "
    "and please never share your seed words with anyone, me included. An "
    "xpub or zpub is a long string of letters like xpubabc123... If you "
    "don't know where to get that, let me know and I will guide you."
)

#: Step 2 — the node ask, copy block (a), offered right after the xpub
#: while the scan runs; never blocking, silence keeps the public default.
#: Reused verbatim on re-offers.
NODE_ASK: Final[str] = (
    "One thing worth knowing: for maximum privacy, you should use your "
    "own Bitcoin node — something running Bitcoin Core, or an electrum "
    "server, or a private mempool.space server. Start9 and Umbrel and "
    "MyNode are great options for a standalone way to run these services, "
    "but you can run Bitcoin Core on most computers. One honest limit for "
    "today: the app connects to a mempool.space-style address, which all "
    "of those boxes offer — a plain Bitcoin Core install by itself doesn't "
    "give the app one yet.\n"
    "\n"
    "You can pick either, and skipping is a fine answer:\n"
    "\n"
    "1. Public server (default) — nothing to set up. A public server sees "
    "which addresses you check, and can link them to your IP.\n"
    "2. Your own node — only your own machine sees which addresses you "
    "check.\n"
    "\n"
    "If you're not sure what any of this means, ask me \"what's a node?\" "
    "and I'll explain."
)

#: Step 3 — load narration, the MAIN (non-blocking) variant: TCK-SCAN-003
#: has landed, so the honest promise "you can keep asking me questions" is
#: now true (the interim blocking variant is retired per ADR-0023 §flow).
LOAD_NARRATION: Final[str] = (
    "I'm loading your wallet right now. While I look through its history "
    "you can keep asking me questions — I'll give you the most up-to-date "
    "information I have."
)

#: Step 4 — load completes (the "should" is signed-off verbatim; never
#: over-claim verification, §9).
LOAD_COMPLETE: Final[str] = (
    "I've finished loading your wallet, so I should have up to date "
    "information for all of your questions.\nHow can I help you?"
)

#: Copy block (b) — URL entry after choosing 2.
URL_PROMPT: Final[str] = (
    "Type the web address of your node's mempool.space app — its API "
    "address is usually the same, with /api at the end. Nothing is saved, "
    "and no address of your wallet goes to it, until the app has checked "
    "that it answers correctly."
)

#: Copy block (c) — validation failure: plain cause, next step, nothing
#: saved, never a silent public fallback.
VALIDATION_FAIL: Final[str] = (
    "That address didn't check out: it wasn't reachable, or it didn't "
    "answer as a mainnet mempool.space API. Nothing was saved, and no "
    "address of your wallet was ever sent to it. Ask \"node status\" to "
    "see what the app can detect on this machine — then say \"retry\" "
    "with the same or a new address, or pick the public server instead."
)

#: Copy block (d) — confirmation after a successful own-node setup
#: (carries the L5 ownership honesty).
CONFIRMED: Final[str] = (
    "Set. From now on the app asks your own server about your addresses "
    "instead of a public one — the startup notice will say so too. One "
    "caution: if that server isn't actually yours, whoever runs it can "
    "still see which addresses you check."
)

#: Copy block (e) — skip / "not now" / explicit public pick: never a dead
#: end; the public default is retained (an explicit non-opt-in, decision 2).
SKIP_ACK: Final[str] = (
    "No problem — the app keeps using the public server for now, and "
    "that's not a verdict. Paste a mempool.space-style address, or ask to "
    "\"switch to my node\", any time and we'll set it up. Ask \"what's a "
    "node?\" whenever you want the long version."
)

#: Copy block (f) — the guide-path answer to "what's a node?".
GUIDE: Final[str] = (
    "A node is a computer that keeps its own copy of Bitcoin's history "
    "instead of borrowing someone else's. It matters here because every "
    "time the app checks an address, whoever runs the server it talks to "
    "can see that address and link it to you. Bitcoin itself is public — "
    "no address is a secret — but which addresses are yours is, and a "
    "node you run keeps that piece to yourself. You don't need one to use "
    "the app; you'd want one to keep that link private. When you have "
    "one, the app connects to its mempool.space-style address. Want to "
    "try setting one up now, or continue without?"
)

#: ADR-0018 config-only semantics: the live client is built at bootstrap,
#: so the persisted switch lands on the next launch. Appended verbatim
#: after :data:`CONFIRMED` (implementation-time honesty line; the signed
#: copy alone would over-claim an in-session switch).
EFFECTS_NEXT_LAUNCH: Final[str] = (
    "The switch is saved for your next launch — quit and start the app "
    "again to run on your own node (until then this session keeps its "
    "current backend)."
)

#: Implementation-time copy (flagged in ADR-0023 §9 as awaiting a draft):
#: the decision-5 *syncing-node* branch. Figures are filled VERBATIM from
#: tool output (doctor advice fields + Core's own progress numbers);
#: nothing is invented and nothing is saved on this path.
NODE_SYNCING: Final[str] = (
    "{headline}. {detail}\n"
    "Sync progress right now: {progress}% verified — block {blocks} of "
    "{headers}.\n{next_step}\n"
    "Nothing was saved, and no address of your wallet was ever sent to "
    "it. Once the sync finishes, say \"retry\" with the same address — "
    "or pick the public server instead."
)

#: The web transport's one-line hint (requirement 5: no onboarding surface
#: in the browser; ADR-0023 says nothing about web, so the fallback the
#: ticket prescribes: existing behavior + a pointer to run the CLI once).
WEB_SETUP_HINT: Final[str] = (
    "Tip: no backend choice is saved yet — the app uses the public server "
    "(the notice above is honest about what that means). Run the terminal "
    "app once to pick your own node or keep the default; the choice is "
    "saved for this web UI too."
)

# --- key-ask guidance (step 1 side branches; code-owned, value-free) -----

#: Refusal + redirect when the key prompt receives seed-word-shaped input.
KEY_SEED_REFUSAL: Final[str] = (
    "That looks like seed words — please never share those with anyone, "
    "and this app is HARDWARE-WALLET-ONLY: it never handles seed phrases "
    "or private keys, and it could not use them anyway. It works only "
    "with the public key (xpub or zpub) from a hardware wallet (e.g. Jade "
    "or Coldcard — see docs/device-notes.md). Type 'help' and I'll point "
    "you at where to find it, or 'exit' to quit."
)

#: The "I don't know where to get that" help answer (pre-model: the AI
#: itself is not running yet, so the guidance is code-owned).
KEY_HELP: Final[str] = (
    "Your xpub or zpub comes from the hardware wallet that holds your "
    "bitcoin: a Jade, Coldcard, or similar device's settings usually have "
    "'export public key' or 'account descriptor' — the public key, never "
    "the seed words. This app is HARDWARE-WALLET-ONLY: it only ever works "
    "with a hardware wallet's public key, never a seed phrase or private "
    "key. It is ONE long string starting with xpub, ypub, or zpub. Paste "
    "it here when you have it, type 'help' to see this again, or 'exit' "
    "to quit."
)

#: Shown after a failed key parse (the parser's own value-free reason line
#: prints first).
KEY_RETRY_HINT: Final[str] = (
    "I need the wallet's xpub, ypub, or zpub — one long string, never "
    "your seed words. Type 'help' for where to find it, or 'exit' to quit."
)

# ---------------------------------------------------------------------------
# Deterministic classification sets (keyword rules, never a model call).
# ---------------------------------------------------------------------------

_SKIP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "1",
        "public",
        "public server",
        "default",
        "skip",
        "not now",
        "no",
        "no thanks",
        "later",
        "pass",
        "continue without",
        "continue without it",
    }
)
_OWN_NODE_WORDS: Final[frozenset[str]] = frozenset(
    {"2", "own node", "my node", "switch to my node", "i have a node", "yes"}
)
_HELP_WORDS: Final[frozenset[str]] = frozenset(
    {"help", "where", "where do i find it", "where do i get that", "idk"}
)
_BACK_WORDS: Final[frozenset[str]] = frozenset(
    {"back", "cancel", "never mind", "nevermind"}
)
_GUIDE_PREFIXES: Final[tuple[str, ...]] = (
    "what's a node",
    "whats a node",
    "what is a node",
)


class _AskState(Enum):
    """Where the step-2/5 conversation stands (closed set)."""

    OPEN = "open"        # node ask unanswered — stays open across chat turns
    URL_ASK = "url_ask"  # copy (b) shown; the next line is a URL candidate
    DONE = "done"        # confirmed (d) or skipped (e); chat is plain again


def _norm(line: str) -> str:
    """Keyword-comparison form: lowercase, stripped of edge punctuation."""
    return line.strip().lower().rstrip(" .!?")


def _looks_like_seed(line: str) -> bool:
    """BIP39-SHAPED input detection via the sanctioned scrubber (the same
    regex the transcript redactor uses — no duplicated pattern). The line
    is only scanned, never stored, never echoed."""
    return "<seed>" in redact_transcript(line.lower())


def _is_url_candidate(line: str) -> bool:
    return line.strip().lower().startswith(("http://", "https://"))


def ask_watch_key(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str | None:
    """Step 1: greeting + key ask on an interactive first launch.

    Returns the watch-key string once it parses through the SAME gated
    parser as startup (mainnet-only, value-free errors, ADR-0021), or
    ``None`` when the user exits (EOF/Ctrl-D or an exit word). Every input
    line is validated, so the caller never continues with a key startup
    would refuse.
    """
    output_fn(GREETING)
    while True:
        try:
            line = input_fn("you> ")
        except EOFError:
            return None
        text = line.strip()
        if text.lower() in ("exit", "quit"):
            return None
        if not text:
            continue
        if text.strip().lower() in _HELP_WORDS or text.strip().lower().startswith("help"):
            output_fn(KEY_HELP)
            continue
        if _looks_like_seed(text):
            output_fn(KEY_SEED_REFUSAL)
            continue
        try:
            parse_wallet_key(text)
        except WatchKeyError as exc:
            # Value-free by the descriptor layer's contract (never echoes
            # the key); same shape as startup's refusal line.
            output_fn(f"Watch key rejected: {exc}")
            output_fn(KEY_RETRY_HINT)
            continue
        return text


class OnboardingFlow:
    """The step-2..5 conversation state machine for one first-run CLI session.

    Constructed by ``_wire`` ONLY when: CLI transport, interactive launch, no
    chain backend on any rung (env > config file > stored), and the wallet
    profile was created THIS run (ADR-0023: step 1 skipped when the key was
    supplied; returning users never see the startup ask — skipped asks are
    re-offered by the chat-time triggers, deferred, see ticket return notes).

    Dependencies are injected callables so the whole flow is testable with
    zero network: ``check_backend`` (the chain probe), ``node_report``
    (loopback detection), ``loopback_host`` (host extraction, ADR-0016 gate).
    ``store`` receives the typed ``set_chain_base_url`` write on success —
    the ONLY sanctioned writer (ONB-002), and it is NEVER called with a
    URL that failed validation (decision 4: no silent public fallback —
    the default stays because the user chose it, never because a probe was
    papered over).
    """

    def __init__(
        self,
        *,
        store: Store,
        check_backend: Callable[[str], bool],
        node_report: Callable[[], LocalNodeReport] | None = None,
        loopback_host: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._check_backend = check_backend
        self._node_report = node_report
        self._loopback_host = loopback_host
        self._state = _AskState.OPEN
        self._last_failed: str | None = None

    @property
    def done(self) -> bool:
        return self._state is _AskState.DONE

    def opening_lines(self, *, load_started: bool) -> list[str]:
        """Step 2 ask + step 3 narration, printed once at startup (the scan
        began with the key, so the narration is honest only while it runs)."""
        lines = [NODE_ASK]
        if load_started:
            lines.append(LOAD_NARRATION)
        return lines

    def emit_load_complete(self, output_fn: Callable[[str], None]) -> None:
        """Step 4 — invoked by the scan flow when the FIRST startup scan
        persists successfully (never on a failure: the load did not
        complete)."""
        output_fn(LOAD_COMPLETE)

    def handle_line(
        self, line: str, output_fn: Callable[[str], None]
    ) -> bool:
        """One user line through the onboarding classifier.

        ``True`` = consumed on the deterministic channel (never reaches the
        model); ``False`` = ordinary chat (the ask stays open — answers may
        arrive at any point, decision: 'after step 4 it is an ordinary turn
        of chat' in reverse: ordinary turns run mid-ask).
        """
        if self._state is _AskState.DONE:
            return False
        text = line.strip()
        if not text:
            return False
        key = _norm(text)

        if key in _SKIP_WORDS:
            output_fn(SKIP_ACK)
            self._state = _AskState.DONE
            return True
        if key.startswith(_GUIDE_PREFIXES):
            output_fn(GUIDE)
            # The guide answers the question; the ask stays open (URL mode
            # returns to the ask, whose closing line the guide repeats).
            self._state = _AskState.OPEN
            return True
        if key in _OWN_NODE_WORDS:
            output_fn(URL_PROMPT)
            self._state = _AskState.URL_ASK
            return True
        if key == "retry" and self._last_failed is not None:
            return self._validate(self._last_failed, output_fn)
        if key.startswith("retry "):
            candidate = text[len("retry ") :].strip()
            if _is_url_candidate(candidate):
                return self._validate(candidate, output_fn)
        if _is_url_candidate(text):
            return self._validate(text, output_fn)
        if self._state is _AskState.OPEN and key in _HELP_WORDS:
            # Nothing to help with here — the key is already loaded; re-offer
            # the ask verbatim rather than dead-ending.
            output_fn(NODE_ASK)
            return True
        if self._state is _AskState.URL_ASK:
            if key in _BACK_WORDS:
                output_fn(NODE_ASK)
                self._state = _AskState.OPEN
                return True
            # Asked for an address (copy (b)); a non-URL line is exactly the
            # "didn't check out" case — nothing probed, nothing saved.
            output_fn(VALIDATION_FAIL)
            self._last_failed = None
            return True
        return False

    # ------------------------------------------------------------- validation

    def _validate(self, url: str, output_fn: Callable[[str], None]) -> bool:
        """Decision-5 validation: chain probe + (loopback only) doctor's
        IBD facts; success is the typed store write, failure is copy (c)
        or the syncing branch. NEVER saves on failure."""
        if not self._check_backend(url):
            output_fn(VALIDATION_FAIL)
            self._last_failed = url
            return True
        syncing = self._syncing_node_line(url)
        if syncing is not None:
            output_fn(syncing)
            self._last_failed = url
            return True
        try:
            self._store.set_chain_base_url(url)
        except StoreError:
            # Unreachable behind the probe (the writer's shape rules are a
            # subset the probe already enforced); fail closed, value-free.
            output_fn(VALIDATION_FAIL)
            self._last_failed = url
            return True
        output_fn(CONFIRMED)
        output_fn(EFFECTS_NEXT_LAUNCH)
        self._state = _AskState.DONE
        return True

    def _syncing_node_line(self, url: str) -> str | None:
        """The CORE_SYNCING refusal line for a LOOPBACK URL, or ``None``
        when the candidate passes (or IBD state is unobservable — the
        ADR-0016 contract: ``node/`` probes loopback hosts only, and a
        remote server's sync state is not observable through the Esplora
        API, so v1 accepts remote shape+mainnet proof alone)."""
        if self._node_report is None or self._loopback_host is None:
            return None
        if self._loopback_host(url) is None:
            return None
        try:
            report = self._node_report()
        except Exception:  # noqa: BLE001 — detection is designed never to raise; fail open to accept (the probe already passed; advise-only gate)
            return None
        reachable = [
            p for p in report.core if p.status is NodeStatus.REACHABLE and p.health
        ]
        if reachable and all(p.health is not None and p.health.chain != "main" for p in reachable):
            # Core reachable but not on mainnet — the ADR-0021 refusal
            # (regtest shares the mainnet genesis, so the chain probe alone
            # cannot see it; the loopback RPC's own report can).
            return VALIDATION_FAIL
        advice = NodeDoctor().recommend(
            any_core_reachable=bool(reachable),
            core_synced=any(p.health is not None and p.health.is_synced for p in reachable),
            core_auth_issue=any(p.status is NodeStatus.AUTH_FAILED for p in report.core),
            indexer_reachable=report.mempool is NodeStatus.REACHABLE
            or report.electrs is NodeStatus.REACHABLE,
        )
        if advice.state is not NodeStateKind.CORE_SYNCING:
            return None
        health = next(
            (p.health for p in reachable if p.health is not None and not p.health.is_synced),
            None,
        )
        if health is None:  # defensive: CORE_SYNCING implies an unsynced reachable probe
            return None
        return NODE_SYNCING.format(
            headline=advice.headline,
            detail=advice.detail,
            progress=f"{health.sync_percent:.1f}",
            blocks=health.blocks,
            headers=health.headers,
            next_step=advice.next_step,
        )
