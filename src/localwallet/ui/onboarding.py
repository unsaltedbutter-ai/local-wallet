"""First-run onboarding conversation (TCK-ONB-003, ADR-0023) + /setup re-entry.

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
- :class:`OnboardingFlow` — the step-2 node ask (copy (a)), the MANDATORY
  pre-scan decision on any launch whose backend is unresolved (TCK-ONB-006,
  ADR-0022 amendment 1 / ADR-0023 amendment 2): while the startup scan is
  deferred the wallet stays unloaded until the ask resolves — a URL line or
  ``2`` enters step 5 (URL prompt (b), validation, confirmation (d) / failure
  copy (c) with doctor guidance), an EXPLICIT public pick (``1``/``public``)
   records the opt-in (:data:`BACKEND_CHOICE_SETTING`) and releases the held
   scan through the ``public_chosen`` hook (which REPORTS whether the load
   actually started — the "loading now" line rides on that answer only),
   and "not now" is NOT consent — the copy says what waits and the ask
   stays open. The hold is armed on EVERY unresolved interactive launch
   regardless of AUTO_SCAN (security review F1: turning off the automatic
   scan is not consent to an unchosen server), so the plain skip answer
   (e) only remains where nothing is held — a resolved launch whose /setup
   ask is a pure re-choice. "What's a node?" reads (f). Anything else
   falls through to the model untouched. Steps 3/4
  narration rides :meth:`opening_lines` / :meth:`emit_load_complete`
  (non-blocking variant, ADR-0022 / TCK-SCAN-003 landed; the deferred
  variant LOAD_WAIT replaces it while the scan is held).
- :meth:`OnboardingFlow.begin_setup` — the ``/setup`` transcript command
  (TCK-ONB-005, ADR-0023 step 5 run against an EXISTING wallet): the flow
  exists DORMANT on every interactive CLI launch and /setup arms it. A
  stored choice is shown first (mode framing, value-free) and an explicit
  ``y`` is required before it can be overwritten; ``n`` exits with no
  change. Non-http(s) schemes (``ssl://`` and the electrum kinds) are
  refused plainly — v1 speaks Esplora over http(s) only (ADR-0023
  decision 7, TCK-ONB-004 backlog) — and the entry re-prompts.
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
    "ASK_WAITS_ACK",
    "BACKEND_CHOICE_PUBLIC",
    "BACKEND_CHOICE_SETTING",
    "DEFERRED_RESTART",
    "GREETING",
    "LOAD_COMPLETE",
    "LOAD_NARRATION",
    "LOAD_WAIT",
    "NODE_ASK",
    "NON_ESPLORA_URL",
    "PUBLIC_CHOSEN_ACK",
    "PUBLIC_LOADING_NOW",
    "SETUP_CURRENT",
    "SETUP_KEEP_CURRENT",
    "SETUP_KEPT",
    "SETUP_OVERWRITE",
    "SETUP_REVERTED",
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

#: Step 2 — the node ask, copy block (a) AS AMENDED by TCK-ONB-006 (ADR-0023
#: amendment 2, user direction 2026-09-09): the server options are named
#: plainly (Bitcoin Core / Electrum server / private mempool.space), the
#: public option carries the leak in plain words, and on an unresolved
#: launch this ask now precedes the scan (nothing is checked until it
#: answers). Reused verbatim on re-offers and by /setup.
NODE_ASK: Final[str] = (
    "A decision that's yours to make: which server should the app ask "
    "about your wallet's addresses? Your own server — Bitcoin Core (with "
    "the mempool.space app), an Electrum server, or a private "
    "mempool.space server; Start9, Umbrel and MyNode run all of these — "
    "means only your own machine ever sees which addresses you check. The "
    "public mempool.space server needs no setup, but be clear about its "
    "price: whoever runs it sees every address you check, can link those "
    "addresses together and to your IP, and watches when your "
    "transactions move.\n"
    "\n"
    "One honest limit for today: the app connects to an Esplora-style "
    "http(s) address — the mempool.space app — which those boxes all "
    "offer; a plain Bitcoin Core install doesn't serve one yet, and "
    "ssl://-style Electrum addresses come in a later version.\n"
    "\n"
    "1. Public mempool.space server — nothing to set up, with the leak "
    "above.\n"
    "2. Your own server — paste its address or take me through it.\n"
    "\n"
    "If you're not sure what any of this means, ask me \"what's a node?\" "
    "and I'll explain."
)

#: The TCK-ONB-006 backend-resolution marker (ADR-0023 amendment 2): with
#: no URL on any ladder rung, an explicit public pick is ONLY "a backend
#: choice exists" once it is recorded — an unset stored rung means "never
#: chose", not "chose public". Written through the store's generic settings
#: table by this module (the only writer), read by ``app._backend_resolved``
#: (the single source of truth for scan gating + ask arming). Deliberately
#: NOT in the web ``/settings`` allowlist: consenting to the public server
#: happens in the warned conversation, never by silent API write.
BACKEND_CHOICE_SETTING: Final[str] = "chain_backend_choice"
BACKEND_CHOICE_PUBLIC: Final[str] = "public"

#: Step 3 — load narration, the MAIN (non-blocking) variant: TCK-SCAN-003
#: has landed, so the honest promise "you can keep asking me questions" is
#: now true (the interim blocking variant is retired per ADR-0023 §flow).
LOAD_NARRATION: Final[str] = (
    "I'm loading your wallet right now. While I look through its history "
    "you can keep asking me questions — I'll give you the most up-to-date "
    "information I have."
)

#: Step 3 while the startup scan is DEFERRED (TCK-ONB-006, ADR-0022
#: amendment 1): replaces :data:`LOAD_NARRATION` — nothing has been loaded
#: or checked yet, so it promises nothing except the honest wait.
LOAD_WAIT: Final[str] = (
    "Until you choose, I haven't checked a single address against the "
    "network — your balance and history read empty, and sending stays "
    "off. There's no rush: say 1 or 2 whenever you're ready and I'll load "
    "your wallet the moment you do. Ask me anything else in the meantime."
)

#: "Not now" while the scan is deferred: skipping is NOT a public consent
#: (ADR-0023 amendment 2) — what waits is said plainly, and the ask stays
#: OPEN so a later answer this session still resolves it.
ASK_WAITS_ACK: Final[str] = (
    "Understood — I won't use a server you haven't chosen. Your wallet "
    "stays unloaded and no address goes anywhere for checks; the two "
    "choices above stay open whenever you're ready, and I'll ask again "
    "next launch."
)

#: Explicit public pick with nothing stored: consent recorded WITH the leak
#: named once more, then the held scan is released.
PUBLIC_CHOSEN_ACK: Final[str] = (
    "Understood — the public mempool.space server it is, chosen with "
    "eyes open: whoever runs it sees the addresses we check and when "
    "your transactions move. You can switch to your own server any "
    "time with /setup."
)

#: Appended to :data:`PUBLIC_CHOSEN_ACK` only when the release hook reports
#: that the pick ACTUALLY started the deferred startup scan (security
#: review F2: a failed plan stands the scan down and nothing loads).
PUBLIC_LOADING_NOW: Final[str] = "Loading your wallet from it now."

#: An own-server choice saved WHILE the startup scan was deferred
#: (TCK-ONB-006): the live client is still the public default (ADR-0018
#: config-only), and loading from it would leak exactly what the choice
#: refuses — so the load waits for the restart the honesty line promises.
DEFERRED_RESTART: Final[str] = (
    "Your server is saved, and your wallet is still unloaded — I won't "
    "check its addresses against a server you didn't choose just to fill "
    "the gap. Quit and start the app again: from that launch, everything "
    "loads from the server you set up."
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

#: The web transport's launch hint, shown whenever the backend is
#: unresolved — and since TCK-ONB-006 (ADR-0022 amendment 1) the web
#: first-run scan is DEFERRED too: nothing is looked up before a choice,
#: so the hint says what waits and why (requirement 5 stands: the browser
#: gets no conversation, only this pointer; there is still no public
#: consent surface on the web — the warned conversation is CLI-only).
WEB_SETUP_HINT: Final[str] = (
    "No server choice has been made yet, so the app has NOT looked up "
    "your wallet — no address has gone to any server, and balances and "
    "history stay empty until a backend is chosen. Run the terminal app "
    "once to decide: your own server (Bitcoin Core with the "
    "mempool.space app, an Electrum server, or a private mempool.space "
    "install) or the public mempool.space server, whose operator can see "
    "the addresses you check and when your transactions move. Saving a "
    "backend address in Settings works too; either choice takes effect "
    "on the next launch."
)

# --- /setup re-entry copy (TCK-ONB-005; implementation-time, value-free) ---
#
# The stored URL is NEVER echoed, even though it is the user's own config:
# every line below frames the current choice by MODE ("your own server" vs
# "the public server"), matching the privacy banner's classification — the
# surfaces can then never disagree, and a screenshot of the chat leaks
# nothing. The node ask (a), URL prompt (b), failure (c), confirmation (d)
# and next-launch honesty line are REUSED VERBATIM from the ADR-0023 blocks.

#: Shown by /setup when a choice is already stored (before anything else).
SETUP_CURRENT: Final[str] = (
    "You already have a backend choice saved — right now the app asks "
    "your own server, not a public one."
)

#: The overwrite gate: an explicit y/n inside the /setup loop, required
#: BEFORE the new choice can replace the stored one (value-free).
SETUP_OVERWRITE: Final[str] = (
    "Type y to set up a different server, or n to keep this one and "
    "finish. Nothing changes until a new address has been checked out and "
    "saved."
)

#: Declining the overwrite gate (also "back"/"cancel"): exit, no change.
SETUP_KEPT: Final[str] = (
    "Kept — your current backend choice stands, untouched."
)

#: Skipping the privacy ask while a choice is stored: keeps CURRENT, which
#: is NOT what :data:`SKIP_ACK` claims — hence this separate line.
SETUP_KEEP_CURRENT: Final[str] = (
    "No problem — the app keeps using the backend it has now, untouched. "
    "Nothing was changed."
)

#: Picking the public server explicitly (/setup with a stored choice):
#: clears the stored rung; honesty about the session riding the old backend
#: (ADR-0018 config-only) lives in this line's own wording.
SETUP_REVERTED: Final[str] = (
    "Set — from your next launch the app uses the public server again; "
    "this session keeps the backend it started with. Your saved address "
    "has been removed."
)

#: A URL whose scheme v1 cannot speak (ssl:// and the other Electrum-
#: protocol kinds, ADR-0023 decision 7 / TCK-ONB-004): plain statement, no
#: probe, nothing saved — and the entry re-prompts (never a dead end).
NON_ESPLORA_URL: Final[str] = (
    "That's not an address this app can use yet: right now it connects "
    "only to Esplora-protocol servers over http(s) — the web address of a "
    "mempool.space app. Electrum servers (ssl:// and the like) are planned "
    "for a later version. Nothing was probed and nothing was saved. Type "
    "an http(s) address, or 1 for the public server."
)

#: /setup refuses DORMANT when the stored rung cannot be read (no gate can
#: be promised over an unseen choice; value-free, retryable).
_SETUP_STORE_ERROR: Final[str] = (
    "I couldn't read your saved backend choice — the database is busy; "
    "try /setup again."
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
#: The subset of the skip words that NAMES the public server: on /setup
#: over a stored choice it is an explicit revert (clears the stored rung),
#: not a mere "keep current". Security review F3: "default" is RETIRED
#: from the vocabulary — the amended ask lists "1"/"public", never
#: "default", and durable public consent may only ride on words the
#: warned conversation actually presents (the free default the word named
#: no longer exists: while unresolved nothing is current). "default" now
#: falls through as ordinary chat and records nothing.
_PUBLIC_WORDS: Final[frozenset[str]] = frozenset(
    {"1", "public", "public server"}
)
_OWN_NODE_WORDS: Final[frozenset[str]] = frozenset(
    {"2", "own node", "my node", "switch to my node", "i have a node", "yes"}
)
_CONFIRM_YES: Final[frozenset[str]] = frozenset({"y", "yes"})
_CONFIRM_NO: Final[frozenset[str]] = frozenset({"n", "no"})
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

    OPEN = "open"  # node ask unanswered — stays open across chat turns
    URL_ASK = "url_ask"  # copy (b) shown; the next line is a URL candidate
    DONE = "done"  # confirmed (d) or skipped (e); chat is plain again
    #: /setup only (TCK-ONB-005): a stored choice was shown; the next line
    #: must be the deterministic y/n before the ask even appears.
    SETUP_CONFIRM = "setup_confirm"


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


def _is_other_scheme_url(line: str) -> bool:
    """A bare URL with a scheme v1 cannot speak — ``ssl://host:50001`` and
    the other Electrum-protocol shapes (ADR-0023 decision 7: Esplora over
    http(s) only; TCK-ONB-004 owns the future adapter). Scheme token
    immediately before ``://`` (no spaces): free prose merely MENTIONING a
    URL ("why is https://x slow?") stays ordinary chat."""
    low = line.strip().lower()
    if "://" not in low or low.startswith(("http://", "https://")):
        return False
    return " " not in low.split("://", 1)[0]


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
    """The step-2..5 conversation state machine for one CLI session.

    Constructed by ``_wire`` for EVERY interactive CLI launch
    (TCK-ONB-005), but ARMED at startup when the backend is UNRESOLVED and
    either the wallet profile was created THIS run (the first-run ask,
    ADR-0023 step 2: step 1 is skipped when the key was supplied) or the
    startup scan is being HELD for this ask (TCK-ONB-006, ADR-0023
    amendment 2: the ask is mandatory pre-scan, so it is re-asked on every
    launch until it resolves — a returning wallet that never answered is
    still unresolved). Otherwise it is DORMANT: :meth:`handle_line`
    consumes nothing (ordinary chat reaches the model untouched) until
    the ``/setup`` transcript command arms the same backend branch via
    :meth:`begin_setup` — node ask → URL entry → validation → write, with
    the overwrite gate on top of a stored choice.

    Dependencies are injected callables so the whole flow is testable with
    zero network: ``check_backend`` (the chain probe), ``node_report``
    (loopback detection), ``loopback_host`` (host extraction, ADR-0016 gate),
    and ``public_chosen`` (TCK-ONB-006: invoked after an explicit public
    consent is recorded, so the app can release a deferred startup scan —
    a "not now" never reaches it; it RETURNS whether the load actually
    started, and the "loading now" line is gated on that answer — security
    review F2). ``deferred`` states whether the scan is being held for this
    ask (it changes what the skip and confirmation branches promise).
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
        armed: bool = True,
        deferred: bool = False,
        public_chosen: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._check_backend = check_backend
        self._node_report = node_report
        self._loopback_host = loopback_host
        self._state = _AskState.OPEN
        self._last_failed: str | None = None
        self._armed = armed
        self._had_choice = False
        self._deferred = deferred
        self._public_chosen = public_chosen

    @property
    def done(self) -> bool:
        return self._state is _AskState.DONE

    def opening_lines(self, *, load_started: bool, deferred: bool = False) -> list[str]:
        """Step 2 ask + step 3 narration, printed once at startup. With the
        scan HELD (TCK-ONB-006) the honest step-3 line is :data:`LOAD_WAIT`
        — nothing is loading yet; :data:`LOAD_NARRATION` only fits a scan
        that actually started."""
        if deferred:
            return [NODE_ASK, LOAD_WAIT]
        lines = [NODE_ASK]
        if load_started:
            lines.append(LOAD_NARRATION)
        return lines

    def emit_load_complete(self, output_fn: Callable[[str], None]) -> None:
        """Step 4 — invoked by the scan flow when the FIRST startup scan
        persists successfully (never on a failure: the load did not
        complete)."""
        output_fn(LOAD_COMPLETE)

    def begin_setup(self, output_fn: Callable[[str], None]) -> None:
        """The ``/setup`` transcript-command entry (TCK-ONB-005): run the
        ADR-0023 backend branch against an EXISTING wallet.

        A stored choice is shown first (mode framing, value-free — the
        stored URL is never echoed) and an explicit ``y`` at the overwrite
        gate is required BEFORE the branch can reach a write; ``n``/back
        exits with no change. With nothing stored the ask leads directly
        (a skip keeps the current public default). The live client is NOT
        touched — the write rides the ADR-0018 config-only ladder and is
        honest about taking effect next launch.
        """
        self._last_failed = None
        try:
            current = self._store.get_chain_base_url()
        except StoreError:
            # Cannot see the stored rung → cannot promise the gate — fail
            # closed DORMANT, nothing changes (value-free).
            output_fn(_SETUP_STORE_ERROR)
            return
        self._armed = True
        if current is not None:
            self._had_choice = True
            output_fn(SETUP_CURRENT)
            output_fn(SETUP_OVERWRITE)
            self._state = _AskState.SETUP_CONFIRM
        else:
            self._had_choice = False
            output_fn(NODE_ASK)
            self._state = _AskState.OPEN

    def handle_line(
        self, line: str, output_fn: Callable[[str], None]
    ) -> bool:
        """One user line through the onboarding classifier.

        ``True`` = consumed on the deterministic channel (never reaches the
        model); ``False`` = ordinary chat (the ask stays open — answers may
        arrive at any point, decision: 'after step 4 it is an ordinary turn
        of chat' in reverse: ordinary turns run mid-ask). A flow never
        armed (dormant ``/setup``-capable session) consumes NOTHING.
        """
        if not self._armed or self._state is _AskState.DONE:
            return False
        text = line.strip()
        if not text:
            return False
        key = _norm(text)

        if self._state is _AskState.SETUP_CONFIRM:
            # The overwrite gate: only the deterministic y/n answers it;
            # anything else re-prompts INSIDE the gate (never a model
            # turn, never a skip through).
            if key in _CONFIRM_YES:
                output_fn(NODE_ASK)
                self._state = _AskState.OPEN
            elif key in _CONFIRM_NO or key in _BACK_WORDS:
                output_fn(SETUP_KEPT)
                self._state = _AskState.DONE
            else:
                output_fn(SETUP_OVERWRITE)
            return True
        if _is_other_scheme_url(text):
            # ssl://-style Electrum-protocol address: v1 cannot speak it
            # (ADR-0023 decision 7; adapter = TCK-ONB-004 backlog) — said
            # plainly, never probed, never saved, the entry re-prompts.
            # Both the ask state and the URL-entry state take this line
            # (a first-run paste gets the same honest treatment).
            output_fn(NON_ESPLORA_URL)
            return True
        if key in _SKIP_WORDS:
            if not self._had_choice:
                if key in _PUBLIC_WORDS:
                    # An EXPLICIT public pick is a backend choice: record it
                    # and release any held scan (TCK-ONB-006).
                    self._accept_public(output_fn)
                    self._state = _AskState.DONE
                elif self._deferred:
                    # "Not now" is NOT public consent while the scan waits
                    # on the answer (ADR-0023 amendment 2): say what stays
                    # off, keep the ask OPEN (a later 1/2 still resolves it
                    # this session; the ask re-arms next launch).
                    output_fn(ASK_WAITS_ACK)
                else:
                    output_fn(SKIP_ACK)  # nothing stored, nothing held:
                    self._state = _AskState.DONE  # public IS current (e)
            elif key in _PUBLIC_WORDS:
                self._revert_to_public(output_fn)  # an EXPLICIT public pick
                self._state = _AskState.DONE
            else:
                output_fn(SETUP_KEEP_CURRENT)  # skip ≠ public (choice held)
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

    def _accept_public(self, output_fn: Callable[[str], None]) -> None:
        """Explicit public consent with no stored choice (TCK-ONB-006):
        record :data:`BACKEND_CHOICE_SETTING` (an unset stored rung means
        "never chose", not "chose public" — the marker is what makes the
        NEXT launch's backend resolved), then release the held startup
        scan through the app's hook. The record write is best-effort: the
        session's consent stands either way; a failed write just means the
        ask re-appears next launch (fail-closed toward asking, never
        toward leaking)."""
        was_deferred = self._deferred
        try:
            self._store.set_setting(BACKEND_CHOICE_SETTING, BACKEND_CHOICE_PUBLIC)
        except StoreError:
            pass
        started = self._public_chosen() if self._public_chosen is not None else False
        output_fn(PUBLIC_CHOSEN_ACK)
        if was_deferred and started:
            # Honest ONLY because the release reported that it actually
            # started the load (security review F2: a failed plan stands
            # the scan down — nothing is loading this session, and the
            # line would be a claim about money-work that never began).
            output_fn(PUBLIC_LOADING_NOW)
        self._deferred = False

    def _revert_to_public(self, output_fn: Callable[[str], None]) -> None:
        """/setup over a stored choice, public picked EXPLICITLY (``1``/
        ``public``): clear the stored rung through the typed writer (the
        ``""``-clears convention, ONB-002) AND record the public marker —
        without it the cleared rung would read "never chose" next launch
        and the ask would re-arm. A failed write keeps the stored choice
        and says so — the ack is never a lie either way."""
        try:
            self._store.set_chain_base_url("")
        except StoreError:
            output_fn(SETUP_KEEP_CURRENT)
        else:
            try:
                self._store.set_setting(BACKEND_CHOICE_SETTING, BACKEND_CHOICE_PUBLIC)
            except StoreError:
                pass
            output_fn(SETUP_REVERTED)

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
        if self._deferred:
            # TCK-ONB-006: the held scan does NOT start on this choice —
            # the live client is the old (public-default) one, and loading
            # through it would leak what the user just refused. The scan
            # runs from the server they chose at the next launch.
            output_fn(DEFERRED_RESTART)
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
