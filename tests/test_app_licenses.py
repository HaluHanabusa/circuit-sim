"""依存のライセンスの一覧（docs/design/dependencies.md）が、実際に入る依存の全部を覆っているか（決まり7）。

Python: pyproject.toml の dependencies から、入っている依存の要件（extra の付かないもの）を辿る。
画面: frontend/package-lock.json の、配る物に入る依存（dev でないもの）を辿る。
"""
from __future__ import annotations

import importlib.metadata as md
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "design" / "dependencies.md"
ALLOWED = re.compile(r"^(MIT|BSD-[23]-Clause|Apache-2\.0|PSF-2\.0|ISC|0BSD|Zlib|CC0-1\.0)$")


def documented() -> dict[str, str]:
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        header = cells[0] == "名前" or set(cells[0]) <= {"-"}
        if line.startswith("|") and len(cells) == 4 and not header:
            rows[cells[0].lower()] = cells[2]
    return rows


def name_of(requirement: str) -> str:
    return re.match(r"[A-Za-z0-9_.-]+", requirement).group(0).lower().replace("_", "-")


def python_closure() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    seen: set[str] = set()
    todo = list(deps)
    while todo:
        req = todo.pop()
        marker = req.split(";", 1)[1] if ";" in req else ""
        if "extra" in marker:
            continue
        name = name_of(req)
        if name in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            assert marker, f"{name} が入っていない（条件の無い依存）"
            continue  # 条件つき（python_version < "3.11" など）で、この Python では入らない
        seen.add(name)
        todo.extend(dist.requires or [])
    return seen


def screen_closure() -> set[str]:
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]
    seen: set[str] = set()
    todo = list(packages[""].get("dependencies", {}))
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(packages[f"node_modules/{name}"].get("dependencies", {}))
    return seen


def licenses(cell: str) -> list[str]:
    head = re.sub(r"（.*?）", "", cell)
    inner = re.findall(r"（同梱の部品に ([^）]+)）", cell)
    names = [head.strip()] + [n.strip() for part in inner for n in part.split("、")]
    return [n for n in names if n]


def test_every_installed_python_dependency_is_listed():
    rows = documented()
    missing = sorted(python_closure() - set(rows))
    assert missing == [], f"一覧に無い依存（推移的な依存を含む）: {missing}"


def test_every_bundled_screen_dependency_is_listed_with_its_license():
    rows = documented()
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    for name in screen_closure():
        assert name in rows, f"一覧に無い画面の依存: {name}"
        assert lock[f"node_modules/{name}"].get("license") == rows[name], name


def test_listed_licenses_are_allowed():
    rows = documented()
    assert {"opentelemetry-api", "annotated-doc", "typing-extensions"} <= set(rows)
    for name, cell in rows.items():
        for lic in licenses(cell):
            assert ALLOWED.match(lic), f"{name}: {lic}（GPL / AGPL は使わない。LGPL は決定記録が要る）"
