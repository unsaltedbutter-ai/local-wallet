"""TCK-WEB-003 XSS render contract (ADR-0024 §7): textContent-only + red-team.

Two enforcement layers, one ticket:

* **Static client audit** — the REAL web-builder client (``app.js`` /
  ``index.html``) is scanned for every HTML-string sink and inline-handler /
  ``javascript:`` surface the contract bans. This is the durable guard: if a
  future client edit reaches for ``innerHTML``, the XSS fixtures' whole premise
  (values are inert text) silently dies, and ONLY a source pin catches it in CI
  with no browser. ``sanitize_tool_output`` is NOT an HTML escaper (the ADR is
  explicit) so the render layer is the real defense — this test IS that defense.
* **Render-fixture inertness** — every ``evals/redteam/render/*.json`` hostile
  payload is run through the deterministic ``textContent`` model the eval runner
  ships (``run_evals.render_text_content``) and asserted to survive verbatim as
  one element-free text node — markup never becomes a node / handler / URL.
* **Live CSP + island nonce** — a real served index carries a CSP that allows NO
  inline script except the island (per-response nonce) and NO eval, proving the
  shrink-the-blast-radius layer end to end (the client source audit alone
  cannot see the header).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_EVALS_PATH = REPO_ROOT / "evals" / "run_evals.py"
_RENDER_DIR = REPO_ROOT / "evals" / "redteam" / "render"
_STATIC = REPO_ROOT / "src" / "localwallet" / "ui" / "web" / "static"


def _load_run_evals():
    spec = importlib.util.spec_from_file_location("render_run_evals", RUN_EVALS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUN_EVALS = _load_run_evals()

# The banned sinks/handlers (ADR-0024 §7 + web-builder "HARD" contract). Each is
# matched as a *code* usage, so the contract's own explanatory comments (which
# name them in prose) must not trip the scan.
_BANNED_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def _strip_js_comments(source: str) -> str:
    """Remove // line and /* block */ comments so the sink scan sees only code
    (the client comments the sinks in prose; those are not violations)."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in source.splitlines()
    )


def test_real_client_uses_no_html_string_sinks() -> None:
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(app_js)
    for sink in _BANNED_SINKS:
        assert sink not in code, f"app.js writes through the banned sink {sink!r}"
    # Dynamic model output goes in via text nodes ONLY.
    assert "textContent" in code or "createTextNode" in code
    assert "createTextNode" in code or ".textContent =" in code


def test_real_client_has_no_inline_handlers_or_eval_or_javascript_urls() -> None:
    app_js = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\bon[a-z]+\s*[:=]", app_js), "inline handler assignment"
    assert not re.search(r"\son[a-z]+=", index_html), "inline on*= handler in markup"
    assert "eval(" not in app_js and "new Function" not in app_js
    assert "javascript:" not in app_js.lower()
    # Buttons are wired with addEventListener (canonical utterances, §8), and the
    # shipped index declares the module script with NO inline body.
    assert "addEventListener" in app_js
    assert "<script" in index_html  # the module tag
    assert 'src="/static/app.js"' in index_html


# TCK-WEB-008 mechanical pins on the shipped client (static, browser-free):
# the watch-key form dismisses on the engine's own accept (never waits on a
# /state round-trip), a stale snapshot cannot re-show it, and the settings
# replace flow rides the ONE existing POST /watchkey channel — no second,
# handler-shaped endpoint.
def test_client_dismisses_the_form_on_accept_and_reuses_watchkey_endpoint() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "dismissWatchKeyForm" in code
    assert "watchKeyDismissed" in code  # terminal dismiss: stale /state can't undo it
    # exactly ONE fetch to /watchkey (postWatchKey), shared by form + replace:
    assert code.count('fetch("/watchkey"') == 1
    assert "replaceStage" in code and "watchKeyRow" in code


# TCK-ONB-007 STATIC half (user correction 2026-09-11) first-run pins: the
# settings pane NEVER self-opens and chat is NEVER disabled while the wallet
# still needs its key — the engine's chat beats own first-run. The pane
# opens ONLY from its explicit controls (header Settings toggle / quick
# action), and the whole WEB-008/009-era auto-open episode machinery is
# gone from the shipped client. The needs-key placeholder rides the TYPED
# snapshot (restore keyed on it too — no local flag), and the grouped
# greeting bubble renders its "\n" line breaks because .turn-text is
# pre-wrap (the SSE/parser round-trip already joins data: lines with \n).
def test_first_run_never_opens_the_pane_and_chat_stays_enabled() -> None:
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    # the retired episode machinery is gone ENTIRELY (not just re-gated):
    for dead in ("settingsAutoShown", "firstRunBeat", "revealFirstRunBeat"):
        assert dead not in raw, f"app.js still carries {dead!r}"
    # the watch-key gate neither disables chat nor touches the pane:
    gate = code[code.index("function applyWatchKeyGate"):
                code.index("function dismissWatchKeyForm")]
    assert "openSettings" not in gate and "closeSettings" not in gate
    assert "inputEl.disabled" not in gate and "sendBtn.disabled" not in gate
    # placeholder flips ON the typed needs (restore rides the same flag):
    assert (
        "inputEl.placeholder = needs ? LABELS.chatNeedsKeyPlaceholder : chatPlaceholder;"
        in gate
    )
    # openSettings has exactly three occurrences: definition + the two
    # EXPLICIT controls (the settings quick action + the header toggle).
    assert code.count("openSettings()") == 3
    # the needs-key ask copy (engine beats own the rest of the prose):
    assert 'chatNeedsKeyPlaceholder: "Paste your xpub or zpub to get started…"' in code
    # the normal placeholder lives in the markup (app.js reads it once):
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'placeholder="Ask your wallet…"' in index_html
    assert "const chatPlaceholder = inputEl.placeholder;" in code
    # multi-line bubbles: the transcript paints \n as breaks (pre-wrap).
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    turn_text = styles[styles.index(".turn-text {"):]
    turn_text = turn_text[: turn_text.index("}")]
    assert "white-space: pre-wrap" in turn_text


# TCK-ONB-007 static half, behavioral (node if present): the SHIPPED
# applyWatchKeyGate runs against DOM stubs — a needs_watch_key snapshot
# leaves chat ENABLED with the key placeholder, opens NOTHING, and the next
# provisioned snapshot restores the normal placeholder (typed truth, no
# local flag).
def test_watch_key_gate_keeps_chat_open_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    gate = re.search(
        r"function applyWatchKeyGate\(snap\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    script = """
      const LABELS = { chatNeedsKeyPlaceholder: "Paste your xpub or zpub to get started…" };
      const chatPlaceholder = "Ask your wallet…";
      const state = { watchKeyDismissed: false, watchKeyPresent: null, watchKeyNeeded: false };
      const inputEl = { disabled: false, placeholder: chatPlaceholder };
      const sendBtn = { disabled: false };
      const quickbarEl = { hidden: true };
      const settingsPanelEl = { hidden: true }; // stays closed: NOTHING may open it
      let openCalls = 0, closeCalls = 0, renders = 0;
      const openSettings = () => { settingsPanelEl.hidden = false; openCalls++; };
      const closeSettings = () => { closeCalls++; };
      const renderSettings = () => { renders++; };
      const focusWatchInput = () => {};
      __GATE__
      applyWatchKeyGate({ schema: "state/1", needs_watch_key: true });
      if (inputEl.disabled || sendBtn.disabled) throw new Error("chat-disabled");
      if (inputEl.placeholder !== LABELS.chatNeedsKeyPlaceholder) throw new Error("placeholder");
      if (openCalls || closeCalls || !settingsPanelEl.hidden) throw new Error("pane-touched");
      if (state.watchKeyNeeded !== true) throw new Error("needed-not-tracked");
      applyWatchKeyGate({ schema: "state/1", needs_watch_key: false });
      if (inputEl.placeholder !== chatPlaceholder) throw new Error("placeholder-restore");
      if (openCalls || closeCalls) throw new Error("pane-touched-2");
      console.log("ok");
    """.replace("__GATE__", gate)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-UX-010 static pin: the persistent privacy chip renders ONLY the closed
# privacy_mode enum NAMES (unknown/absent → hidden), styles it via the
# data-privacy attribute (CSP-clean: classes/attrs, never inline styles), and
# never writes the raw enum as visible text (the word shown is the static
# "Privacy notice" node in index.html).
def test_privacy_chip_is_enum_gated_and_never_paints_raw_enum() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "applyPrivacyChip" in code and "privacy_mode" in code
    assert "PRIVACY_SUBLINE" in code
    assert 'dataset.privacy = mode' in code  # color rides the attribute, not inline style
    assert "privacyChipEl.textContent" not in code  # visible text stays static markup
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="privacy-chip"' in index_html
    assert "Privacy notice" in index_html
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    # TCK-WEB-023: the FIVE-name closed enum — every /state privacy_mode
    # NAME has a shipped color rule (an unmapped name would HIDE the chip:
    # the client map + CSS must land together for a new enum member).
    for name in (
        "public",
        "own_node_local",
        "own_node_private",
        "own_node_remote",
        "awaiting_backend",
    ):
        assert f'data-privacy="{name}"' in styles


# TCK-WEB-010 static pins: per-bubble copy control. The copyable text of a
# turn excludes transient telemetry (progress dots + model-download bar); an
# empty / progress-only bubble gets no button (addCopyButton early-returns on
# the .copy-btn idempotence check OR an empty bubbleText); exactly ONE button
# class assignment exists, shared by every call site.
def test_copy_button_selector_guard_and_single_class_assignment() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # copy-text selector excludes progress + model-download telemetry verbatim.
    assert ".turn-text:not(.turn-progress):not(.turn-model)" in code
    # empty-bubble guard sits alongside the wiring, and the .copy-btn early
    # return makes addCopyButton idempotent (no double buttons on re-render).
    assert "!bubbleText(turn)" in code
    assert 'turn.querySelector(".copy-btn")' in code
    # exactly ONE button class assignment, shared by every call site.
    assert code.count('btn.className = "copy-btn"') == 1
    # real call sites today: appendText, appendSystem, appendUser (3).
    assert code.count("addCopyButton(turn);") == 3


# TCK-WEB-011 static pin: the engine's user_text echo is deduped against this
# tab's locally-echoed pending submits (renderUserText), and that branch rides
# the SAME handleEvent AFTER the event-id replay-duplicate guard — so a
# replayed echo of an already-consumed pending is dropped before renderUserText
# ever runs (ordering pinned by source index).
def test_render_user_text_dedupes_after_event_id_guard() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "renderUserText" in code
    assert "state.pendingEchos" in code
    guard = code.index("id <= state.lastEventId")  # replay duplicate guard
    user_text = code.index('kind === "user_text"')  # the dedupe branch
    assert guard < user_text


# TCK-WEB-012/013 static pins: the privacy subline is VISIBLE text painted onto
# privacySublineEl (per privacy_mode NAME) while the raw enum name is never the
# chip's visible text; the chain-row trust badge derives ONLY from the
# state.privacyMode closed enum — never from the effective chain URL content.
def test_privacy_subline_visible_and_trust_badge_keys_off_privacy_mode() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # visible subline: the closed enum NAME maps to prose on privacySublineEl.
    assert "privacySublineEl.textContent" in code
    # the raw privacy enum is never painted as the chip's visible text.
    assert "privacyChipEl.textContent" not in code
    # the chain-row trust badge derives ONLY from the privacyMode state.
    assert "TRUST_BADGE_WORDS[state.privacyMode]" in code
    # ...and no trust decision reads the effective chain URL content.
    trust_block = code[code.index("const TRUST_BADGE_WORDS"):code.index("function paintTrustBadges")]
    assert "effectiveChainUrl" not in trust_block


# TCK-DESCOPE-M3B (user direction 2026-09-11) ELIMINATED the kind badges;
# the TCK-WEB-023 AMENDMENT (user MW-17 direction 2026-09-13) re-admits
# EXACTLY electrum & bitcoind as pills tinted by the shared privacy_mode
# classification — MEMPOOL STAYS GONE, and the whole old machinery stays
# dead: no legend, no dim/lit trust tiers, no family mapping, no old class
# names. The TRUST badge (privacy_mode) is untouched.
def test_kind_badges_return_amendment_shaped_mempool_stays_gone() -> None:
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    # the old shape is still fully gone (only the new minimal painter exists):
    for gone in (
        "BADGE_FAMILIES",
        "paintBackendBadges(",
        "applyBackendKind(",
        "badgeMempool",
        "badgeElectrum",
        "badgeBitcoind",
        "badgeLegend",
        "badgeInUse",
        "badgeIdle",
        "settingsEmptyApplied",
        "backendKind",
        "backend-badge",
        "mempool",
        "esplora",
    ):
        assert gone not in code, gone
    # the amendment painter: word map keyed by the CLOSED engine enum MINUS
    # none (none = no pill), tint map keyed by the four RESOLVED modes
    # (awaiting_backend absent = no pill), pure planner shipped.
    words = code[code.index("const KIND_PILL_WORDS"):code.index("const KIND_PILL_TINTS")]
    assert "electrum:" in words and "bitcoind:" in words and "none" not in words
    tints = code[code.index("const KIND_PILL_TINTS"):code.index("function kindPillPaint")]
    for mode in ("own_node_local", "own_node_private", "public", "own_node_remote"):
        assert mode in tints
    assert "awaiting_backend" not in tints
    assert "kind-pill-private" in tints and "kind-pill-public" in tints
    assert tints.count("kind-pill-private") == 2  # the two GREEN names only
    assert tints.count("kind-pill-public") == 2
    # the pill is consumed ONLY where the status zone builds it, beside the
    # trust badge, and re-painted in the SAME trust pass (one truth/paint):
    assert "const pill = kindPill();" in code
    assert "kindPillPaint(state.backendName, state.privacyMode)" in code
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    assert ".backend-badge" not in css
    assert ".kind-pill" in css
    # no coverage loss on the trust badge: word map + painter still shipped.
    assert "const TRUST_BADGE_WORDS" in code
    assert "function paintTrustBadges" in code
    assert ".trust-badge" in css


# ============================================================== TCK-WEB-021
# Settings-pane "stars" rework (UX council 2026-09-11, arbitrated; de-scope
# redirection: NO kind badges — the trust badge is the pane's only badge).
# All textContent-only / CSP-safe (the global sink scan covers the new
# builders); every rule below is a browser-free source pin unless a node
# behavioral check is named.

# (1) HUMAN LABELS: the closed key→word map with the four mandated words;
# unknown keys fall back to the raw key (never a wrong guess); every row
# label goes through settingLabel — the snake_case headings are gone from
# the builders. Section titles are promoted in the stylesheet (bigger step,
# primary ink — no longer a muted caption).
def test_settings_rows_carry_human_labels_not_snake_case_keys() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    assert "const SETTING_LABELS = {" in code
    for word in (
        'watch_key: "Public account key",',
        'chain_base_url: "Server address",',
        'gap_limit: "Gap limit",',
        'display_currency: "Display currency",',
    ):
        assert word in code, word
    # raw-key fallback for unknown keys (the map discriminates by OWN
    # property only — an inherited key can never fabricate a label):
    assert "function settingLabel(key)" in code
    assert "Object.prototype.hasOwnProperty.call(SETTING_LABELS, key)" in code
    assert ": key;" in code  # fallback returns the RAW key
    # every pane label paints through settingLabel; no snake_case literal
    # row headings survive in the builders:
    assert code.count("settingLabel(") >= 4  # def-excluding uses: rows + watch
    assert 'el("label", "setting-key", entry.key)' not in code
    assert 'el("p", "setting-key", "watch_key")' not in code
    # section-title promotion: the pane's zone heading out-weights rows.
    heading = css[css.index(".setting-heading {") :]
    heading = heading [: heading.index("}")]
    assert "var(--fs-lg)" in heading and "var(--c-text)" in heading


# (2) WEIGHT: the configured wallet line and the "Now using: <url>" line —
# the pane's two heaviest facts — carry real size/ink in the stylesheet,
# and the builders place the URL line in the status zone (app.js side is
# pinned by the zone test below).
def test_wallet_and_now_using_lines_carry_real_weight() -> None:
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    for selector in (".chain-now {", ".watchkey-value {"):
        block = css[css.index(selector):]
        block = block[: block.index("}")]
        assert "var(--fs-md)" in block, selector  # the body step, not --fs-xs
        assert "var(--c-text)" in block, selector  # primary ink, not muted
        assert "font-weight" in block, selector


# (3) ZONES in the server card + the collapse discipline: status zone
# (URL + trust badge; the slot WEB-023's kind pills join), act zone (field
# + Apply/Cancel + the empty .chain-chips slot), rest zone (explanatory
# prose behind a NATIVE <details>). Resync and the gap row stay VISIBLE —
# built before/outside the rest zone. Entry form (7): ONE reassurance line
# visible; the lecture collapses into its own <details>.
def test_server_card_zones_and_collapse_discipline() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    chain = code[code.index("function chainBaseRow") : code.index("function backendCredFlags")]
    # three zones + the WEB-022 chip slot:
    for zone in ('"chain-status"', '"chain-act"', '"setting-details chain-rest"', '"chain-chips"'):
        assert zone in chain, zone
    # rest zone = native <details> + <summary> (no JS toggle, no aria fake):
    assert 'el("details", "setting-details chain-rest")' in chain
    assert 'el("summary", "setting-details-summary"' in chain
    # the prose rides the collapsed zone, the recovery path does NOT:
    assert 'restZone.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault))' in chain
    assert "LABELS.chainEnvOverride" in chain[chain.index("restZone"):]
    assert "LABELS.settingsRestart" in chain[chain.index("restZone"):]
    assert chain.index("li.appendChild(resyncLine)") < chain.index('el("details"')
    assert 'li.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault))' not in chain
    # trust badge joins the STATUS zone (and WEB-023's pills would too):
    assert "statusZone.appendChild(nowLine)" in chain
    # gap_limit is its own GENERIC row outside the chain builder entirely:
    render = code[code.index("function renderSettings") : code.index("function settingsCol")]
    assert "settingRow(gap" in render and "chainBaseRow" in render
    # (7) entry form: visible label + input + Connect + ONE reassurance
    # line; the lede and the warning collapse behind a native <details>.
    watch = code[code.index("function watchKeyRow") : code.index("function watchKeyInput")]
    assert "label.htmlFor = inputId;" in watch  # (6): a VISIBLE label names the input
    assert "el(\"p\", \"setting-hint\", LABELS.watchkeyReassure)" in watch
    assert 'el("details", "setting-details")' in watch
    assert 'el("summary", "setting-details-summary", LABELS.watchkeyFindSummary)' in watch
    assert code.count('el("details"') == 2  # exactly the two collapse slots


# (4) ONE COLOR VOCABULARY: the trust dimension owns the risk colors, and
# the TCK-WEB-023 kind pill takes its tint ONLY as a DERIVED class from that
# same closed classification (base neutral pill + tint class — no third
# palette, and the risk tokens never reach any other selector).
def test_color_vocabulary_contract_and_kind_pill_tint_derivation() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    assert "ONE COLOR VOCABULARY" in css  # the contract block is present
    # the pill rule exists (amendment), built NEUTRAL first: the base class
    # carries muted ink, the tint classes carry ONLY --c-priv-* pairs.
    base = re.search(r"\.kind-pill \{[^}]*\}", css).group(0)
    assert "--c-text-muted" in base and "--c-priv-" not in base
    for tint, token in (
        ("kind-pill-private", "--c-priv-local"),
        ("kind-pill-public", "--c-priv-trust"),
    ):
        rule = re.search(rf"\.{tint} \{{[^}}]*\}}", css).group(0)
        assert token in rule
    # no client-side color logic: app.js picks a CLASS NAME from the closed
    # tint map; --c-priv-* tokens never appear in the client, and NO trust
    # or tint decision reads the effective chain URL / any URL content.
    assert "--c-priv-" not in code
    trust_block = code[code.index("const PRIVACY_SUBLINE"):code.index("function refreshState")]
    assert "effectiveChainUrl" not in trust_block
    assert "href" not in trust_block
    # risk tokens stay exclusively on the trust dimension (chip + badge):
    priv_rules = re.findall(
        r"^\.([a-z-]+)\[data-privacy", css, flags=re.MULTILINE
    )
    assert set(priv_rules) == {"privacy", "trust-badge"}


# TCK-WEB-023 FIVE-ENUM MATRIX (static): every /state privacy_mode NAME maps
# to a chip color and (where resolved) a subline + trust word; the TWO GREEN
# names are exactly {own_node_local, own_node_private} and share the SAME
# CSS rule (one classification, two names); YELLOW stays with remote/awaiting
# and public keeps its existing danger-red chip. The two host-bearing
# sublines carry the council copy verbatim; own_node_private's green copy
# keeps the glm trust HEDGE ("run this server yourself") — the color claims
# private-network, the words never do.
def test_web023_five_mode_matrix_maps_and_copy() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    sublines = code[code.index("const PRIVACY_SUBLINE"):code.index("const PRIVACY_HOST_TEMPLATES")]
    for mode in ("public", "own_node_local", "own_node_private",
                 "own_node_remote", "awaiting_backend"):
        assert mode in sublines, mode
    # unknown names still hide (the hasOwnProperty gate on the SHIPPED map):
    assert 'hasOwnProperty.call(PRIVACY_SUBLINE, mode)' in code
    words = code[code.index("const TRUST_BADGE_WORDS"):code.index("const KIND_PILL_WORDS")]
    for mode in ("public", "own_node_local", "own_node_private",
                 "own_node_remote", "awaiting_backend"):
        assert mode in words, mode
    # the council-folded copy, verbatim:
    assert "Your node at {host} — private only if you trust it." in raw
    assert "Your node at {host} — only private if you run this server yourself." in raw
    assert "Your node — only private if you run this server yourself." in raw
    # unchanged sublines keep their pre-WEB-023 sentences (public/local/
    # awaiting — the ticket pins them):
    assert "Public explorer — the operator can associate queried addresses with your IP." in raw
    assert "Your node on this machine — lookups stay here." in raw
    assert "No backend chosen yet." in raw
    # the host arrives ONLY through the typed wire key (no sniffing source):
    assert "snap.backend_host" in code
    assert "BARE_HOST_RE" in code  # every host passes the bare-host gate
    # CSS: the GREEN rule lists BOTH green names (and only them) and uses
    # the green token pair; own_node_remote + awaiting share the yellow.
    green = re.search(
        r"\.privacy\[data-privacy=\"own_node_local\"\],[^{]*\{[^}]*\}", css
    ).group(0)
    assert green.count("data-privacy") == 4  # chip+badge × the two GREEN names
    assert 'data-privacy="own_node_private"' in green and "--c-priv-local" in green
    yellow = re.search(
        r"\.privacy\[data-privacy=\"own_node_remote\"\],[^{]*\{[^}]*\}", css
    ).group(0)
    assert 'data-privacy="awaiting_backend"' in yellow and "--c-priv-trust" in yellow
    red = re.search(r"\.privacy\[data-privacy=\"public\"\][^{]*\{[^}]*\}", css).group(0)
    assert "--c-priv-public" in red  # public chip: existing danger-red, unchanged


# (5) STALE NOW-LINE: the reload trigger is PINNED — once per /state
# snapshot flip of backend_kind or privacy_mode, typed snapshots only,
# evaluated AFTER both halves carry this snapshot's truth, never while the
# pane is closed (opening fetches fresh).
def test_settings_reload_is_pinned_to_the_trust_flip() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    flip = code[code.index("function noteTrustFlip") : code.index("function paintSettingsDot")]
    assert 'const sig = state.backendName + "|" + state.privacyMode;' in flip
    # one reload per FLIP, open pane only, and the baseline never fires:
    assert "state.trustSig !== null && sig !== state.trustSig && !settingsPanelEl.hidden" in flip
    assert "loadSettings();" in flip
    assert "state.trustSig = sig;" in flip  # re-baseline on EVERY typed pass
    # called from applyState AFTER applyPrivacyChip (both halves updated),
    # and gated on the TYPED flag (state/0 carries no new signature):
    apply = code[code.index("function applyState") : code.index("function applyModelPrompt")]
    assert "noteTrustFlip(typed);" in apply
    assert apply.index("applyPrivacyChip(snap);") < apply.index("noteTrustFlip(typed);")
    assert 'typeof snap.backend_kind === "string"' in apply  # consumed READ-ONLY


def test_settings_reload_fires_once_per_flip_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    flip = re.search(
        r"function noteTrustFlip\(typed\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    script = """
      const state = { backendName: "", privacyMode: "", trustSig: null };
      const settingsPanelEl = { hidden: false };
      let loads = 0;
      const loadSettings = () => { loads++; };
      __FLIP__
      // the FIRST typed snapshot only lays the baseline down (opening fetched):
      noteTrustFlip(true);
      noteTrustFlip(true);
      if (loads !== 0) throw new Error("baseline-fired");
      // an identical snapshot never reloads:
      state.backendName = "electrum"; state.privacyMode = "public";
      noteTrustFlip(true);
      noteTrustFlip(true);
      if (loads !== 1) throw new Error("not-once");
      // a privacy_mode flip reloads exactly once:
      state.privacyMode = "own_node_local";
      noteTrustFlip(true);
      if (loads !== 2) throw new Error("mode-flip");
      // a backend_kind flip reloads exactly once too:
      state.backendName = "bitcoind";
      noteTrustFlip(true);
      if (loads !== 3) throw new Error("kind-flip");
      // untyped (state/0) replies never reload nor re-baseline:
      noteTrustFlip(false);
      if (loads !== 3) throw new Error("untyped-fired");
      // a flip while the pane is CLOSED re-baselines silently (open fetches):
      settingsPanelEl.hidden = true;
      state.privacyMode = "public";
      noteTrustFlip(true);
      if (loads !== 3) throw new Error("closed-fired");
      settingsPanelEl.hidden = false;
      noteTrustFlip(true); // sig unchanged since baseline: stays quiet
      if (loads !== 3) throw new Error("closed-rebaseline-missed");
      console.log("ok");
    """.replace("__FLIP__", flip)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# (6) A11Y: the visible zpub <label> is pinned above; here — the wait word
# (the chain row names the seconds-class PROBE, generic rows keep the old
# word), the refocus seam on every Cancel/Escape rebuild, and the QR/pane
# Escape interplay (QR is topmost: it consumes the press, the settings
# handler also returns early — double-guarded).
def test_a11y_wait_word_refocus_and_escape_layering() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    # 'Checking the server…' rides ONLY the chain write (the probe):
    assert 'isChain ? LABELS.settingsChecking : LABELS.settingsSaving' in code
    # the entry input's NAME is the visible label now (no aria duplicate):
    watch_input = code[code.index("function watchKeyInput") :]
    watch_input = watch_input[: watch_input.index("\n}")]
    assert "aria-label" not in watch_input
    # refocus: the helper exists and rides BOTH cancel buttons and BOTH
    # Escape-rebuild branches (4 call sites):
    assert "function refocusRowControl(key)" in code
    assert code.count("refocusRowControl(") == 5  # def + 4 rebuild seams
    assert "(target || settingsHeadingEl).focus();" in code  # never <body>
    # Escape layering: the QR listener consumes the press while open...
    assert "event.stopImmediatePropagation();" in code
    # ...and the settings handler returns early on the same condition:
    esc = code[code.index('if (event.key !== "Escape"') :]
    esc = esc[: esc.index("});")]
    assert "if (!qrViewerEl.hidden) return;" in esc
    # the dot rides a data-attribute styled in the stylesheet (no inline
    # style anywhere — the global pin covers it; this names the pair):
    assert 'settingsToggleEl.dataset.needsSetup = unfinished ? "1" : "";' in code
    assert '#settings-toggle[data-needs-setup="1"]::after' in css


def test_escape_layering_qr_consumes_before_the_pane_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    qr_handler = re.search(
        r'document\.addEventListener\("keydown", \(event\) => \{\s*\n\s*if \(event\.key === "Escape"',
        code,
    )
    assert qr_handler is not None
    qr_listener = code[qr_handler.start() :]
    qr_listener = qr_listener[: qr_listener.index("});") + 3]
    pane_listener = code[code.index('document.addEventListener("keydown", (event) => {\n  if (event.key !== "Escape"') :]
    pane_listener = pane_listener[: pane_listener.index("});") + 3]
    script = """
      // shipped registration ORDER (QR first) on one shared node:
      const listeners = [];
      globalThis.document = {
        addEventListener: (_name, fn) => listeners.push(fn),
      };
      const qrViewerEl = { hidden: false }; // QR dialog OPEN (over the pane)
      const settingsPanelEl = {
        hidden: false,
        querySelector: () => null, // nothing mid-edit in this scenario
      };
      const state = { watchKeyReplaceOpen: false };
      let qrCloses = 0, paneCloses = 0, rebuilds = 0, refocuses = [];
      const closeQr = () => { qrCloses++; qrViewerEl.hidden = true; };
      const closeSettings = () => { paneCloses++; settingsPanelEl.hidden = true; };
      const renderSettings = () => { rebuilds++; };
      const refocusRowControl = (key) => { refocuses.push(key); };
      __QR__
      __PANE__
      const press = () => {
        const event = {
          key: "Escape",
          prevented: false,
          preventDefault() { this.prevented = true; },
          stopImmediatePropagation() { this._stopped = true; },
        };
        // same-node semantics: stopImmediatePropagation skips the REST.
        for (const fn of listeners) {
          fn(event);
          if (event._stopped) break;
        }
        return event;
      };
      // 1. QR + pane both open: ONE press closes ONLY the QR.
      press();
      if (qrCloses !== 1 || paneCloses !== 0 || rebuilds !== 0)
        throw new Error("qr-did-not-consume");
      if (settingsPanelEl.hidden) throw new Error("pane-hidden");
      // 2. pane open, QR closed, replace form open: Escape CANCELS the form
      //    (rebuild + refocus) and does NOT close the pane.
      state.watchKeyReplaceOpen = true;
      press();
      if (paneCloses !== 0 || rebuilds !== 1 || refocuses[0] !== "watch_key")
        throw new Error("escape-cancel");
      if (state.watchKeyReplaceOpen !== false) throw new Error("cancel-state");
      // 3. pane open, nothing edited: the NEXT Escape closes the pane.
      press();
      if (paneCloses !== 1) throw new Error("escape-close");
      // 4. pane closed: Escape is nobody's business (early return).
      press();
      if (qrCloses !== 1 || paneCloses !== 1 || rebuilds !== 1)
        throw new Error("quiet-when-closed");
      console.log("ok");
    """
    script = script.replace("__QR__", qr_listener).replace("__PANE__", pane_listener)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# (7) is pinned inside the zone test (the entry form). (8) SETTINGS-DOOR
# DOT: lit from typed truth only — wallet key needed (tracked by
# applyWatchKeyGate) or backend unresolved; never on an unknown mode.
def test_settings_door_dot_rides_typed_truth_only() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    dot = re.search(
        r"function paintSettingsDot\(\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert 'state.watchKeyNeeded === true || state.privacyMode === "awaiting_backend"' in dot
    # no third condition, no guess: an empty privacyMode never lights it.
    assert dot.count("||") == 1 and "&&" not in dot
    apply = code[code.index("function applyState") : code.index("function applyModelPrompt")]
    assert "paintSettingsDot();" in apply  # every snapshot repaints from state


# (9) 320px: the row lines WRAP (no horizontal scroll, no clipped control);
# the buttons keep their word-sized boxes.
def test_phone_width_lines_wrap() -> None:
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    for selector in (".setting-line {", ".watchkey-line {", ".chain-now-line {"):
        block = css[css.index(selector):]
        block = block[: block.index("}")]
        assert "flex-wrap: wrap;" in block, selector
    assert ".setting-line > .btn { flex: none; }" in css
    assert ".watchkey-line > .btn { flex: none; }" in css


# (10) REJECTION LINES: every rejection rendering carries the SAME static
# value-free next-step suffix (the dynamic part stays the server's own
# reason; the suffix never varies and never echoes a value).
def test_rejection_lines_end_in_a_static_next_step() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert 'settingsRejectNext: " — check the value and apply again.",' in code
    assert 'watchkeyRejectNext: " — check the key and try again.",' in code
    # settings rejections (delegated Apply ternary: 2 branches) + creds clear:
    assert code.count("LABELS.settingsRejectNext") == 3
    # watchkey rejections (entry/replace submit + replaceStage apply rung):
    assert code.count("LABELS.watchkeyRejectNext") == 2
    # the suffix joins ONLY the error branches — busy/stale/transport lines
    # keep their own (already prescriptive) sentences:
    assert 'LABELS.settingsBusy + LABELS.settingsRejectNext' not in code
    assert 'LABELS.sessionStale + LABELS' not in code


# TCK-LINK-001 (revised by TCK-WEB-014, critique D8) static pins: the
# qualifying-token scan is unchanged, but the affordance is a COPY BUTTON —
# the visible label is the verbatim token, no navigation machinery survives
# (no EXPLORER_ORIGIN, no explorerHref, no href/target/rel, no mempool
# disclosure string), and the feedback reuses the shared WEB-010 ok/fail
# pattern. Classes/attributes only (no inline style on the generated control).
def test_linkify_client_shape_pins() -> None:
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    # regexes unchanged: only the navigation died, not the scanner
    assert "const ADDRESS_RE = " in raw
    assert "const TXID_RE = " in raw
    assert "const LINK_SCAN_RE =" in raw  # (its literal wraps to the next line)
    # D8: the dead explorer machinery and its privacy disclosure are gone
    assert "EXPLORER_ORIGIN" not in raw
    assert "explorerHref" not in raw
    assert "explorerLink" not in raw
    assert "Opens mempool.space" not in raw
    # no navigation of any kind: an <a> is never built, href/target/rel absent
    assert 'createElement("a")' not in code
    assert "href" not in code
    assert "_blank" not in code and "noopener" not in code
    # copy affordance: button with the verbatim token as its only content
    assert 'btn.className = "explorer-link"' in code
    assert "btn.textContent = token;" in code
    assert "clipboardWrite(token)" in code
    assert "navigator.clipboard.writeText" in code
    assert '"Click to copy"' in raw
    # WEB-010 ok/fail feedback pattern shared: one helper, three call sites —
    # the token button, the bubble button, and (TCK-WEB-027) the header
    # wallet-fingerprint chip. Any NEW copy affordance must ride the helper,
    # never re-implement it — the count going up with a new flashCopyResult
    # CALLER is the pin doing its job; an unshared copy path is the failure.
    assert code.count("flashCopyResult(") == 4
    assert code.count("1600") == 1  # the revert window lives in the helper only
    assert ".style" not in code and "setAttribute(\"style\"" not in code
    # only the two bubble painters call the token pass (appendText,
    # appendUser); progress + model lines keep plain createTextNode telemetry.
    assert code.count("appendBubbleText(line, text);") == 2
    assert 'el("p", "turn-text turn-progress")' in code  # progress line intact


# TCK-PRIVACY-001B static pins: the ONE web trigger of public-backend consent
# is the chain row's "Use public server" button — exactly one fetch path to
# /consent, reached only by that button; the pane-close path fires NO request
# (closing ≠ consent, the client half); the button's visibility rides ONLY the
# typed /state privacy_mode closed enum (no client-side inference), repainted
# by the same chip pass as the trust badges; and the disclosure reuses the
# pane's own leak sentence verbatim (one voice, no invented copy).
def test_consent_button_is_the_only_consent_path_and_state_gated() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert code.count('fetch("/consent"') == 1  # one handler, no bypass routes
    assert code.count("requestPublicConsent(") == 2  # definition + button wiring
    close = code[code.index("function closeSettings"):
                 code.index("settingsToggleEl.addEventListener")]
    assert "fetch(" not in close  # pane close sends NOTHING: never an implied consent
    # visibility: built gated on the enum, retired by the chip's own pass.
    assert 'consent.hidden = state.privacyMode !== "awaiting_backend";' in code
    painter = code[code.index("function paintConsentRow"):
                    code.index("function applyPrivacyChip")]
    assert 'state.privacyMode === "awaiting_backend"' in painter
    chip = code[code.index("function applyPrivacyChip"):
                 code.index("async function refreshState")]
    assert "paintConsentRow();" in chip
    # honest copy: the button subline IS the pane's leak sentence, and —
    # TCK-DESCOPE-M3B — it names the CONSENTED public Electrum server, never
    # mempool.space (which is public fee/price info, not a wallet backend).
    assert 'consentSubline:' in code
    assert '"The public Electrum server electrum.blockstream.info — "' in code
    assert "PUBLIC_LEAK_SENTENCE," in code
    assert "The public mempool.space server" not in code


# TCK-LINK-001 regexes + TCK-WEB-014 behavior + TCK-WEB-026 upgrade (runs
# under node if present): the SHIPPED scanner regexes are extracted and fed
# the same accept/reject vectors (unchanged), and the SHIPPED
# copyTokenButton/clipboardWrite/flashCopyResult run against DOM stubs: the
# control is a <button> with the verbatim token as its only content and NO
# navigation attributes, its accessible NAME carries the whole value ("Copy
# address bc1q…"), a click writes the verbatim token to the clipboard, lands
# in the ok state (title AND aria-label swapped, shared live region
# announced), reverts after the 1.6s window, and a rejected write lands in
# the visible fail state — which HOLDS (no revert timer on fail).
def test_linkify_regexes_and_click_to_copy_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    address_re = re.search(r"const ADDRESS_RE = (/[^;]+);", code).group(1)
    txid_re = re.search(r"const TXID_RE = (/[^;]+);", code).group(1)
    scan_re = re.search(r"const LINK_SCAN_RE =\s*\n?\s*([^;]+);", code).group(1)
    copy_fn = re.search(
        r"function copyTokenButton\(token\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    clip_fn = re.search(
        r"async function clipboardWrite\(text\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    flash_fn = re.search(
        r"function flashCopyResult\(ctrl, ok, baseTitle, baseAria\) \{.*?\n\}",
        code, re.DOTALL,
    ).group(0)
    script = """
      const ADDRESS_RE = __ADDR_RE__;
      const TXID_RE = __TX_RE__;
      const LINK_SCAN_RE = __SCAN_RE__;
      function linkify(text) {
        const out = [];
        LINK_SCAN_RE.lastIndex = 0;
        let m;
        while ((m = LINK_SCAN_RE.exec(text)) !== null) out.push(m[0]);
        return out;
      }
      const addr = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq";
      const tx = "f".repeat(64);
      // accepted: standalone tokens only — the scan yields the token verbatim
      let a = linkify("send to " + addr + " now");
      if (a.length !== 1 || a[0] !== addr) throw new Error("addr");
       a = linkify("tx " + tx + " confirmed");
       if (a.length !== 1 || a[0] !== tx) throw new Error("tx");
       // TCK-TXID-001: the SHIPPED narration sentences (full 64-hex ack +
       // lineage copy) yield the token verbatim...
       a = linkify("Sent! txid " + tx + " — tracking…");
       if (a.length !== 1 || a[0] !== tx) throw new Error("ack-sentence");
       if (linkify("Sent! txid " + tx.slice(0, 12) + "… — tracking…").length)
         throw new Error("truncated-fragment-qualifies");
      // rejected: uppercase, longer-word embedding, oversized, wrong charset
      if (linkify("BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ").length) throw new Error("upper");
      if (linkify("x" + addr).length) throw new Error("embedded-left");
      if (linkify("pre" + tx).length) throw new Error("tx-embedded");
      if (linkify(tx + "f").length) throw new Error("tx-65");
      if (linkify(addr.slice(0, 3) + "!" + addr.slice(4)).length) throw new Error("charset");
      if (linkify("/address/x bc1q").length) throw new Error("fragment");
      if (linkify("x".repeat(100)).length) throw new Error("noise");
      if (linkify("b".repeat(100)).length) throw new Error("oversize");
      // --- the copy control itself, shipped functions on DOM stubs ---
      const LABELS = {
        clickToCopy: "Click to copy", copyDone: "OK",
        copyOk: "COPIED-LIVE", copyFail: "FAILED-LIVE",
        copyAddress: "Copy address", copyTxid: "Copy transaction id",
      };
      const copyStatusEl = { textContent: "" }; // shared live-region stub
      let resetFn = null;
      globalThis.setTimeout = (fn) => { resetFn = fn; return 1; };
      globalThis.clearTimeout = () => { resetFn = null; };
      globalThis.document = { createElement: (tag) => {
        if (tag !== "button") throw new Error("not-a-button: " + tag);
        const classes = new Set();
        return {
          type: "", className: "", textContent: "", title: "",
          setAttribute(name, value) { this[name] = value; },
          addEventListener(_name, fn) { this._click = fn; },
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
          },
        };
      }};
      let copied = null;
      let refuse = false;
      // navigator is a getter-only global on modern node — define, don't assign
      Object.defineProperty(globalThis, "navigator", {
        configurable: true,
        value: { clipboard: { writeText: async (t) => {
          if (refuse) throw new Error("denied");
          copied = t;
        } } },
      });
      __COPY_FN__
      __CLIP_FN__
      __FLASH_FN__
      const main = async () => {
        const btn = copyTokenButton(addr);
        if (btn.type !== "button") throw new Error("no-type");
        if (btn.textContent !== addr) throw new Error("label-not-verbatim");
        // no navigation machinery on the control (it is not a link at all)
        if ("href" in btn || "target" in btn || "rel" in btn) throw new Error("navigation");
        if (btn.title !== LABELS.clickToCopy) throw new Error("title");
        // TCK-WEB-026: the accessible NAME carries the WHOLE value (the old
        // "Click to copy" name erased the token from the SR queue) and the
        // ADDRESS_RE discriminates address vs txid wording.
        if (btn["aria-label"] !== LABELS.copyAddress + " " + addr) throw new Error("aria");
        if (copyTokenButton(tx)["aria-label"] !== LABELS.copyTxid + " " + tx)
          throw new Error("aria-txid");
        await btn._click(); // clipboard receives the VERBATIM token
        if (copied !== addr) throw new Error("clipboard-value");
        if (!btn.classList.contains("copy-ok") || btn.classList.contains("copy-fail"))
          throw new Error("ok-state");
        if (btn.title !== LABELS.copyDone) throw new Error("ok-title");
        if (btn["aria-label"] !== LABELS.copyDone) throw new Error("ok-aria");
        if (copyStatusEl.textContent !== LABELS.copyOk) throw new Error("ok-live");
        resetFn(); // the 1.6s window reverts class + base title + base (value-bearing) aria
        if (btn.classList.contains("copy-ok") || btn.title !== LABELS.clickToCopy)
          throw new Error("revert");
        if (btn["aria-label"] !== LABELS.copyAddress + " " + addr) throw new Error("revert-aria");
        refuse = true; // a rejected write = visible fail state, no silent swallow
        await btn._click();
        // TCK-WEB-026 review fix: the fail CLASS holds (no revert timer) and
        // the live sentence announces it — but the title/aria-label revert to
        // the value-bearing base immediately, so the SR user still hears
        // WHICH token failed. The name never loses the value.
        if (!btn.classList.contains("copy-fail")) throw new Error("fail-state");
        if (btn.title !== LABELS.clickToCopy) throw new Error("fail-title-reverts");
        if (btn["aria-label"] !== LABELS.copyAddress + " " + addr)
          throw new Error("fail-aria-keeps-value");
        if (copyStatusEl.textContent !== LABELS.copyFail) throw new Error("fail-live");
        // the state itself HOLDS: no revert timer is ever registered on fail.
        if (resetFn !== null) throw new Error("fail-class-must-hold");
        console.log("ok");
      };
      main().catch((e) => { console.error(e); process.exit(1); });
    """
    script = (
        script.replace("__ADDR_RE__", address_re)
        .replace("__TX_RE__", txid_re)
        .replace("__SCAN_RE__", scan_re)
        .replace("__COPY_FN__", copy_fn)
        .replace("__CLIP_FN__", clip_fn)
        .replace("__FLASH_FN__", flash_fn)
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-026 static pins: click-to-copy made unambiguous. ONE shared
# visually-hidden role=status live region in the markup (exactly one) driven
# by flashCopyResult through textContent with the value-free LABELS sentences
# (the copied token NEVER enters it); the token button's base aria-label is
# the VALUE-bearing name; the CSS carries the non-color cues (soft
# backgrounds, the ::after words — which live ONLY in the stylesheet, never
# as DOM text, so the lineText/bubbleText verbatim contract stands), the
# manual-fallback user-select, the padding-block tap target, the dotted rest
# underline, and the copy-ok ink de-aliased from the hover accent.
def test_copy_affordance_upgrade_pins() -> None:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    # (1) one shared SR channel: exactly one #copy-status, hidden + polite
    assert html.count('id="copy-status"') == 1
    node = html[html.index('<p id="copy-status"'):]
    node = node[: node.index(">")]
    assert 'class="visually-hidden"' in node
    assert 'role="status"' in node and 'aria-live="polite"' in node
    assert 'getElementById("copy-status")' in code
    # the live write is textContent with the value-free sentences only
    assert 'copyStatusEl.textContent = ok ? LABELS.copyOk : LABELS.copyFail;' in code
    assert 'copyOk: "Copied.",' in code
    assert 'copyFail: "Copy failed — select it and copy manually.",' in code
    # (2) the name carries the whole value; title keeps the hover garnish
    assert 'copyAddress: "Copy address",' in code
    assert 'copyTxid: "Copy transaction id",' in code
    assert "LABELS.copyAddress : LABELS.copyTxid" in code  # ADDRESS_RE discriminates
    assert 'btn.setAttribute("aria-label", name);' in code
    # (4) fail holds the CLASS only: ONE 1600 revert timer, in the ok branch;
    # the fail branch reverts title + value-bearing aria-label immediately.
    assert code.count("1600") == 1
    assert code.count("_copyReset = setTimeout") == 1
    assert 'ctrl.setAttribute("aria-label", baseAria);' in code  # both reverts
    # (5) manual fallback + tap target + de-linked dotted rest underline
    assert "user-select: text;" in css
    assert "padding-block: 0.25rem;" in css
    assert "text-decoration: underline dotted;" in css
    # (3) non-color cues: soft backgrounds from the existing state tokens
    assert "background: var(--c-accent-soft);" in css
    assert "background: var(--c-danger-soft);" in css
    # (8) the ::after words live ONLY in the stylesheet (never DOM text —
    # no JS string builds them; textContent contract untouched)
    assert 'content: "Copied ✓";' in css and 'content: "Copy failed";' in css
    assert "Copied ✓" not in code
    # de-alias: copy-ok ink differs from the hover accent value
    accent = re.search(r"--c-accent: (#[0-9a-f]{6});", css).group(1)
    copy_ok = re.search(r"--c-copy-ok: (#[0-9a-f]{6});", css).group(1)
    assert accent != copy_ok


# TCK-WEB-015 static pins (user direction): the in-flight indicator is a
# TRANSIENT pending bubble IN the transcript — the old below-input busy
# element is gone from markup, client and stylesheet (no id, no ref, no
# selector; the connection-status area survives). The bubble's lifecycle
# hooks are pinned at every seam: both submit paths (shared submit()) and
# the remote user_text echo show it, noteTurnEnd and a failed submit clear
# it under the SAME !busy guard (replace-on-turn_end, queue-shared), and
# every transcript append re-tails the single node (so replay/echo storms
# can never duplicate it — show is guarded and appendChild MOVES). The
# UX-008 animation (keyframes + reduced-motion opt-out) still exists and
# now rides inside the bubble; the bubble itself carries no text nodes
# (transient — never copyable, never persisted).
def test_pending_bubble_lives_in_the_transcript_and_the_old_indicator_is_gone() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    # the removed below-input widget leaves NOTHING behind (comments too):
    assert "turn-busy" not in code + index_html + styles
    assert "busyEl" not in code
    # connection status ≠ turn status: the header conn-status machinery stays.
    assert 'id="conn-status"' in index_html and "function setStatus" in code
    # lifecycle seams: show on submit + remote echo; a failed submit clears
    # behind the !busy (drained-queue) guard; noteTurnEnd clears ONLY with
    # nothing promoted — a promoted queued turn is still in flight and its
    # echo dedupes, so the single bubble must ride on for it (MINOR fix).
    submit_fn = re.search(
        r"async function submit\(path, field, value\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    note_end = re.search(r"function noteTurnEnd\(\) \{.*?\n\}", code, re.DOTALL).group(0)
    render_user = re.search(
        r"function renderUserText\(text\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert "showPendingBubble();" in submit_fn
    assert re.search(r"if \(!state\.busy\) clearPendingBubble\(\);", submit_fn)
    assert "showPendingBubble();" in render_user
    assert re.search(r"if \(!next\) clearPendingBubble\(\);", note_end)
    assert "if (!state.busy) clearPendingBubble();" not in note_end
    # single tail node: guarded show + one shared retailer used by every
    # transcript appender (ensureTurn, appendSystem, appendUser, show).
    assert code.count("if (!state.pendingBubble) {") == 1  # the guarded show
    assert code.count("tailPendingBubble();") == 4
    # the bubble is the dots, not content: no .turn-text line (the copy
    # selectors never see it), aria name is the relocated UX-008 word.
    assert 'el("li", "turn turn-pending")' in code
    assert '"busy-dots"' in code and "LABELS.turnWorking" in code
    assert "turnWorking: " in code and "Working…" in code  # existing word, not new copy
    # transient: the client persists nothing, ever (the bubble included).
    assert "localStorage" not in code and "sessionStorage" not in code
    # the animation survived the move: keyframes + reduced-motion opt-out,
    # plus the bubble's own style rule.
    assert "@keyframes busy-dot" in styles
    assert "prefers-reduced-motion: reduce" in styles
    assert ".turn-pending" in styles


# TCK-WEB-015 behavioral check (node if present): the SHIPPED bubble trio +
# renderUserText + noteTurnEnd run against a moving-child DOM stub — a
# remote user_text echo shows the bubble at the tail, repeated echoes and
# own-echo dedupe never duplicate it, a content append re-tails the single
# node, turn_end with a non-empty queue KEEPS it (the queue shares the one
# bubble), the turn_end PROMOTING the last queued turn keeps it too (the
# promoted turn is in flight — review MINOR), and only the turn_end with
# nothing left to promote removes it.
def test_pending_bubble_lifecycle_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fns = [
        re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", code, re.DOTALL).group(0)
        for name in (
            "showPendingBubble", "clearPendingBubble",
            "tailPendingBubble", "renderUserText", "noteTurnEnd", "closeOpenTurn",
            # TCK-WEB-019: noteTurnEnd now calls announceTurn (the stubbed
            # querySelector() finds no lines, so it stays inert here).
            "announceTurn", "lineText",
        )
    ]
    script = """
      const mkNode = (tag) => ({
        tag, className: "", textContent: "", attrs: {}, children: [], parent: null,
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(n) {
          if (n.parent) {
            const i = n.parent.children.indexOf(n);
            if (i !== -1) n.parent.children.splice(i, 1);
          }
          n.parent = this; this.children.push(n); return n;
        },
        append(...ns) { for (const n of ns) this.appendChild(n); },
        remove() {
          if (!this.parent) return;
          const i = this.parent.children.indexOf(this);
          if (i !== -1) this.parent.children.splice(i, 1);
          this.parent = null;
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        get classList() {
          const self = this;
          const set = () => new Set(self.className.split(/\\s+/).filter(Boolean));
          return {
            add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
            remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
            contains(c) { return set().has(c); },
          };
        },
      });
      globalThis.document = { createElement: (t) => mkNode(t) };
      const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const LABELS = { turnWorking: "Working…" };
      const state = {
        busy: true, openTurn: null, progressLine: null, queue: [],
        pendingEchos: [], pendingBubble: null,
      };
      const transcriptEl = mkNode("ol");
      const appendUser = (text) => { state._echoes = (state._echoes || 0) + 1; return mkNode("li"); };
      const setBusy = (b) => { state.busy = b; };
      const scrollToEnd = () => {};
      const refreshState = () => {};
      const bubbles = () =>
        transcriptEl.children.filter((n) => n.className.includes("turn-pending"));
      __SHOW__
      __CLEAR__
      __TAIL__
      __RENDER__
      __CLOSE__
      __ANN__
      __LINETEXT__
      __END__
      // 1. remote echo (other tab/CLI): the bubble appears at the tail
      renderUserText("hello from the CLI");
      if (bubbles().length !== 1) throw new Error("show-remote");
      // 2. replay storm: repeated echoes never duplicate (guarded show)
      renderUserText("hello from the CLI");
      if (bubbles().length !== 1) throw new Error("no-duplicate");
      // 3. own-echo dedupe: suppressed event adds no bubble, keeps the one
      state.pendingEchos.push("mine");
      renderUserText("mine");
      if (bubbles().length !== 1 || state._echoes !== 2) throw new Error("own-echo");
      // 4. content appended mid-stream re-tails the SINGLE node
      const content = mkNode("li");
      transcriptEl.appendChild(content);
      tailPendingBubble();
      if (bubbles().length !== 1) throw new Error("retail-dup");
      if (transcriptEl.children[transcriptEl.children.length - 1] !== bubbles()[0])
        throw new Error("retail-tail");
      // 5. no text nodes inside (transient, copy-invisible): just the dots
      const b = bubbles()[0];
      if (b.children.some((n) => n.textContent !== "")) throw new Error("bubble-text");
      if (b.attrs["aria-label"] !== LABELS.turnWorking) throw new Error("bubble-name");
      // 6. turn_end with a still-full queue: the shared tail bubble KEEPS
      state.busy = true;
      state.queue.push(mkNode("li"), mkNode("li"));
      state.queue[0].classList.add("turn-queued");
      noteTurnEnd();
      if (bubbles().length !== 1 || !state.busy) throw new Error("queue-keeps-bubble");
      // 7. the LAST queued turn's PROMOTION at turn_end keeps the bubble
      //    too (the review MINOR): busy flips false but the promoted turn is
      //    in flight and its echo dedupes — nothing else would re-show it.
      noteTurnEnd();
      if (bubbles().length !== 1 || state.busy) throw new Error("promoted-keeps-bubble");
      // 8. turn_end with nothing promoted and the queue empty: removed
      //    (replace-on-turn_end, pinned)
      noteTurnEnd();
      if (bubbles().length !== 0 || state.pendingBubble !== null)
        throw new Error("drain-clears");
      // 9. a later submit re-shows a FRESH node (old ref gone)
      showPendingBubble();
      if (bubbles().length !== 1 || bubbles()[0] === b) throw new Error("reshow");
      console.log("ok");
    """
    script = (
        script.replace("__SHOW__", fns[0])
        .replace("__CLEAR__", fns[1])
        .replace("__TAIL__", fns[2])
        .replace("__RENDER__", fns[3])
        .replace("__CLOSE__", fns[5])
        .replace("__ANN__", fns[6])
        .replace("__LINETEXT__", fns[7])
        .replace("__END__", fns[4])
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-024 behavioral check (node if present): background narration can
# never steal a later reply's turn anchor. The SHIPPED client functions
# (ensureTurn/appendUser/appendSystem/appendText/appendProgress/renderUserText/
# noteTurnEnd/closeOpenTurn) run against a moving-child DOM stub across the
# four real server orderings — a non-engine bubble (user/system) closes any
# open engine turn (FIX 2), so the reply always lands in its OWN engine turn
# AFTER the user's bubble, never inside the narration bubble. Plus the
# client-only pin: even an UNCLOSED background turn (the pre-fix engine
# stream) cannot re-anchor the reply.
def test_reply_never_lands_in_the_narration_bubble_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fns = [
        re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", code, re.DOTALL).group(0)
        for name in (
            "closeOpenTurn", "ensureTurn", "appendText", "appendProgress",
            "appendUser", "appendSystem", "noteTurnEnd", "renderUserText",
            "showPendingBubble", "clearPendingBubble", "tailPendingBubble",
            "handleEvent", "announceTurn", "lineText",
        )
    ]
    script = """
      const mkNode = (tag) => ({
        tag, className: "", textContent: "", attrs: {}, children: [], parent: null,
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(n) {
          if (n.parent) {
            const i = n.parent.children.indexOf(n);
            if (i !== -1) n.parent.children.splice(i, 1);
          }
          n.parent = this; this.children.push(n); return n;
        },
        append(...ns) { for (const n of ns) this.appendChild(n); },
        remove() {
          if (!this.parent) return;
          const i = this.parent.children.indexOf(this);
          if (i !== -1) this.parent.children.splice(i, 1);
          this.parent = null;
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        get classList() {
          const self = this;
          const set = () => new Set(self.className.split(/\\s+/).filter(Boolean));
          return {
            add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
            remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
            contains(c) { return set().has(c); },
          };
        },
      });
      globalThis.document = {
        createElement: (t) => mkNode(t),
        createTextNode: (d) => {
          const n = mkNode("#text");
          n.textContent = d;
          n.appendData = (s) => { n.textContent += s; };
          return n;
        },
      };
      const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const LABELS = { turnWorking: "Working…" };
      const hintEl = { hidden: true };
      const scrollToEnd = () => {};
      const refreshState = () => {};
      const addCopyButton = () => {}; // leaf copy control: no ordering effect
      const appendBubbleText = (line, text) => { line.textContent = text; };
      const state = { busy: false, openTurn: null, progressLine: null,
                      downloadLine: null, queue: [], pendingEchos: [],
                      pendingBubble: null, lastEventId: 0 };
      const setBusy = (b) => { state.busy = b; };
      const transcriptEl = mkNode("ol");
      __FNS__
      let id = 0;
      function drive(events) {
        // fresh transcript + pristine state per interleaving
        transcriptEl.children.length = 0;
        state.openTurn = null; state.progressLine = null; state.downloadLine = null;
        state.queue = []; state.pendingEchos = []; state.pendingBubble = null;
        state.lastEventId = 0; state.busy = false;
        for (const [kind, data] of events) handleEvent(++id, kind, data);
      }
      const userIdx = () => transcriptEl.children.findIndex(
        (n) => n.className.includes("turn-user"));
      const hasText = (n, s) => (n.textContent || "").includes(s) ||
        n.children.some((c) => hasText(c, s));
      const replyTurn = () => transcriptEl.children.find(
        (n) => n.className.includes("turn-engine") && hasText(n, "REPLY"));
      const narrationTurn = (t) => transcriptEl.children.find(
        (n) => n.className.includes("turn-engine") && hasText(n, t));
      function check(label) {
        const u = userIdx(), r = replyTurn();
        if (u === -1) throw new Error(label + ": no user bubble");
        if (!r) throw new Error(label + ": no reply turn");
        if (transcriptEl.children.indexOf(r) <= u)
          throw new Error(label + ": reply before user bubble");
        // the reply must NOT share the narration bubble's engine turn
        const nTurn = narrationTurn("SCAN") || narrationTurn("WATCH");
        if (nTurn && r === nTurn) throw new Error(label + ": reply inside narration");
      }
      // A: [scan text][turn_end][user][reply][turn_end]
      drive([["text","SCAN"],["turn_end",""],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("A");
      // B: [user][scan text][turn_end][reply][turn_end]
      drive([["user_text","u"],["text","SCAN"],["turn_end",""],
             ["text","REPLY"],["turn_end",""]]);
      check("B");
      // C: the REAL rescan stream — dots then summary then user then reply.
      //    (FAILS pre-fix: the summary opened a turn the reply then joined.)
      drive([["progress","."],["progress","."],["progress","."],
             ["progress","\\n"],["text","SCAN"],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("C");
      // D: [watch text][turn_end][user][reply][turn_end]
      drive([["text","WATCH"],["turn_end",""],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("D");
      // client-only pin: an UNCLOSED background turn (no turn_end marker)
      // still renders the reply below the user bubble, not inside it.
      drive([["text","SCAN"],["user_text","u"],["text","REPLY"]]);
      check("unclosed");
      console.log("ok");
    """
    script = script.replace("__FNS__", "\n".join(fns))
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-016 + TCK-WEB-028(2) behavioral check (node if present): the SHIPPED
# submit() and the form listener are extracted from app.js and run against a
# stubbed fetch — a 401 (stale-tab token from a previous launch) renders the
# session-stale/reload line, NOT "server unreachable"; a TRANSPORT failure
# (fetch throws, status 0) keeps the unreachable sentence; any OTHER non-ok
# code PROVES the server was reached and renders the refusal sentence — the
# server's own value-free ``error`` when the body carries one, the plain
# sentence otherwise. A stopped tab shows the stale line instead of swallowing
# the press, and every failed POST re-reads snapshot truth (the WEB-028(5)
# button-restore seam).
def test_submit_401_and_stopped_tab_label_the_stale_session_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    submit_fn = re.search(
        r"async function submit\(path, field, value\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    form_listener = re.search(
        r'formEl\.addEventListener\("submit", \(event\) => \{.*?\n\}\);',
        code,
        re.DOTALL,
    ).group(0)
    script = """
      const LABELS = {
        unreachable: "UNREACHABLE-LINE", sessionStale: "STALE-LINE",
        turnRejected: "REJECTED-LINE", turnRejectedPrefix: "REJECTED-WITH-REASON: ",
      };
      const state = { busy: false, stopped: false, queue: [], pendingEchos: [] };
      let systemLines = [];
      const appendSystem = (s) => systemLines.push(s);
      const setBusy = (b) => { state.busy = b; };
      // TCK-WEB-015: submit shows/clears the transient transcript bubble.
      let bubbles = 0;
      const showPendingBubble = () => { bubbles++; };
      const clearPendingBubble = () => { bubbles--; };
      // TCK-WEB-028 (5): a failed POST re-reads snapshot truth (button restore).
      let stateRefreshes = 0;
      const refreshState = () => { stateRefreshes++; };
      const authHeaders = (h) => h;
      let lastEcho = null;
      const appendUser = (value) => {
        lastEcho = { removed: false, remove() { this.removed = true; } };
        return lastEcho;
      };
      let fetchImpl = null;
      const fetch = (path, opts) => fetchImpl(path, opts);
      __SUBMIT__
      let formHandler = null;
      const formEl = { addEventListener: (_n, fn) => { formHandler = fn; } };
      const inputEl = { value: "" };
      __FORM__
      const main = async () => {
        // accepted: echo stays, no system line, no refresh
        fetchImpl = async () => ({ status: 202, ok: true });
        await submit("/turn", "text", "hello");
        if (lastEcho.removed || systemLines.length || stateRefreshes !== 0)
          throw new Error("202-path");
        // 401: the stale-session line (never "unreachable"), echo un-rendered
        fetchImpl = async () => ({
          status: 401, ok: false, json: async () => ({ error: "no" }),
        });
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (!lastEcho.removed) throw new Error("401-kept-echo");
        if (systemLines.length !== 1 || systemLines[0] !== LABELS.sessionStale)
          throw new Error("401-label");
        if (stateRefreshes !== 1) throw new Error("401-no-refresh");
        // TCK-WEB-028 (2) 503 WITH the server's value-free reason: reached ≠
        // unreachable — the refusal sentence carries the reason.
        fetchImpl = async () => ({
          status: 503, ok: false, json: async () => ({ error: "engine busy" }),
        });
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (systemLines[0] !== LABELS.turnRejectedPrefix + "engine busy")
          throw new Error("503-reason-label");
        // TCK-WEB-028 (2) 500 without a readable reason body: the plain line.
        fetchImpl = async () => ({ status: 500, ok: false });
        systemLines = [];
        await submit("/action", "utterance", "confirm");
        if (systemLines[0] !== LABELS.turnRejected) throw new Error("500-label");
        if (!lastEcho.removed) throw new Error("500-kept-echo");
        // transport failure (fetch throws, status stays 0): unreachable stands
        fetchImpl = async () => { throw new TypeError("network"); };
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (systemLines[0] !== LABELS.unreachable) throw new Error("throw-label");
        // stopped tab: the press is NOT swallowed — stale line, text kept
        systemLines = [];
        inputEl.value = "typed into a stopped tab";
        state.stopped = true;
        formHandler({ preventDefault: () => {} });
        if (systemLines[0] !== LABELS.sessionStale) throw new Error("stopped-line");
        if (inputEl.value === "") throw new Error("stopped-consumed");
        // TCK-WEB-015: every failed submit cleared its own bubble; only the
        // one accepted turn's shared bubble remains (never one per submit).
        if (bubbles !== 1) throw new Error("bubble-count");
        console.log("ok");
      };
      main().catch((e) => { console.error(e); process.exit(1); });
    """.replace("__SUBMIT__", submit_fn).replace("__FORM__", form_listener)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# ============================================================== TCK-WEB-028
# Static robustness batch (council quick wins). Five rules, all browser-free
# source pins unless a node behavioral check is named. textContent-only +
# CSP-safe throughout (the global sink scan covers the new code; the QR focus
# trap deliberately queries NO anchor/href selector — the WEB-014 pin bans
# that literal in the client entirely).

# (1) STUCK PENDING BUBBLE: applyState reconciles the transient "Working…"
# bubble ONLY from a typed state/1 snapshot whose flow_state is "idle" with
# the LOCAL queue empty (a typed reply is answered by the engine between
# turns, so idle+drained means nothing is in flight). state/0, a non-idle
# flow, or a queued-but-unpromoted line never clear it.
# (2) FAILURE COPY: the submit() failure ternary splits transport-down
# (status 0 → unreachable) from server-reached refusals (the ticket's
# sentence, or the server's value-free reason through the data.error/
# textContent pattern). 401 keeps the stale-session line (WEB-016).
# (3) RECONNECT ANNOUNCEMENT: both live-region writes in listen() sit behind
# the state.reconnecting transition guard; the "Connected" write doubles as
# the out-of-state announcement.
# (5) IN-FLIGHT DISABLE: both /action click listeners disable the clicked
# utterance button synchronously; applyState's repaint restores disabled on
# EVERY action-bar and quickbar button (model/quick ones included — the
# restore lands before the model-only continue); a failed POST also
# refreshes snapshot truth (no turn_end will ever come for a dead line).
def test_web028_static_pins() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # (1) the reconcile sits in applyState, gated on typed + idle + drained.
    apply = code[code.index("function applyState") : code.index("function noteTrustFlip")]
    assert 'if (typed && snap.flow_state === "idle" && state.queue.length === 0) {' in apply
    assert apply.index("clearPendingBubble();") < apply.index("const visible =")
    # (2) the two sentences exist verbatim (the ticket's line; the reason
    # variant follows the established rejectedPrefix pattern).
    assert 'turnRejected: "The wallet couldn\'t run that — try again.",' in code
    assert 'turnRejectedPrefix: "The wallet couldn\'t run that: ",' in code
    submit_fn = re.search(
        r"async function submit\(path, field, value\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert "status === 0" in submit_fn and "LABELS.unreachable" in submit_fn
    assert "LABELS.turnRejectedPrefix + reason" in submit_fn
    assert "typeof data.error === \"string\"" in submit_fn  # the safe data.error path
    # (3) transition-only announcements: the connecting write is guarded,
    # and the reconnecting write happens once on entry into the state.
    listen = code[
        code.index("async function listen()") : code.index("async function submit(")
    ]
    assert 'if (!state.reconnecting) setStatus("connecting", "Connecting…");' in listen
    assert "state.reconnecting = false;\n      setStatus(\"live\"" in listen  # out-transition
    assert re.search(
        r"if \(!state\.reconnecting\) \{\s*\n\s*state\.reconnecting = true;\s*\n"
        r'\s*setStatus\("reconnecting"', listen
    )
    assert listen.count('setStatus(') == 4  # connecting/live/reconnecting/unauthorized only
    # (5) disable-on-click in both listeners; restore on every repaint.
    actions = code[
        code.index("actionsEl.addEventListener(") :
        code.index("quickbarEl.addEventListener(")
    ]
    quick = code[
        code.index("quickbarEl.addEventListener(") : code.index("let inputSeq = 0;")
    ]
    assert "btn.disabled = true;\n    submit(" in actions
    assert re.search(r"btn\.disabled = true;[^\n]*\n  submit\(", quick)
    assert apply.count("btn.disabled = false;") == 2  # action bar + quickbar
    # and the restore lands BEFORE the model-only continue (those buttons
    # get it too — never a permanently disabled control):
    assert apply.index("btn.disabled = false;") < apply.index('continue;')
    # a failed POST re-reads snapshot truth (no turn_end will restore it):
    assert "refreshState();" in submit_fn


def test_stuck_pending_bubble_reconciles_from_typed_idle_state_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    apply = re.search(r"function applyState\(snap\) \{.*?\n\}", code, re.DOTALL).group(0)
    script = """
      const mkBtn = (action, extraClass) => ({
        dataset: action ? { action } : {},
        hidden: false, disabled: false,
        classList: { contains: (c) => c === extraClass },
      });
      const flowBtn = mkBtn("confirm"); flowBtn.disabled = true;   // in flight
      const modelBtn = mkBtn("model-download", "model-only"); modelBtn.disabled = true;
      const quickBtn = mkBtn("quick-balance"); quickBtn.disabled = true;
      const actionsEl = { querySelectorAll: () => [flowBtn, modelBtn] };
      const quickbarEl = { querySelectorAll: () => [quickBtn] };
      const state = {
        queue: [], busy: true, backendName: "", privacyMode: "",
        trustSig: null, watchKeyNeeded: false, pendingBubble: {},
      };
      let clears = 0;
      const clearPendingBubble = () => { clears++; state.pendingBubble = null; };
      const visibleActions = () => [];
      const applyScanChip = () => {}; const applyPrivacyChip = () => {};
      const applyWalletFpChip = () => {}; // TCK-WEB-027 chip painter (own harness)
      const applySuggestedServers = () => {}; // TCK-WEB-022 chips (own harness)
      const applyWatchKeyGate = () => {}; const applyModelPrompt = () => {};
      const paintSettingsDot = () => {}; const noteTrustFlip = () => {};
      const settingsPanelEl = { hidden: true };
      __APPLY__
      // stuck: typed IDLE + empty local queue → the bubble is reconciled away
      applyState({ schema: "state/1", flow_state: "idle" });
      if (clears !== 1) throw new Error("idle-did-not-clear");
      // a queued-but-unpromoted line keeps the bubble (its turn IS in flight)
      state.pendingBubble = {}; clears = 0; state.queue = [{}];
      applyState({ schema: "state/1", flow_state: "idle" });
      if (clears !== 0) throw new Error("queue-cleared");
      // a live flow (created) keeps it
      state.pendingBubble = {}; state.queue = []; clears = 0;
      applyState({ schema: "state/1", flow_state: "created", pending_present: true });
      if (clears !== 0) throw new Error("flow-cleared");
      // the UNTYPED transport-only shape never clears (busy/dead engine)
      state.pendingBubble = {}; clears = 0;
      applyState({ schema: "state/0", flow_state: "idle" });
      if (clears !== 0) throw new Error("state0-cleared");
      // TCK-WEB-028 (5): the same repaint restored EVERY in-flight-disabled
      // button — flow, model-only (before its continue) and quickbar alike.
      if (flowBtn.disabled || modelBtn.disabled || quickBtn.disabled)
        throw new Error("disabled-stuck");
      console.log("ok");
    """.replace("__APPLY__", apply)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-028 (3) behavioral (node): the SHIPPED listen() runs against a
# stubbed fetch that fails TWICE then connects — the reconnecting sentence is
# written EXACTLY once (the polite live region announces the transition, not
# every backoff attempt), "Connecting…" is written once on the first pass and
# never re-flipped mid-ladder, and the recovery writes "Connected" once (the
# out-transition).
def test_reconnect_announces_only_the_transition_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    listen = re.search(r"async function listen\(\) \{.*?\n\}", code, re.DOTALL).group(0)
    script = """
      const state = { stopped: false, lastEventId: 0, backoffMs: 500,
                      everConnected: false, reconnecting: false };
      const writes = [];
      const setStatus = (kind, label) => writes.push(kind + "|" + label);
      const authHeaders = (h) => h;
      const location = { origin: "http://127.0.0.1:8243" };
      const refreshState = () => {};
      const sleep = () => Promise.resolve(); // collapse the backoff ladder
      let attempt = 0;
      const fetch = async () => {
        attempt += 1;
        if (attempt < 3) throw new TypeError("network");
        return { ok: true, status: 200, body: {} };
      };
      const consumeStream = async () => { state.stopped = true; }; // one stream, then stop
      __LISTEN__
      listen().then(() => {
        const reconnecting = writes.filter((w) => w.startsWith("reconnecting|"));
        const connecting = writes.filter((w) => w.startsWith("connecting|"));
        const live = writes.filter((w) => w.startsWith("live|"));
        if (attempt !== 3) throw new Error("attempts");
        if (reconnecting.length !== 1) throw new Error("reconnecting-spam");
        if (connecting.length !== 1) throw new Error("connecting-spam");
        if (live.length !== 1) throw new Error("live-count");
        // ORDER: connecting → (one) reconnecting → live (the out-transition).
        if (writes[0] !== 'connecting|Connecting…') throw new Error("first");
        if (!writes[1].startsWith("reconnecting|")) throw new Error("entry");
        if (writes[2] !== "live|Connected") throw new Error("exit");
        if (state.reconnecting !== false) throw new Error("flag-not-reset");
        console.log("ok");
      }).catch((e) => { console.error(e); process.exit(1); });
    """.replace("__LISTEN__", listen)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-028 (4) behavioral (node): the SHIPPED Tab listener is extracted and
# driven — while the dialog is OPEN, Tab at the last control wraps to the
# first, Shift+Tab at the first wraps to the last, a strayed focus is pulled
# back in, a mid-dialog Tab is left to the browser, and while the dialog is
# HIDDEN the listener touches nothing (the settings Escape tiering and all
# background tabbing are unaffected).
def test_qr_dialog_tab_wraps_focus_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    prefix = 'document.addEventListener("keydown", '
    start = code.index(prefix + '(event) => {\n  if (event.key !== "Tab"')
    tab_listener = code[start : code.index("});", start) + 3]
    assert tab_listener.count("addEventListener") == 1  # exactly the one listener
    handler = tab_listener[len(prefix) : -2]  # the bare arrow function
    assert handler.startswith("(event) =>") and handler.endswith("}")
    script = """
      const mkFocusable = (name) => ({ name, focused: 0, focus() { this.focused++; } });
      const first = mkFocusable("first");
      const last = mkFocusable("last");
      const qrViewerEl = {
        hidden: false,
        querySelectorAll: () => [first, last],
        contains: (n) => n === first || n === last,
      };
      globalThis.document = { addEventListener: () => {}, activeElement: null };
      const tabHandler = __HANDLER__;
      const press = (key, shiftKey) => {
        const event = {
          key, shiftKey: !!shiftKey, prevented: false,
          preventDefault() { this.prevented = true; },
        };
        tabHandler(event);
        return event;
      };
      // 1. Tab at the LAST control: wrapped to the first, browser move blocked
      document.activeElement = last;
      let e = press("Tab");
      if (!e.prevented || first.focused !== 1) throw new Error("wrap-forward");
      // 2. Shift+Tab at the FIRST control: wrapped to the last.
      document.activeElement = first;
      e = press("Tab", true);
      if (!e.prevented || last.focused !== 1) throw new Error("wrap-back");
      // 3. Focus strayed OUTSIDE the dialog: Tab pulls it back to the first.
      document.activeElement = { name: "background" };
      e = press("Tab");
      if (!e.prevented || first.focused !== 2) throw new Error("stray-in");
      // 4. A tab stop INSIDE (not at the boundary): left to the browser.
      document.activeElement = first;
      e = press("Tab");
      if (e.prevented) throw new Error("clobbered-inner");
      // 5. Dialog CLOSED: the listener is deaf (background tabbing intact).
      qrViewerEl.hidden = true;
      document.activeElement = last;
      e = press("Tab");
      if (e.prevented) throw new Error("trap-when-closed");
      console.log("ok");
    """
    script = script.replace("__HANDLER__", handler)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-028 (5) behavioral (node): the SHIPPED actionsEl click listener
# disables the clicked utterance button SYNCHRONOUSLY, before submit() ever
# awaits — the browser (emulated here: a disabled button dispatches no click)
# therefore queues "confirm" exactly once per double-click; the qa-settings
# control (no utterance) is never disabled; and the WEB-028(1) harness above
# pins the restore-on-repaint half.
def test_action_click_disables_until_repaint_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    start = code.index('actionsEl.addEventListener("click", (event) => {')
    listener = code[start : code.index("});", start) + 3]
    handler = listener[len('actionsEl.addEventListener("click", ') : -2]
    script = """
      const mkBtn = (action, utterance) => ({
        dataset: { action, ...(utterance ? { utterance } : {}) },
        disabled: false,
      });
      const confirmBtn = mkBtn("confirm", "confirm");
      const settingsBtn = mkBtn("qa-settings"); // client-only control
      const state = { stopped: false };
      const openSettings = () => {};
      const queued = [];
      const submit = (path, field, value) => { queued.push([path, field, value]); };
      const handler = __HANDLER__;
      // browser-emulated delegated dispatch: disabled buttons fire no click.
      const click = (btn) => {
        if (btn.disabled) return;
        handler({ target: { closest: () => btn } });
      };
      click(confirmBtn);          // 1st: queues, disables synchronously
      if (queued.length !== 1 || !confirmBtn.disabled) throw new Error("first-click");
      click(confirmBtn);          // 2nd (the double-click): swallowed disabled
      if (queued.length !== 1) throw new Error("double-queued");
      // a DIFFERENT control still works while confirm is in flight
      const cancelBtn = mkBtn("cancel", "cancel");
      click(cancelBtn);
      if (queued.length !== 2 || !cancelBtn.disabled) throw new Error("sibling");
      // no-utterance settings opener: never disabled, nothing submitted
      click(settingsBtn);
      if (settingsBtn.disabled || queued.length !== 2) throw new Error("settings-btn");
      console.log("ok");
    """
    script = script.replace("__HANDLER__", handler)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-019 static pins: ONE visually-hidden polite live region in the
# markup (distinct from WEB-026's #copy-status), written ONLY from the
# turn_end handler — never from appendText/appendProgress/appendUser/
# appendSystem — with bubbleText's line seam (.turn-text minus
# .turn-progress/.turn-model), the "\n" join pinned, and textContent-only
# (createTextNode + replaceChildren; the global sink scan covers innerHTML).
def test_turn_status_live_region_markup_and_wiring() -> None:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    nodes = re.findall(r"<p[^>]*id=\"turn-status\"[^>]*>", html)
    assert len(nodes) == 1, "exactly ONE #turn-status live region"
    node = nodes[0]
    assert 'class="visually-hidden"' in node
    assert 'role="status"' in node and 'aria-live="polite"' in node
    assert "copy-status" not in node  # distinct from the WEB-026 region
    assert 'id="turn-status"' not in html.replace(node, "")

    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # the ONLY write site is noteTurnEnd, before the turn closes;
    assert code.count("announceTurn(state.openTurn)") == 1
    note = code[code.index("function noteTurnEnd"):]
    note = note[: note.index("\n}")]
    assert "announceTurn(state.openTurn)" in note
    assert note.index("announceTurn") < note.index("closeOpenTurn()")
    # never on user echoes, system lines, progress ticks, or settings paths:
    for name in ("appendUser", "appendSystem", "appendProgress", "renderUserText"):
        fn = code[code.index(f"function {name}"):]
        assert "announceTurn" not in fn[: fn.index("\n}")], name
    # the line seam: announceTurn selects EXACTLY bubbleText's lines. The two
    # selector LITERALS are extracted from their own function bodies and
    # compared for equality (a shared-substring `in` check would let a drift
    # in either seam pass silently — code-review MINOR).
    def body_of(name: str) -> str:
        body = code[code.index(f"function {name}"):]
        return body[: body.index("\n}")]

    def selector_of(name: str) -> str:
        found = re.search(r'querySelectorAll\("([^"]+)"\)', body_of(name))
        assert found, f"{name} lost its querySelectorAll literal"
        return found.group(1)

    selector = '.turn-text:not(.turn-progress):not(.turn-model)'
    assert selector_of("bubbleText") == selector_of("announceTurn") == selector
    fn = body_of("announceTurn")
    assert 'join("\\n")' in fn  # pinned join shape (same as the copy text)
    assert "replaceChildren" in fn and "createTextNode" in fn


# TCK-WEB-019 behavioral check (node if present): against a DOM stub with a
# minimal :not() selector, the SHIPPED stream path (handleEvent → text /
# progress / user_text / turn_end) announces a settled engine turn's text
# ONCE — progress dots and the model line never reach the region, the user's
# echo and the pending bubble are excluded by construction, a line-less or
# already-closed turn stays silent, and a replayed turn_end (the id-guard) is
# a no-op: exactly one write per turn.
def test_turn_end_announces_the_settled_turn_once_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fns = [
        re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", code, re.DOTALL).group(0)
        for name in (
            "lineText", "announceTurn", "closeOpenTurn", "ensureTurn",
            "appendText", "appendProgress", "appendUser", "appendSystem",
            "renderUserText", "showPendingBubble", "clearPendingBubble",
            "tailPendingBubble", "noteTurnEnd", "handleEvent",
        )
    ]
    script = """
      const mkNode = (tag, nodeType) => ({
        tag, nodeType: nodeType || 1, className: "", textContent: "",
        attrs: {}, children: [], parent: null,
        get childNodes() { return this.children; },
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(n) {
          if (n.parent) {
            const i = n.parent.children.indexOf(n);
            if (i !== -1) n.parent.children.splice(i, 1);
          }
          n.parent = this; this.children.push(n); return n;
        },
        append(...ns) { for (const n of ns) this.appendChild(n); },
        replaceChildren(...ns) { this.children = []; this.append(...ns); },
        remove() {
          if (!this.parent) return;
          const i = this.parent.children.indexOf(this);
          if (this.parent) this.parent.children.splice(i, 1);
          this.parent = null;
        },
        querySelector() { return null; },
        // mini matcher for the ONE selector the shipped seam uses
        querySelectorAll() {
          const out = [];
          const walk = (n) => {
            for (const c of n.children) {
              const cs = c.className.split(/\\s+/);
              if (cs.includes("turn-text") && !cs.includes("turn-progress")
                  && !cs.includes("turn-model")) out.push(c);
              walk(c);
            }
          };
          walk(this);
          return out;
        },
        get classList() {
          const self = this;
          const set = () => new Set(self.className.split(/\\s+/).filter(Boolean));
          return {
            add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
            remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
            contains(c) { return set().has(c); },
          };
        },
      });
      globalThis.Node = { ELEMENT_NODE: 1 };
      globalThis.document = {
        createElement: (t) => mkNode(t),
        createTextNode: (d) => {
          const n = mkNode("#text", 3);
          n.textContent = d;
          n.appendData = (s) => { n.textContent += s; };
          return n;
        },
      };
      const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const LABELS = { turnWorking: "Working…", queuedTag: "queued" };
      const state = { busy: false, openTurn: null, progressLine: null,
                      downloadLine: null, queue: [], pendingEchos: [],
                      pendingBubble: null, lastEventId: 0 };
      const transcriptEl = mkNode("ol");
      const turnStatusEl = mkNode("p");
      const hintEl = { hidden: true };
      const scrollToEnd = () => {};
      const refreshState = () => {};
      const setBusy = (b) => { state.busy = b; };
      const addCopyButton = () => {};
      const renderModelProgress = () => {};
      // the REAL token transform's inert shape: one text child per line
      const appendBubbleText = (line, text) => {
        line.appendChild(document.createTextNode(text));
      };
      __FNS__
      let writes = 0;
      const origReplace = turnStatusEl.replaceChildren.bind(turnStatusEl);
      turnStatusEl.replaceChildren = (...ns) => { writes++; origReplace(...ns); };
      const regionText = () => turnStatusEl.children.map((n) => n.textContent).join("");
      // 1. a settled turn: text + progress dots + second line → announced once
      handleEvent(1, "user_text", "show balance");
      handleEvent(2, "text", "Balance: 0.05 BTC");
      handleEvent(3, "progress", "..dot-telemetry..");
      handleEvent(4, "text", "across 2 addresses");
      handleEvent(5, "turn_end", "");
      if (regionText() !== "Balance: 0.05 BTC\\nacross 2 addresses")
        throw new Error("announce-content");  // \\n join pinned, dots excluded
      if (writes !== 1) throw new Error("announce-once");
      if (turnStatusEl.children.length !== 1) throw new Error("one-node");
      // 2. mid-stream progress ticks never touch the region
      handleEvent(6, "user_text", "rescan");
      handleEvent(7, "progress", "....");
      handleEvent(8, "progress", "\\n");
      if (writes !== 1 || regionText() !== "Balance: 0.05 BTC\\nacross 2 addresses")
        throw new Error("progress-silent");
      // 3. replay: a duplicate turn_end inside the id guard's window is a
      //    no-op (handleEvent drops it before noteTurnEnd runs) — once only
      handleEvent(5, "turn_end", "");
      handleEvent(9, "text", "Scan done");
      handleEvent(10, "turn_end", "");
      if (writes !== 2 || regionText() !== "Scan done") throw new Error("replay-dupe");
      // 4. a line-less / already-closed turn announces nothing
      handleEvent(11, "turn_end", "");
      if (writes !== 2) throw new Error("empty-turn-quiet");
      // 5. user echo and the pending bubble are never announced: the region
      //    holds only the engine's settled line (appendUser ran first and
      //    closed the turn — the "show balance" echo is absent above)
      if (regionText().includes("show balance")) throw new Error("echo-leak");
      console.log("ok");
    """
    script = script.replace("__FNS__", "\n".join(fns))
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
    "path", sorted(_RENDER_DIR.glob("*.json")), ids=lambda p: p.stem
)
def test_render_fixture_payload_stays_inert(path: Path) -> None:
    """Every hostile payload survives as ONE element-free text node (the
    render contract), and the fixture declares the correct contract shape."""
    case = json.loads(path.read_text(encoding="utf-8"))
    RUN_EVALS._validate_render_expectation(case["expectation"])
    ok, reason = RUN_EVALS._render_case_is_inert(case)
    assert ok, reason
    payload = case["payload"]
    node = RUN_EVALS.render_text_content(payload)
    # The browser would show the string verbatim — no parsing, no node growth.
    assert node == {"node_type": "#text", "data": payload, "children": []}
    # Control-char / oversized payloads stay byte-exact (never "corrected").
    assert node["data"] == payload


def test_render_fixture_corpus_covers_the_hostile_vectors() -> None:
    """The set is a genuine red-team corpus: the vectors span event handlers,
    script elements, javascript: URLs, attribute breakouts, control chars and an
    oversized line — not one payload copy-pasted."""
    vectors = set()
    for path in _RENDER_DIR.glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        vectors.add(case["vector"])
        assert case["category"] == "render"
    assert {
        "event_handler_attribute",
        "inline_script_element",
        "javascript_url_href",
        "attribute_breakout",
        "control_characters",
        "oversized_single_line",
    } <= vectors


# ------------------------------------------------ live server: CSP + nonce pins


def _bootstrap() -> Any:
    """A model-free stub engine (mirrors tests/test_web_server._bootstrap): the
    render-contract tests serve the REAL static client, so the engine only has
    to answer turns, not run a wallet."""
    from localwallet import app as _app
    from localwallet.agent.loop import AgentLoop
    from localwallet.protocol import IntentName
    from localwallet.tx.flow import TxFlow

    table = {
        IntentName.RESPOND: _app._respond_handler,
        IntentName.CLARIFY: _app._clarify_handler,
    }
    return _app.EngineContext(
        loop=AgentLoop(_app.stub_generate, table),
        flow=TxFlow(),
        session=_app.SendSession(),
        table=table,
    )


def _serve_real_static() -> Any:
    from localwallet.ui.web.server import serve_web

    server = serve_web(_bootstrap, static_dir=_STATIC)
    server.serve()
    return server


def test_served_index_csp_allows_only_the_nonced_island() -> None:
    import http.client

    server = _serve_real_static()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1])
        conn.request("GET", "/", headers={"X-Auth-Token": server.token})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        csp = response.getheader("Content-Security-Policy")
        conn.close()
    finally:
        server.stop()
        if server.handle.thread is not None:  # FLAKE-FIX TCK-TEST-002
            server.handle.thread.join(5)

    assert csp is not None
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "script-src 'self' 'nonce-" in csp
    # Exactly ONE inline <script> — the token island — and it carries a nonce.
    inline = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", body)
    assert len(inline) == 1
    nonce = re.search(r'<script nonce="([^"]+)"', inline[0])
    assert nonce is not None, "the island inline script must carry the CSP nonce"
    assert nonce.group(1) in csp
    # The external module script (src=) needs no nonce (covered by 'self').
    assert re.search(r'<script[^>]*\ssrc="/static/app\.js"', body)


def test_static_asset_served_with_no_inline_nonce_csp() -> None:
    import http.client

    server = _serve_real_static()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1])
        conn.request("GET", "/static/app.js", headers={"X-Auth-Token": server.token})
        response = conn.getresponse()
        ctype = response.getheader("Content-Type")
        csp = response.getheader("Content-Security-Policy")
        response.read()
        conn.close()
    finally:
        server.stop()
        if server.handle.thread is not None:  # FLAKE-FIX TCK-TEST-002
            server.handle.thread.join(5)
    assert ctype.startswith(("application/javascript", "text/javascript"))
    assert "script-src 'self';" in csp  # strict no-inline policy, no island nonce


# ============================================================== TCK-WEB-020
# Persistent scan/rescan-failure line (static half; the engine-side
# /state scan_error field and its clear-on-start/complete semantics are
# pinned in the engine tests). Discipline: typed-state-driven, verbatim
# textContent, transition-gated DOM writes (the polite live region must
# never re-announce per snapshot), absent key = zero residue.

def test_scan_error_line_source_pins() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    # the element sits next to the scan chip with status-live semantics
    # and starts hidden (never prose-inferred into view).
    assert (
        '<p id="scan-error" class="chip scan-error" role="status" '
        'aria-live="polite" hidden></p>'
    ) in index_html
    assert not re.search(r"\son[a-z]+=", index_html)  # global pin, restated
    # rendered INSIDE the scan-chip snapshot-apply path, off a typed
    # state/1 only, verbatim via textContent, transition-gated (the
    # state.scanError guard). Exactly ONE reader of the wire key exists.
    fn = code[code.index("function applyScanChip"):code.index("const PRIVACY_SUBLINE")]
    assert "snap.scan_error" in fn
    assert "scanErrorEl.textContent = scanError" in fn
    assert "if (scanError !== state.scanError)" in fn
    assert "scanErrorEl.hidden = scanError" in fn
    assert code.count("scan_error") == 1
    # no innerHTML sink anywhere is already pinned globally; the styling
    # is token-warn (no inline style in markup or JS).
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    assert ".scan-error" in css and "--c-warn-soft" in css
    assert ".style" not in fn and 'style="' not in index_html


def test_scan_error_line_render_contract_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fn = re.search(
        r"function applyScanChip\(snap\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    script = """
      const LABELS = { scanLoading: "Loading\\u2026", scanSkipped: "Skipped" };
      let errWrites = 0;
      const scanChipEl = { hidden: true, textContent: "" };
      const scanErrorEl = {
        hidden: true, _t: "",
        set textContent(v) { errWrites++; this._t = v; },
        get textContent() { return this._t; },
      };
      const state = { scanError: "" };
      __FN__
      const ERR = "<img src=x onerror=alert(1)> [class=http-status exc=HTTPStatus]";
      // key present -> visible, VERBATIM, exactly one DOM write.
      applyScanChip({ schema: "state/1", scan_state: "done", scan_error: ERR });
      if (scanErrorEl.hidden || scanErrorEl.textContent !== ERR) throw new Error("show");
      if (errWrites !== 1) throw new Error("write-count");
      // identical snapshot re-applied -> NO re-write (no announce spam).
      applyScanChip({ schema: "state/1", scan_state: "done", scan_error: ERR });
      if (errWrites !== 1) throw new Error("spam");
      // the engine clears the key when a scan starts -> line gone, no residue.
      applyScanChip({ schema: "state/1", scan_state: "running" });
      if (!scanErrorEl.hidden || scanErrorEl.textContent !== "") throw new Error("residue");
      if (errWrites !== 2) throw new Error("clear-write-count");
      // state/0 (busy engine) touches NOTHING (privacyMode discipline).
      applyScanChip({ schema: "state/0" });
      applyScanChip(null);
      if (errWrites !== 2) throw new Error("state0-wrote");
      // re-failure -> fresh line, visible, verbatim.
      applyScanChip({ schema: "state/1", scan_error: ERR + " | 2" });
      if (scanErrorEl.hidden || scanErrorEl.textContent !== ERR + " | 2") throw new Error("refail");
      // empty-string key (contract says never sent) renders as absent.
      applyScanChip({ schema: "state/1", scan_error: "" });
      if (!scanErrorEl.hidden) throw new Error("empty-shown");
      console.log("ok");
    """.replace("__FN__", fn)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# ============================================================== TCK-WEB-023
# Private-IP GREEN + kind-pill return + the backend_host subline (AMENDMENT +
# COUNCIL FOLD). The SHIPPED maps and functions are extracted from app.js and
# executed under node (browser-free): five-enum chip/subline/pill matrices,
# host present/absent/fallback, creds-refusal, unknown-name hide, and the
# last-known-value lifecycle. Copy prose is the real shipped LABELS — the
# matrices assert against it verbatim, so a reword fails the pin.

def _web023_blocks() -> str:
    """The shipped LABELS + both map/function blocks, verbatim (comments
    stripped). One stub set (state, chip/subline/badge/pill elements, el)
    satisfies both blocks; tests then drive only what they name."""
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    leak = re.search(r"const PUBLIC_LEAK_SENTENCE =.*?;\n", code, re.DOTALL).group(0)
    labels = re.search(r"const LABELS = \{.*?\n\};", code, re.DOTALL).group(0)
    privacy = code[code.index("const PRIVACY_SUBLINE"):code.index("const TRUST_BADGE_WORDS")]
    badges = code[code.index("const TRUST_BADGE_WORDS"):code.index("async function refreshState")]
    stubs = """
      const state = { privacyMode: "", backendHost: "", backendName: "" };
      const privacyChipEl = {
        hidden: true, dataset: {}, removedAttrs: [],
        removeAttribute(k) { this.removedAttrs.push(k); delete this.dataset[k]; },
      };
      let sublineWrites = 0;
      const privacySublineEl = {
        _t: "",
        set textContent(v) { sublineWrites++; this._t = v; },
        get textContent() { return this._t; },
      };
      const badgeStub = { hidden: true, dataset: {}, textContent: "" };
      const pillStub = { hidden: true, className: "", textContent: "" };
      const settingsListEl = {
        querySelectorAll(sel) {
          if (sel === ".trust-badge") return [badgeStub];
          if (sel === ".kind-pill") return [pillStub];
          return []; // .chain-consent: hidden-ness of the consent box is
                     // WEB-022/001B territory, untouched here.
        },
      };
      const el = (tag, cls, text) => ({ className: cls, textContent: text });
    """
    return stubs + leak + labels + privacy + badges


def test_web023_subline_host_matrix_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    script = (
        _web023_blocks()
        + """
      const GENERIC_PRIVATE = "Your node — only private if you run this server yourself.";
      const GENERIC_REMOTE = "Your node on another machine — private only if you trust it.";
      // five-enum matrix: every shipped mode has a host-less subline,
      // none leaks the {host} placeholder, unknown names render "" (hide).
      for (const m of ["public", "own_node_local", "own_node_private",
                       "own_node_remote", "awaiting_backend"]) {
        const s = privacySublineText(m, "");
        if (!s || s.includes("{host}") || s.includes("1.2.3.4")) throw new Error(m);
      }
      if (privacySublineText("brand_new_mode", "1.2.3.4") !== "") throw new Error("unknown-shown");
      // council fold: remote gains the host, private goes green-with-hedge.
      if (privacySublineText("own_node_remote", "node.example.com")
          !== "Your node at node.example.com — private only if you trust it.") throw new Error("remote-host");
      if (privacySublineText("own_node_private", "192.168.1.50")
          !== "Your node at 192.168.1.50 — only private if you run this server yourself.") throw new Error("private-host");
      // fallbacks (host absent = nothing to name, never an empty hole):
      if (privacySublineText("own_node_remote", "") !== GENERIC_REMOTE) throw new Error("remote-fb");
      if (privacySublineText("own_node_private", "") !== GENERIC_PRIVATE) throw new Error("private-fb");
      if (privacySublineText("own_node_private", undefined) !== GENERIC_PRIVATE) throw new Error("private-undef");
      // the three UNCHANGED modes ignore the host key entirely:
      for (const m of ["public", "own_node_local", "awaiting_backend"]) {
        if (privacySublineText(m, "10.0.0.7").includes("10.0.0.7")) throw new Error(m + "-host-leak");
      }
      // NO-CREDS-ON-WIRE client defense: a regressed wire value carrying
      // creds/scheme/path/whitespace/markup falls back to the generic
      // hedge — the refused text NEVER renders, and no sink ever shows
      // @, ://, or markup from a host-shaped value.
      for (const bad of ["rpcuser:hunter2@10.0.0.7", "://10.0.0.7", "10.0.0.7/p",
                         "10.0.0.7 ", "<img src=x onerror=alert(1)>", "@", "x".repeat(300)]) {
        const s = privacySublineText("own_node_private", bad);
        if (s !== GENERIC_PRIVATE) throw new Error("creds-refused:" + bad);
        if (s.includes("@") || s.includes("://") || s.includes("<")) throw new Error("wire-leak");
      }
      // honest shapes that DO ride the subline (IPv6 bracket literal, FQDN):
      if (!privacySublineText("own_node_private", "[::ffff:10.1.1.1]").includes("[::ffff:10.1.1.1]")) throw new Error("v6");
      console.log("ok");
    """
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_web023_kind_pill_tint_matrix_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    script = (
        _web023_blocks()
        + """
      const GREEN = new Set(["own_node_local", "own_node_private"]);
      const RESOLVED = new Set(["public", "own_node_local", "own_node_private", "own_node_remote"]);
      for (const kind of ["none", "electrum", "bitcoind", "mempool", "", "future"]) {
        for (const mode of [...RESOLVED, "awaiting_backend", "", "brand_new"]) {
          const p = kindPillPaint(kind, mode);
          const should = (kind === "electrum" || kind === "bitcoind") && RESOLVED.has(mode);
          if (p.visible !== should) throw new Error("visible:" + kind + "/" + mode);
          if (!should) continue;
          const wantTint = GREEN.has(mode) ? "kind-pill-private" : "kind-pill-public";
          if (p.tint !== wantTint) throw new Error("tint:" + kind + "/" + mode);
          if (p.text !== (kind === "electrum" ? "Electrum" : "Bitcoin Core")) throw new Error("word");
        }
      }
      console.log("ok");
    """
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_web023_chip_lifecycle_pill_repaint_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    script = (
        _web023_blocks()
        + """
      // (backendName is stamped upstream by applyState — pinned in
      // test_settings_reload_is_pinned_to_the_trust_flip; this harness
      // stamps it the same way before the chip pass repaints the badges.)
      // private mode + host + kind: GREEN chip, host hedge, green pill —
      // chip, trust badge and pill ALL painted from the one snapshot.
      state.backendName = "electrum";
      applyPrivacyChip({ schema: "state/1", privacy_mode: "own_node_private",
                         backend_kind: "electrum", backend_host: "10.1.2.3" });
      if (privacyChipEl.hidden) throw new Error("chip-hidden");
      if (privacyChipEl.dataset.privacy !== "own_node_private") throw new Error("tint-attr");
      if (!privacySublineEl.textContent.includes("10.1.2.3")
          || !privacySublineEl.textContent.includes("run this server yourself")) throw new Error("subline");
      if (badgeStub.hidden || badgeStub.dataset.privacy !== "own_node_private") throw new Error("badge");
      if (pillStub.hidden || pillStub.className !== "kind-pill kind-pill-private"
          || pillStub.textContent !== "Electrum") throw new Error("pill");
      // typed flip to REMOTE with the host key OMITTED: the stale private
      // host is gone (omit-never-empty), the pill re-tints YELLOW.
      state.backendName = "bitcoind";
      applyPrivacyChip({ schema: "state/1", privacy_mode: "own_node_remote",
                         backend_kind: "bitcoind" });
      if (privacySublineEl.textContent.includes("10.1.2.3")) throw new Error("stale-host");
      if (privacySublineEl.textContent !== "Your node on another machine — private only if you trust it.") throw new Error("remote-fallback");
      if (pillStub.className !== "kind-pill kind-pill-public"
          || pillStub.textContent !== "Bitcoin Core") throw new Error("pill-retint");
      // state/0 (busy) persists the last known truth untouched.
      const before = sublineWrites;
      applyPrivacyChip({ schema: "state/0" });
      if (sublineWrites !== before) throw new Error("state0-repaint");
      if (privacySublineEl.textContent.includes("10.1.2.3")) throw new Error("state0-revive");
      // kind none / awaiting mode: NO pill (both branches).
      state.backendName = "none";
      applyPrivacyChip({ schema: "state/1", privacy_mode: "own_node_private",
                         backend_kind: "none", backend_host: "10.1.2.3" });
      if (!pillStub.hidden) throw new Error("none-pill");
      state.backendName = "electrum";
      applyPrivacyChip({ schema: "state/1", privacy_mode: "awaiting_backend",
                         backend_kind: "electrum", backend_host: "surprise" });
      if (!pillStub.hidden) throw new Error("awaiting-pill");
      if (privacySublineEl.textContent !== "No backend chosen yet.") throw new Error("awaiting-subline");
      // unknown mode NAME (newer engine): chip HIDES (existing discipline),
      // and the host it carried is dropped, never rendered anywhere.
      state.backendName = "electrum";
      applyPrivacyChip({ schema: "state/1", privacy_mode: "brand_new_mode",
                         backend_kind: "electrum", backend_host: "9.9.9.9" });
      if (!privacyChipEl.hidden || state.privacyMode !== "" || state.backendHost !== "") throw new Error("unknown-mode");
      if (privacySublineEl.textContent !== "") throw new Error("unknown-subline");
      if (privacyChipEl.removedAttrs.length === 0) throw new Error("attr-residue");
      if (!badgeStub.hidden || !pillStub.hidden) throw new Error("unknown-badges");
      console.log("ok");
    """
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# ============================================================== TCK-WEB-027
# Wallet-fingerprint header chip (static half; the typed /state field and its
# closed 8-hex shape are engine-pinned in test_engine_pump). The value is the
# descriptor-origin ACCOUNT-key fingerprint — NEVER the device's master
# (HW-002) — so every copy line keeps that honesty. Lifecycle is typed-state-
# only: present = shown VERBATIM, absent = hidden (never fabricated, never
# client-derived), typed-omit = cleared, state/0 = keeps the last value;
# one DOM write per transition. Click-to-copy rides the SHARED WEB-026
# flashCopyResult path with the value-bearing accessible name
# "Copy wallet fingerprint <fp>".

def test_web027_chip_markup_and_source_pins() -> None:
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # markup: a click-to-copy BUTTON beside the privacy chip (before the
    # conn status), starting hidden, no role=status of its own (feedback
    # rides the ONE shared #copy-status region), no inline handler/style.
    chip = '<button id="wallet-fp" class="chip wallet-fp" type="button" hidden></button>'
    assert chip in index_html
    assert index_html.index('id="privacy-chip"') < index_html.index(chip)
    assert index_html.index(chip) < index_html.index('id="conn-status"')
    assert not re.search(r"\son[a-z]+=", index_html)  # global pin, restated
    # typed-only lifecycle: state/1 gate, regex gate, transition gate,
    # verbatim textContent, hide-on-empty, name-bearing aria-label.
    fn = re.search(
        r"function applyWalletFpChip\(snap\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert 'snap.schema !== "state/1"' in fn
    assert "WALLET_FP_RE.test(raw)" in fn
    assert "if (fp === state.walletFingerprint) return;" in fn  # one write/change
    assert 'walletFpEl.textContent = fp ? walletFpChipText(fp) : "";' in fn
    assert 'walletFpEl.hidden = fp === "";' in fn
    assert "walletFpEl.setAttribute(\"aria-label\", walletFpCopyName(fp));" in fn
    assert "removeAttribute" in fn  # hidden chip carries no stale name
    assert ".style" not in fn and "innerHTML" not in fn
    # exactly ONE reader of the wire key exists (the painter).
    assert code.count("wallet_fingerprint") == 1
    # wired into the snapshot path beside the other chips.
    assert "applyPrivacyChip(snap);\n  applyWalletFpChip(snap);" in code
    # click-to-copy rides the SHARED helper with the value read AT CLICK
    # (no new copy machinery, no stale-render value).
    wiring = code[code.index('walletFpEl.addEventListener("click"'):
                  code.index("function handleEvent(id")]
    assert "state.walletFingerprint" in wiring
    assert "flashCopyResult(walletFpEl, await clipboardWrite(fp)" in wiring
    # settings wallet section: the hint line rides the same typed truth.
    wk = code[code.index("function watchKeyRow"):code.index("function watchKeyInput")]
    assert "if (state.walletFingerprint) {" in wk
    assert "walletFpHintText(state.walletFingerprint)" in wk
    assert 'el("p", "setting-hint"' in wk
    # CSS: token-only chip-button reset; nowrap = the 320px no-layout-shift
    # guarantee (the .bar's existing flex-wrap moves the whole chip, the
    # value never splits mid-hash — the existing block-pin pattern).
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    block = css[css.index(".wallet-fp {"):]
    block = block[: block.index("}")]
    for decl in ("font: inherit;", "cursor: pointer;", "white-space: nowrap;",
                 "position: relative;", "color: var(--c-text);"):
        assert decl in block, decl
    assert "#" not in block  # no hardcoded visual values — tokens only
    bar = css[css.index(".bar {"):]
    assert "flex-wrap: wrap;" in bar[: bar.index("}")]  # 320px-safe family
    # the WEB-026 shared ok/fail states reach the chip (copy feedback).
    assert ".wallet-fp.copy-ok {" in css and ".wallet-fp.copy-fail {" in css


def test_web027_chip_lifecycle_copy_and_label_copy_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    labels = re.search(r"const LABELS = \{.*?\n\};", code, re.DOTALL).group(0)
    leak = re.search(r"const PUBLIC_LEAK_SENTENCE =.*?;\n", code, re.DOTALL).group(0)
    block = code[code.index("const WALLET_FP_RE"):code.index("function handleEvent(id")]
    script = (
        """
      const state = { walletFingerprint: "" };
      let fpWrites = 0, paneRenders = 0;
      const walletFpEl = {
        hidden: true, _t: "", attrs: {}, title: "", handler: null,
        set textContent(v) { fpWrites++; this._t = v; },
        get textContent() { return this._t; },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        addEventListener(kind, fn) { this.handler = fn; },
      };
      const settingsPanelEl = { hidden: true };
      const renderSettings = () => { paneRenders++; };
      let copied = null;
      const clipboardWrite = async (t) => { copied = t; return true; };
      const flashCalls = [];
      const flashCopyResult = (ctrl, ok, baseTitle, baseAria) => {
        flashCalls.push([ctrl === walletFpEl, ok, baseTitle, baseAria]);
      };
      __LABELS__
      __BLOCK__
      // present -> shown VERBATIM, value-bearing name, ONE write.
      applyWalletFpChip({ schema: "state/1", wallet_fingerprint: "f1a2b3c4" });
      if (walletFpEl.hidden) throw new Error("hidden-when-present");
      if (walletFpEl.textContent !== "Wallet f1a2b3c4") throw new Error("text");
      if (walletFpEl.attrs["aria-label"] !== "Copy wallet fingerprint f1a2b3c4") throw new Error("name");
      if (fpWrites !== 1) throw new Error("write-count");
      const w1 = fpWrites;
      // identical snapshot re-applied -> zero DOM writes (transition gate).
      applyWalletFpChip({ schema: "state/1", wallet_fingerprint: "f1a2b3c4" });
      if (fpWrites !== w1) throw new Error("spam");
      // state/0 (busy engine) keeps the last value untouched.
      applyWalletFpChip({ schema: "state/0" });
      applyWalletFpChip(null);
      if (fpWrites !== w1 || walletFpEl.hidden || state.walletFingerprint !== "f1a2b3c4") throw new Error("state0");
      // click-to-copy rides the SHARED helper with the whole value.
      await walletFpEl.handler();
      if (copied !== "f1a2b3c4") throw new Error("copy-value");
      const call = flashCalls[flashCalls.length - 1];
      if (!call[0] || call[1] !== true || call[2] !== LABELS.clickToCopy
          || call[3] !== "Copy wallet fingerprint f1a2b3c4") throw new Error("copy-wiring");
      // typed snapshot OMITS the field -> cleared + hidden, name gone
      // (unprovisioned / replace in flight: never a stale or empty chip).
      applyWalletFpChip({ schema: "state/1" });
      if (!walletFpEl.hidden || walletFpEl.textContent !== "" || "aria-label" in walletFpEl.attrs) throw new Error("omit-clears");
      // a hidden chip's click is a no-op (nothing to copy, nothing flashed).
      const n = flashCalls.length;
      await walletFpEl.handler();
      if (flashCalls.length !== n) throw new Error("ghost-copy");
      // junk shapes are REFUSED at the gate (the wire is untrusted): never
      // painted, never a partial render — uppercase/short/long/markup/non-string.
      for (const bad of ["F1A2B3C4", "f1a2b3c", "f1a2b3c45", "<img src=x>",
                         "f1a2b3c4 onerror", 12345678, null, {}, "0xdeadbeef"]) {
        applyWalletFpChip({ schema: "state/1", wallet_fingerprint: bad });
        if (!walletFpEl.hidden || walletFpEl.textContent !== "") throw new Error("junk:" + bad);
      }
      // the hint + chip + name texts render the WHOLE value, never truncated.
      const hint = walletFpHintText("f1a2b3c4");
      if (hint !== "First characters: f1a2b3c4 — your wallet's fingerprint. "
                 + "Your hardware wallet shows its own, different number "
                 + "(the device fingerprint) — they won't match, and that's expected.") throw new Error("hint-copy");
      if (hint.includes("{fp}")) throw new Error("placeholder-leak");
      if (walletFpChipText("abcdef01") !== "Wallet abcdef01") throw new Error("chip-text");
      // the ticket's exact replace-copy sentence ships in the confirm rung.
      if (!LABELS.watchKeyReplaceConfirm.includes(
          "The header's Wallet … number changes with the new key.")) throw new Error("replace-copy");
      // open pane follows the same typed truth — render only ON transition.
      settingsPanelEl.hidden = false;
      applyWalletFpChip({ schema: "state/1", wallet_fingerprint: "abcdef01" });
      if (paneRenders !== 1) throw new Error("pane-flip");
      applyWalletFpChip({ schema: "state/1", wallet_fingerprint: "abcdef01" });
      if (paneRenders !== 1) throw new Error("pane-spam");
      console.log("ok");
    """
        .replace("__LABELS__", leak + labels)
        .replace("__BLOCK__", block)
    )
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )


# --------------------------------------------------------------------------
# TCK-WEB-022: suggested public-Electrum chips (click-to-FILL) in the server
# card's act zone. COUNCIL BINDING (glm#2/#3/#10, qwen#5) pinned below: the
# chips NEVER apply (the one Apply+probe path keeps the leak disclosure even
# for a public→public switch), the warn sentence precedes the chips in DOM
# order (the SR order), the group is a real role="group" of real <button>s,
# warn styling is token-only with a pinned ≥4.5:1 pair, and the tracker is
# typed-only + change-gated (state/0 keeps the last known group; a typed
# missing key renders NO group, never an empty one).


def test_suggested_chips_are_click_to_fill_source_pins() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # (glm#3) group semantics + REAL buttons + warn BEFORE chips (DOM = SR).
    group = re.search(
        r"function chainChipsGroup\(servers, fill\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert 'group.setAttribute("role", "group");' in group
    assert 'group.setAttribute("aria-label", LABELS.chainChipsGroup);' in group
    assert 'el("p", "chain-chips-warn"' in group
    assert group.index("chain-chips-warn") < group.index('el("button", "chain-chip"')
    assert 'chip.type = "button";' in group
    # (glm#2) the chips do NOT ride the delegated Apply handler or any POST:
    assert "settingKey" not in group and "fetch" not in group
    fill = re.search(
        r"function fillChainServer\(input, applyBtn, url\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert "input.value = url;" in fill  # verbatim — no trim, no rewrite
    assert "input.focus();" in fill  # (qwen#5) focus the URL field after fill
    assert "settingKey" not in fill and "fetch(" not in fill and "/settings" not in fill
    # one disclosure voice: the group's warn line REUSES the pane's sentence.
    assert 'chainChipsWarn: "A public server — " + PUBLIC_LEAK_SENTENCE' in code
    # (glm#3 / WEB-021) the group rides the act zone, below the URL line.
    chain = code[code.index("function chainBaseRow") : code.index("function backendCredFlags")]
    assert "renderChainChips(li);" in chain
    assert chain.index('actZone.appendChild(el("div", "chain-chips"));') < chain.index(
        "li.appendChild(actZone)"
    )
    # typed-only + change-gated re-render (render-once today — static list).
    apply = re.search(
        r"function applySuggestedServers\(snap\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert 'if (!snap || snap.schema !== "state/1") return;' in apply  # state/0 keeps it
    assert "if (sig === state.suggestedServersSig) return;" in apply
    # (glm#10) chip styling: TOKENS ONLY (no raw hex), the dedicated pinned
    # warn pair, wrapping chip row (flex-wrap survives 320px).
    css = (_STATIC / "styles.css").read_text(encoding="utf-8")
    chip = css[css.index(".chain-chip {") :]
    chip = chip[: chip.index("}")]
    assert "#" not in chip
    assert "var(--c-chip-warn-ink)" in chip and "var(--c-chip-warn-bg)" in chip
    assert "overflow-wrap: anywhere;" in chip  # long label wraps, never clips
    assert ".chain-chips { display: flex; flex-wrap: wrap;" in css
    warn = css[css.index(".chain-chips-warn {") :]
    warn = warn[: warn.index("}")]
    assert "color: var(--c-chip-warn-ink);" in warn


def test_suggested_chips_fill_focus_and_never_apply_under_node() -> None:
    """Node-executed behavioral pin: against DOM stubs, the SHIPPED builder +
    fill run — a chip click fills the field with the verbatim vetted URL and
    focuses it (opening the Edit rung when the field was read-only), fires NO
    apply/POST; malformed wire entries die at the gate; no list = no group."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    funcs = "\n".join(
        re.search(rf"function {name}\(.*?\n\}}", code, re.DOTALL).group(0)
        for name in ("el", "readSuggestedServers", "chainChipsGroup", "fillChainServer")
    )
    script = """
      const LABELS = {
        chainChipsGroup: "Suggested public Electrum servers",
        chainChipsWarn: "A public server — whoever runs it sees every address.",
      };
      const document = { createElement: (tag) => ({
        tag, className: "", textContent: "", type: "", dataset: {},
        attrs: {}, children: [], handlers: {},
        setAttribute(k, v) { this.attrs[k] = String(v); },
        appendChild(n) { this.children.push(n); return n; },
        addEventListener(t, fn) { this.handlers[t] = fn; },
      }) };
      let posts = 0;
      globalThis.fetch = () => { posts++; throw new Error("chip-posted"); };
      __FUNCS__
      const URL = "ssl://electrum.blockstream.info:50002";
      const LABEL = "Blockstream public electrum";
      // the wire gate: missing key / non-array / junk entries die, valid ride verbatim.
      if (readSuggestedServers(undefined).length !== 0) throw new Error("missing-key");
      if (readSuggestedServers("junk").length !== 0) throw new Error("non-array");
      const servers = readSuggestedServers([null, "x", { url: URL }, { label: LABEL },
                                           { url: 42, label: LABEL }, { url: URL, label: LABEL }]);
      if (servers.length !== 1 || servers[0].url !== URL) throw new Error("gate");
      // no chips = NO group rendered at all (never an empty one).
      if (chainChipsGroup([], () => {}) !== null) throw new Error("empty-group");
      // n = 1: warn sentence FIRST, then exactly one real button (DOM = SR).
      let focusCalls = 0, applyClicks = 0;
      const input = { readOnly: true, value: "", dataset: {}, focus() { focusCalls++; } };
      const applyBtn = { click() { applyClicks++; input.readOnly = false; } };
      const group = chainChipsGroup(servers, (url) => fillChainServer(input, applyBtn, url));
      if (group.attrs.role !== "group" || !group.attrs["aria-label"]) throw new Error("semantics");
      if (group.children.length !== 2) throw new Error("arity:" + group.children.length);
      if (group.children[0].className !== "chain-chips-warn") throw new Error("warn-order");
      if (group.children[0].textContent !== LABELS.chainChipsWarn) throw new Error("warn-copy");
      const chip = group.children[1];
      if (chip.tag !== "button" || chip.type !== "button") throw new Error("not-a-button");
      if (chip.textContent !== LABEL) throw new Error("label-not-textContent");
      if ("settingKey" in chip.dataset) throw new Error("rides-apply-delegate");
      // CLICK on a stored (read-only) row: Edit rung opens, URL fills verbatim,
      // focus moves to the FIELD (qwen#5) — and NOTHING is ever POSTed.
      chip.handlers.click();
      if (applyClicks !== 1) throw new Error("rung-not-opened");
      if (input.value !== URL) throw new Error("not-verbatim:" + input.value);
      if (focusCalls !== 1) throw new Error("focus");
      if (input.dataset.dirty !== "1") throw new Error("not-dirty");
      if (posts !== 0) throw new Error("applied");
      // CLICK on an already-editable field: no rung click, fill + focus only.
      chip.handlers.click();
      if (applyClicks !== 1 || input.value !== URL || focusCalls !== 2 || posts !== 0) {
        throw new Error("second-click");
      }
      console.log("ok");
    """.replace("__FUNCS__", funcs)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_suggested_chips_tracker_is_typed_only_and_change_gated_under_node() -> None:
    """Node-executed lifecycle pin (the WEB-020/023/027 discipline): state/0
    never touches the group (keeps the last known), a typed snapshot without
    the key clears it (no group, not an empty one), and the DOM repaints ONLY
    when the gated array actually changes."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    funcs = "\n".join(
        re.search(rf"function {name}\(.*?\n\}}", code, re.DOTALL).group(0)
        for name in ("readSuggestedServers", "applySuggestedServers")
    )
    script = """
      const state = { suggestedServers: [], suggestedServersSig: null };
      let paints = 0;
      const paintChainChips = () => { paints++; };
      __FUNCS__
      const ONE = [{ url: "ssl://electrum.blockstream.info:50002", label: "Blockstream public electrum" }];
      // state/0 BEFORE any typed truth: nothing happens (no flash, no paint).
      applySuggestedServers({ schema: "state/0" });
      if (paints || state.suggestedServers.length) throw new Error("state0-first");
      // the first typed snapshot lands the group once.
      applySuggestedServers({ schema: "state/1", suggested_servers: ONE });
      if (paints !== 1 || state.suggestedServers.length !== 1) throw new Error("first");
      // an identical typed list NEVER re-paints (change-gated DOM writes).
      applySuggestedServers({ schema: "state/1", suggested_servers: ONE });
      if (paints !== 1) throw new Error("re-paint-spam");
      // state/0 mid-turn keeps the last known group.
      applySuggestedServers({ schema: "state/0" });
      if (paints !== 1 || state.suggestedServers.length !== 1) throw new Error("state0-blanked");
      // a typed snapshot that OMITS the key clears it — no group, once.
      applySuggestedServers({ schema: "state/1" });
      if (paints !== 2 || state.suggestedServers.length !== 0) throw new Error("clear");
      applySuggestedServers({ schema: "state/1" });
      if (paints !== 2) throw new Error("clear-spam");
      console.log("ok");
    """.replace("__FUNCS__", funcs)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
