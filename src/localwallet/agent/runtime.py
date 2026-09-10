"""Model runtime for the agent subsystem (TCK-P0-005; ADR-0001).

Wraps the local GGUF model behind :class:`ModelRuntime` so the rest of the
agent never touches llama.cpp directly. Design points:

- **Lazy wheel import.** ``llama_cpp`` is imported only when a real
  generation is first attempted, so this module (and the whole ``agent``
  package) imports cleanly on machines without the wheel installed. Tests
  inject a ``generate_fn`` stub instead. The lazy *build* gained a
  thread-safe eager entry point (:meth:`ModelRuntime.load`, TCK-LAUNCH-003)
  so the engine can preload the model at launch instead of making the
  first user query pay for it.
- **Grammar-constrained decoding.** The real path loads the envelope GBNF
  grammar (``agent/grammar/envelope.gbnf`` via :data:`GRAMMAR_PATH`) and
  passes it to every completion, so malformed envelope JSON is
  syntactically impossible at decode time (PROJECT.md §5 principle 4).
- **Model path at call time.** The default model path comes from the
  ``LOCALWALLET_MODEL_PATH`` environment variable and is read when
  generation happens — never at import time.
- **No network I/O.** This module performs no network imports and no
  network calls (lint-enforced; only ``chain/`` may network).
- **No logging.** Library code never logs (and never logs model paths or
  user text).

Sampling defaults follow PROJECT.md §7.1 (Gemma 4 model-card defaults:
top_p 0.95, top_k 64) with a v0 context budget of 8K (ADR-0006).
Temperature is 0.2 rather than the model-card 1.0: grammar-constrained
JSON emission is a low-temperature task, and 0.2 reduces envelope-wording
flakiness (live evidence: escalation on a prompt that passes 5/5 isolated).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from localwallet.agent.grammar import GRAMMAR_PATH

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_N_CTX",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TOP_K",
    "DEFAULT_TOP_P",
    "LOAD_WAIT_TIMEOUT_S",
    "MODEL_PATH_ENV_VAR",
    "GenerateFn",
    "ModelRuntime",
    "ModelRuntimeError",
    "load_grammar_text",
]

#: Environment variable that supplies the default GGUF model path.
#: Read at generation time (call time), never at import time.
MODEL_PATH_ENV_VAR: Final[str] = "LOCALWALLET_MODEL_PATH"

#: v0 context budget — ADR-0006 caps context at 8K tokens.
DEFAULT_N_CTX: Final[int] = 8192

#: Sampling defaults (PROJECT.md §7.1, Gemma 4 model-card defaults).
#: Grammar-constrained JSON envelope emission is a low-temperature task:
#: 0.2 (down from the model-card 1.0) reduces envelope-wording flakiness
#: (live evidence: a user send request that escalated while passing 5/5
#: isolated). top_p/top_k are left at the model-card values.
DEFAULT_TEMPERATURE: Final[float] = 0.2
DEFAULT_TOP_P: Final[float] = 0.95
DEFAULT_TOP_K: Final[int] = 64

#: Completion budget. ``llama_cpp.create_completion`` defaults to a 16-token
#: ceiling, which truncates EVERY non-trivial envelope mid-string (the
#: grammar-constrained output is a single complete envelope — 16 tokens is far
#: below even the shortest ``create_tx``). The bridge never hit this because an
#: OpenAI-compat server applies its own large default, and the local path was
#: never exercised past the TCK-P6-004 grammar-load segfault, so the default
#: silently shipped. This is a correctness floor, not a sampling knob: the
#: grammar forces EOS the instant ``root`` is satisfied, so the model can never
#: emit more than one envelope — the ceiling only bounds a pathological trailing
#: whitespace loop (the recursive ``ws`` rule is unbounded).
DEFAULT_MAX_TOKENS: Final[int] = 512

#: TCK-LAUNCH-003: how long a mid-flight background preload may keep a
#: first ``generate`` waiting on the construction lock before the wait is
#: reported as a busy error (the next turn retries — never a silent drop).
#: 600s is deliberately far beyond the worst real load of the pinned 3GB
#: GGUF; hitting the bound means the load thread is genuinely wedged.
LOAD_WAIT_TIMEOUT_S: Final[float] = 600.0

#: Test injection seam: given the prompt and the grammar text, return the
#: raw model completion. ``grammar_text`` is ``None`` only when a caller
#: explicitly suppresses the grammar; the runtime always passes real text.
type GenerateFn = Callable[[str, str | None], str]


class ModelRuntimeError(RuntimeError):
    """The model runtime could not produce a completion.

    Raised for: the ``llama-cpp-python`` wheel being absent, a missing or
    unset model path, a model file that does not exist, or a llama.cpp
    runtime failure. Messages name configuration values (such as the model
    path) so the user can fix them; nothing is logged anywhere.
    """


def load_grammar_text() -> str:
    """Return the envelope GBNF grammar text (read fresh from disk).

    Reads :data:`localwallet.agent.grammar.GRAMMAR_PATH` — a package
    resource, so no network and no user-visible configuration involved.
    The file is tiny; caching is deliberately skipped so tests can
    monkeypatch ``GRAMMAR_PATH`` freely.

    Raises:
        OSError: the grammar package resource is missing or unreadable
            (a packaging bug, not a runtime condition).
    """
    return GRAMMAR_PATH.read_text(encoding="utf-8")


class ModelRuntime:
    """Single-model completion runtime (llama-cpp-python, in-process).

    The heavy llama.cpp objects are created on the first real generation
    and reused afterwards. With ``generate_fn`` set, no llama.cpp machinery
    is ever constructed — the callable *is* the runtime (test seam).

    Args:
        model_path: Explicit GGUF model path. When ``None``,
            :data:`MODEL_PATH_ENV_VAR` is consulted at generation time.
        n_ctx: Context window size (v0 budget: 8K, ADR-0006).
        temperature: Sampling temperature (default 0.2); kept low because
            grammar-constrained JSON emission is a low-temperature task.
        top_p: Nucleus sampling cutoff (default 0.95).
        top_k: Top-k sampling cutoff (default 64).
        max_tokens: Completion budget (default :data:`DEFAULT_MAX_TOKENS`);
            a correctness floor that keeps llama.cpp's 16-token default from
            truncating an envelope (see :data:`DEFAULT_MAX_TOKENS`).
        generate_fn: Optional injection seam
            (``generate_fn(prompt, grammar_text) -> str``). When provided,
            llama.cpp is never imported and the model path is irrelevant.
    """

    def __init__(
        self,
        model_path: str | None = None,
        *,
        n_ctx: int = DEFAULT_N_CTX,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        top_k: int = DEFAULT_TOP_K,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        generate_fn: GenerateFn | None = None,
    ) -> None:
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.max_tokens = max_tokens
        self._generate_fn = generate_fn
        self._llama: object | None = None
        # TCK-LAUNCH-003: guards model construction so a background preload
        # (:meth:`load`) and the engine thread's first :meth:`generate`
        # cannot build the (multi-GB) runtime twice.
        self._llama_lock = threading.Lock()
        self._grammar_cls: type | None = None
        self._grammar: object | None = None
        self._grammar_text: str | None = None

    def resolve_model_path(self) -> str | None:
        """Return the effective model path, resolved **at call time**.

        Precedence: the explicit constructor argument, then the
        :data:`MODEL_PATH_ENV_VAR` environment variable. ``None`` when
        neither is set.
        """
        if self.model_path:
            return self.model_path
        return os.environ.get(MODEL_PATH_ENV_VAR) or None

    def generate(self, prompt: str, *, grammar_text: str | None = None) -> str:
        """Produce one grammar-constrained completion for ``prompt``.

        With an injected ``generate_fn``, delegates to it, passing the
        envelope grammar text (loaded from :data:`GRAMMAR_PATH` unless
        overridden). Otherwise runs the llama.cpp real path with the
        grammar applied to decoding.

        Args:
            prompt: The fully assembled prompt (system + facts +
                conversation + user turn — see ``agent/loop.py``).
            grammar_text: Optional grammar override; defaults to the
                package's ``envelope.gbnf`` text.

        Returns:
            The raw model output string. Callers must treat it as
            untrusted input — the only sanctioned consumer is
            :func:`localwallet.protocol.handle_raw`.

        Raises:
            ModelRuntimeError: the runtime is unusable (wheel absent,
                model path unset/missing) or generation failed.
        """
        text = grammar_text if grammar_text is not None else load_grammar_text()
        if self._generate_fn is not None:
            return self._generate_fn(prompt, text)
        return self._generate_with_llama(prompt, text)

    def load(self) -> None:
        """Build the llama runtime NOW instead of lazily (TCK-LAUNCH-003
        preload hook; also what the lazy first ``generate`` funnels into).

        Idempotent and thread-safe by construction: the (multi-GB) build
        happens exactly once under ``_llama_lock``. A background preload
        thread and the engine thread's first :meth:`generate` may race
        here by design — whoever loses waits on the lock and then finds
        the finished runtime, so the first query WAITS for an in-flight
        load (never a double build, never a silent drop). Only a load
        that still holds the lock past :data:`LOAD_WAIT_TIMEOUT_S` (a
        wedged read — no timeout could rescue that either) is surfaced
        as a busy :class:`ModelRuntimeError` the next turn retries. With
        an injected ``generate_fn`` there is nothing to build.

        Raises:
            ModelRuntimeError: same conditions as the lazy path (wheel
                absent, path unset/missing, llama failure), or the
                bounded wait on a concurrent load expiring.
        """
        if self._generate_fn is not None:
            return
        self._ensure_llama()

    def _generate_with_llama(self, prompt: str, grammar_text: str) -> str:
        """Real llama.cpp path: cached model + cached grammar, one call.

        ``Llama.__call__`` returns the full non-streaming completion mapping,
        but the runtime contract (mirrored by the injected ``generate_fn``
        seam and by :class:`RemoteOpenAIRuntime`, which unwraps
        ``choices[0].message.content``) is the raw completion *text* — so
        unwrap ``choices[0]["text"]`` here. This was latent only because the
        TCK-P6-004 grammar-load segfault meant this path never returned a
        value, so the mapping leaked straight through to ``handle_raw``.
        """
        llama = self._ensure_llama()
        grammar = self._ensure_grammar(grammar_text)
        result = llama(  # type: ignore[operator]
            prompt=prompt,
            grammar=grammar,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            max_tokens=self.max_tokens,
        )
        try:
            return result["choices"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            # Value-free: never leak prompt/model text or raw completions.
            msg = "llama.cpp returned an unexpected completion shape"
            raise ModelRuntimeError(msg) from exc

    def _ensure_llama(self) -> object:
        """Lazily import llama_cpp and load the model (first use only).

        The import lives here — not at module top level — so this module
        imports cleanly without the wheel installed (TCK-P0-005).

        TCK-LAUNCH-003: the build is lock-guarded so a background preload
        (:meth:`load`) and an engine-thread ``generate`` cannot both
        construct the model. The bounded acquire is the WAIT primitive
        the first query rides: a concurrent in-flight load releases the
        lock when it finishes (successfully or not), so the waiter then
        either uses the loaded model or constructs it itself.
        """
        llama = self._llama
        if llama is not None:
            return llama
        if not self._llama_lock.acquire(timeout=LOAD_WAIT_TIMEOUT_S):
            msg = "the model is still loading — try again"
            raise ModelRuntimeError(msg)
        try:
            llama = self._llama
            if llama is not None:
                return llama
            try:
                from llama_cpp import (  # deliberate lazy import (ADR-0001)
                    Llama,
                    LlamaGrammar,
                )
            except ImportError as exc:
                msg = (
                    "llama-cpp-python is not installed; install it to run the local "
                    "model, or inject a generate_fn for testing"
                )
                raise ModelRuntimeError(msg) from exc

            model_path = self.resolve_model_path()
            if not model_path:
                msg = f"no model path configured: pass model_path= or set {MODEL_PATH_ENV_VAR}"
                raise ModelRuntimeError(msg)
            if not Path(model_path).is_file():
                raise ModelRuntimeError(f"model file not found: {model_path}")

            self._llama = Llama(model_path=model_path, n_ctx=self.n_ctx, verbose=False)
            self._grammar_cls = LlamaGrammar
            return self._llama
        finally:
            self._llama_lock.release()

    def _ensure_grammar(self, grammar_text: str) -> object:
        """Compile (and cache) the GBNF grammar for constrained decoding."""
        if self._grammar is not None and self._grammar_text == grammar_text:
            return self._grammar
        grammar_cls = self._grammar_cls
        if grammar_cls is None:  # pragma: no cover — always set together with _llama
            raise ModelRuntimeError("llama runtime is not initialized")
        self._grammar = grammar_cls.from_string(grammar_text)
        self._grammar_text = grammar_text
        return self._grammar
