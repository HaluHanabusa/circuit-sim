"""H2 範囲19・20・24（試験の遮断）の受け入れ試験（検証担当。ツリー 53e9779f）。

実測値と測り方は `docs/verification/H2-verifier.md` にある。

確かめること: 本物の claude と同じ場所に、claude 以外の実行ファイル（リンクと普通のファイル）がある PATH で
試験のセッションを始めても、(1) 本物は起動されない、(2) 同じ場所の道具は名前で起動できる、(3) PATH の
どの位置に置いても同じ。

期待値の作り方:
- 本物の代わりに「身代わり」（起動されたら印のファイルを書く /bin/sh のスクリプト）を置く。
- 遮断の仕組み（tests/conftest.py）を写した小さな試験のセッションを、子プロセスで起動する。中の試験は、
  CIRCUIT_SIM_SAFE_PATH で claude と道具を探し、シェルから claude を起動する（見張り役は使わない）。
- 子のセッションには、このリポジトリの試験を含めないので、入れ子の目印を外して起動してよい。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="リンクで道具を残すのは Linux / macOS の仕組み")

STAND_IN = """\
#!/bin/sh
echo started >> {mark}
exit 0
"""

INNER = """\
import json
import os
import shutil
import subprocess


def test_probe():
    safe = os.environ["CIRCUIT_SIM_SAFE_PATH"]
    shell = subprocess.run(["/bin/sh", "-c", "claude; echo $?"], env={{**os.environ, "PATH": safe}},
                           capture_output=True, text=True)
    found = {{
        "claude": shutil.which("claude", path=safe),
        "tools": {{name: shutil.which(name, path=safe) for name in ("h2tool", "h2link")}},
        "shell": shell.stdout.strip(),
        "tool_output": subprocess.run(["h2tool"], env={{**os.environ, "PATH": safe}},
                                      capture_output=True, text=True).stdout.strip(),
    }}
    with open({out!r}, "w", encoding="utf-8") as handle:
        json.dump(found, handle)
"""

DROPPED = ("CIRCUIT_SIM_PYTEST_RUNNING", "CIRCUIT_SIM_CLAUDE_SENTINEL", "CIRCUIT_SIM_SAFE_PATH")


def place_real_one(tmp_path: Path) -> tuple[Path, Path]:
    """本物の身代わりと、同じ場所の道具（普通のファイル h2tool と、別の場所へのリンク h2link）。"""
    real = tmp_path / "real-bin"
    real.mkdir()
    mark = tmp_path / "REAL-CLAUDE-STARTED"
    (real / "claude").write_text(STAND_IN.format(mark=mark), encoding="utf-8")
    (real / "claude").chmod(0o755)
    (real / "h2tool").write_text("#!/bin/sh\necho h2tool-ok\n", encoding="utf-8")
    (real / "h2tool").chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "target").write_text("#!/bin/sh\necho h2link-ok\n", encoding="utf-8")
    (elsewhere / "target").chmod(0o755)
    (real / "h2link").symlink_to(elsewhere / "target")
    return real, mark


@pytest.mark.parametrize("position", ["front", "back"])
def test_guard_keeps_the_tools_next_to_claude_and_never_starts_it(tmp_path, position):
    real, mark = place_real_one(tmp_path)
    inner = tmp_path / "inner"
    inner.mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", inner / "conftest.py")
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    out = tmp_path / "found.json"
    (inner / "test_inner.py").write_text(INNER.format(out=str(out)), encoding="utf-8")
    safe = os.environ.get("CIRCUIT_SIM_SAFE_PATH", os.defpath)
    path = f"{real}{os.pathsep}{safe}" if position == "front" else f"{safe}{os.pathsep}{real}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k not in DROPPED}
    env["PATH"] = path
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
        cwd=inner, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120,
    )
    assert not mark.exists(), "本物（の身代わり）の claude が起動された"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = json.loads(out.read_text(encoding="utf-8"))
    assert found["claude"] is None
    assert found["shell"].endswith("127")             # シェルからも claude は見つからない
    assert all(found["tools"].values()), found         # 同じ場所の道具は残る
    assert found["tool_output"] == "h2tool-ok"
    removed = [line for line in proc.stderr.splitlines() if line.startswith("  PATH から外した場所: ")]
    assert any(real.name in line and "（claude がある）" in line for line in removed), proc.stderr
    assert "h2link" in proc.stderr and "h2tool" in proc.stderr       # リンクで残した道具の表示
