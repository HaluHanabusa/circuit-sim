"""画面のソース（frontend/）の試験。Node.js のある環境でだけ動く（D-041）。

- 同梱した画面のビルド結果（circuit_sim/app/static/）が、いまのソースをビルドした結果と同じか。
  作業ツリーには書かない（一時フォルダにビルドして比べる）。
- 型の確かめ（tsc）と、部品の試験（vitest）。

Node.js か frontend/node_modules が無い環境（利用者とPOのPCを含む）では、確かめなかったことを、スキップの理由
として結果に残す（scripts/check.py は -rs で一覧にする）。
本物の claude と同じ場所にある node は、試験の遮断（tests/conftest.py）で PATH から外れる。手元で流すときは、
claude の無い場所に node へのリンクを置いて、その場所を PATH に入れる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
STATIC = ROOT / "circuit_sim" / "app" / "static"
TEXT_SUFFIXES = {".html", ".js", ".css", ".json", ".svg"}


def node() -> str:
    found = shutil.which("node")
    if not found:
        pytest.skip("Node.js が無いので、画面のビルド結果とソースの食い違い・部品の試験を確かめなかった（D-041）")
    if not (FRONTEND / "node_modules" / "vite").is_dir():
        pytest.skip("frontend/node_modules が無いので、画面のビルドと部品の試験を確かめなかった"
                    "（cd frontend && npm ci で入れると確かめる）")
    return found


def run_node(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, "CI": "1", "NO_COLOR": "1"}
    result = subprocess.run(
        [node(), *args], cwd=FRONTEND, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return result


def files(base: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            if path.suffix in TEXT_SUFFIXES:
                data = data.replace(b"\r\n", b"\n")  # 取り出したときの改行の変換に左右されない
            out[path.relative_to(base).as_posix()] = data
    return out


def test_bundled_build_matches_the_source(tmp_path):
    run_node("node_modules/typescript/bin/tsc", "--noEmit", "-p", ".")
    out = tmp_path / "static"
    run_node("node_modules/vite/bin/vite.js", "build", "--outDir", str(out), "--emptyOutDir", "--logLevel", "warn")
    built, bundled = files(out), files(STATIC)
    assert sorted(built) == sorted(bundled), "ビルド結果のファイルが違う。cd frontend && npm run build で作り直す"
    for name, data in built.items():
        assert bundled[name] == data, f"{name} がソースと違う。cd frontend && npm run build で作り直す"


def test_component_tests_pass():
    run_node("node_modules/vitest/vitest.mjs", "run", "--reporter=dot")  # 失敗が1件でもあれば終了コードが 0 でない
