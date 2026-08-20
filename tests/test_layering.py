from __future__ import annotations

import ast
import os

TRADING_PATH_PACKAGES = ("config", "auth", "feed", "backtest", "model", "exec", "ops")
FORBIDDEN_ROOT = "research"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def module_files():
    for package in TRADING_PATH_PACKAGES:
        directory = os.path.join(REPO_ROOT, package)
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".py"):
                yield os.path.join(directory, entry)


def imported_roots(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_no_trading_path_module_imports_research():
    offenders = [
        os.path.relpath(path, REPO_ROOT)
        for path in module_files()
        if FORBIDDEN_ROOT in imported_roots(path)
    ]
    assert offenders == []


def test_feed_does_not_import_execution_or_model_layers():
    offenders = []
    for path in module_files():
        if not os.path.relpath(path, REPO_ROOT).startswith(("feed", "config", "auth")):
            continue
        roots = imported_roots(path)
        for forbidden in ("exec", "backtest", "model", "ops"):
            if forbidden in roots:
                offenders.append((os.path.relpath(path, REPO_ROOT), forbidden))
    assert offenders == []


def test_no_phase_one_module_can_submit_an_order():
    order_markers = ("/portfolio/events/orders", "create_order", "submit_order")
    offenders = []
    for path in module_files():
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        for marker in order_markers:
            if marker in body:
                offenders.append((os.path.relpath(path, REPO_ROOT), marker))
    assert offenders == []
