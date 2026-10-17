"""Inspect literal eager/conditional/delayed imports without importing SMM."""
import ast
import hashlib
from pathlib import Path


def inspect_module(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    lines = text.encode("utf-8").splitlines(keepends=True)
    entries = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            continue
        ancestry = []
        links = []
        parent = node
        while parent in parents:
            child = parent
            parent = parents[parent]
            links.append((parent, child))
            ancestry.append(parent)
        # Decorators/default expressions execute on definition, not on function call.
        inside_body = any(isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                          child in parent.body for parent, child in links)
        conditional = any(isinstance(parent, (ast.If, ast.Try, ast.For, ast.While, ast.IfExp)) for parent in ancestry)
        phase = "DELAYED_BODY" if inside_body else "CONDITIONAL_INITIALIZATION" if conditional else "DEFINITION_OR_MODULE_INITIALIZATION"
        if node.lineno == node.end_lineno:
            segment = lines[node.lineno-1][node.col_offset:node.end_col_offset]
        else:
            segment = (lines[node.lineno-1][node.col_offset:] +
                       b"".join(lines[node.lineno:node.end_lineno-1]) +
                       lines[node.end_lineno-1][:node.end_col_offset])
        entries.append({"line": node.lineno, "phase": phase,
                        "kind": type(node).__name__, "source": segment.decode("utf-8")})
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "entries": entries,
            "warning": "Initialization candidates aren't execution proof. Imported parent packages execute first; external/native implementations and conditional branches still require review."}
