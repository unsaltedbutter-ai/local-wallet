# skipped-tests.md — the 7 skipped tests: what they are, why they skip, fix-or-remove

Baseline context: **2897 passed / 7 skipped** (run 2026-09-11, this venv). All 7 skips are
**env-gated by design** — the default suite stays hermetic (offline, no hardware, no
multi-GB loads). Two of them, however, skip *unnecessarily* in this repo because their
gate is narrower than reality — see #1/#2.

Verified by: `.venv/bin/python -m pytest -rs -q` → the exact skip reasons below.

---

## 1. `tests/test_agent_prompt_context.py:349` — `test_grammar_parses_under_llama_cpp`

**What it tests:** that the real llama-cpp GBNF parser can build `envelope.gbnf` at
generate-time (the TCK-P6-004 regression pin — llama-cpp-python 0.3.35 segfaulted on
underscore rule names / multi-line rules, and `LlamaGrammar.from_string` is a no-op
holder, so this is the *only* test that catches a grammar dialect regression).

**Why it skips here:** two gates — wheel present (`llama_cpp` importable) **✓**, but
`MODEL_FILE_AVAILABLE` **✗**. The gate helper (`_model_file_available()`,
test_agent_prompt_context.py:54-56) checks **only** the `LOCALWALLET_MODEL_PATH` env
var — it does not know about the `models/bin/` default path. The pinned GGUF
(`models/bin/gemma-4-E2B-it-Q4_K_M.gguf`, 3.1 GB) **is downloaded**, so this skip is
**unnecessary in this repo**. (`tests/test_grammar_conformance.py:57-61` got this
right: its gate accepts env **or** the models/bin default — which is why its own
model-gated tests pass.)

**Fix (cheap, recommended):** align the gate — env path OR the models/bin default,
same resolution `test_grammar_conformance.py` uses. The test only does a vocab-only
grammar build (weights untouched), so it costs ~seconds. → Filed as **TCK-TEST-001**.

**Remove?** No — it is the live grammar-dialect regression pin.

## 2. `tests/test_agent_prompt_context.py:495` — `test_real_generation_returns_string`

**What it tests:** the real llama.cpp generation path end-to-end (loads the GGUF,
generates against the grammar, asserts a non-empty string comes back).

**Why it skips here:** same over-narrow gate as #1 (env-only model check). Unlike #1
it is a *real* full-model load — minutes-class and heavy, which is why it's gated at
all.

**Fix:** same gate alignment as #1 (TCK-TEST-001). With the models/bin fallback the
test will actually run in the default suite on a bootstrapped checkout — acceptable
(the suite already runs it whenever the env var is set; the run cost is ~a minute on
an M-series mac). If suite-time ever matters, a `LOCALWALLET_SKIP_SLOW_LLM=1` opt-out
is the upgrade path — don't build it now.

**Remove?** No — it's the only automated proof the real generation path works.

## 3. `tests/test_e2e_skeleton.py:5438` — `test_live_mainnet_balance_via_mempool_space`

**What it tests:** the real Phase-0 AC path against public mempool.space mainnet:
scan → store → balance → new_address index-0 derivation.

**Why it skips:** `LOCALWALLET_E2E_LIVE=1` unset — deliberate, keeps the suite
deterministic/offline (a public-API dependency must never flake the default run).

**Fix:** run it manually once in a while:
`LOCALWALLET_E2E_LIVE=1 LOCALWALLET_E2E_ZPUB=<zpub> pytest tests/test_e2e_skeleton.py -k live`.
Worth doing after backend/scan changes. No fixture zpub needed if you pass your own.

**Remove?** No — it is literal AC #1, continuously valuable as a manual smoke.

## 4. `tests/test_phase1_ac.py:630` — `test_live_phase1_ac_explorer_crosscheck_sheet`

**What it tests:** the literal Phase-1 AC sign-off aid: gap-30 scan of a funded
mainnet zpub, prints a human comparison sheet (balance, utxo/tx counts, per-branch
max_used) to cross-check against mempool.space/Electrum per docs/phase1-ac.md.

**Why it skips:** `LOCALWALLET_E2E_LIVE=1` **and** `LOCALWALLET_AC_ZPUB` both unset.

**Fix:** runnable **today** — the funded mainnet wallet has existed since MW-3
(2026-09-06):
`LOCALWALLET_E2E_LIVE=1 LOCALWALLET_AC_ZPUB=<zpub> pytest tests/test_phase1_ac.py -k live -s`.
Good candidate to run once after the MW-16/17 verification passes to re-confirm the
literal Phase-1 AC on current code.

**Remove?** No — it's the documented AC procedure's automation.

## 5. `tests/test_remote_runtime.py:498` — `test_live_smoke_remote_llm_plumbing`

**What it tests:** live reachability + response shape of the ADR-0007 remote
OpenAI-compatible bridge (`notible.local:8083/v1`). Explicitly NOT an eval signal —
the bridge model is a different capability class than the pinned E2B.

**Why it skips:** `LOCALWALLET_LLM_LIVE=1` unset — needs the user's LAN endpoint up.

**Fix:** only meaningful while the ADR-0007 bridge exists. The bridge is **temporary**
(removal criterion: pinned GGUF bootstrap — which is DONE per MW-2). The in-process
llama.cpp runtime is the runtime of record and is covered by #2.

**Remove?** **Candidate for removal** the day TCK-P6-002/ADR-0007 retirement lands;
until then keep (harmless, and the bridge is still the dev fallback documented in
ADR-0007).

## 6. `tests/test_signer_hwi.py:989` — `test_live_hwi_enumerate_no_device_is_absent_error`

**What it tests:** the real hwilib HID enumeration path with no device attached —
signing must refuse via the clean `DeviceAbsentError` path (or mismatch/locked if a
device happens to be plugged in).

**Why it skips:** `LOCALWALLET_HWI_LIVE=1` unset — real HID access, no device in
sandboxed runs.

**Fix:** cheap and safe to run locally any time (no device interaction beyond
enumerate): `LOCALWALLET_HWI_LIVE=1 pytest tests/test_signer_hwi.py -k live`.
The mocked suites already cover the logic matrix; this only proves the real wheel +
HID layer wires up. MW-4 (Jade live) already exercised far more than this.

**Remove?** No — it's the only test that runs real hwilib enumeration.

## 7. `tests/test_sparrow_ac.py:235` — `test_dump_fixture_for_sparrow_import`

**What it tests:** not an assertion suite — it **writes** the canonical fixture PSBT
(`.psbt` + summary JSON into pytest tmp) so a human can import it into Sparrow
(manual step of docs/sparrow-ac.md).

**Why it skips:** `LOCALWALLET_DUMP_PSBT=1` unset — writing artifacts is pointless
unless a human is about to import them.

**Fix:** run on demand: `LOCALWALLET_DUMP_PSBT=1 pytest tests/test_sparrow_ac.py -k dump -s`.
**Already served its purpose:** MW-5 closed 2026-09-08 (user verified both file forms
import cleanly into Sparrow).

**Remove?** **Keep** — it regenerates the Sparrow fixture whenever PSBT/fee changes
alter the canonical bytes (it was re-pinned once already, TCK-HW-003). Cost: one
dormant test. Alternative (don't bother): fold the dump into a `--dump-artifacts`
flag of the AC doc procedure.

---

## Summary table

| # | Test | Gate | Skips unnecessarily here? | Action |
|---|------|------|---------------------------|--------|
| 1 | grammar parses under llama.cpp | wheel + env-only model check | **YES** (GGUF in models/bin) | fix gate → TCK-TEST-001 |
| 2 | real generation returns string | wheel + env-only model check | **YES** (same) | fix gate → TCK-TEST-001 |
| 3 | live mainnet balance E2E | `LOCALWALLET_E2E_LIVE=1` | no (by design) | run manually post-backend-changes |
| 4 | live Phase-1 AC sheet | `LIVE=1` + `AC_ZPUB` | no (by design) | runnable now (funded wallet since MW-3) |
| 5 | live bridge smoke | `LOCALWALLET_LLM_LIVE=1` | no (by design) | keep until ADR-0007 retirement, then remove |
| 6 | live HWI enumerate | `LOCALWALLET_HWI_LIVE=1` | no (by design) | cheap, run occasionally |
| 7 | Sparrow PSBT dump | `LOCALWALLET_DUMP_PSBT=1` | no (by design) | keep as regeneration aid (MW-5 closed) |

**Net:** nothing to remove today; one concrete fix (TCK-TEST-001, gate alignment)
turns 2 of the 7 into real passes on any bootstrapped checkout; the other 5 are
correctly-gated manual/deferred-run aids that stay.

*Note: HANDOFF §6 still describes the older "6 skipped" inventory (from the pre-LAUNCH
era). Its substance is still accurate; this document supersedes it as the authoritative
skip inventory — update HANDOFF §6 pointer at the next doc sweep.*
