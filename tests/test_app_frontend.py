"""画面のソース（frontend/）の試験。Node.js のある環境でだけ動く（D-041）。

- 同梱した画面のビルド結果（circuit_sim/app/static/）が、いまのソースをビルドした結果と同じか。
  作業ツリーには書かない（一時フォルダにビルドして比べる）。
- 型の確かめ（tsc）と、部品の試験（vitest）。

node の見つけ方（上から順）:
  1. CIRCUIT_SIM_NODE に node の絶対パスがあれば、それ
  2. PATH の node
  3. 試験の遮断（tests/conftest.py）で、claude と同じ場所なので PATH から外した場所
     （Homebrew の bin、npm -g の場所など）の node。PATH には戻さず、絶対パスで呼ぶ
     （ビルドの道具が PATH から claude を探しても届かない）
見つからないとき、frontend/node_modules が無いときは、確かめなかったことを理由と一緒にスキップとして残す
（scripts/check.py は -rs で一覧にする）。
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
NODE_VAR = "CIRCUIT_SIM_NODE"
REMOVED_PATH_VAR = "CIRCUIT_SIM_GUARD_REMOVED"  # tests/conftest.py と同じ名前


def find_node(env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """（node の絶対パス、どこで見つけたか / 見つからない理由）。"""
    env = dict(os.environ) if env is None else env
    named = env.get(NODE_VAR)
    if named:
        if Path(named).is_file():
            return named, f"{NODE_VAR} の指定"
        return None, f"{NODE_VAR} に指定された node（{named}）が無い"
    found = shutil.which("node", path=env.get("PATH", ""))
    if found:
        return found, "PATH"
    removed = [entry for entry in env.get(REMOVED_PATH_VAR, "").split(os.pathsep) if entry]
    for entry in removed:
        hit = shutil.which("node", path=entry)
        if hit:
            return hit, f"claude と同じ場所なので PATH から外した場所（{entry}）"
    if removed:
        return None, ("Node.js が無い（PATH にも、claude と同じ場所として PATH から外した場所にも無い）ので、"
                      "画面のビルド結果とソースの食い違いと、部品の試験を確かめなかった")
    return None, "Node.js が無いので、画面のビルド結果とソースの食い違いと、部品の試験を確かめなかった"


def node() -> str:
    found, why = find_node()
    if not found:
        pytest.skip(why)
    if not (FRONTEND / "node_modules" / "vite").is_dir():
        pytest.skip("frontend/node_modules が無いので、画面のビルドと部品の試験を確かめなかった"
                    "（cd frontend && npm ci で入れると確かめる）")
    return found


def run_node(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, "CI": "1", "NO_COLOR": "1"}  # PATH は試験の遮断のまま（node の場所を足さない）
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
    vite = "node_modules/vite/bin/vite.js"
    run_node(vite, "build", "--outDir", str(out), "--emptyOutDir", "--logLevel=warn")
    built, bundled = files(out), files(STATIC)
    rebuild = "cd frontend && npm run build で作り直す"
    assert sorted(built) == sorted(bundled), f"ビルド結果のファイルの組が違う。{rebuild}"
    for name, data in built.items():
        assert bundled[name] == data, f"{name} がソースと違う。{rebuild}"


def test_component_tests_pass():
    # 失敗が1件でもあれば、終了コードが 0 でない
    run_node("node_modules/vitest/vitest.mjs", "run", "--reporter=dot")


# ------------------------------------------------------------ node の見つけ方そのもの（node は要らない）


def fake_node(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    name = "node.exe" if os.name == "nt" else "node"
    path = folder / name
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_node_next_to_claude_is_used_by_absolute_path(tmp_path):
    """Homebrew や npm -g のように、node が claude と同じ場所にあって PATH から外されたときも、

    確かめを飛ばさない（絶対パスで呼ぶ）。"""
    brew = tmp_path / "homebrew" / "bin"
    node_path = fake_node(brew)
    env = {"PATH": str(tmp_path / "empty"), REMOVED_PATH_VAR: str(brew)}
    if os.name == "nt":
        env["PATHEXT"] = ".EXE"
    found, why = find_node(env)
    assert found is not None and Path(found).resolve() == node_path.resolve()
    assert "claude と同じ場所" in why
    assert str(brew) not in env["PATH"], "PATH に戻してはいけない"


def test_reasons_for_missing_node_are_told_apart(tmp_path):
    none_at_all = find_node({"PATH": str(tmp_path / "empty")})
    removed_but_no_node = find_node({"PATH": str(tmp_path / "empty"), REMOVED_PATH_VAR: str(tmp_path / "x")})
    wrong_name = find_node({NODE_VAR: str(tmp_path / "missing-node")})
    assert none_at_all[0] is None and removed_but_no_node[0] is None and wrong_name[0] is None
    assert len({none_at_all[1], removed_but_no_node[1], wrong_name[1]}) == 3
    assert "PATH から外した場所にも無い" in removed_but_no_node[1]
    named = fake_node(tmp_path / "named")
    assert find_node({NODE_VAR: str(named), "PATH": ""})[0] == str(named)
