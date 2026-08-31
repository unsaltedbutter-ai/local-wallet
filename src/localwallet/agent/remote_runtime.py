"""TEMPORARY remote OpenAI-compatible LLM runtime (debug bridge; ADR-0007).

.. warning:: **TEMPORARY — dev/debug only.** This module exists so the agent
   can be exercised while the pinned Gemma 4 E2B GGUF bootstrap (ADR-0001)
   is still pending. It points the agent at a LAN OpenAI-compatible endpoint
   (``LOCALWALLET_LLM_BASE_URL``). ADR-0001 (llama-cpp-python, in-process)
   remains the runtime decision of record; this bridge must be deleted or
   retired when the E2B bootstrap lands (see ADR-0007's removal criterion).

Privacy note (deliberate, disclosed exception): unlike the in-process
runtime, **chat text leaves this machine** and is sent to the configured
LAN host on every generation. The CLI banner prints a one-line disclosure
whenever this runtime is selected (via :func:`debug_notice`), and the
endpoint is only ever used after an explicit environment opt-in — never
selected implicitly. Do **not** draw E2B eval conclusions from this
endpoint: the debug model is a much larger capability class (see R1), so
golden-set scores observed here say nothing about the pinned E2B model.

Design points (mirrors :mod:`localwallet.agent.runtime` and
:class:`localwallet.chain.esplora.EsploraClient`):

- **Same generate seam.** ``generate(prompt, grammar_text) -> str`` with
  ``grammar_text`` positional-or-keyword, so the instance slots into
  ``AgentLoop`` exactly like a ``GenerateFn`` (the loop wraps it in a
  ``ModelRuntime(generate_fn=...)``, which calls it positionally).
- **Config at call time.** ``base_url`` / ``model`` / ``api_key`` /
  ``timeout_s`` default from ``LOCALWALLET_LLM_*`` environment variables
  read when generation happens — never at import time (same pattern as
  :meth:`ModelRuntime.resolve_model_path`).
- **Injectable transport seam.** Like ``EsploraClient(transport=...)``: a
  callable ``(method, url, headers, json_body) -> (status_code, parsed_json
  or None)`` replaces HTTP entirely (tests inject it; no live network).
- **Grammar best-effort, validation never skipped.** The envelope GBNF
  grammar is attempted as an extra top-level ``"grammar"`` field (llama.cpp
  server convention). If the server rejects the field, the runtime
  downgrades once and remembers it — but the raw completion still goes
  through the normal 3-layer validation pipeline downstream. There is no
  output "repair" here: the only transformation applied to model content is
  ``str.strip()``.
- **Scrubbed errors.** Error messages carry status codes, exception class
  names, and the URL host only — never request/response bodies, never the
  prompt (it contains user chat text), never the API key.
- **No proxy interference.** The HTTP client is created with
  ``trust_env=False``, so proxy environment variables are deliberately
  ignored and egress goes only to the configured host.
- **No logging.** Library code never logs.

This is the ONLY file outside ``src/localwallet/chain/`` allowed to import
network modules (lint exception in ``tools/lint_network.py``,
``AGENT_LLM_TRANSPORT_FILES``; ADR-0007).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

from localwallet.agent.runtime import ModelRuntimeError

__all__ = [
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TOP_P",
    "LLM_API_KEY_ENV_VAR",
    "LLM_BASE_URL_ENV_VAR",
    "LLM_MODEL_ENV_VAR",
    "LLM_TIMEOUT_ENV_VAR",
    "RemoteOpenAIRuntime",
    "TransportFn",
    "debug_notice",
    "host_only",
]

#: Environment variable supplying the OpenAI-compatible base URL
#: (e.g. ``http://notible.local:8083/v1``). Read at generation time.
LLM_BASE_URL_ENV_VAR: Final[str] = "LOCALWALLET_LLM_BASE_URL"

#: Environment variable supplying the model id to request. Read at
#: generation time.
LLM_MODEL_ENV_VAR: Final[str] = "LOCALWALLET_LLM_MODEL"

#: Optional environment variable supplying a bearer API key. Sent ONLY when
#: non-empty; never logged and never included in error messages.
LLM_API_KEY_ENV_VAR: Final[str] = "LOCALWALLET_LLM_API_KEY"

#: Optional environment variable overriding the per-request timeout
#: (seconds, a positive number). Read at generation time.
LLM_TIMEOUT_ENV_VAR: Final[str] = "LOCALWALLET_LLM_TIMEOUT_S"

#: Default per-request timeout in seconds.
DEFAULT_TIMEOUT_S: Final[float] = 120.0

#: Sampling defaults (mirror :mod:`localwallet.agent.runtime`; PROJECT.md
#: §7.1 model-card defaults).
DEFAULT_TEMPERATURE: Final[float] = 1.0
DEFAULT_TOP_P: Final[float] = 0.95

#: Identifies the client to the endpoint (consistent with the chain client).
_USER_AGENT: Final[str] = "local-wallet/0.1.0 (remote LLM debug bridge; ADR-0007)"

#: Test injection seam mirroring ``EsploraClient(transport=...)``: perform
#: one HTTP request and return ``(status_code, parsed_json_or_None)``.
#: ``parsed_json_or_None`` is ``None`` when the body was not valid JSON.
type TransportFn = Callable[[str, str, Mapping[str, str], dict[str, object]], tuple[int, object | None]]


def host_only(url: str) -> str:
    """Return the ``host[:port]`` portion of ``url`` for scrubbed messages.

    The port is omitted when the URL does not carry an explicit one. On an
    unparseable URL, ``"(unparseable host)"`` is returned — this helper is
    used on error paths, so it must never raise and never echo the path,
    query, or any other part of the URL.
    """
    try:
        parsed = httpx.URL(url)
        host = parsed.host
    except httpx.InvalidURL:
        return "(unparseable host)"
    if not host:
        return "(unknown host)"
    port = parsed.port
    return f"{host}:{port}" if port is not None else host


def debug_notice(base_url: str, model: str | None) -> str:
    """Build the one-line startup disclosure for the remote debug bridge.

    Shown by the CLI banner and by ``evals/run_evals.py --model`` whenever
    the remote runtime is selected. Contains the host:port and the model id
    only — never the API key, never any URL path.
    """
    shown_model = model.strip() if isinstance(model, str) and model.strip() else "(model unset)"
    return (
        f"DEBUG: using remote LLM {host_only(base_url)} ({shown_model}) — "
        "chat text leaves this machine."
    )


class RemoteOpenAIRuntime:
    """TEMPORARY debug bridge: generate via an OpenAI-compatible endpoint.

    Implements the exact :class:`~localwallet.agent.runtime.ModelRuntime`
    generate seam (``generate(prompt, grammar_text) -> str``) against a
    remote OpenAI-compatible ``/chat/completions`` endpoint, so the agent
    can run before the pinned E2B GGUF is bootstrapped. See the module
    docstring for the temporary status (ADR-0007), the privacy disclosure,
    and the no-eval-conclusions caveat.

    Error contract: every failure raises
    :class:`~localwallet.agent.runtime.ModelRuntimeError` with a scrubbed
    message (status codes, exception class names, URL host only — no
    bodies, no prompt text, no API key). There are no transient retries;
    the single grammar-downgrade retry described below is the only retry.

    Args:
        base_url: Explicit OpenAI-compatible base URL. When ``None``,
            :data:`LLM_BASE_URL_ENV_VAR` is consulted at generation time.
        model: Explicit model id. When ``None``, :data:`LLM_MODEL_ENV_VAR`.
        api_key: Explicit bearer API key. When ``None``,
            :data:`LLM_API_KEY_ENV_VAR` is consulted; when the empty
            string, no key is sent and the environment is not consulted.
            Whatever the source, a key is sent only when non-empty, and it
            never appears in exceptions or output.
        timeout_s: Per-request timeout in seconds. When ``None``,
            :data:`LLM_TIMEOUT_ENV_VAR` (falling back to
            :data:`DEFAULT_TIMEOUT_S`).
        temperature: Sampling temperature (default 1.0).
        top_p: Nucleus sampling cutoff (default 0.95).
        transport: Optional injection seam
            (:data:`TransportFn`). When provided, no HTTP client is created
            and every request goes through the callable (test seam; the
            production path leaves it as ``None``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: float | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        transport: TransportFn | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self.temperature = temperature
        self.top_p = top_p
        self._transport = transport
        self._client: httpx.Client | None = None
        # Grammar downgrade flag (ADR-0007): once the endpoint has shown it
        # does not accept the top-level "grammar" field, later calls skip
        # the field entirely. Never reset within an instance.
        self._grammar_downgraded: bool = False

    # ------------------------------------------------------- resolvers

    def resolve_base_url(self) -> str | None:
        """Effective base URL: explicit arg, then env, at call time."""
        if self._base_url is not None:
            return self._base_url.strip() or None
        return os.environ.get(LLM_BASE_URL_ENV_VAR, "").strip() or None

    def resolve_model(self) -> str | None:
        """Effective model id: explicit arg, then env, at call time."""
        if self._model is not None:
            return self._model.strip() or None
        return os.environ.get(LLM_MODEL_ENV_VAR, "").strip() or None

    def resolve_api_key(self) -> str | None:
        """Effective API key: explicit arg, then env, at call time.

        An explicitly passed empty string suppresses the key entirely
        (environment not consulted). A key is returned only when non-empty.
        """
        if self._api_key is not None:
            return self._api_key.strip() or None
        return os.environ.get(LLM_API_KEY_ENV_VAR, "").strip() or None

    def resolve_timeout_s(self) -> float:
        """Effective timeout: explicit arg, then env, then the default.

        Raises:
            ModelRuntimeError: the configured timeout is not a positive
                number (the env var's value is never echoed).
        """
        if self._timeout_s is not None:
            timeout = self._timeout_s
        else:
            raw = os.environ.get(LLM_TIMEOUT_ENV_VAR, "").strip()
            if not raw:
                return DEFAULT_TIMEOUT_S
            try:
                timeout = float(raw)
            except ValueError:
                # from None: the ValueError text embeds the env value; never
                # chain it so even a traceback dump can't surface the value.
                raise ModelRuntimeError(
                    f"{LLM_TIMEOUT_ENV_VAR} must be a number of seconds"
                ) from None
        if not timeout > 0:  # also rejects NaN
            raise ModelRuntimeError(
                f"{LLM_TIMEOUT_ENV_VAR} must be a positive number of seconds"
            )
        return timeout

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the underlying HTTP client (no-op with an injected transport)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------ generate

    def __call__(self, prompt: str, grammar_text: str | None = None) -> str:
        """Alias of :meth:`generate` so the instance satisfies :data:`GenerateFn`.

        ``AgentLoop`` wraps any non-``ModelRuntime`` argument as a bare
        ``generate_fn`` callable and invokes it positionally — this makes
        the runtime object itself directly usable there.
        """
        return self.generate(prompt, grammar_text)

    def generate(self, prompt: str, grammar_text: str | None = None) -> str:
        """Produce one completion for ``prompt`` via the remote endpoint.

        ``POST {base_url}/chat/completions`` with ``{"model", "messages",
        "temperature", "top_p"}``; when ``grammar_text`` is not ``None``
        (and the endpoint has not previously rejected the field), the
        grammar is additionally attempted as a top-level ``"grammar"``
        field (llama.cpp server convention — an OpenAI-schema extension).

        Downgrade rule: if the attempt with the grammar field is answered
        ``400``, or any other 4xx whose body strings mention "grammar",
        the request is retried ONCE without the field and the downgrade is
        remembered for all subsequent calls on this instance. No other
        retry exists; every other failure raises immediately (fail closed).

        Args:
            prompt: The fully assembled prompt (system + facts +
                conversation + user turn — see ``agent/loop.py``).
            grammar_text: Optional GBNF grammar text. Positional-or-keyword
                so the instance satisfies :data:`GenerateFn` call sites
                (``AgentLoop`` wraps it and calls it positionally).

        Returns:
            The raw ``choices[0].message.content`` string, whitespace-
            stripped. Callers must treat it as untrusted input — the only
            sanctioned consumer is :func:`localwallet.protocol.handle_raw`.

        Raises:
            ModelRuntimeError: endpoint/model unset, network or timeout
                failure, non-2xx status, unparseable or unexpected response
                shape, or missing/empty choices. Messages are scrubbed.
        """
        base_url = self.resolve_base_url()
        if not base_url:
            raise ModelRuntimeError(
                f"no remote LLM endpoint configured: pass base_url= or set {LLM_BASE_URL_ENV_VAR}"
            )
        model = self.resolve_model()
        if not model:
            raise ModelRuntimeError(
                f"no remote model configured: pass model= or set {LLM_MODEL_ENV_VAR}"
            )
        url = f"{base_url.rstrip('/')}/chat/completions"

        use_grammar = grammar_text is not None and not self._grammar_downgraded
        body = self._build_body(model, prompt, grammar_text if use_grammar else None)
        headers = self._build_headers()

        status, payload = self._post(url, headers, body)
        if 200 <= status < 300:
            return self._extract_content(payload, url)

        if (
            use_grammar
            and 400 <= status < 500
            and self._body_suggests_grammar_rejected(status, payload)
        ):
            # Single downgrade retry: drop the grammar field, remember it.
            self._grammar_downgraded = True
            body = self._build_body(model, prompt, None)
            status, payload = self._post(url, headers, body)
            if 200 <= status < 300:
                return self._extract_content(payload, url)

        raise ModelRuntimeError(
            f"remote LLM request failed: status {status} (host {host_only(url)})"
        )

    # ----------------------------------------------------------- internals

    def _build_body(self, model: str, prompt: str, grammar_text: str | None) -> dict[str, object]:
        """OpenAI chat-completions payload (plus the optional grammar field)."""
        body: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if grammar_text is not None:
            body["grammar"] = grammar_text
        return body

    def _build_headers(self) -> dict[str, str]:
        """Request headers; ``Authorization`` only when a key is configured."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        api_key = self.resolve_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: dict[str, object],
    ) -> tuple[int, object | None]:
        """Perform one POST; return ``(status_code, parsed_json_or_None)``.

        Network-level failures (connection errors, timeouts — the
        ``httpx.TransportError`` family) are converted to
        :class:`ModelRuntimeError` naming only the exception class and the
        URL host. JSON parsing happens for the real-HTTP path; an
        unparseable 2xx body surfaces as ``None`` and is rejected by the
        response-shape check in :meth:`generate`.
        """
        if self._transport is not None:
            try:
                return self._transport("POST", url, headers, body)
            except httpx.TransportError as exc:
                raise self._network_error(exc, url) from exc

        client = self._ensure_client()
        try:
            response = client.post(
                url, json=body, headers=headers, timeout=self.resolve_timeout_s()
            )
        except httpx.TransportError as exc:
            raise self._network_error(exc, url) from exc
        try:
            parsed: object | None = response.json()
        except ValueError:
            parsed = None
        return response.status_code, parsed

    def _ensure_client(self) -> httpx.Client:
        """Lazily create the shared HTTP client (first real request only).

        ``trust_env=False`` deliberately ignores proxy environment variables
        (``HTTP(S)_PROXY`` etc.) so egress goes only to the configured host.
        """
        if self._client is None:
            self._client = httpx.Client(headers={"User-Agent": _USER_AGENT}, trust_env=False)
        return self._client

    @staticmethod
    def _network_error(exc: httpx.TransportError, url: str) -> ModelRuntimeError:
        """Scrubbed wrap of a transport-level failure (class name + host)."""
        return ModelRuntimeError(
            f"remote LLM request failed: network error ({type(exc).__name__}) "
            f"(host {host_only(url)})"
        )

    @staticmethod
    def _body_suggests_grammar_rejected(status: int, payload: object | None) -> bool:
        """Whether a 4xx answer indicates the ``grammar`` field is unsupported.

        Any ``400`` counts (the ticket's conservative rule). For other 4xx,
        the response payload is inspected internally for any string value
        mentioning "grammar" — the payload itself is never echoed anywhere.
        """
        if status == 400:
            return True
        return "grammar" in _string_values(payload).lower()

    @staticmethod
    def _extract_content(payload: object | None, url: str) -> str:
        """Pull ``choices[0].message.content`` out of a 2xx payload.

        Fails closed on any unexpected shape with a scrubbed message (no
        body echo). The only transformation applied to the content is
        ``str.strip()`` — no output repair of any kind.
        """
        host = host_only(url)
        if not isinstance(payload, dict):
            raise ModelRuntimeError(
                f"remote LLM response was not a JSON object (host {host})"
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelRuntimeError(f"remote LLM response had no choices (host {host})")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelRuntimeError(f"remote LLM choice was not an object (host {host})")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelRuntimeError(f"remote LLM choice had no message (host {host})")
        content = message.get("content")
        if not isinstance(content, str):
            raise ModelRuntimeError(
                f"remote LLM message content was missing or not a string (host {host})"
            )
        return content.strip()


def _string_values(node: Any) -> str:
    """Concatenate every string reachable in a JSON-ish structure.

    Internal-only helper for grammar-rejection detection; the result is
    inspected locally and never surfaces in exceptions or output.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return " ".join(_string_values(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return " ".join(_string_values(item) for item in node)
    return ""
