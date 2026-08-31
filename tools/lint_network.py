#!/usr/bin/env python3
"""AST-based lint: forbid network/shell imports outside src/localwallet/chain/.

Only ``src/localwallet/chain/`` may import network modules (or shell out).
Run directly (``python tools/lint_network.py``) or import ``check_tree``.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "localwallet"
CHAIN_DIR = SRC_ROOT / "chain"

# Top-level module names that imply network or shell access.
# NOTE: ``asyncio`` is deliberately NOT here (it is local concurrency).
BANNED_TOP_LEVELS = {
    "urllib", "http", "httpx", "requests", "socket", "ssl",
    "aiohttp", "websockets", "ftplib", "smtplib", "subprocess",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    module: str


def _check_file(path: Path, violations: list[Violation]) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return  # unparseable files fail elsewhere, not here

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in BANNED_TOP_LEVELS:
                    violations.append(Violation(path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BANNED_TOP_LEVELS:
                violations.append(Violation(path, node.lineno, node.module))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr == "system"
            ):
                violations.append(Violation(path, node.lineno, "os.system"))


def check_tree(root: Path) -> list[Violation]:
    """Return all violations found under ``root`` (recursively).

    Files under ``root/chain/`` are exempt from the network/shell ban.
    """
    violations: list[Violation] = []
    chain_dir = root / "chain"
    for path in sorted(root.rglob("*.py")):
        try:
            path.relative_to(chain_dir)
            continue  # chain/ is the only networked module
        except ValueError:
            pass
        _check_file(path, violations)
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]).resolve() if argv else SRC_ROOT
    violations = check_tree(root)
    if not violations:
        return 0
    for v in violations:
        rel = v.path.relative_to(SRC_ROOT.parent.parent)
        print(f"{rel}:{v.lineno}: forbidden import '{v.module}' outside chain/")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
