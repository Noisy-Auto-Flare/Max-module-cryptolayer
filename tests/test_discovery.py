"""Task 8: integration compatibility - module_manager-style discovery + scheme.

Replicates cryptolayer-cli src/module_manager.py load() logic faithfully:
sys.path.insert(0, modules_path); importlib.import_module("<Name>.main");
inspect.getmembers(module, inspect.isclass) filtered by
`issubclass(obj, BaseModule) and obj is not BaseModule`.
"""

import ast
import importlib
import inspect
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_DIR = REPO_ROOT / "Max"
CONVENTION_FILES = {"__init__.py", "main.py", "requirements.txt"}


def _discover_modules(modules_path: Path, name: str = "Max"):
    """Exact replication of module_manager.load() against one module folder."""
    sys.path.insert(0, str(modules_path))
    try:
        mod = importlib.import_module(f"{name}.main")
    finally:
        # keep sys.modules entries for cleanup by caller; drop our path entry
        try:
            sys.path.remove(str(modules_path))
        except ValueError:
            pass

    from base_module import BaseModule

    found = [
        obj
        for _, obj in inspect.getmembers(mod, inspect.isclass)
        if issubclass(obj, BaseModule) and obj is not BaseModule
    ]
    return found


def _fresh_import_state():
    """Purge cached package entries so a re-import actually hits sys.path."""
    for key in [k for k in sys.modules if k == "Max" or k.startswith("Max.")]:
        del sys.modules[key]


# ---------------------------------------------------------------- (а) discovery


def test_a_module_manager_style_discovery_finds_max_class():
    """Loader logic replicated verbatim over OUR repo root finds exactly class Max."""
    _fresh_import_state()
    found = _discover_modules(REPO_ROOT)
    assert len(found) == 1, f"expected exactly one module class, got {found}"
    assert found[0].__name__ == "Max"


# ------------------------------------------------- (б) folder self-sufficiency


def _is_stdlib_or_builtin(mod_name: str) -> bool:
    return (
        mod_name in sys.builtin_module_names
        or mod_name in sys.stdlib_module_names
        or mod_name.split(".")[0] in sys.stdlib_module_names
    )


def test_b_main_py_imports_are_self_sufficient():
    """Every import of Max/main.py must be stdlib / base_module / inside Max pkg.
    vkmax allowed ONLY lazily (inside a function body)."""
    tree = ast.parse((MAX_DIR / "main.py").read_text(encoding="utf-8"))

    def check(full: str) -> None:
        root = full.split(".")[0]
        allowed = (
            _is_stdlib_or_builtin(root)
            or root == "base_module"
            or root == "Max"
        )
        assert allowed, f"top-level import of {full!r} is not self-sufficient"

    def walk(node, inside_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, True)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if not inside_function:
                        check(alias.name)
            elif isinstance(child, ast.ImportFrom):
                if child.level > 0:
                    continue  # relative import -> inside package
                if child.module and not inside_function:
                    check(child.module)

    walk(tree, False)


# ------------------------------------------- (в) docs §2.1 scheme compatibility


def test_c_discovery_from_modules_layout_smoke(tmp_path):
    """docs README §2.1: module lives as <root>/modules/<Name>/ with main.py."""
    modules_root = tmp_path / "modules"
    shutil.copytree(MAX_DIR, modules_root / "Max")
    assert (modules_root / "Max" / "main.py").is_file()

    _fresh_import_state()
    found = _discover_modules(modules_root)
    assert len(found) == 1
    assert found[0].__name__ == "Max"


# ------------------------------------------------------- (г) empty __init__.py


def test_d_init_py_exists_and_is_empty():
    init_file = MAX_DIR / "__init__.py"
    assert init_file.is_file()
    assert init_file.stat().st_size == 0


# ------------------------------------------- negative sanity: convention checker


def _has_convention_files(folder: Path) -> bool:
    return CONVENTION_FILES.issubset({p.name for p in folder.iterdir() if p.is_file()})


def test_convention_checker_detects_missing_requirements_txt(tmp_path):
    """Positive: real Max/ satisfies the convention. Negative: crippled temp copy
    without requirements.txt is detected (checker returns False)."""
    assert _has_convention_files(MAX_DIR)

    crippled = tmp_path / "Max"
    shutil.copytree(MAX_DIR, crippled)
    (crippled / "requirements.txt").unlink()
    assert not _has_convention_files(crippled)
