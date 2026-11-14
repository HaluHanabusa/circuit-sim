"""UTF-8 でない端末でも、コマンドラインからの実行が落ちないことを確かめる（W1 の基準3）。

日本語 Windows の既定の端末は cp932。cp932 には µ（U+00B5）と Ω（U+2126、オーム記号）が無いので、
接頭語つきの単位を換算した注意を、そのまま出そうとすると例外になる。
tests/data/rc_lowpass_prefixed_units.json は、その2つの文字を単位に使った仕様。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from circuit_sim.__main__ import main as cli_main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = "examples/rlc_series.json"
PREFIXED = "tests/data/rc_lowpass_prefixed_units.json"


def run_cli(spec: str, encoding: str) -> tuple[int, str, str]:
    """端末の文字コードを encoding にして実行する。返すのは（終了コード、出力、エラー出力）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "circuit_sim", spec], cwd=ROOT, capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": encoding},
    )
    return proc.returncode, proc.stdout.decode(encoding), proc.stderr.decode(encoding)


def test_example_runs_on_a_cp932_terminal():
    code, out, err = run_cli(EXAMPLE, "cp932")
    assert code == 0 and err == ""
    assert "テンプレート: rlc_series" in out and "v(out): 最終値" in out


def test_units_that_cp932_cannot_show_are_written_in_plain_letters():
    code, out, err = run_cli(PREFIXED, "cp932")
    assert code == 0 and err == ""
    assert "注意: R: 1 kohm を 1000 ohm に換算した" in out
    assert "注意: C: 10 uF を 1e-05 F に換算した" in out


def test_terminal_without_japanese_does_not_crash_either():
    code, out, err = run_cli(PREFIXED, "ascii")
    assert code == 0 and err == ""
    assert "R: 1 kohm" in out and "C: 10 uF" in out and "1e-05 F" in out


def test_utf8_terminal_shows_the_units_as_written():
    code, out, err = run_cli(PREFIXED, "utf-8")
    assert code == 0 and err == ""
    assert "注意: R: 1 k\u2126 を 1000 ohm に換算した" in out
    assert "注意: C: 10 \u00b5F を 1e-05 F に換算した" in out


def test_error_message_with_a_unit_cp932_cannot_show_is_still_one_readable_line(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        '{"template": "rc_lowpass", "plant": {"Vin": {"value": 5, "unit": "V"},'
        ' "R": {"value": 1, "unit": "ohm"}, "C": {"value": 10, "unit": "\u00b5X"}}}', encoding="utf-8",
    )
    code, out, err = run_cli(str(spec), "cp932")
    assert code == 2 and out == ""
    assert "計算できません。" in err and "単位 'uX' は読めない" in err
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize("encoding", ["ascii", "cp932"])
def test_replacement_table(encoding):
    """単位の文字は同じ意味の英数字に、それ以外の表せない文字は ? にする。"""
    text = "\u00b5 \u03bc \u03a9 \u2126 \u2192 \u20ac"         # µ μ Ω Ω → €（€ は表に無い）
    plain = text.encode("ascii", errors="circuit_sim.plain").decode("ascii")
    assert plain == "u u ohm ohm -> ?"
    # その文字コードで表せる文字は、置き換えない
    kept = text.encode(encoding, errors="circuit_sim.plain").decode(encoding)
    assert kept == (plain if encoding == "ascii" else "u \u03bc \u03a9 ohm \u2192 ?")


def test_spec_saved_with_a_byte_order_mark_is_read(tmp_path, capsys):
    """Windows のメモ帳などは、UTF-8 の先頭に BOM を付けて保存する。"""
    spec = tmp_path / "spec.json"
    spec.write_bytes(b"\xef\xbb\xbf" + (ROOT / EXAMPLE).read_bytes())
    assert cli_main([str(spec)]) == 0
    assert "テンプレート: rlc_series" in capsys.readouterr().out
