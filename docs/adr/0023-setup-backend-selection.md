# ADR-0023: First-run backend selection — explicit opt-in to a self-hosted Esplora at setup

- **Status:** Accepted (copy drafts below await §9 designer sign-off;
  TCK-ONB-001). Revised 2026-09-07 for the user-directed onboarding
  conversation flow — the load-narration string depends on ADR-0022 /
  TCK-SCAN-003 (interim variant defined below for shipping before it).
  Revised again 2026-09-07: the educational node ask moves *into* the
  onboarding conversation, immediately after the xpub is received
  (user goal: get users the most private setup possible), replacing the
  post-load placement of the prior revision. The ask stays skippable and
  non-blocking; the opt-in, validation, and precedence machinery is
  unchanged. **Amended 2026-09-09 (TCK-ONB-006): on a first run with no
  backend choice anywhere, the ask is no longer skippable-in-passing —
  it is MANDATORY pre-scan and the startup scan waits for it (see
  §Amendment 2 below and ADR-0022 amendment 1); the non-blocking rule
  survives for everything EXCEPT the scan's start.**
- **Date:** 2026-09-07
- **Decides:** How the user chooses their chain backend at first run: the
  first-run conversation flow (greeting → xpub ask → skippable node ask
  during load → load completes; URL capture + validation on opt-in)
  offers the public default or the user's own Esplora-protocol server,
  without ever gating first use; a skipped ask is re-offered later. The
  choice is persisted and rides the existing ADR-0018 single selection
  point. Default stays public.
- **Scope:** the first-run conversation flow — greeting, xpub ask, load
  narration, backend choice (`app.py` + `ui/cli.py`, ticket TCK-ONB-003),
  persisted settings (`store/`, `config.py`, ticket TCK-ONB-002), and the
  setup/privacy copy (§9). Relates to ADR-0003 (the
  public-backend privacy caveat this closes for self-hosters), ADR-0018
  (the config-only switch this surfaces in the UI), ADR-0016/0017 (node
  detection, loopback contract, advise-only doctor), ADR-0021 (mainnet-only
  — what URL validation must enforce).

## Context

ADR-0003 accepted a public mempool.space Esplora for the MVP and recorded
the honest caveat: while the public backend is active, the operator sees
every queried address together with the requesting IP and can associate or
cluster them. ADR-0021 (mainnet-only) sharpened that caveat from testnet
activity into the user's real wallet. ADR-0018 made the switch to a
self-hosted instance a pure config change (`LOCALWALLET_CHAIN_BASE_URL`),
but it deliberately stayed config-only: ADR-0017/0018 rejected
auto-following the doctor's detection, and the only user-visible
acknowledgment of the gap is the passive startup banner. Today a
self-hoster must already know the env var exists — the privacy payoff
reaches only the user who reads the docs.

Onboarding is the cheapest point to fix that: the user importing a zpub is
already being asked to trust the app, and one extra question converts the
banner's "here is your leak" into "here is how to close it".

## Decision

1. **The backend-choice prompt offers exactly two choices** — placed in
   the first-run conversation *immediately after the xpub is received,
   while the wallet loads* (step 2 of "Onboarding conversation flow"),
   never as a blocking modal and never requiring an answer before the
   app works: the public default, or the user's own Esplora-protocol
   server (a self-hosted mempool.space / Esplora instance, including the
   mempool.space apps on Umbrel, Start9, and MyNode — the tiers of
   ADR-0017 that serve Esplora). The ask is educational and privacy-
   motivated (user direction: "we want to get users the most private
   setup possible"), but a skip or no-answer silently keeps the public
   default and is re-offered later (decision 2, flow step 2); silence is
   never a dead end. Choosing public requires nothing further; choosing
   own-node leads to the URL-entry step (copy drafts below).

2. **The default stays public.** First run with no answer given, or on any
   earlier version, behaves exactly as before (ADR-0003): zero change when
   unset. Accessibility over purity — requiring a node would reproduce the
   Phase 0 rejection in ADR-0003.

3. **The choice rides the existing ADR-0018 selection point — no second
   mechanism.** The UI never calls a different code path: the stored choice
   surfaces as the value of `Settings.chain_base_url`, and resolution stays
   in exactly one function (`ChainConfig.from_settings`). Precedence, which
   TCK-ONB-002 must implement and test:

   ```
   env LOCALWALLET_CHAIN_BASE_URL  >  stored choice (settings DB)  >  public default
   ```

   An explicitly exported env var is the operator's override and always
   wins; the setup prompt writes only the stored layer; the public default
   is what remains when neither is set. With no choice stored, behavior is
   bit-identical to pre-ONB main (`esplora_base_url` fallback untouched,
   ADR-0018 decision 2).

4. **Explicit opt-in only; never a silent public fallback.** Two directions
   matter. At entry: a URL that fails validation is *not* saved and not
   used — the user is told plainly (copy below) and the flow stays where it
   was. At run time, once the own-node choice is saved: if the user's node
   is unreachable, the app errors with the backend named in the message and
   routes to the doctor's guidance (ADR-0017) — it does **not** quietly
   send addresses to the public server instead. A backend the user chose is
   a promise; failing it loudly is honest, falling back silently is the
   leak the choice was made to avoid. This is the load-bearing AC for
   TCK-ONB-003.

5. **Validation at entry, before anything is saved.** The entered URL must:
   be a well-formed http(s) URL (the existing fail-closed
   `ChainConfig.__post_init__` check, ADR-0018 decision 5, fires on save);
   answer in Esplora shape; serve **mainnet** (ADR-0021 — a testnet backend
   is refused with a value-free error); and be within initial block download
   range (a syncing node is reported with its progress rather than silently
   accepted, mirroring the doctor's `CORE_SYNCING` state). Which module
   probes is governed by the loopback contract (ADR-0016): `node/` may
   probe only loopback hosts, so validation of a **remote** URL is a
   `chain/` concern (it is the only module with general network access).
   On failure, copy points at the doctor; the doctor remains advise-only —
   validation never configures or repairs the user's node.

6. **The privacy indicator must reflect the actual choice.** The 3-state
   banner (public / own node on this machine / own node on another machine,
   TCK-SEC-004 change 5) derives from the *effective* resolved selection
   (env > stored > default after decision 3), not from the mere presence of
   a URL string. The known **L5 finding** (TCK-SEC-006 follow-up register)
   is recorded here: "your own node" keys on configuration, not ownership —
   a *configured* URL can be any third party's server. v1 keeps the
   approved banner strings (changing them is a separate §9 sign-off) and
   closes the honesty gap in the setup copy instead: the confirmation line
   explicitly states that whoever runs the configured server can see the
   queried addresses. TCK-ONB-003 must ensure the banner's mode derivation
   goes through the resolved backend, so the banner, the `node_status`
   narration, and the new stored choice can never disagree.

7. **v1 scope is Esplora-protocol URLs only.** Electrum-protocol servers
   (electrs/Fulcrum JSON-RPC, Neutrino) and raw Bitcoin Core RPC scanning
   are explicitly out: they need a second protocol client in `chain/` and,
   for Core RPC, an auth surface (cookie/rpcuser) this flow must not touch.
    Both are named as future work, tracked as TCK-ONB-004 (backlog, unsched-
    uled for v1). The setup prompt says "Esplora" in product terms ("the
    mempool.space app") so users are not led to enter a URL v1 cannot use.
    The step-2 educational ask may name Electrum servers and Bitcoin Core —
    they genuinely are the good privacy options, and the named boxes serve
    them behind a mempool.space app — but only with block (a)'s limit
    sentence attached: the app connects to a mempool.space-style address,
    a plain Core install doesn't offer one yet. The copy must never read
    as "paste your electrum/cookie path".

## Onboarding conversation flow

The first-run conversation is a five-step sequence (steps 1–4 are the
default linear path; step 5 runs only on opt-in). Its shape is user
direction; the strings below are the canonical drafts (each a single named
constant in `ui/cli.py`, value-free per §9/AGENTS.md — the literal
`xpubabc123...` is a placeholder in the copy, never user data, and nothing
here logs xpubs, addresses, or amounts). The governing rule: **never block
the user from using the app.** No step is a modal; skipping is always
allowed and a skipped step just keeps the default — the node ask in
particular is *asked early because it matters, not because it gates
anything*: the scan runs and the app becomes usable whether or not the
user answers.

**Step 1 — greeting + key ask.** Skipped entirely if an xpub/zpub is
already on file (returning user resumes at step 2's load narration from
disk):

```
Hi, nice to meet you. I'm local wallet, your personal AI bitcoin wallet.
Our conversation stays private — the AI runs right here on your machine.
To get started, can you give me the xpub or zpub of the wallet you want to
work with? These are not your seed words — and please never share your
seed words with anyone, me included. An xpub or zpub is a long string of
letters like xpubabc123... If you don't know where to get that, let me
know and I will guide you.
```

"everything we say and do together stays private" (the prior draft) was an
over-claim per §9: chat stays local, but address queries go to the backend
and transactions are public by nature on-chain. The promise is now scoped
to the conversation, which is exactly what the §9 table guarantees; the
address-visibility caveat is carried by the startup banner and the step-2
ask. "me included" is honest — the app refuses seed phrases in chat and
redirects to guidance, so the app truly is one of the "anyone."

The seed-words clarification is load-bearing, not flavor: watch-only means
seed phrases are refused in chat with guidance (AGENTS.md invariant), and
the app must say what to look for before the user pastes the wrong thing.
"If you don't know where to get that" maps to the existing help path.

**Step 2 — the node ask, offered not imposed.** Immediately after the
xpub is received (or read from disk), before the load narration, and while
the scan runs in the background, the privacy ask (copy block (a) below) is
offered conversationally. The ask never blocks: steps 3 and 4 fire on
their own schedule even while the ask is unanswered, and an answer may
arrive at any point (mid-load or days later — after step 4 it is an
ordinary turn of chat). Outcomes:

- **Answer with a URL** → validate per decision 5 (the probe runs
  alongside the scan; both are `chain/` calls). Success → confirmation
  copy (d); failure → failure copy (c), nothing saved, the public
  default keeps serving, and the ask stays open — never a dead end.
- **Pick 2 without a URL** → the URL-entry prompt (b), then as above.
- **"Not now" / skip / silence** → skip acknowledgment (e), public
  default silently retained (decision 2 — an explicit non-opt-in, not a
  decision the app needs from the user to proceed), and the ask is
  re-offered later: the first conversation that mentions privacy, nodes,
  or the startup banner, and once before the first-ever send confirmation
  (the moment the privacy difference has money attached to it).
- **"What's a node?"** → guide-path copy (f), then the ask stays open.

This placement replaces the prior revision's post-load positioning. The
educational ask moved earlier because the user's stated goal is "the most
private setup possible" and an ask buried after first use converts fewer
users; the non-blocking rule survives intact because the ask requires no
answer (default is public either way, decision 2), so asking during load
gates nothing. Returning users with a stored choice never see it again;
returning users on the default see it only through the re-offer triggers.

**Step 3 — load starts, chat stays open.** The load itself began the
moment the xpub was received (before step 2's ask); this is its narration,
sent right after the ask, non-blocking:

```
I'm loading your wallet right now. While I look through its history you
can keep asking me questions — I'll give you the most up-to-date
information I have.
```

**Dependency (exact):** this string is honest only once TCK-SCAN-002/003 —
ADR-0022's non-blocking startup scan with the deterministic tool-owned
freshness flag — has landed; until then the startup scan blocks and the
promise "you can keep asking me questions" is false. If TCK-ONB-003 ships
before TCK-SCAN-003, it **must** use this interim variant instead, which
promises nothing about being able to answer during load — neither wallet
data nor app questions — because the blocking scan can keep no such
promise (the prior interim draft's "you can ask me anything about how the
app works" was exactly such a promise):

```
I'm loading your wallet right now. Until the load finishes I can't answer
questions or tell you what's in your wallet — I'll let you know when I'm
done.
```

The phrase "the most up to date information I have" is deliberate and ties
to ADR-0022's stale-flag semantics: during the first scan an answer may be
cache-served and stale-flagged, and the model narrates freshness only from
the tool's flag — it never authors a freshness claim. Note also that
unblocking chat does not unblock value movement: per ADR-0022 (as
specified in the TCK-SCAN-002 task row; the ADR file is written by that
ticket), **create_tx must refuse until the first scan completes** — the
send flow stays gated even while questions are answered. The flow must
therefore never imply sending is possible mid-load.

**Step 4 — load completes:**

```
I've finished loading your wallet, so I should have up to date information
for all of your questions.
How can I help you?
```

("should" is kept verbatim from the user's draft and kept honest per §9:
never over-claim verification.)

**Step 5 — backend setup branch (opt-in only).** Reached from step 2 when
the user shows interest: URL entry (b), validation (decision 5), then
confirmation (d) or failure copy (c). If the node ask was skipped, step 5
is reached later via the re-offer triggers above — same strings, same
rules. Nothing in steps 3–4 depends on it, and the app stays fully usable
on the public default forever if the user never opts in.

## Setup & privacy copy (drafts for §9 sign-off)

Value-free by construction: no addresses, amounts, xpubs, or echoed URL
material in any string (AGENTS.md); each string is a single named constant
in `ui/cli.py` so the copy is structured for later i18n (OQ15). Tone
follows §10 (knowledgeable friend, no unexplained jargon) and §9 honesty
(never over-claim privacy; never over-claim verification).

**(a) Node-ask prompt (step 2 of the flow; offered right after the xpub,
during load, never blocking, silence keeps the default; reused verbatim on
re-offers):**

```
One thing worth knowing: for maximum privacy, you should use your own
Bitcoin node — something running Bitcoin Core, or an electrum server,
or a private mempool.space server. Start9 and Umbrel and MyNode are
great options for a standalone way to run these services, but you can
run Bitcoin Core on most computers. One honest limit for today: the
app connects to a mempool.space-style address, which all of those
boxes offer — a plain Bitcoin Core install by itself doesn't give the
app one yet.

You can pick either, and skipping is a fine answer:

1. Public server (default) — nothing to set up. A public server sees
   which addresses you check, and can link them to your IP.
2. Your own node — only your own machine sees which addresses you
   check.

If you're not sure what any of this means, ask me "what's a node?"
and I'll explain.
```

**(b) URL-entry prompt (after choosing 2):**

```
Type the web address of your node's mempool.space app — its API
address is usually the same, with /api at the end. Nothing is saved,
and no address of your wallet goes to it, until the app has checked
that it answers correctly.
```

**(c) Validation-failure path (probe failed — plain cause, next step,
no silent fallback, doctor pointer):**

*Awaiting implementation-time copy (flagged, not sign-off-able now):*
decision 5's *syncing-node* branch ("reported with its progress") has no
drafted string — its wording depends on the doctor's `CORE_SYNCING` fact
keys and must quote the progress verbatim from tool output when
TCK-ONB-003 builds it. Likewise the step-3 non-blocking string and step 4
may need a stale/out-of-window tail once TCK-SCAN-003's freshness flag and
the existing `OUT_OF_WINDOW_NOTICE` semantics meet this flow; until then
ship step 3's interim variant only.

```
That address didn't check out: it wasn't reachable, or it didn't
answer as a mainnet mempool.space API. Nothing was saved, and no
address of your wallet was ever sent to it. Ask "node status" to see
what the app can detect on this machine — then say "retry" with the
same or a new address, or pick the public server instead.
```

**(d) Confirmation after a successful own-node setup (carries the L5
ownership honesty):**

```
Set. From now on the app asks your own server about your addresses
instead of a public one — the startup notice will say so too. One
caution: if that server isn't actually yours, whoever runs it can
still see which addresses you check.
```

**(e) Skip acknowledgment (after "not now" or no answer — never a dead
end, public default retained, change later stays open):**

```
No problem — the app keeps using the public server for now, and that's
not a verdict. Paste a mempool.space-style address, or ask to "switch
to my node", any time and we'll set it up. Ask "what's a node?"
whenever you want the long version.
```

**(f) Guide-path response to "what's a node?" (plain-language education,
then the ask stays open — no jargon, and honest about what is and isn't
private):**

```
A node is a computer that keeps its own copy of Bitcoin's history
instead of borrowing someone else's. It matters here because every
time the app checks an address, whoever runs the server it talks to
can see that address and link it to you. Bitcoin itself is public —
no address is a secret — but which addresses are yours is, and a node
you run keeps that piece to yourself. You don't need one to use the
app; you'd want one to keep that link private. When you have one, the
app connects to its mempool.space-style address. Want to try setting
one up now, or continue without?
```

## Amendment 2 (2026-09-09, TCK-ONB-006): the ask becomes mandatory pre-scan

User report 2026-09-09: entering a zpub on a fresh install immediately
started probing addresses against public mempool.space — before the node
ask had been answered, or even read. "We should require the user to give us
a server. Tell them it can be a bitcoind, or an electrum server, or a
private mempool. We should say if they don't have one they can use the
public mempool.space but be clear that this leaks their transaction
information and wallet addresses." Decisions 1/2's "never gates anything"
turned out to gate the one thing it shouldn't: the wallet's first address
query. This amendment supersedes the skippable-in-passing posture **for
the startup scan only**; everything else — accessibility (the public
option stays available forever), validation, precedence, no-silent-fallback
— stands unchanged.

1. **Unresolved means unresolved.** The backend is *resolved* when a URL
   sits on any rung of the decision-3 ladder (env > config file > stored)
   **or** an explicit public opt-in is recorded: settings key
   `chain_backend_choice="public"` (`BACKEND_CHOICE_SETTING`/`_PUBLIC` in
   `ui/onboarding.py`), written by the warned conversation only —
   deliberately NOT in the web `/settings` allowlist, because consent to
   the leak happens where the leak is named. The empty stored rung means
   "never chose", never "chose public"; today's implicit-public
   representation was "nothing at all", which could not carry a consent
   record — hence this explicit one (single source of truth:
   `app._backend_resolved`).
2. **While unresolved, no address query runs** (ADR-0022 amendment 1):
   the startup scan holds at `awaiting_backend`, the lazy in-handler scan
   and watch drain stand down with it, cache reads are `stale`-flagged and
   `create_tx` keeps refusing. The user report's leak is closed at the
   choke point, not by copy.
 3. **The ask is mandatory pre-scan while unresolved** on launches that can
    carry it: the interactive CLI re-arms the node-ask branch at startup on
    EVERY unresolved launch (fresh wallet or a returning wallet whose ask
    was never answered; regardless of `AUTO_SCAN` — a scan opt-out is not
    a server consent, security-review finding 1) until it resolves, and
    the web UI holds the scan too, pointing at the terminal (requirement 5
    keeps the browser free of consent surfaces). A headless scripted
    launch stays exactly as before — never blocked, never deferred (the
    command line is the operator's decision; decision 1's "never gating
    first use" survives there).
4. **Copy superseded (awaiting §9 re-sign-off; constants authoritative):**
   block (a) is rewritten per the report — it names the server kinds
   plainly (Bitcoin Core with the mempool.space app / an Electrum server /
   a private mempool.space server), states the v1 Esplora-over-http(s)
   limit and that `ssl://`-style Electrum addresses come later (decision 7
   unchanged), and states the public option's cost in plain words: the
   operator sees every address checked, can link them and to the IP, and
   watches when transactions move. New first-run-only blocks: `LOAD_WAIT`
   replaces step 3's narration while the scan is held (nothing "loading"
   is claimed); `ASK_WAITS_ACK` answers "not now"; `PUBLIC_CHOSEN_ACK`
   (+`PUBLIC_LOADING_NOW` when it starts the held scan) answers an
   explicit public pick; `DEFERRED_RESTART` is appended to confirmation
   (d) when the wallet is still unloaded. §9 voice: calm, honest,
   value-free — the public path is stated as a legitimate choice with a
   named price, never as a verdict.
 5. **Skip ≠ consent.** "Not now"/silence is NOT a public opt-in: it
    records nothing, and the wallet simply stays unloaded (ask stays open
    in-session; re-armed next launch). Only an explicit public answer
    (``1``/``public``, word set `_PUBLIC_WORDS`) records the opt-in and
    releases the held scan — onto the public server the user just accepted.
    ("default" is dropped from the vocabulary, security-review finding 3:
    the amended ask never lists it and public is no longer an implicit
    default but a warned choice, so the word would overclaim consent; it
    now falls through as ordinary chat.) An own-server
    choice records the stored rung but does NOT fire an in-session scan
    (decision 4's promise in new clothes: the live client is still the
    pre-choice one; loading through it would be the silent fallback this
    ADR forbids); the copy says the load comes after the restart, matching
    the ADR-0018 config-only semantics decision 5 already shipped.
6. **Reverts record too.** `/setup`'s explicit-public revert clears the
   stored URL *and* writes the public marker — otherwise the cleared rung
   would read "never chose" and the next launch would re-arm the ask (and
   hold the scan) behind the user's back.

## Alternatives considered

- **Auto-detect a local node and silently prefer it.** Rejected (and
  already rejected twice, ADR-0018/0017): it couples detection to
  selection, surprises the user about where their addresses went, and
  contradicts the explicit-opt-in principle this ADR exists to serve.
  Detection stays advisory; the user makes the choice.
- **Ship an Electrum-protocol adapter in v1.** Rejected for scope: a
  second protocol client plus a second validation and error surface, for
  data the Esplora shape already serves from the same self-hosted
  mempool/electrs stacks ADR-0017 recommends. Future work = TCK-ONB-004
  (same for raw Core-RPC scanning).
- **Require a self-hosted backend (no public option at setup).** Rejected:
  accessibility — it inverts ADR-0003's Phase 0 reasoning (initial block
  download cannot be a prerequisite for the 10-minute north-star flow) and
  would strand users of the app who cannot run hardware.
- **Write the choice to the env var / a config file from the UI.**
  Rejected: mutating the user's environment is out of the advise-only
  posture, and a stored setting behind the same `Settings` field keeps
  ADR-0018's single selection point intact with a smaller, testable surface.

## Consequences

- **TCK-ONB-002 (store+config):** `chain_base_url` gains a persisted
  settings-layer source with the decision-3 precedence; when neither env
  nor stored choice is set, behavior is unchanged (zero-diff guarantee for
  existing deployments); a settings-row migration and a precedence matrix
  are in scope; nothing may bypass `ChainConfig.from_settings`.
- **TCK-ONB-003 (ui+app):** the five-step onboarding conversation flow
  (strings in those sections above; the step-3 interim blocking variant is
  mandatory unless TCK-SCAN-003 has landed), the step-2 ask with its skip/
  guide/failure branches (copy blocks e and f), URL entry with
  decision-5 validation, the copy blocks above (subject to §9
  sign-off; string edits land there), the skip re-offer triggers, and the
  run-time no-silent-fallback
  error surface. The banner/`node_status` mode derivation must read the
  resolved selection so the three surfaces cannot disagree; the L5 note
  stays open as a known limitation until a banner-wording sign-off revisits
  "your own node".
- **TCK-ONB-004:** Electrum protocol and raw Core-RPC remain backlog; any
  re-entry needs a new ADR because decision 7 scopes v1 to Esplora.
- **ADR-0018 amendment note:** its decision 4 still says a self-hosted
  instance "must serve testnet4"; that predates the ADR-0021 mainnet flip
  and is superseded for this flow — setup validation requires **mainnet**
  and refuses testnet backends. (ADR text is preserved as history per repo
  convention; this paragraph is the authoritative correction.)
- **Privacy posture:** the §9 "what leaves the machine" table becomes a
  choice, not a fate — a self-hoster's address queries stop leaving the
  machine at first run instead of at some later config discovery. The
  public path's honest banner is unchanged.
