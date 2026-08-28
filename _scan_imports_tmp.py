import ast
import sys
from pathlib import Path

stdlib = set(sys.stdlib_module_names)
LOCAL = {"core", "services", "ui", "tests"}
IMPORT_TO_PYPI = {
    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "socks": "PySocks",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
}


def top_level(name: str) -> str:
    return name.split(".")[0]


def walk_imports(node: ast.AST):
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            yield node.module
        else:
            for alias in node.names:
                yield alias.name


targets = [Path("main.py")]
targets.extend(Path("services").rglob("*.py"))
targets.extend(Path("ui").rglob("*.py"))
for p in Path(".").glob("*worker*.py"):
    targets.append(p)

imports: set[str] = set()
for fp in sorted(set(targets)):
    if not fp.is_file():
        continue
    try:
        src = fp.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(fp))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        for mod in walk_imports(node):
            tl = top_level(mod)
            if tl in LOCAL or tl in stdlib or tl.startswith("_"):
                continue
            imports.add(tl)

pypi: set[str] = set()
for imp in sorted(imports):
    pkg = IMPORT_TO_PYPI.get(imp, imp)
    if pkg:
        pypi.add(pkg)

print("IMPORT_TOP_LEVELS:")
for x in sorted(imports, key=str.lower):
    print(f"  {x}")
print("PYPI_PACKAGES:")
for x in sorted(pypi, key=str.lower):
    print(f"  {x}")
