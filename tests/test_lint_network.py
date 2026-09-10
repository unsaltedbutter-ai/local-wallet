"""Tests for the network-import lint tool (tools/lint_network.py)."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_PATH = REPO_ROOT / "tools" / "lint_network.py"


def _load_lint():
    spec = importlib.util.spec_from_file_location("lint_network", LINT_PATH)
    module = importlib.util.module_from_spec(spec)
    # py3.14: dataclass resolution (Violation) requires the module to be in
    # sys.modules before exec_module, so register it first.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LINT = _load_lint()
SRC_ROOT = LINT.SRC_ROOT


def test_lint_passes_on_current_scaffold():
    """The shipped scaffold contains no network imports outside chain/."""
    assert LINT.check_tree(SRC_ROOT) == []


def test_lint_catches_network_import_outside_chain(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import httpx\n"
        "from urllib.request import urlopen\n"
        "import socket\n",
        encoding="utf-8",
    )
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 3
    for v in violations:
        assert v.path == evil
    modules = {v.module for v in violations}
    assert "httpx" in modules
    assert "urllib" in modules
    assert "socket" in modules


def test_lint_allows_network_import_inside_chain(tmp_path):
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    ok = chain_dir / "esplora.py"
    ok.write_text("import httpx\n", encoding="utf-8")
    assert LINT.check_tree(tmp_path) == []


# ------------------------------------------------- ADR-0007 bridge exception


def test_lint_exception_names_exactly_the_bridge_file():
    """The single lint exception is agent/remote_runtime.py (ADR-0007)."""
    assert LINT.AGENT_LLM_TRANSPORT_FILES == ("agent/remote_runtime.py",)


def test_lint_allows_network_import_in_remote_runtime_bridge(tmp_path):
    """Network imports are permitted in exactly the ADR-0007 bridge file."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    bridge = agent_dir / "remote_runtime.py"
    bridge.write_text(
        "import httpx\nfrom httpx import URL\nfrom urllib.parse import urlsplit\n",
        encoding="utf-8",
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_still_flags_network_imports_in_other_agent_files(tmp_path):
    """Every other agent/ file stays banned — the exception is not a prefix."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "remote_runtime.py").write_text("# no imports here\n", encoding="utf-8")
    (agent_dir / "loop.py").write_text("import httpx\n", encoding="utf-8")
    (agent_dir / "runtime.py").write_text("import socket\n", encoding="utf-8")
    (agent_dir / "remote_runtime_v2.py").write_text("import requests\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"loop.py", "runtime.py", "remote_runtime_v2.py"}


def test_lint_passes_on_real_tree_with_bridge_import():
    """The real remote_runtime.py (which imports httpx) lints clean."""
    assert LINT.check_tree(LINT.SRC_ROOT) == []


# ------------------------------------------------- ADR-0016 node/ exception


def test_lint_exception_names_exactly_the_node_dir():
    """The node-doctor exception is exactly the node/ package (ADR-0016)."""
    assert LINT.NODE_NETWORK_DIRS == ("node",)


def test_lint_allows_network_import_inside_node_dir(tmp_path):
    """Network imports are permitted anywhere under node/ (loopback-only)."""
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "detect.py").write_text("import httpx\n", encoding="utf-8")
    (node_dir / "__init__.py").write_text(
        "from localwallet.node.detect import foo\n", encoding="utf-8"
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_does_not_exempt_a_sibling_file_named_node(tmp_path):
    """The node/ exception is a directory, not a name prefix."""
    (tmp_path / "node.py").write_text("import httpx\n", encoding="utf-8")
    (tmp_path / "node_extra.py").write_text("import socket\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"node.py", "node_extra.py"}


def test_lint_still_flags_network_imports_in_other_packages(tmp_path):
    """Unrelated packages stay banned — only chain/, agent/remote_runtime.py,
    and node/ are exempt."""
    (tmp_path / "wallet.py").write_text("import httpx\n", encoding="utf-8")
    (tmp_path / "store.py").write_text("import socket\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"wallet.py", "store.py"}


# ------------------------------------------------- ADR-0024 ui/web exception


def test_lint_exception_names_exactly_the_web_server_dir():
    """The web-server exception is exactly the ui/web/ package (ADR-0024 §10,
    pinned by TCK-WEB-001): a directory list mirroring NODE_NETWORK_DIRS."""
    assert LINT.WEB_SERVER_DIRS == ("ui/web",)


def test_lint_allows_inbound_loopback_imports_inside_ui_web(tmp_path):
    """ui/web/** may import the stdlib HTTP server (INBOUND loopback listen);
    the exception is scoped to the package (ADR-0024 decision 10)."""
    web_dir = tmp_path / "ui" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "server.py").write_text(
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "import socketserver\n",
        encoding="utf-8",
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_does_not_exempt_a_sibling_file_named_web(tmp_path):
    """The ui/web exception is a DIRECTORY, not a name prefix: a sibling
    ``ui/web.py`` (or a sibling package dir) stays flagged."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "web.py").write_text("from http.server import ThreadingHTTPServer\n", encoding="utf-8")
    (ui_dir / "webby").mkdir()
    (ui_dir / "webby" / "srv.py").write_text("import httpx\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert {v.path.name for v in violations} == {"web.py", "srv.py"}


def test_lint_web_exception_is_not_an_outbound_pass(tmp_path):
    """The exception text names the inbound-loopback rationale (ADR-0024 §10
    records it); outside ui/web/, http imports still fire everywhere."""
    (tmp_path / "transport.py").write_text("import http\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].module == "http"


# ------------------------------------------------- TCK-SEC-003 ban extensions


def test_lint_catches_extended_ban_list(tmp_path):
    """poplib/imaplib/xmlrpc/multiprocessing now fire outside chain/."""
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import poplib\n"
        "import imaplib\n"
        "import xmlrpc.client\n"
        "import multiprocessing.connection\n",
        encoding="utf-8",
    )
    modules = {v.module for v in LINT.check_tree(tmp_path)}
    assert modules == {"poplib", "imaplib", "xmlrpc.client", "multiprocessing.connection"}


def test_lint_flags_asyncio_outbound_apis(tmp_path):
    """asyncio's outbound socket entry points are flagged outside chain/."""
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import asyncio\n"
        "async def a():\n"
        "    r, w = await asyncio.open_connection('host', 443)\n"
        "asyncio.create_connection('host', 443)\n"
        "asyncio.create_server(None, '127.0.0.1', 0)\n"
        "asyncio.start_server(None, '127.0.0.1', 0)\n",
        encoding="utf-8",
    )
    modules = {v.module for v in LINT.check_tree(tmp_path)}
    assert modules == {
        "asyncio.open_connection",
        "asyncio.create_connection",
        "asyncio.create_server",
        "asyncio.start_server",
    }


def test_lint_allows_asyncio_local_concurrency(tmp_path):
    """Ordinary asyncio use (no sockets) is NOT flagged."""
    (tmp_path / "ok.py").write_text(
        "import asyncio\n"
        "async def task():\n"
        "    await asyncio.sleep(1)\n"
        "async def main():\n"
        "    await asyncio.gather(task(), task())\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_flags_subprocess_import(tmp_path):
    """The subprocess shell-out ban is exercised."""
    (tmp_path / "evil.py").write_text("import subprocess\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].module == "subprocess"


def test_lint_flags_os_system_call(tmp_path):
    """The os.system call check is exercised."""
    (tmp_path / "evil.py").write_text("import os\nos.system('ls')\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].module == "os.system"


def test_lint_allows_os_getenv_call(tmp_path):
    """Benign os.* calls (non-system) are not flagged."""
    (tmp_path / "ok.py").write_text("import os\nx = os.getenv('PATH')\n", encoding="utf-8")
    assert LINT.check_tree(tmp_path) == []


def test_lint_does_not_exempt_chain_lookalike_dir(tmp_path):
    """A directory that merely looks like chain/ is NOT exempted."""
    (tmp_path / "chain_extra").mkdir()
    (tmp_path / "chain_extra" / "probe.py").write_text("import httpx\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].path.name == "probe.py"
    assert violations[0].module == "httpx"


# ------------------------------------------------------- dynamic imports


def test_lint_flags_dynamic_import_of_banned_literal(tmp_path):
    """importlib.import_module / __import__ with a banned literal are flagged."""
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import importlib\n"
        "importlib.import_module('httpx')\n"
        "importlib.import_module('xmlrpc.client')\n"
        "importlib.import_module(name='asyncio')\n"
        "__import__('socket')\n",
        encoding="utf-8",
    )
    modules = {v.module for v in LINT.check_tree(tmp_path)}
    assert modules == {"httpx", "xmlrpc.client", "asyncio", "socket"}


def test_lint_flags_dynamic_import_non_literal(tmp_path):
    """A non-literal module arg is flagged conservatively (file+line, value-free)."""
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import importlib\n"
        "mod = 'httpx'\n"
        "importlib.import_module(mod)\n",
        encoding="utf-8",
    )
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == evil
    assert "importlib.import_module" in violations[0].module
    assert "<non-literal" in violations[0].module


def test_lint_allows_dynamic_import_of_safe_literal(tmp_path):
    """Dynamic import of a non-banned module is fine."""
    (tmp_path / "ok.py").write_text(
        "import importlib\n"
        "importlib.import_module('json')\n"
        "__import__('os')\n",
        encoding="utf-8",
    )
    assert LINT.check_tree(tmp_path) == []


def test_dynamic_import_allowed_inside_chain(tmp_path):
    """chain/ remains exempt even for dynamic imports."""
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "esplora.py").write_text(
        "import importlib\nimportlib.import_module('httpx')\n", encoding="utf-8"
    )
    assert LINT.check_tree(tmp_path) == []


# ----------------------------------------------------- unparseable files


def test_lint_reports_unparseable_file(tmp_path):
    """A syntax-error file is reported, never silently skipped."""
    bad = tmp_path / "broken.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == bad
    assert violations[0].module == LINT.UNPARSEABLE


def test_lint_main_exits_nonzero_on_unparseable(tmp_path, capsys):
    """main() exits nonzero and names the unparseable file."""
    bad = tmp_path / "broken.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    rc = LINT.main([str(tmp_path)])
    assert rc == 1
    assert "broken.py" in capsys.readouterr().out



# --------------------- ADR-0001 amendment: per-file module exceptions (002)


def test_file_module_exception_is_scoped_to_one_file_and_module(tmp_path):
    """TCK-LAUNCH-002: app.py's grant is EXACTLY ``subprocess`` — another
    file importing subprocess is still flagged, and app.py importing any
    other banned module (network!) is still flagged."""
    root = tmp_path / "localwallet"
    root.mkdir()
    (root / "app.py").write_text("import subprocess\n", encoding="utf-8")
    assert LINT.check_tree(root) == []
    (root / "other.py").write_text("import subprocess\n", encoding="utf-8")
    violations = LINT.check_tree(root)
    assert [v.module for v in violations] == ["subprocess"]
    assert violations[0].path.name == "other.py"
    (root / "other.py").unlink()
    (root / "app.py").write_text(
        "import subprocess\nimport httpx\n", encoding="utf-8"
    )
    violations = LINT.check_tree(root)
    assert [v.module for v in violations] == ["httpx"]  # subprocess stays granted


def test_real_tree_grant_list_is_exactly_app_py():
    """The mutation gate on the exception itself: the ONLY per-file module
    grant is app.py's subprocess (the model-download child), and the real
    tree passes WITH it (test_lint_passes_on_current_scaffold)."""
    assert LINT.FILE_MODULE_EXCEPTIONS == {
        "app.py": frozenset({"subprocess"})
    }


def test_app_py_actually_needs_the_grant():
    """If app.py ever drops its subprocess import, the grant should be
    reconsidered — pin that the download orchestration is what uses it."""
    app_py = SRC_ROOT / "app.py"
    source = app_py.read_text(encoding="utf-8")
    assert "subprocess.Popen(" in source
    # no shell, ever: the one spawn is an argument list
    assert "shell=True" not in source
