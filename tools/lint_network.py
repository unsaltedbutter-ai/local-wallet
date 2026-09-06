#!/usr/bin/env python3
"""AST-based lint: forbid network/shell imports outside src/localwallet/chain/.

Only ``src/localwallet/chain/`` may import network modules (or shell out),
plus exactly ONE additional file: ``src/localwallet/agent/remote_runtime.py``
(the ADR-0007 TEMPORARY remote-LLM debug bridge — see
:data:`AGENT_LLM_TRANSPORT_FILES`) and the whole ``src/localwallet/node/``
package (the ADR-0016 localhost node doctor — see :data:`NODE_NETWORK_DIRS`).
Everything else outside ``chain/`` — including every other ``agent/`` file and
``evals/`` — stays banned. The node doctor is a localhost-only Phase 4 privacy
upgrade (no public-network calls); see docs/adr/0016-localhost-node-io.md.

Dynamic imports are re-checked: ``importlib.import_module(...)`` and
``__import__(...)`` can smuggle a banned module past the static
``Import``/``ImportFrom`` scan above. When their module argument is a string
literal naming a banned top-level module it is flagged; when the argument is
anything **other** than a string literal (a variable, an expression, or a
missing argument) the call is flagged *conservatively* (file + line, value-free)
rather than silently passed — we cannot prove it is safe, so we fail closed
(see :data:`DYNAMIC_IMPORT_BANNED`).

Unparseable ``.py`` files are reported as errors (fail loud) and make the lint
exit nonzero — never silently skipped.

Run directly (``python tools/lint_network.py``) or import ``check_tree``.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "localwallet"
CHAIN_DIR = SRC_ROOT / "chain"

# ADR-0007 (TEMPORARY remote-LLM debug bridge): network imports are also
# permitted in exactly ONE file outside chain/ — the OpenAI-compatible
# remote runtime transport. Paths are relative to the lint root (SRC_ROOT
# in production). Retire this entry together with ADR-0007 when the pinned
# E2B GGUF bootstrap lands (or amend it only via a new ADR).
AGENT_LLM_TRANSPORT_FILES: Final[tuple[str, ...]] = ("agent/remote_runtime.py",)

# ADR-0016 (Phase 4 localhost node doctor): the whole node/ package may use
# network modules (httpx) to probe the user's OWN local daemon on loopback.
# This is the Phase 4 privacy upgrade (moving chain I/O onto the user's
# machine), NOT a leak — every request targets 127.0.0.1/localhost and nothing
# leaves the machine. Directory names are relative to the lint root (SRC_ROOT).
# Amend only via a new ADR.
NODE_NETWORK_DIRS: Final[tuple[str, ...]] = ("node",)

# Top-level module names that imply network or shell access. Matched by their
# *top-level* component, so dotted forms (``xmlrpc.client``,
# ``multiprocessing.connection``, ``urllib.request``, ...) resolve to their ban.
# ``asyncio`` is deliberately NOT here: it is legitimate local concurrency and
# is used across the codebase; only its *outbound socket* entry points are
# banned (attribute-level, see ASYNCIO_OUTBOUND_APIS).
BANNED_TOP_LEVELS = {
    # --- pre-existing ---
    "urllib", "http", "httpx", "requests", "socket", "ssl",
    "aiohttp", "websockets", "ftplib", "smtplib", "subprocess",
    # --- TCK-SEC-003 additions (each with rationale) ---
    "poplib",           # POP3 client — outbound network (legacy plaintext)
    "imaplib",          # IMAP4 client — outbound network
    "xmlrpc",           # xmlrpc.client / xmlrpc.server — HTTP RPC over the wire
    "multiprocessing",  # multiprocessing.connection — cross-process sockets
}

# asyncio's *outbound socket* APIs. asyncio itself is allowed (it is local
# concurrency), but these open real sockets to a peer. Attribute-level match
# (consistent with the ``os.system`` call check below) so ordinary use of
# ``asyncio.run``/``gather``/``sleep`` on local coroutines stays legal.
ASYNCIO_OUTBOUND_APIS = frozenset(
    {
        "open_connection",
        "create_connection",
        "create_server",
        "start_server",
    }
)

# Modules also banned from *dynamic* import. Dynamic import bypasses the static
# ``Import``/``ImportFrom`` scan above, so ``importlib.import_module`` and
# ``__import__`` are re-checked against this set. ``asyncio`` is included here
# (it is absent from BANNED_TOP_LEVELS) because dynamically importing it would
# let code call its outbound APIs through a fetched module object, defeating
# the attribute-level check above. Conservative: fail closed.
DYNAMIC_IMPORT_BANNED = frozenset(BANNED_TOP_LEVELS | {"asyncio"})


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    module: str


# Marker ``module`` used when a ``.py`` file cannot be parsed: the file's
# banned imports are unknowable, so we fail loud (never silent skip).
UNPARSEABLE: Final[str] = "<unparseable file>"


def _check_file(path: Path, violations: list[Violation]) -> None:
    """Scan one ``.py`` file, appending any :class:`Violation`.

    Unparseable files are reported as a violation (fail loud) rather than
    silently skipped, so a syntax error can no longer hide banned imports.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        violations.append(Violation(path, 1, UNPARSEABLE))
        return
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError):
        violations.append(Violation(path, 1, UNPARSEABLE))
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in BANNED_TOP_LEVELS:
                    violations.append(Violation(path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BANNED_TOP_LEVELS:
                violations.append(Violation(path, node.lineno, node.module.split(".")[0]))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr == "system"
            ):
                violations.append(Violation(path, node.lineno, "os.system"))
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
                and func.attr in ASYNCIO_OUTBOUND_APIS
            ):
                violations.append(Violation(path, node.lineno, f"asyncio.{func.attr}"))
            else:
                _check_dynamic_import(path, node, violations)


def _dynamic_import_name(func: ast.expr) -> str | None:
    """Human-readable name if ``func`` is a recognized dynamic-import call.

    Returns ``"importlib.import_module"``, ``"__import__"``, or ``None``.
    """
    if isinstance(func, ast.Name) and func.id == "__import__":
        return "__import__"
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
        and func.attr == "import_module"
    ):
        return "importlib.import_module"
    return None


def _dynamic_import_module_arg(call: ast.Call) -> ast.expr | None:
    """The module argument of a dynamic-import call (positional or ``name=``)."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "name":
            return kw.value
    return None


def _literal_module(arg: ast.expr | None) -> str | None:
    """Return the module string when ``arg`` is a string literal, else ``None``."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _check_dynamic_import(path: Path, call: ast.Call, violations: list[Violation]) -> None:
    """Flag ``importlib.import_module``/``__import__`` that may reach a banned module.

    A string-literal argument naming a banned module is flagged by name; any
    non-literal (or missing) argument is flagged conservatively (file + line,
    value-free) — we cannot prove it is safe, so we fail closed.
    """
    name = _dynamic_import_name(call.func)
    if name is None:
        return
    literal = _literal_module(_dynamic_import_module_arg(call))
    if literal is None:
        violations.append(Violation(path, call.lineno, f"{name}(<non-literal module>)"))
    elif literal.split(".")[0] in DYNAMIC_IMPORT_BANNED:
        violations.append(Violation(path, call.lineno, literal))


def _is_under(path: Path, dir_: Path) -> bool:
    """Return True if ``path`` is at or below ``dir_`` (like a subdir check)."""
    try:
        path.relative_to(dir_)
        return True
    except ValueError:
        return False


def check_tree(root: Path) -> list[Violation]:
    """Return all violations found under ``root`` (recursively).

    Files under ``root/chain/`` are exempt from the network/shell ban, as are
    the ADR-0007 remote-LLM transport file (:data:`AGENT_LLM_TRANSPORT_FILES`,
    relative to ``root``) and the ADR-0016 node-doctor package
    (:data:`NODE_NETWORK_DIRS`, directories relative to ``root``).
    """
    violations: list[Violation] = []
    chain_dir = root / "chain"
    transport_files = {root / rel for rel in AGENT_LLM_TRANSPORT_FILES}
    node_dirs = [root / rel for rel in NODE_NETWORK_DIRS]
    for path in sorted(root.rglob("*.py")):
        if path in transport_files:
            continue  # ADR-0007 temporary bridge — the one agent/ exception
        if _is_under(path, chain_dir):
            continue  # chain/ is the canonical networked module
        if any(_is_under(path, nd) for nd in node_dirs):
            continue  # ADR-0016 localhost node doctor — loopback-only
        _check_file(path, violations)
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]).resolve() if argv else SRC_ROOT
    violations = check_tree(root)
    if not violations:
        return 0
    for v in violations:
        rel = v.path.relative_to(root.parent.parent)
        if v.module == UNPARSEABLE:
            print(f"{rel}: unparseable file — could not scan for forbidden imports (fail closed)")
        else:
            print(
                f"{rel}:{v.lineno}: forbidden import '{v.module}' outside chain/ "
                f"(exceptions: {AGENT_LLM_TRANSPORT_FILES[0]}, ADR-0007; "
                f"{NODE_NETWORK_DIRS[0]}/, ADR-0016)"
            )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
