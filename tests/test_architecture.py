"""The dependency rule, enforced.

Every simplification in this codebase is a one-off; this test is the only thing that keeps
it. Layers may import downwards and sideways within their own package, never upwards - so
``domain`` cannot reach for a file format, ``store`` cannot reach for a handler, and a
resolver cannot reach for the registry.

It also bans function-local imports of internal modules. Those are how a cycle hides: the
codebase had five of them, every one papering over a dependency that should not have
existed. If you need one to make something work, the layering is wrong.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Dict, List, Set, Tuple

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "rewind"

#: Lower may not import higher. Equal may import equal.
LAYERS: Dict[str, int] = {
    "errors": 0,
    "aws": 0,
    "domain": 1,
    "timeutil": 2,
    "trail": 2,
    "store": 2,
    "handlers": 3,
    "resolvers": 4,
    "pipeline": 5,
    "report": 6,
    "cli": 7,
}

#: Modules exempt because they only re-export.
ENTRYPOINTS = {"__init__", "__main__"}


def modules() -> List[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def top_package(path: pathlib.Path) -> str:
    relative = path.relative_to(SRC)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def resolve(module: pathlib.Path, node: ast.ImportFrom) -> str:
    """The top-level rewind package a relative import points at."""
    relative = module.relative_to(SRC)
    # level 1 == this module's own package, 2 == its parent, and so on
    here = list(relative.parts[:-1])
    up = node.level - 1
    base = here[: len(here) - up] if up else here
    if node.module:
        return (base + node.module.split("."))[0] if not base else base[0]
    return base[0] if base else ""


def internal_imports(module: pathlib.Path) -> Set[str]:
    tree = ast.parse(module.read_text())
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            target = resolve(module, node)
            if target and target != top_package(module):
                found.add(target)
    return found


def function_local_imports(module: pathlib.Path) -> List[Tuple[int, str]]:
    tree = ast.parse(module.read_text())
    offenders: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.level:
                offenders.append((inner.lineno, inner.module or ""))
    return offenders


def test_every_module_is_assigned_a_layer():
    """A new top-level module must be placed deliberately, not land wherever."""
    unplaced = {top_package(p) for p in modules()} - set(LAYERS) - ENTRYPOINTS
    assert unplaced == set(), (
        "add these to LAYERS with the layer they belong to: %s" % sorted(unplaced)
    )


@pytest.mark.parametrize("module", modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_imports_only_point_downwards(module: pathlib.Path):
    package = top_package(module)
    if package in ENTRYPOINTS:
        return
    here = LAYERS[package]
    for target in internal_imports(module):
        assert target in LAYERS, "%s imports unplaced %r" % (module.name, target)
        assert LAYERS[target] <= here, (
            "%s (layer %d) imports %s (layer %d) - that is upwards"
            % (module.relative_to(SRC), here, target, LAYERS[target])
        )


def test_domain_depends_on_nothing_internal():
    """The bottom layer has to stay at the bottom, or everything above it is negotiable."""
    for module in modules():
        if top_package(module) != "domain":
            continue
        assert internal_imports(module) == set(), module.name


@pytest.mark.parametrize("module", modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_function_local_internal_imports(module: pathlib.Path):
    offenders = function_local_imports(module)
    assert offenders == [], (
        "%s hides an import inside a function: %s. That is how a cycle survives - fix the "
        "layering instead." % (module.relative_to(SRC), offenders)
    )


def test_the_layer_map_is_a_total_order_with_no_gaps():
    """A gap in the numbering usually means a layer was deleted and the map not updated."""
    used = sorted(set(LAYERS.values()))
    assert used == list(range(len(used)))
