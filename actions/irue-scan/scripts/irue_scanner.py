#!/usr/bin/env python3
"""IRUE Repository Scanner.

Inspect a repository and emit a JSON profile on stdout that describes:

  * identity        - name, description, repository URL, commit, license
  * languages       - file counts / bytes per language (extension based)
  * manifests       - package.json, pyproject.toml, requirements, Dockerfile,
                      go.mod, Cargo.toml, compose files, etc.
  * python_ast      - modules, public classes/functions with docstrings,
                      CLI entry points, ``if __name__ == "__main__"`` guards
  * node_scripts    - npm scripts (candidates for WebMCP tools)
  * workflows       - GitHub Actions workflows and their triggers
  * docs            - README headline / summary, documentation files
  * webmcp_tools    - normalized tool candidates derived from the above

Standard library only, so it runs anywhere ``python3`` exists.

Usage:
    irue_scanner.py [ROOT] [--exclude DIR ...] [--max-files N] > repo_profile.json
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for 3.10 and older
    tomllib = None

SCHEMA_VERSION = "1.0"

DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".cache", ".idea",
    ".vscode", "coverage", ".central-actions", "site-packages",
}

LANGUAGE_BY_EXT = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".sql": "SQL", ".yml": "YAML", ".yaml": "YAML", ".json": "JSON",
    ".toml": "TOML", ".md": "Markdown", ".dockerfile": "Dockerfile",
    ".tf": "Terraform", ".proto": "Protobuf", ".dart": "Dart", ".lua": "Lua",
}

MANIFEST_FILES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "poetry.lock", "go.mod", "Cargo.toml",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml",
    "compose.yaml", "Makefile", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "Package.swift", "pubspec.yaml", "app.yaml", "cloudbuild.yaml",
    "serverless.yml", "vercel.json", "netlify.toml", "firebase.json",
    "mcp.json", ".mcp.json", "webmcp.json", "action.yml", "action.yaml",
}

LICENSE_HINTS = [
    ("MIT License", "MIT"), ("Apache License", "Apache-2.0"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL"), ("Mozilla Public License", "MPL-2.0"),
    ("BSD 3-Clause", "BSD-3-Clause"), ("BSD 2-Clause", "BSD-2-Clause"),
    ("The Unlicense", "Unlicense"),
]


# --------------------------------------------------------------------------- utils
def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")
    return value.lower() or "tool"


def first_paragraph(markdown: str) -> str:
    lines = [ln.rstrip() for ln in markdown.splitlines()]
    buf: list[str] = []
    for ln in lines:
        if ln.startswith(("#", "<", "[![")):
            if buf:
                break
            continue
        if not ln.strip():
            if buf:
                break
            continue
        if ln.startswith(("```", "|", "- ", "* ", "> ")):
            if buf:
                break
            continue
        buf.append(ln.strip())
    text = " ".join(buf)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # links -> text
    text = re.sub(r"[*_`]", "", text)
    return text[:400]


def readme_title(markdown: str) -> str | None:
    for ln in markdown.splitlines():
        if ln.startswith("# "):
            return re.sub(r"[*_`]", "", ln[2:]).strip()
    return None


# --------------------------------------------------------------------------- walkers
def walk_files(root: Path, excludes: set[str], max_files: int):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excludes and not d.startswith(".git"))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            yield p
            count += 1
            if count >= max_files:
                return


def detect_languages(files: list[Path]) -> list[dict]:
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for p in files:
        name = p.name
        lang = "Dockerfile" if name == "Dockerfile" or name.endswith(".Dockerfile") else LANGUAGE_BY_EXT.get(p.suffix.lower())
        if not lang:
            continue
        counts[lang] += 1
        try:
            sizes[lang] += p.stat().st_size
        except OSError:
            pass
    total = sum(sizes.values()) or 1
    return [
        {"name": lang, "files": counts[lang], "bytes": sizes[lang], "share": round(sizes[lang] / total, 4)}
        for lang, _ in sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    ]


# --------------------------------------------------------------------------- manifests
def parse_package_json(path: Path) -> dict:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {"error": "invalid JSON"}
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "description": data.get("description"),
        "scripts": data.get("scripts", {}),
        "bin": data.get("bin"),
        "dependencies": len(data.get("dependencies", {}) or {}),
        "devDependencies": len(data.get("devDependencies", {}) or {}),
        "engines": data.get("engines"),
        "type": data.get("type"),
    }


def parse_pyproject(path: Path) -> dict:
    text = read_text(path)
    if tomllib is None:
        return {"raw_preview": text[:500]}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"error": "invalid TOML"}
    project = data.get("project", {}) or data.get("tool", {}).get("poetry", {}) or {}
    scripts = project.get("scripts", {}) or {}
    return {
        "name": project.get("name"),
        "version": project.get("version") if isinstance(project.get("version"), str) else None,
        "description": project.get("description"),
        "requires_python": project.get("requires-python"),
        "dependencies": len(project.get("dependencies", []) or []),
        "console_scripts": scripts,
        "build_backend": (data.get("build-system", {}) or {}).get("build-backend"),
    }


def parse_requirements(path: Path) -> dict:
    pkgs = []
    for ln in read_text(path).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", "-")):
            continue
        pkgs.append(re.split(r"[<>=!~\[; ]", ln, maxsplit=1)[0])
    return {"count": len(pkgs), "packages": pkgs[:60]}


def parse_dockerfile(path: Path) -> dict:
    text = read_text(path)
    base = re.findall(r"^FROM\s+([^\s]+)", text, flags=re.MULTILINE | re.IGNORECASE)
    ports = re.findall(r"^EXPOSE\s+([0-9 /tcpud]+)", text, flags=re.MULTILINE | re.IGNORECASE)
    cmd = re.findall(r"^(?:CMD|ENTRYPOINT)\s+(.+)$", text, flags=re.MULTILINE | re.IGNORECASE)
    return {"base_images": base, "expose": [p.strip() for p in ports], "entry": cmd[-1].strip() if cmd else None}


def parse_go_mod(path: Path) -> dict:
    text = read_text(path)
    mod = re.search(r"^module\s+(\S+)", text, flags=re.MULTILINE)
    go = re.search(r"^go\s+(\S+)", text, flags=re.MULTILINE)
    return {"module": mod.group(1) if mod else None, "go": go.group(1) if go else None,
            "requires": len(re.findall(r"^\s+\S+\s+v[0-9]", text, flags=re.MULTILINE))}


def parse_cargo(path: Path) -> dict:
    text = read_text(path)
    if tomllib is None:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"error": "invalid TOML"}
    pkg = data.get("package", {})
    return {"name": pkg.get("name"), "version": pkg.get("version"), "description": pkg.get("description"),
            "dependencies": len(data.get("dependencies", {}) or {}), "bins": [b.get("name") for b in data.get("bin", [])]}


def parse_makefile(path: Path) -> dict:
    targets = re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", read_text(path), flags=re.MULTILINE)
    targets = [t for t in dict.fromkeys(targets) if not t.startswith(".")]
    return {"targets": targets[:40]}


MANIFEST_PARSERS = {
    "package.json": parse_package_json,
    "pyproject.toml": parse_pyproject,
    "requirements.txt": parse_requirements,
    "requirements-dev.txt": parse_requirements,
    "Dockerfile": parse_dockerfile,
    "go.mod": parse_go_mod,
    "Cargo.toml": parse_cargo,
    "Makefile": parse_makefile,
}


def collect_manifests(files: list[Path], root: Path) -> list[dict]:
    out = []
    for p in files:
        if p.name in MANIFEST_FILES or p.name.endswith(".Dockerfile"):
            parser = MANIFEST_PARSERS.get("Dockerfile" if p.name.endswith(".Dockerfile") else p.name)
            entry = {"path": rel(p, root), "type": p.name, "depth": len(p.relative_to(root).parts) - 1}
            if parser:
                entry["details"] = parser(p)
            out.append(entry)
    return sorted(out, key=lambda e: (e["depth"], e["path"]))


# --------------------------------------------------------------------------- python AST
def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    params = []
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(positional, defaults):
        if a.arg in ("self", "cls"):
            continue
        params.append({
            "name": a.arg,
            "annotation": ast.unparse(a.annotation) if a.annotation else None,
            "required": d is None,
            "default": ast.unparse(d) if d is not None else None,
        })
    for a in args.kwonlyargs:
        params.append({"name": a.arg, "annotation": ast.unparse(a.annotation) if a.annotation else None,
                       "required": False, "default": None})
    return params


def scan_python(files: list[Path], root: Path, max_modules: int = 400) -> dict:
    modules = []
    entry_points = []
    total_functions = total_classes = 0
    for p in files:
        if p.suffix != ".py":
            continue
        if len(modules) >= max_modules:
            break
        src = read_text(p)
        try:
            tree = ast.parse(src, filename=str(p))
        except (SyntaxError, ValueError):
            modules.append({"path": rel(p, root), "error": "syntax error"})
            continue
        mod = {
            "path": rel(p, root),
            "docstring": (ast.get_docstring(tree) or "").split("\n\n")[0][:300] or None,
            "classes": [],
            "functions": [],
            "imports": sorted({(n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0])
                               for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))} - {""})[:40],
            "has_main_guard": False,
        }
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
                mod["classes"].append({"name": node.name, "doc": (ast.get_docstring(node) or "")[:200] or None,
                                       "bases": [ast.unparse(b) for b in node.bases], "methods": methods[:30], "line": node.lineno})
                total_classes += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                mod["functions"].append({"name": node.name, "doc": (ast.get_docstring(node) or "")[:240] or None,
                                         "params": _sig(node), "async": isinstance(node, ast.AsyncFunctionDef),
                                         "returns": ast.unparse(node.returns) if node.returns else None, "line": node.lineno,
                                         "decorators": [ast.unparse(d) for d in node.decorator_list][:5]})
                total_functions += 1
            elif isinstance(node, ast.If):
                test = ast.unparse(node.test).replace("'", '"')
                if "__name__" in test and '"__main__"' in test:
                    mod["has_main_guard"] = True
                    entry_points.append(rel(p, root))
        modules.append(mod)
    return {"modules": modules, "entry_points": entry_points,
            "totals": {"modules": len(modules), "classes": total_classes, "functions": total_functions}}


# --------------------------------------------------------------------------- workflows
def _workflow_triggers(text: str) -> list[str]:
    """Extract event names from the ``on:`` block without a YAML dependency."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(?:on|'on'|\"on\"):[ \t]*(.*?)\s*(?:#.*)?$", ln)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline.startswith("["):
            return [t.strip().strip("'\"") for t in inline.strip("[]").split(",") if t.strip()]
        if inline:
            return [inline.strip("'\"")]
        block = []
        for nxt in lines[i + 1:]:
            if nxt.strip() == "" or nxt.lstrip().startswith("#"):
                continue
            if not nxt.startswith((" ", "\t")):
                break
            block.append(nxt)
        if not block:
            return []
        indent = min(len(b) - len(b.lstrip()) for b in block)
        out = []
        for b in block:
            if len(b) - len(b.lstrip()) == indent:
                key = re.match(r"^\s*-?\s*([A-Za-z_]+)\s*:?", b)
                if key:
                    out.append(key.group(1))
        return out
    return []


def scan_workflows(root: Path) -> list[dict]:
    wf_dir = root / ".github" / "workflows"
    out = []
    if not wf_dir.is_dir():
        return out
    for p in sorted(wf_dir.glob("*.y*ml")):
        text = read_text(p)
        name = re.search(r"^name:\s*['\"]?(.+?)['\"]?\s*$", text, flags=re.MULTILINE)
        triggers = _workflow_triggers(text)
        uses = sorted(set(re.findall(r"uses:\s*([^\s@]+)@", text)))
        out.append({"path": rel(p, root), "name": name.group(1) if name else p.stem,
                    "triggers": triggers, "reusable": "workflow_call" in triggers, "actions_used": uses[:20]})
    return out


# --------------------------------------------------------------------------- docs / license
def scan_docs(files: list[Path], root: Path) -> dict:
    readme = next((p for p in files if p.name.lower() in ("readme.md", "readme.rst", "readme.txt", "readme") and p.parent == root), None)
    docs = [rel(p, root) for p in files if p.suffix.lower() in (".md", ".rst") and p.name.lower() != "readme.md"][:80]
    license_id = None
    lic = next((p for p in files if p.name.upper().startswith("LICENSE") and p.parent == root), None)
    if lic:
        text = read_text(lic, 4000)
        license_id = next((sid for hint, sid in LICENSE_HINTS if hint.lower() in text.lower()), "Custom")
    md = read_text(readme) if readme else ""
    return {"readme": rel(readme, root) if readme else None, "title": readme_title(md), "summary": first_paragraph(md),
            "docs": docs, "license": license_id, "has_agents_md": any(p.name.upper() == "AGENTS.MD" for p in files),
            "has_contributing": any(p.name.upper().startswith("CONTRIBUTING") for p in files)}


# --------------------------------------------------------------------------- webmcp tools
def derive_tools(profile: dict) -> list[dict]:
    """Turn scan results into normalized WebMCP tool candidates."""
    tools: list[dict] = []
    seen: set[str] = set()

    def add(name: str, description: str, params: list[dict] | None, source: str, kind: str):
        key = slug(name)
        if key in seen:
            return
        seen.add(key)
        props = {}
        required = []
        for p in params or []:
            props[p["name"]] = {"type": _json_type(p.get("annotation")), "description": p.get("annotation") or ""}
            if p.get("required"):
                required.append(p["name"])
        tools.append({
            "name": key,
            "description": (description or f"{kind} {name}").strip()[:300],
            "inputSchema": {"type": "object", "properties": props, "required": required},
            "source": source,
            "kind": kind,
        })

    # Python console scripts and public documented functions in entry-point modules.
    for m in profile["manifests"]:
        if m["type"] == "pyproject.toml":
            for name, target in (m.get("details", {}).get("console_scripts") or {}).items():
                add(name, f"Console script -> {target}", None, m["path"], "console_script")
        if m["type"] == "package.json":
            for name, cmd in (m.get("details", {}).get("scripts") or {}).items():
                if name in ("test", "lint", "build", "start", "dev", "deploy", "preview"):
                    add(f"npm_{name}", f"npm run {name}: {cmd}", None, m["path"], "npm_script")
        if m["type"] == "Makefile":
            for t in m.get("details", {}).get("targets", [])[:12]:
                add(f"make_{t}", f"make {t}", None, m["path"], "make_target")

    entry_modules = set(profile["python_ast"]["entry_points"])
    for mod in profile["python_ast"]["modules"]:
        prefer = mod["path"] in entry_modules or any(seg in mod["path"] for seg in ("cli", "scripts/", "tools/", "commands"))
        for fn in mod.get("functions", []):
            if fn.get("doc") and (prefer or len(tools) < 12):
                add(f"{Path(mod['path']).stem}_{fn['name']}", fn["doc"].splitlines()[0], fn["params"], f"{mod['path']}:{fn['line']}", "python_function")
        if len(tools) >= 40:
            break
    return tools


def _json_type(annotation: str | None) -> str:
    if not annotation:
        return "string"
    a = annotation.lower()
    if a.startswith(("int",)):
        return "integer"
    if a.startswith(("float", "decimal")):
        return "number"
    if a.startswith("bool"):
        return "boolean"
    if a.startswith(("list", "tuple", "set", "sequence", "iterable")):
        return "array"
    if a.startswith(("dict", "mapping")):
        return "object"
    return "string"


# --------------------------------------------------------------------------- main
def build_profile(root: Path, excludes: set[str], max_files: int) -> dict:
    files = list(walk_files(root, excludes, max_files))
    languages = detect_languages(files)
    manifests = collect_manifests(files, root)
    docs = scan_docs(files, root)

    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    name = (docs.get("title") or (repo_slug.split("/")[-1] if repo_slug else root.resolve().name))
    description = docs.get("summary") or next((m["details"].get("description") for m in manifests
                                                if m.get("details", {}).get("description")), "") or ""

    profile = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": {
            "name": name,
            "description": description,
            "repository": f"{server}/{repo_slug}" if repo_slug else None,
            "owner": repo_slug.split("/")[0] if repo_slug else None,
            "default_branch": os.environ.get("GITHUB_REF_NAME"),
            "commit": os.environ.get("GITHUB_SHA"),
            "license": docs.get("license"),
        },
        "stats": {"files_scanned": len(files), "bytes_scanned": sum((p.stat().st_size for p in files if p.exists()), 0)},
        "languages": languages,
        "manifests": manifests,
        "python_ast": scan_python(files, root),
        "workflows": scan_workflows(root),
        "docs": docs,
        "top_level": sorted({rel(p, root).split("/")[0] for p in files})[:60],
    }
    profile["webmcp_tools"] = derive_tools(profile)
    return profile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".", help="repository root to scan")
    ap.add_argument("--exclude", action="append", default=[], help="extra directory name to skip (repeatable)")
    ap.add_argument("--max-files", type=int, default=20000)
    ap.add_argument("--compact", action="store_true", help="single-line JSON output")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    excludes = DEFAULT_EXCLUDES | {Path(e).name for e in args.exclude if e}
    profile = build_profile(root, excludes, args.max_files)
    json.dump(profile, sys.stdout, indent=None if args.compact else 2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
