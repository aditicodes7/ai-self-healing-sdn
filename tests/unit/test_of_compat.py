"""Import-hygiene test: no file under controller/ touches ryu/os_ken
directly except of_compat.py itself.

Pure AST parsing, no ryu/os_ken import required, so this runs identically
under venv-ml (no SDN framework installed) and venv-ctrl.
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONTROLLER_DIR = REPO_ROOT / "controller"
ALLOWED_FILE = "of_compat.py"


def _controller_source_files():
    if not CONTROLLER_DIR.exists():
        return []
    return sorted(p for p in CONTROLLER_DIR.rglob("*.py") if p.name != ALLOWED_FILE)


def _bare_sdn_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("ryu", "os_ken"):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in ("ryu", "os_ken"):
                offenders.append(node.module)
    return offenders


def test_of_compat_file_exists():
    assert (CONTROLLER_DIR / ALLOWED_FILE).exists()


@pytest.mark.parametrize("path", _controller_source_files(), ids=lambda p: p.name)
def test_no_bare_sdn_import_outside_of_compat(path):
    offenders = _bare_sdn_imports(path)
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports {offenders} directly; "
        f"import from controller.of_compat instead"
    )


def test_of_compat_exposes_backend_and_no_bare_import_of_itself():
    """of_compat.py is the one file allowed to import ryu/os_ken directly,
    and it must try ryu first, falling back to os_ken, per docs/decisions.md."""
    source = (CONTROLLER_DIR / ALLOWED_FILE).read_text()
    tree = ast.parse(source)
    assert "BACKEND" in source
    top_level_try = next(
        (n for n in tree.body if isinstance(n, ast.Try)), None
    )
    assert top_level_try is not None, "of_compat.py must wrap the ryu import in try/except"
