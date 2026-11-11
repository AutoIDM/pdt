import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "pdt"
HEADER_LIMIT = re.compile(r'^# requires-python = "([^"]+)"$', re.M)
HEADER_PACKAGE = re.compile(r'^#     "([A-Za-z0-9_.-]+)', re.M)
PACKAGE_LOAD = re.compile(r'^if "pdt" not in sys\.modules:\n(?:    .*\n)+', re.M)
# The distribution that holds an import name, where the two names differ.
DISTRIBUTIONS = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "botocore": "boto3",
}


def internal_scripts() -> list[Path]:
    return [path for path in PACKAGE.glob("*.py") if path.read_text(encoding="utf-8").startswith("#!/usr/bin/env -S uv run --script")]


def module_level_imports(path: Path) -> set[str]:
    """The dotted names a file imports when it loads. An import inside a function or under
    TYPE_CHECKING does not count."""
    names, nodes = set(), [ast.parse(path.read_text(encoding="utf-8"))]
    while nodes:
        node = nodes.pop()
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            continue
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.extend(ast.iter_child_nodes(node))
    return names


def packages_missing_from_header(script: Path) -> set[str]:
    """The third-party packages that a script and the pdt modules it loads import, and that the
    script header does not list."""
    listed = {name.lower() for name in HEADER_PACKAGE.findall(script.read_text(encoding="utf-8").split("# ///")[1])}
    needed, read, files = set(), set(), [script]
    while files:
        path = files.pop()
        if path in read:
            continue
        read.add(path)
        for name in module_level_imports(path):
            parts = name.split(".")
            prefixes = [".".join(parts[:end]) for end in range(len(parts), 0, -1)]
            if parts[0] == "pdt":
                for prefix in prefixes:
                    module = PACKAGE.joinpath(*prefix.split(".")[1:])
                    files += [file for file in (module / "__init__.py", module.with_suffix(".py")) if file.is_file()]
            elif parts[0] not in sys.stdlib_module_names:
                needed.add(next((DISTRIBUTIONS[prefix] for prefix in prefixes if prefix in DISTRIBUTIONS), parts[0]))
    return needed - listed


def test_every_internal_script_has_the_same_python_limit():
    scripts = internal_scripts()
    assert len(scripts) >= 8
    limits = {script.name: HEADER_LIMIT.findall(script.read_text(encoding="utf-8")) for script in scripts}
    assert set(map(tuple, limits.values())) == {(">=3.12,<3.15",)}, limits


def test_every_script_header_lists_the_packages_the_script_loads():
    for script in internal_scripts():
        assert packages_missing_from_header(script) == set(), script.name


def test_no_internal_script_puts_the_folder_above_it_on_the_import_path():
    for script in internal_scripts():
        text = script.read_text(encoding="utf-8")
        assert "resolve().parent.parent))" not in text, script.name
        if "from pdt import" in text:
            assert PACKAGE_LOAD.search(text), script.name


def test_a_script_loads_only_pdt_from_the_folder_above_it(tmp_path):
    site = tmp_path / "site-packages"
    (site / "pdt").mkdir(parents=True)
    shutil.copy(PACKAGE / "__init__.py", site / "pdt" / "__init__.py")
    (site / "pdt" / "probe_module.py").write_text("NAME = 'probe'\n")
    (site / "tool_only_package").mkdir()
    (site / "tool_only_package" / "__init__.py").write_text("")
    load = PACKAGE_LOAD.search((PACKAGE / "deploy_azure.py").read_text(encoding="utf-8")).group(0)
    (site / "pdt" / "probe.py").write_text(
        "import importlib.util\nimport sys\nfrom pathlib import Path\n\n" + load
        + "from pdt import probe_module\nprint(probe_module.NAME)\n"
        + "try:\n    import tool_only_package\n    print('tool package loaded')\n"
        + "except ModuleNotFoundError:\n    print('tool package not loaded')\n")
    done = subprocess.run([sys.executable, "-I", str(site / "pdt" / "probe.py")], capture_output=True, text=True)
    assert (done.returncode, done.stdout, done.stderr) == (0, "probe\ntool package not loaded\n", "")

