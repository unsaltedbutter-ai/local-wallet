"""Grammar for the v0 intent envelope.

This package exposes the GBNF grammar that constrains model decoding to the
canonical envelope contract (see ``envelope.gbnf``). ``GRAMMAR_PATH`` points
at the grammar file so the agent runtime (TCK-P0-005) can load it via
``importlib.resources`` or ``Path`` and pass it to ``LlamaGrammar``.

IMPORTANT: this grammar maps 1:1 to the pydantic envelope schema v0 and MUST
be updated together with it (see docs/adr/0002-envelope-spec.md).
"""

from pathlib import Path

GRAMMAR_PATH: Path = Path(__file__).resolve().parent / "envelope.gbnf"

__all__ = ["GRAMMAR_PATH"]
