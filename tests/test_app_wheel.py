"""配る形（wheel）の中身の試験（D-041。利用者に Node.js は要らない）。

作業ツリーの写しを一時フォルダに置いて wheel を作り（作業ツリーに build/ や egg-info を書かない）、
画面のビルド結果（static/）と同梱のサンプル（samples/）が入っていることを確かめる。
ネットワークは使わない（--no-build-isolation。setuptools はこの Python に入っているものを使う）。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "circuit_sim" / "app" / "static"
SAMPLES = ROOT / "circuit_sim" / "app" / "samples"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools が無いので、wheel を作って中身を確かめなかった")
    src = tmp_path_factory.mktemp("src")
    for name in ("pyproject.toml", "LICENSE", "NOTICE", "README.md"):
        shutil.copy2(ROOT / name, src / name)
    shutil.copytree(ROOT / "circuit_sim", src / "circuit_sim",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
         "-w", str(out), str(src)],
        capture_output=True, text=True, errors="replace", timeout=300, check=False,
    )
    if result.returncode != 0 and "No module named pip" in result.stderr:
        pytest.skip("pip が無いので、wheel を作って中身を確かめなかった")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    built = sorted(out.glob("circuit_sim-*.whl"))
    assert len(built) == 1, built
    with zipfile.ZipFile(built[0]) as zf:
        yield zf


def bundled(base: Path) -> set[str]:
    return {p.relative_to(ROOT).as_posix() for p in base.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def test_wheel_contains_the_built_screen(wheel):
    names = set(wheel.namelist())
    expected = bundled(STATIC)
    assert "circuit_sim/app/static/index.html" in expected
    assert any(n.startswith("circuit_sim/app/static/assets/") and n.endswith(".js") for n in expected)
    missing = expected - names
    assert missing == set(), f"wheel に入っていない画面のファイル: {sorted(missing)}"
    for name in expected:
        assert wheel.read(name) == (ROOT / name).read_bytes(), name


def test_wheel_contains_the_samples(wheel):
    names = set(wheel.namelist())
    expected = {n for n in bundled(SAMPLES) if n.endswith(".json")}
    assert expected and expected <= names, sorted(expected - names)


def test_wheel_has_the_command_and_no_frontend_sources(wheel):
    names = wheel.namelist()
    entry = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    assert "circuit-sim = circuit_sim.app.cli:main" in wheel.read(entry).decode("utf-8")
    assert not [n for n in names if n.startswith("frontend/") or n.endswith((".tsx", ".ts"))]
    assert not [n for n in names if "node_modules" in n]
    meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
    text = wheel.read(meta).decode("utf-8")
    assert "Requires-Dist: fastapi>=0.133" in text and "Requires-Dist: starlette>=1.1" in text
