"""Application wiring for the Phase 0 walking skeleton (TCK-P0-006).

This module only glues existing pieces together — it implements no
protocol, wallet, or chain logic itself:

- :func:`build_dispatch_table` — the allowlist dispatch table (closed
  intent enum → handlers). ``get_balance`` scans the derived receive
  addresses against the Esplora client and returns plain sats totals;
  ``respond``/``clarify`` pass the model's text through unchanged.
- :func:`run` / :func:`main` — CLI wiring: read the watch-only key from
  ``--zpub`` or ``LOCALWALLET_ZPUB``, derive addresses (the testnet gate
  lives in the wallet stub), build the chain client from
  :class:`~localwallet.config.Settings`, pick the model runtime (the real
  local GGUF via ``LOCALWALLET_MODEL_PATH``, or the documented
  ``--stub-llm`` dev mode), and run the chat REPL.

Invariants honored here:

- Model output is untrusted input handled exclusively by
  ``AgentLoop`` → ``handle_raw`` (3-layer validation → allowlist
  dispatch). Nothing in this module parses or executes model text.
- Network I/O happens only inside ``localwallet.chain``; this module
  imports that local module, never a network library (lint-enforced).
- The UI prints balances verbatim from handler result dicts — it computes
  nothing, and the model narrates no numbers.
- Nothing (banner, REPL, errors, exit paths) ever echoes the zpub. Watch
  key error strings are value-free by contract; chain error strings are
  scrubbed by the chain layer (no addresses/txids/amounts).
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from localwallet.agent.context import sanitize_tool_output
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, GenerateFn, ModelRuntime
from localwallet.chain import ChainError, EsploraClient, balance_from_utxos
from localwallet.config import Settings
from localwallet.protocol import (
    ClarifyParams,
    DispatchTable,
    Envelope,
    Handler,
    IntentName,
    RespondParams,
)
from localwallet.wallet.zpub_stub import (
    WatchKeyError,
    derive_receive_addresses,
    parse_watch_key,
)

__all__ = [
    "DEFAULT_SCAN_COUNT",
    "PRIVACY_INDICATOR",
    "ZPUB_ENV_VAR",
    "build_dispatch_table",
    "main",
    "run",
    "stub_generate",
]

#: Environment variable supplying the watch-only account key
#: (``--zpub`` overrides it).
ZPUB_ENV_VAR: Final[str] = "LOCALWALLET_ZPUB"

#: Receive addresses scanned per balance check. Kept deliberately small:
#: the scan is sequential, one HTTP request per address, and Phase 1's
#: gap-limit scanner replaces this constant.
DEFAULT_SCAN_COUNT: Final[int] = 5

#: The §9 honest privacy indicator, shown verbatim at startup (PROJECT.md
#: §9 / R7 — never over-claim privacy while querying a public explorer).
PRIVACY_INDICATOR: Final[str] = (
    "Querying public mempool.space — the operator can associate queried "
    "addresses with your IP."
)

_BANNER_TITLE: Final[str] = (
    "local-wallet — watch-only Bitcoin wallet, Phase 0 walking skeleton"
)
_BANNER_TESTNET: Final[str] = (
    "Network: Bitcoin TESTNET only — mainnet keys are refused until Phase 6."
)

#: Fallback UI strings (mirror the agent loop's generic containment
#: messages; the loop normally supplies these).
_GENERIC_FAILURE: Final[str] = (
    "Something went wrong while processing that request. Please try again."
)

_STUB_RESPOND_TEXT: Final[str] = (
    "(stub model, dev mode) I am a canned stand-in for the local LLM. "
    'Ask "What\'s my balance?" to exercise the get_balance intent.'
)
_STUB_RESPOND_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "respond", "params": {"text": _STUB_RESPOND_TEXT}}
)
_STUB_BALANCE_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "get_balance", "params": {}}
)


def stub_generate(prompt: str, grammar_text: str | None) -> str:
    """Deterministic stub model for ``--stub-llm`` — dev/test mode ONLY.

    This is **not** the acceptance path: the AC path is the real local
    GGUF runtime (:data:`~localwallet.agent.runtime.MODEL_PATH_ENV_VAR`).
    The stub exists so the full wiring (agent loop → validation →
    allowlist dispatch → chain handler) can be exercised deterministically
    without a model file.

    Behavior: emits a canned ``get_balance`` envelope when the current
    user turn mentions "balance", else a canned ``respond`` envelope. The
    current user turn is the last ``user: `` segment of the assembled
    prompt (see ``AgentLoop._build_prompt``). ``grammar_text`` is accepted
    for :data:`~localwallet.agent.runtime.GenerateFn` compatibility and
    ignored.

    Args:
        prompt: The fully assembled agent prompt.
        grammar_text: The envelope GBNF grammar (ignored by the stub).

    Returns:
        A canned envelope JSON document (untrusted-input contract still
        applies: it flows through ``handle_raw`` like any model output).
    """
    del grammar_text
    user_turn = prompt.rsplit("user: ", 1)[-1].lower()
    if "balance" in user_turn:
        return _STUB_BALANCE_ENVELOPE
    return _STUB_RESPOND_ENVELOPE


def build_dispatch_table(
    client: EsploraClient,
    addresses: Sequence[str],
    get_tip: Callable[[], int],
) -> DispatchTable:
    """Build the allowlist dispatch table for the running app.

    Args:
        client: The chain client used by the ``get_balance`` handler.
        addresses: Derived receive addresses to scan (Phase 0: a small,
            fixed list — default 5).
        get_tip: Zero-argument callable returning the current tip height
            (``EsploraClient.get_tip_height`` in production).

    Returns:
        A :class:`~localwallet.protocol.DispatchTable` covering the whole
        closed intent enum: ``respond``/``clarify`` identity passthroughs
        and the ``get_balance`` chain scanner.
    """
    return {
        IntentName.RESPOND: _respond_handler,
        IntentName.CLARIFY: _clarify_handler,
        IntentName.GET_BALANCE: _make_get_balance_handler(list(addresses), client, get_tip),
    }


def _respond_handler(envelope: Envelope) -> dict[str, object]:
    """``respond``: identity passthrough of the model's chat text."""
    params = envelope.params
    if not isinstance(params, RespondParams):
        # Unreachable via validated envelopes; fail closed anyway.
        return {"error": "internal", "detail": "respond params shape mismatch"}
    return {"text": params.text}


def _clarify_handler(envelope: Envelope) -> dict[str, object]:
    """``clarify``: identity passthrough of the model's question."""
    params = envelope.params
    if not isinstance(params, ClarifyParams):
        return {"error": "internal", "detail": "clarify params shape mismatch"}
    return {"question": params.question}


def _make_get_balance_handler(
    addresses: list[str],
    client: EsploraClient,
    get_tip: Callable[[], int],
) -> Handler:
    """Create the ``get_balance`` handler closed over the scan inputs.

    Scans each address sequentially (``get_address_utxos`` →
    :func:`~localwallet.chain.balance_from_utxos`), sums the sats totals,
    and reads the tip height once. A ``ChainError`` from the UTXO scan is
    caught and surfaced as ``{"error": "chain_unavailable", "detail":
    <scrubbed message>}`` instead of raising: chain-layer messages contain
    no addresses, txids, or amounts, so the dict is safe for the UI and
    logs. The tip height is enrichment only — a ``ChainError`` from the tip
    lookup is non-fatal: the balance totals are still returned with the
    ``tip_height`` key omitted entirely (never an inaccurate value). Any
    other exception propagates to ``handle_raw``'s containment.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        del envelope  # get_balance params are empty by schema
        confirmed = 0
        unconfirmed = 0
        scanned = 0
        try:
            for address in addresses:
                balance = balance_from_utxos(client.get_address_utxos(address))
                confirmed += balance.confirmed_sats
                unconfirmed += balance.unconfirmed_sats
                scanned += 1
        except ChainError as exc:
            # detail is already scrubbed by the chain layer (value-free of
            # addresses/txids/amounts) — safe to surface verbatim.
            return {"error": "chain_unavailable", "detail": str(exc)}
        result: dict[str, object] = {
            "confirmed_sats": confirmed,
            "unconfirmed_sats": unconfirmed,
            "total_sats": confirmed + unconfirmed,
            "addresses_scanned": scanned,
        }
        try:
            result["tip_height"] = get_tip()
        except ChainError:
            # Tip is enrichment, not the Phase 0 AC deliverable — omit the
            # key on failure rather than degrade the whole lookup.
            pass
        return result

    return handler


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point; delegates to :func:`run`."""
    return run(argv)


def run(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Wire the application from ``argv``/environment and run the REPL.

    Configuration precedence: ``--zpub`` overrides ``LOCALWALLET_ZPUB``;
    the real model runtime (:data:`MODEL_PATH_ENV_VAR`) takes precedence
    over ``--stub-llm`` dev mode.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).
        input_fn: REPL line reader (``input``-compatible; test seam).
        output_fn: REPL/banner writer (``print``-compatible; test seam).

    Returns:
        Process exit code: ``0`` on normal exit (including ``exit``,
        Ctrl-D, Ctrl-C), ``2`` on configuration errors (missing key,
        refused key, no model). Configuration errors never echo the key.
    """
    args = _parse_args(argv)

    zpub = (args.zpub or os.environ.get(ZPUB_ENV_VAR, "")).strip()
    if not zpub:
        output_fn(f"No watch key configured: pass --zpub or set {ZPUB_ENV_VAR}.")
        return 2

    try:
        parsed = parse_watch_key(zpub)
        addresses = derive_receive_addresses(parsed, DEFAULT_SCAN_COUNT)
    except WatchKeyError as exc:
        # WatchKeyError messages are value-free (no key material).
        output_fn(f"Watch key rejected: {exc}")
        return 2

    if os.environ.get(MODEL_PATH_ENV_VAR):
        generate: ModelRuntime | GenerateFn = ModelRuntime()
    elif args.stub_llm:
        generate = stub_generate
    else:
        output_fn(f"No model configured: set {MODEL_PATH_ENV_VAR} or pass --stub-llm.")
        return 2

    settings = Settings.from_env()
    client = EsploraClient(
        base_url=settings.esplora_base_url,
        timeout_s=settings.request_timeout_s,
        max_retries=settings.max_retries,
    )
    table = build_dispatch_table(client, addresses, client.get_tip_height)
    loop = AgentLoop(generate, table)

    output_fn(_BANNER_TITLE)
    output_fn(_BANNER_TESTNET)
    output_fn(f"Privacy notice: {PRIVACY_INDICATOR}")
    output_fn("Type a message — 'exit' or Ctrl-D quits.")

    try:
        _repl(loop, output_fn, input_fn)
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C
    finally:
        client.close()
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse CLI arguments (see :func:`run` for the flags)."""
    parser = argparse.ArgumentParser(
        prog="local-wallet",
        description="Watch-only testnet Bitcoin wallet driven by a local LLM (Phase 0).",
    )
    parser.add_argument(
        "--zpub",
        help=(
            "account-level watch-only extended public key "
            "(vpub/tpub for Phase 0); overrides LOCALWALLET_ZPUB"
        ),
    )
    parser.add_argument(
        "--stub-llm",
        action="store_true",
        help=(
            "dev/test mode: deterministic canned model instead of the local "
            "GGUF runtime (NOT the acceptance path)"
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _repl(
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    input_fn: Callable[[str], str],
) -> None:
    """Read user lines until EOF/exit and print each turn's outcome."""
    while True:
        try:
            line = input_fn("you> ")
        except EOFError:
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return
        _print_turn(loop.run(line, {}), output_fn)


def _print_turn(turn: AgentTurnResult, output_fn: Callable[[str], None]) -> None:
    """Print one agent turn according to its status and intent.

    Balance values are printed verbatim from the handler result dict —
    the UI computes nothing, and the model narrates no numbers.
    """
    # Every string that may originate from the model (or that the model can
    # influence) is passed through sanitize_tool_output immediately before
    # printing (SR-006 minor 1): the envelope grammar permits \\uXXXX, so ESC
    # (ANSI) and bidi control characters could otherwise reach the terminal.
    # Code-owned fallback strings are sanitized uniformly too. Plain text is
    # unaffected — only Cc/Cf Unicode categories are stripped.
    if turn.status is AgentTurnStatus.CLARIFIED:
        output_fn(sanitize_tool_output(turn.user_message or _GENERIC_FAILURE))
        return
    if turn.status is AgentTurnStatus.FAILED:
        output_fn(sanitize_tool_output(turn.user_message or _GENERIC_FAILURE))
        return

    envelope = turn.envelope
    if envelope is None:  # pragma: no cover — OK turns always carry one
        output_fn(sanitize_tool_output(_GENERIC_FAILURE))
        return

    if envelope.intent is IntentName.RESPOND and isinstance(envelope.params, RespondParams):
        output_fn(sanitize_tool_output(envelope.params.text))
    elif envelope.intent is IntentName.CLARIFY and isinstance(envelope.params, ClarifyParams):
        output_fn(sanitize_tool_output(envelope.params.question))
    elif envelope.intent is IntentName.GET_BALANCE:
        _print_balance(turn.result or {}, output_fn)
    else:  # pragma: no cover — closed intent enum
        output_fn(sanitize_tool_output(_GENERIC_FAILURE))


def _print_balance(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print the balance verbatim from the handler's result dict."""
    if result.get("error") is not None:
        detail = str(result.get("detail", "")).strip()
        suffix = f" ({detail})" if detail else ""
        output_fn(f"Balance lookup failed — chain unavailable{suffix}")
        return
    confirmed = result.get("confirmed_sats", 0)
    unconfirmed = result.get("unconfirmed_sats", 0)
    total = result.get("total_sats", 0)
    scanned = result.get("addresses_scanned", 0)
    tip = result.get("tip_height", 0)
    # SR-006 minor 2: an absent tip_height (handler deliberately omitted it
    # after a tip-lookup failure) must print "tip unavailable", never a
    # fabricated "tip height 0".
    tip_label = f"tip height {tip}" if "tip_height" in result else "tip unavailable"
    output_fn(
        f"Balance (testnet): {confirmed} sats (confirmed) + {unconfirmed} sats (unconfirmed)"
    )
    output_fn(
        f"Total {total} sats · {scanned} addresses scanned · {tip_label}"
    )
