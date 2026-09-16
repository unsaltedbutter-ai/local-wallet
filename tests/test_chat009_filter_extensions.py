"""TCK-CHAT-009 — flexible UTXO/history filter extension (on CHAT-005's shapes).

USER SPEC gaps, one test family each:

* (a) SIZE — the deterministic PRE-MODEL listing intercept parses stated
  sats thresholds (thousands separators tolerated, strict <</>, bounded by
  MAX_AMOUNT_SATS) and the fuzzy "small/large coins" family resolves to
  the user's OWN coin-size settings ladder (never a hardcoded constant);
  the model never computes a size (the envelope carries no size key at
  all — no protocol shape ships with this ticket);
* (b) TIME on RECEIVE — "new" lists the store's own unconfirmed coins;
  "in <year>" resolves to the CLOSED calendar window via the new ``until``
  bound on :func:`_coin_within_since` (pending coins are arriving NOW —
  never claimed inside a past window; unresolvable times fail CLOSED to
  the existing pipeline, never fabricated);
* (c) LABEL LIKE fallback — an include exact miss runs the substring pass
  over the SAME rows before the honest empty answer (both handlers AND
  the intercept), flagged value-free by ``label_like_fallback`` + the
  ``LABEL_LIKE_NOTE_*`` hedge; an exact hit SKIPS the fallback, exclude
  NEVER falls back;
* (d) the SHARED label-word normalizer :func:`_normalize_label_word` —
  quote-stripping + case-folding + whitespace trim, the pinned contract
  TCK-CONS-003 must import (unit pins below);
* (e) the USER REPRO — 'show me my KYC coins' (capitalized/quoted) must
  answer the FILTERED listing, never all coins, through the PRODUCTION
  ``_run_turn`` chain with the model provably never consulted.

Invariants pinned throughout: values verbatim from the store, label words
never enter model context (consumed turns never reach the transcript),
refusals/hedges value-free, every unmatched phrasing falls through
UNCHANGED (never-trap). Offline: in-memory stores, fixture zpub, frozen
clock, no network (``chain/`` untouched).
"""

from __future__ import annotations

import calendar
import json
import sys
import time
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.protocol import EnvelopeValidationError, IntentName
from localwallet.store import Store, TxRecord, UtxoRecord
from localwallet.tx.flow import TxFlow
from tests.test_chat005_filters import _env, _seed_coin, _seed_tx, _world
from tests.test_e2e_skeleton import derive_fixture_addresses

ADDRS: Final[list[str]] = derive_fixture_addresses(8)

#: Frozen "now": a Tuesday in February 2026 — the year-window bound
#: resolves against this clock only.
NOW: Final[int] = calendar.timegm((2026, 2, 3, 12, 0, 0, 0, 0, 0))
DAY: Final[int] = 86_400

MANAGED_ENV_VARS: Final[tuple[str, ...]] = (
    "LOCALWALLET_GAP_LIMIT",
    "LOCALWALLET_WATCH_INTERVAL_S",
    "LOCALWALLET_UTXO_TARGET_MIN_SATS",
    "LOCALWALLET_UTXO_TARGET_MAX_SATS",
    "LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB",
)


@pytest.fixture(autouse=True)
def _clean_ladder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fuzzy-size ladder reads env/file rungs — start every test from a
    CLEAN ladder pointed at this test's tmp dir (the repo-root config.json
    must never be created or read by this suite)."""
    for var in MANAGED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("LOCALWALLET_CONFIG_PATH", str(cfg))
    return cfg


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    return NOW


def _turn_lines(store: Store, line: str) -> list[str]:
    """Drive the PRODUCTION intercept; asserts the line WAS consumed."""
    out: list[str] = []
    assert app._run_coin_filter_turn(store, line, out.append) is True
    return out


def _released(store: Store, line: str) -> bool:
    """A non-match must consume NOTHING and print NOTHING (never-trap)."""
    out: list[str] = []
    consumed = app._run_coin_filter_turn(store, line, out.append)
    assert not consumed and out == [], (line, out)
    return True


def _said(table: dict, intent: IntentName, params: dict[str, Any]) -> tuple[dict, list[str]]:
    """Dispatch one envelope through the PRODUCTION handler + narration."""
    env = _env(intent, params)
    result = table[intent](env)
    out: list[str] = []
    app._print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=env,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        out.append,
    )
    return result, out


def _rows(lines: list[str]) -> list[str]:
    return [ln for ln in lines if " sats ·" in ln]


def _seed_world() -> tuple[Store, int, dict]:
    """Coins: 150k on ADDRS[0] (labeled kyc, mid-Jan-2025), 20k on
    ADDRS[1] (~1 day old), 50M on ADDRS[2] (labeled "graduation funds",
    now), and a 5k PENDING coin on ADDRS[3]."""
    store, wid, table = _world()
    _seed_tx(store, wid, "1" * 64, direction="in", height=10, block_time=NOW - 384 * DAY)
    _seed_coin(store, wid, "1" * 64, ADDRS[0], 150_000)
    _seed_tx(store, wid, "2" * 64, direction="in", height=11, block_time=NOW - DAY)
    _seed_coin(store, wid, "2" * 64, ADDRS[1], 20_000)
    _seed_tx(store, wid, "3" * 64, direction="in", height=12, block_time=NOW)
    _seed_coin(store, wid, "3" * 64, ADDRS[2], 50_000_000)
    _seed_tx(store, wid, "4" * 64, direction="in", height=None, block_time=None)
    _seed_coin(store, wid, "4" * 64, ADDRS[3], 5_000, confirmed=0, height=None)
    store.add_address_labels(ADDRS[0], ("kyc",))
    store.add_address_labels(ADDRS[2], ("graduation funds",))
    return store, wid, table


# =========================================================================
# (d) THE SHARED NORMALIZER — pinned contract for TCK-CONS-003
# =========================================================================


class TestNormalizeLabelWord:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("KYC", "kyc"),  # case-folding
            ("kyc", "kyc"),
            ("  KYC  ", "kyc"),  # edge whitespace
            ('"KYC"', "kyc"),  # straight double quotes
            ("'KYC'", "kyc"),  # straight single quotes
            ("`KYC`", "kyc"),  # backticks
            ("\u201cKYC\u201d", "kyc"),  # curly doubles
            ("\u2018kyc\u2019", "kyc"),  # curly singles
            ('" kyc "', "kyc"),  # quotes wrapping whitespace
            ('  "KYC"  ', "kyc"),  # whitespace wrapping quotes
            ("\u201c\u201cKYC\u201d\u201d", "kyc"),  # doubled quotes
            ('"KYC', "kyc"),  # unbalanced quote
            ("KYC's", "kyc's"),  # INNER apostrophe is content, kept
            ("graduation funds", "graduation funds"),  # inner whitespace kept
            ('"Graduation Funds" ', "graduation funds"),  # mixed: all three acts
        ],
    )
    def test_normalizes_quoting_case_whitespace(self, raw: str, expected: str) -> None:
        assert app._normalize_label_word(raw) == expected

    @pytest.mark.parametrize("blank", ["", "   ", '"', "\u201c\u201d", "'\"'"])
    def test_blank_and_quote_only_words_normalize_to_empty(self, blank: str) -> None:
        # the never-match guard: "" must never LIKE every label
        assert app._normalize_label_word(blank) == ""

    @pytest.mark.parametrize(
        "raw", ['"KYC"', " KYC ", "\u201cKYC\u201d", "kyc", "graduation funds"]
    )
    def test_idempotent(self, raw: str) -> None:
        once = app._normalize_label_word(raw)
        assert app._normalize_label_word(once) == once

    def test_pinned_contract_shape(self) -> None:
        # CONS-003 consumes EXACTLY this API (the RBF-005 resolver
        # precedent): one positional word, returns str, pure, documented.
        doc = app._normalize_label_word.__doc__ or ""
        assert "_normalize_label_word" in doc
        assert "TCK-CONS-003" in doc
        assert app._normalize_label_word("\u201cKYC\u201d") == "kyc"

    def test_query_form_consumes_the_normalizer_and_drops_empties(self) -> None:
        assert app._label_query_form(["\u201cKYC\u201d", " Spearmint ", '""']) == {
            "kyc",
            "spearmint",
        }


# =========================================================================
# (e) USER REPRO at the HANDLER layer — a quoted/capitalized word the model
#     echoes verbatim still resolves (quote-stripping is not intercept-only)
# =========================================================================


class TestHandlerQuoteNormalization:
    def test_utxos_quoted_capitalized_label_matches(self) -> None:
        _store, _wid, table = _seed_world()
        for word in ('"KYC"', "KYC", "\u201cKYC\u201d", "kyc"):
            result = table[IntentName.GET_UTXOS](
                _env(IntentName.GET_UTXOS, {"label_set": [word]})
            )
            assert [u["address"] for u in result["utxos"]] == [ADDRS[0]], word
            assert "label_like_fallback" not in result  # exact hit: no fallback

    def test_history_quoted_capitalized_label_matches(self) -> None:
        _store, _wid, table = _seed_world()
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"label_set": ['"KYC"']})
        )
        assert [t["txid"] for t in result["transactions"]] == ["1" * 64]

    def test_quote_only_word_answers_empty_never_everything(self) -> None:
        _store, _wid, table = _seed_world()
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ['""']})
        )
        assert result["utxos"] == []  # include: an empty needle matches nothing
        assert "label_like_fallback" not in result


# =========================================================================
# (c) LABEL LIKE FALLBACK — handlers (model path) + ordering + hedge
# =========================================================================


class TestLabelLikeFallbackHandlers:
    def test_utxos_exact_miss_falls_back_to_substring_with_hedge(self) -> None:
        _store, _wid, table = _seed_world()
        result, lines = _said(table, IntentName.GET_UTXOS, {"label_set": ["graduation"]})
        assert [u["address"] for u in result["utxos"]] == [ADDRS[2]]
        assert result["label_like_fallback"] is True
        assert any(line == app.LABEL_LIKE_NOTE_COINS for line in lines)

    def test_history_exact_miss_falls_back_to_substring_with_hedge(self) -> None:
        _store, _wid, table = _seed_world()
        result, lines = _said(table, IntentName.GET_HISTORY, {"label_set": ["graduation"]})
        assert [t["txid"] for t in result["transactions"]] == ["3" * 64]
        assert result["label_like_fallback"] is True
        assert any(line == app.LABEL_LIKE_NOTE_TXS for line in lines)

    def test_exact_hit_skips_the_fallback(self) -> None:
        store, _wid, table = _seed_world()
        store.add_address_labels(ADDRS[1], ("kyc-old",))  # contains "kyc"
        result, lines = _said(table, IntentName.GET_UTXOS, {"label_set": ["kyc"]})
        assert [u["address"] for u in result["utxos"]] == [ADDRS[0]]  # NOT widened
        assert "label_like_fallback" not in result
        assert not any("exact label" in line for line in lines)

    def test_exclude_never_falls_back(self) -> None:
        # the LIKE reading of "kyc" WOULD hit "kyc-old" — exclude must keep
        # it: substring-DROPPING on a closest match is never offered
        store, _wid, table = _seed_world()
        store.add_address_labels(ADDRS[1], ("kyc-old",))
        result = table[IntentName.GET_UTXOS](
            _env(
                IntentName.GET_UTXOS,
                {"label_set": ["kyc"], "label_mode": "exclude"},
            )
        )
        assert {u["address"] for u in result["utxos"]} == {
            ADDRS[1],
            ADDRS[2],
            ADDRS[3],
        }
        assert "label_like_fallback" not in result

    def test_fallback_that_also_misses_answers_the_plain_empty(self) -> None:
        _store, _wid, table = _seed_world()
        result, lines = _said(table, IntentName.GET_UTXOS, {"label_set": ["zebra"]})
        assert result["utxos"] == []
        assert "label_like_fallback" not in result
        assert any("No unspent outputs." in line for line in lines)
        assert not any(line == app.LABEL_LIKE_NOTE_COINS for line in lines)
        result, lines = _said(table, IntentName.GET_HISTORY, {"label_set": ["zebra"]})
        assert result["transactions"] == []
        assert "label_like_fallback" not in result

    def test_like_needle_is_the_query_word_inside_the_stored_label(self) -> None:
        # direction pinned: "graduation" LIKEs a stored "graduation funds"
        # (needle ⊆ member), never the reverse
        _store, _wid, table = _seed_world()
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["graduation funds here"]})
        )
        assert result["utxos"] == []

    def test_fallback_hedge_is_value_free(self) -> None:
        _store, _wid, table = _seed_world()
        _result, lines = _said(table, IntentName.GET_UTXOS, {"label_set": ["graduation"]})
        blob = "".join(lines)
        assert "graduation" not in blob  # the label word never echoes back
        assert app.LABEL_LIKE_NOTE_COINS in blob


# =========================================================================
# (a) SIZE filters — the deterministic pre-model listing intercept
# =========================================================================


class TestSizeFilters:
    def test_smaller_than_sats_is_strict_and_verbatim(self) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my utxos smaller than 150000 sats")
        rows = _rows(lines)
        joined = "".join(rows)
        # 20k and the 5k pending coin qualify; the 150k coin itself does
        # NOT (strict <), nor the 50M one
        assert len(rows) == 2
        assert ADDRS[1] in joined and ADDRS[3] in joined
        assert ADDRS[0] not in joined and ADDRS[2] not in joined
        assert any(ln == "Coins under 150000 sats:" for ln in lines)

    def test_thousands_separators_tolerated(self) -> None:
        store, _wid, _table = _seed_world()
        a = _turn_lines(store, "show me my utxos smaller than 150000 sats")
        b = _turn_lines(store, "show me my utxos smaller than 150,000 sats")
        assert a == b  # identical answer, engine-normalized
        assert any(ln == "Coins under 150000 sats:" for ln in b)

    def test_large_coins_resolves_to_the_users_own_policy(self) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my large coins")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[2] in rows[0]  # above the 10M default
        head = next(ln for ln in lines if ln.startswith("Coins over"))
        assert "10000000 sats" in head and "Largest UTXO target" in head
        assert "shipped default" in head  # names the supplying rung honestly

    def test_small_coins_follow_the_env_rung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALWALLET_UTXO_TARGET_MIN_SATS", "10000")
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my small coins")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[3] in rows[0]  # only the 5k coin under 10k
        assert any("environment" in ln for ln in lines)  # rung named

    def test_above_comparator_and_unit_variants(self) -> None:
        store, _wid, _table = _seed_world()
        for line in (
            "show me my coins bigger than 10000000 sats",
            "list my coins over 10000000 satoshis",
            "show me my coins larger than 10000000",
        ):
            lines = _turn_lines(store, line)
            rows = _rows(lines)
            assert len(rows) == 1 and ADDRS[2] in rows[0], line

    def test_size_and_label_conjunctive(self) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my kyc coins smaller than 200000 sats")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[0] in rows[0]

    @pytest.mark.parametrize(
        "line",
        [
            "show me my utxos smaller than 21000000000000001 sats",  # > MAX_AMOUNT_SATS
            "show me my utxos smaller than 0 sats",  # nothing is under zero
            "show me my utxos smaller than 0.5 sats",  # fractional sats do not exist
            "show me my utxos smaller than 150000 btc",  # non-sats unit
            "show me my utxos smaller than a house",  # a "number" that is not one
            "show me my coins under 100 sats under 200 sats",  # two thresholds
            "show me my big small coins",  # policy words on both sides
            # MAJOR-1: a policy word next to ANY stated number (same side too)
            # is ambiguous — the ladder resolves only a word WITHOUT a number,
            # so a silently-narrowed same-side combo must release, never
            # AND-compose a second hidden threshold.
            "show me my big coins over 10000 sats",  # same-side above
            "show me my small coins under 10000 sats",  # same-side below
            # cross-side fuzzy+stated: "big" (above) vs "under 10000" (below)
            # lock the guard's broader release — a refactor must not
            # re-compose this into a silently-narrowed AND-composite.
            "show me my big coins under 10000 sats",
            # SECURITY: a pathological ≥4301-digit token must RELEASE, not
            # die in int() under int_max_str_digits before the bound check.
            "show me my coins over " + "9" * 5000 + " sats",
        ],
    )
    def test_unresolvable_sizes_fail_closed_to_the_pipeline(self, line: str) -> None:
        store, _wid, _table = _seed_world()
        assert _released(store, line)


# =========================================================================
# (b) TIME on RECEIVE — "new" + the closed absolute window
# =========================================================================


class TestTimeOnReceive:
    def test_new_lists_the_stores_own_unconfirmed_coins(self) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my new utxos")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[3] in rows[0]
        assert "unconfirmed" in rows[0]

    def test_new_with_nothing_pending_answers_empty(self) -> None:
        store, wid, _table = _seed_world()
        coins = store.get_utxos_for_wallet(wid)
        store.replace_utxos_for_wallet(
            wid,
            [
                UtxoRecord(
                    wallet_id=u.wallet_id,
                    txid=u.txid,
                    vout=u.vout,
                    address=u.address,
                    value_sats=u.value_sats,
                    confirmed=1,
                    height=900_000,
                )
                for u in coins
            ],
        )
        lines = _turn_lines(store, "show me my new utxos")
        assert any("No unspent outputs." in ln for ln in lines)

    def test_stored_label_named_new_wins_over_pending(self) -> None:
        # MINOR-2: "new" is resolved as a LABEL FIRST — a stored label named
        # literally "new" must be queryable, never shadowed by the pending
        # reading.
        store, _wid, _table = _seed_world()
        store.add_address_labels(ADDRS[1], ("new",))
        lines = _turn_lines(store, "show me my new coins")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[1] in rows[0]
        assert "unconfirmed" not in rows[0]  # the LABEL reading, not pending
        assert not any("exact label" in ln for ln in lines)  # an EXACT match

    def test_new_falls_back_to_pending_when_no_label_named_new(self) -> None:
        # MINOR-2 fallback: no stored label named "new" anywhere -> the TIME
        # reading (unconfirmed-only), never a release.
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my new coins")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[3] in rows[0]  # the pending coin
        assert "unconfirmed" in rows[0]

    def test_year_window_is_closed_and_honest(self, frozen_clock: int) -> None:
        store, wid, _table = _seed_world()
        # the 384-day coin (~mid-Jan-2025) and a fresh Dec-2025 coin are
        # INSIDE "in 2025"; the day-old/now coins (Feb-2026) and the
        # PENDING coin (arriving NOW) are OUTSIDE the closed window
        _seed_tx(
            store, wid, "5" * 64, direction="in", height=13,
            block_time=calendar.timegm((2025, 12, 20, 0, 0, 0, 0, 0, 0)),
        )
        _seed_coin(store, wid, "5" * 64, ADDRS[4], 77_000)
        lines = _turn_lines(store, "coins I received in 2025")
        rows = _rows(lines)
        joined = "".join(rows)
        assert len(rows) == 2 and ADDRS[0] in joined and ADDRS[4] in joined
        assert ADDRS[1] not in joined and ADDRS[2] not in joined
        assert ADDRS[3] not in joined  # pending: NOW is not "in 2025"
        assert any(ln == "Coins received in 2025:" for ln in lines)

    def test_year_window_confirmed_coin_without_tx_row_fails_closed(
        self, frozen_clock: int
    ) -> None:
        store, wid, _table = _seed_world()
        # a CONFIRMED coin whose creating tx has no row: unresolvable —
        # the window never claims it
        _seed_coin(store, wid, "9" * 64, ADDRS[5], 10_000)
        lines = _turn_lines(store, "show me my coins in 2025")
        rows = _rows(lines)
        assert ADDRS[5] not in "".join(rows)

    @pytest.mark.parametrize(
        "line",
        [
            "show me my coins received in the past month",  # the model's since route
            "show me my coins in 2030",  # a FUTURE window is never fabricated
            "show me my coins in 1899",  # pre-genesis nonsense
            "show me my coins in march",  # finer than a year: unresolvable
            "show me my coins received last week",  # relative: the model's
        ],
    )
    def test_unresolvable_times_fail_closed_to_the_pipeline(
        self, frozen_clock: int, line: str
    ) -> None:
        store, _wid, _table = _seed_world()
        assert _released(store, line)

    def test_tx_within_since_closed_window_unit(self) -> None:
        inside = TxRecord(
            wallet_id=1, txid="a" * 64, height=5,
            block_time=calendar.timegm((2025, 12, 31, 23, 59, 59, 0, 0, 0)),
            fee_sats=None, direction="in", raw_summary=None,
        )
        edge = TxRecord(
            wallet_id=1, txid="b" * 64, height=5, block_time=NOW,
            fee_sats=None, direction="in", raw_summary=None,
        )
        pending = TxRecord(
            wallet_id=1, txid="c" * 64, height=None, block_time=None,
            fee_sats=None, direction="in", raw_summary=None,
        )
        lo = calendar.timegm((2025, 1, 1, 0, 0, 0, 0, 0, 0))
        hi = calendar.timegm((2026, 1, 1, 0, 0, 0, 0, 0, 0))
        assert app._tx_within_since(inside, lo, hi)
        assert not app._tx_within_since(edge, lo, hi)  # < until: Feb-2026 is OUT
        assert not app._tx_within_since(pending, lo, hi)  # pending: NOW, not 2025
        assert app._tx_within_since(pending, lo)  # open window: newest-by-convention kept


# =========================================================================
# (c)/(e) THE INTERCEPT — user phrasings, the repro, and never-trap
# =========================================================================


class TestListingInterceptReproAndFallback:
    @pytest.mark.parametrize(
        "line",
        [
            "show me my KYC coins",
            'show me my "KYC" coins',
            "show me my \u201cKYC\u201d coins",
            "show me my kyc coins",
            "Show me my KYC coins!",
            'can you show me my "KYC" coins?',
        ],
    )
    def test_user_repro_answers_filtered_never_all(self, line: str) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, line)
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[0] in rows[0]  # NOT the other three coins
        assert not any("exact label" in ln for ln in lines)  # an EXACT match

    def test_graduation_coins_like_fallback(self) -> None:
        store, _wid, _table = _seed_world()
        lines = _turn_lines(store, "show me my graduation coins")
        rows = _rows(lines)
        assert len(rows) == 1 and ADDRS[2] in rows[0]
        assert app.LABEL_LIKE_NOTE_COINS in lines  # hedged, never presented as exact

    def test_consumed_turn_never_reaches_the_model(self) -> None:
        store, _wid, _table = _seed_world()
        prompts: list[str] = []

        def gen(prompt: str, grammar: str | None) -> str:
            del grammar
            prompts.append(prompt)
            return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})

        table: dict[str, Any] = {
            IntentName.RESPOND: lambda env: {"text": env.params.text}
        }
        loop = AgentLoop(gen, table)
        out: list[str] = []
        app._run_turn(
            loop, TxFlow(), app.SendSession(), "show me my KYC coins",
            out.append, table=table, store=store,
        )
        assert prompts == []  # the model NEVER sees the label word (repro fixed)
        assert any(" sats ·" in ln and ADDRS[0] in ln for ln in out)

    @pytest.mark.parametrize(
        "line",
        [
            "how many kyc coins do I have?",  # question route: the model's
            "show me my recent transactions",  # history noun
            "show me coins labeled kyc",  # connector family (taught route)
            "show me my coins labeled kyc",  # ... even in this shape
            "show me my coins not labeled kyc",  # exclude family
            "show me my unlabeled coins",  # unlabeled family
            "show me my bitcoin coins",  # word names no label ANYWHERE
            "show me my coins",  # the plain listing
            "list my coins",  # ... in any verb shape
            "what is my balance",  # money shapes stay blocked
            "send 5000 sats to bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            "show me my largest coin",  # a TOP-N superlative
            "show me my top 10 coins",  # quantifier + unparsed digits
            "label bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4 as kyc",
            "coins from my savings account",  # residual names no label
        ],
    )
    def test_never_trap_everything_else_falls_through_unchanged(self, line: str) -> None:
        store, _wid, _table = _seed_world()
        assert _released(store, line)

    def test_released_line_reaches_the_model_unchanged(self) -> None:
        store, _wid, _table = _seed_world()
        prompts: list[str] = []

        def gen(prompt: str, grammar: str | None) -> str:
            del grammar
            prompts.append(prompt)
            return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})

        table: dict[str, Any] = {
            IntentName.RESPOND: lambda env: {"text": env.params.text}
        }
        loop = AgentLoop(gen, table)
        out: list[str] = []
        app._run_turn(
            loop, TxFlow(), app.SendSession(), "show me my coins",
            out.append, table=table, store=store,
        )
        assert prompts  # ordinary pipeline, untouched

    def test_listing_numbers_only_printed_addresses(self) -> None:
        store, wid, _table = _seed_world()
        _turn_lines(store, "show me my kyc coins")
        registry = {r.address for r in store.list_address_registry(wid)}
        assert registry == {ADDRS[0]}  # the CHAT-001 showing rule holds here too

    def test_store_failure_answers_the_error_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _wid, _table = _seed_world()

        def boom(_self: Store) -> dict[str, tuple[str, ...]]:
            raise app.StoreError("value-free fixture failure")

        monkeypatch.setattr(Store, "get_address_label_sets", boom)
        out: list[str] = []
        assert app._run_coin_filter_turn(store, "show me my kyc coins", out.append)
        assert any("UTXO lookup failed" in ln for ln in out)


class TestNoProtocolShapeShips:
    """The CHAT-005 envelope stays byte-identical: the ticket's (a) choice
    is the deterministic intercept BECAUSE a size/time key would put a
    model-authored number on the wire. Pinned so a future widening is a
    deliberate, eval-gated change, never a drift."""

    @pytest.mark.parametrize(
        "params",
        [
            {"below_sats": 150000},
            {"size": {"below": 150000}},
            {"since": {"year": 2025}},
            {"since": {"date": "2025-01-01"}},
        ],
    )
    def test_get_utxos_rejects_size_and_absolute_time_keys(
        self, params: dict[str, Any]
    ) -> None:
        with pytest.raises(EnvelopeValidationError):
            _env(IntentName.GET_UTXOS, params)

    def test_prompt_carries_no_size_or_absolute_time_line(self) -> None:
        from localwallet.agent.prompt import build_system_prompt

        prompt = build_system_prompt().lower()
        # TCK-CHAT-009 ships ZERO prompt lines (no eval gate): everything the
        # intercept owns must be absent from the model's instructions too.
        assert "below_sats" not in prompt
        assert "label_like" not in prompt
        assert "new utxos" not in prompt
        assert "in 2025" not in prompt
        # ...and the CHAT-005 filter section still teaches RELATIVE windows only
        assert "you never compute a timestamp" in prompt
