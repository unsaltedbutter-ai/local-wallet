"""Tests for the node doctor guidance content (src/localwallet/node/doctor.py).

Covers the advise-only invariant (no subprocess / os.system anywhere under
``node/``, enforced via AST — mirroring the network lint's approach), guidance
content completeness (every state and setup tier is present and value-free),
and the pure selection logic in :class:`NodeDoctor`.
"""

import ast
from pathlib import Path

import pytest

from localwallet.node.doctor import (
    SETUP_OPTIONS,
    STATE_ADVICE,
    NodeDoctor,
    NodeStateAdvice,
    NodeStateKind,
    SetupOption,
    SetupOptionId,
    setup_option_by_id,
)

NODE_DIR = Path(__file__).resolve().parent.parent / "src" / "localwallet" / "node"


# ------------------------------------------------------- advise-only (AST)


def test_node_package_never_shells_out():
    """The advise-only invariant: no subprocess / os.system / dynamic exec.

    AST test in the same spirit as tools/lint_network.py — it scans every
    ``.py`` file under ``node/`` and fails if it finds a banned import top-level
    module or a banned call. Detection informs and advises; it never executes
    commands (PROJECT.md §7.7). Forbidden: ``subprocess``, dynamic execution
    (``eval``/``exec``/``compile``/``__import__``), the ``importlib`` loader
    family, ``os.exec*``/``os.popen``/``os.spawn*``/``os.system``, and the
    ``pickle``/``marshal`` serializers (arbitrary-code vectors).
    """
    banned_import_tops = {"subprocess", "importlib", "pickle", "marshal", "os"}
    banned_builtins = {"eval", "exec", "compile", "__import__"}
    banned_os_attrs = {
        "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "execv", "execve",
        "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
    }
    offenders: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_import_tops:
                        offenders.append(f"{path.name}:{node.lineno}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in banned_import_tops:
                    offenders.append(f"{path.name}:{node.lineno}: imports {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in banned_builtins:
                    offenders.append(f"{path.name}:{node.lineno}: calls {func.id}")
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr in banned_os_attrs
                ):
                    offenders.append(f"{path.name}:{node.lineno}: calls os.{func.attr}")
    assert offenders == [], "advise-only violated:\n" + "\n".join(offenders)


def test_node_package_does_not_import_os_at_all():
    """node/ should not even import ``os`` — detection is transport-only."""
    for path in sorted(NODE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert "os" not in [a.name for a in node.names], f"{path.name} imports os"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "os", f"{path.name} imports os"


# ---------------------------------------------------- content completeness


@pytest.mark.parametrize("state", list(NodeStateKind))
def test_every_state_has_advice(state):
    advice = STATE_ADVICE[state]
    assert isinstance(advice, NodeStateAdvice)
    assert advice.state is state
    assert advice.headline and advice.detail and advice.next_step


@pytest.mark.parametrize("option_id", list(SetupOptionId))
def test_every_setup_option_is_present_and_complete(option_id):
    option = setup_option_by_id(option_id)
    assert option is not None
    assert isinstance(option, SetupOption)
    assert option.id is option_id
    assert option.title and option.summary and option.steps
    assert all(isinstance(step, str) and step for step in option.steps)


def test_guidance_content_is_value_free():
    """Guidance must not contain addresses, amounts, or secret-bearing paths."""
    text = " ".join(
        [a.headline + " " + a.detail + " " + a.next_step for a in STATE_ADVICE.values()]
        + [o.title + " " + o.summary + " " + " ".join(o.steps) for o in SETUP_OPTIONS]
    ).lower()
    # No bitcoin address material / amount patterns in any copy.
    assert "bc1" not in text
    assert "sats" not in text
    assert "btc" not in text
    assert "xpub" not in text


# -------------------------------------------------------------- selection


def test_recommend_none_found():
    doctor = NodeDoctor()
    advice = doctor.recommend(
        any_core_reachable=False, core_synced=False,
        core_auth_issue=False, indexer_reachable=False,
    )
    assert advice.state is NodeStateKind.NONE_FOUND


def test_recommend_core_ready():
    advice = NodeDoctor().recommend(
        any_core_reachable=True, core_synced=True,
        core_auth_issue=False, indexer_reachable=False,
    )
    assert advice.state is NodeStateKind.CORE_READY


def test_recommend_core_syncing():
    advice = NodeDoctor().recommend(
        any_core_reachable=True, core_synced=False,
        core_auth_issue=False, indexer_reachable=False,
    )
    assert advice.state is NodeStateKind.CORE_SYNCING


def test_recommend_core_auth_issue_takes_priority():
    advice = NodeDoctor().recommend(
        any_core_reachable=True, core_synced=True,
        core_auth_issue=True, indexer_reachable=False,
    )
    assert advice.state is NodeStateKind.CORE_AUTH_ISSUE


def test_recommend_indexer_only():
    advice = NodeDoctor().recommend(
        any_core_reachable=False, core_synced=False,
        core_auth_issue=False, indexer_reachable=True,
    )
    assert advice.state is NodeStateKind.INDEXER_ONLY


def test_setup_option_by_id_unknown_returns_none():
    assert setup_option_by_id(SetupOptionId.UMBREL_START9) is not None
