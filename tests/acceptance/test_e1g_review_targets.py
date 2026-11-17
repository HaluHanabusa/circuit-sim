"""E1g の独立検証: レビュー（1回目）の指摘と D-075 を受けて、直した後に満たすべきことの的。

E1g の実装 d9e5040 では、この試験の一部が落ちた（落ちるのが正しい。xfail の印は付けなかった）。差し戻しの
直し 48def1c で、全部が通る。

  的1（レビューの中1）  制御周期が長い回路で、進み具合が、時間ステップの終わる前に 0.99 以上にならない。
                        知らせの間隔が 0.2 秒以内。
  的2（D-075）          コマンドラインの CSV は「見出し 1 行（単位つき）＋ サンプルの行」だけ。注意の行を
                        入れない。改行は CR LF。注意は、CSV と同じ場所の <CSV の名前>.notes.txt に、番号つきで
                        1 行 1 件。注意が無ければ作らない。書いた場所を端末に出す。Result.to_csv は改行を
                        引数で選べる。
  的3（レビューの軽2）  極端な値の回路で、発散の大きさの見積もりが番号の無い例外を出さない。
  的4（レビューの軽3）  値があふれるほど発散しても、警告（RuntimeWarning）を出さず、発散のエラーを返す。

2回目の検証（2026-11-17、検証役。オーケストレーターの依頼による書き換え）で変えたこと:
  - D-076 で決まった名前に締めた。注意のファイルは <CSV のファイル名>.notes.txt ちょうど（out.csv なら
    out.csv.notes.txt）。Result.to_csv の改行の引数は newline。1回目は、決まる前だったので、どちらの読み方でも
    通る形にしていた（out.notes.txt も、lineterminator なども通った）。
  - 「済みうる計算」の丸めを直した（下の check_against_work）。確かめる中身は弱めていない。
"""
from __future__ import annotations

import csv
import importlib.util
import inspect
import json
import re
import warnings
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Command, DivergenceError, Result, Spec, expand, simulate
from circuit_sim.__main__ import main as cli_main
from circuit_sim.messages import Coded

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

ROOT = Path(__file__).resolve().parents[2]
LIMIT_S = 0.2
q = hp.q

# ------------------------------------------------------------------ 的1: 制御周期が長い回路の進み具合

def check_against_work(seen):
    """hp.check_progress_against_work に渡す前に、「済みうる計算」の丸めを直す。

    済みうる計算は (制御器が最後に呼ばれた時刻 + 2·制御周期) / 終了時刻 で見積もる。時間ステップが済んだ
    ときに、これが丸めで 1 にわずかに届かない組み合わせがある（制御周期 0.2 s × 3、h = 1e-6 s:
    (200000·1e-6 + 0.4) / (3·0.2) = 0.9999999999999998）。そのとき、時間ステップの終わりの値 0.99 が
    「時間ステップが済む前の 0.99」と誤って判定される。直しの後は、その値が知らせを間引く時刻しだいで届く
    ので、試験が 10 回に 1 回ほど落ちた（製品の誤りではなく、この試験の見積もりの丸め）。
    1 との差が 1e-9 以下なら 1 とみなす（見積もりの刻みは 1 制御周期 = 0.2 以上なので、取り違えない）。
    """
    snapped = [(fraction, 1.0 if done > 1.0 - 1e-9 else done) for fraction, done in seen]
    hp.check_progress_against_work(snapped)



def sample_with_slow_control(t_end=50e-3):
    """同梱のサンプルで、f_ctrl を 100 Hz に（f_sw は 100 kHz のまま）。スイッチングモデル、50 ms。

    制御周期は 20 万ステップ（準備の配列を埋める単位 131072 ステップより長い）。100 万ステップ。"""
    d = json.loads((ROOT / "examples" / "buck_voltage_mode.json").read_text(encoding="utf-8"))
    d["control"]["f_ctrl"] = q(100, "Hz")
    d["simulation"] = {"fidelity": "switching", "t_end": t_end}
    return expand(Spec.model_validate(d))


def commanded_rc():
    return hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=10.0)),
                  hp.comp("R1", "R", "in", "out", 1e3), hp.comp("C1", "C", "out", "0", 1e-6))


def slow_engine_case(n_periods):
    """エンジンの口: 指令で値が決まる電源 → R → C。制御周期 20 万ステップ。"""
    period, h = 0.2, 1e-6
    ctl = hp.Lookup([0.1 * (j + 1) for j in range(n_periods)], period=period)
    t_end = n_periods * period
    return (lambda p: simulate(commanded_rc(), t_end, h, controllers=[ctl], progress=p)), ctl, period, t_end


def test_progress_and_its_intervals_with_a_long_control_period_template():
    """レビューの再現（同梱のサンプル。そのままの条件）。

    実測（d9e5040）: 所要時間の 20% の時点で 0.196 → 0.998 に跳び、その後の知らせの間隔が 1.2 秒。
    跳びが出るかどうかは、知らせを間引く時刻で決まり、毎回は出ない（必ず出る条件は、下の 3 つの試験）。"""
    ex = sample_with_slow_control()
    pid = next(c for c in ex.controllers if c.inputs)
    worst = []
    for _ in range(3):
        seen, gap = hp.watch_with_work(lambda p: ex.run(progress=p), pid, 1e-2, ex.t_end)
        check_against_work(seen)
        worst.append(gap)
        if gap <= LIMIT_S:
            break
    assert min(worst) <= LIMIT_S, worst


def test_progress_and_its_intervals_with_a_long_control_period_engine():
    """エンジンの口での再現（5 周期、100 万ステップ）。上と同じく、跳びは毎回は出ない。"""
    worst = []
    for _ in range(3):
        run, ctl, period, t_end = slow_engine_case(5)
        seen, gap = hp.watch_with_work(run, ctl, period, t_end)
        check_against_work(seen)
        worst.append(gap)
        if gap <= LIMIT_S:
            break
    assert min(worst) <= LIMIT_S, worst


def test_progress_does_not_jump_ahead_when_the_controller_takes_time_template():
    """必ず出る条件 1: 制御器の演算に 60 ms かかる（知らせを間引く 50 ms より長い）。同梱のサンプル、30 ms。

    制御器の演算の直後に、次の制御周期のゲート信号の配列を埋める。そこで進み具合が後処理の値（0.99〜）に
    跳ぶ（d9e5040）。知らせの間隔は、演算の待ちを含むので、ここでは見ない。"""
    ex = sample_with_slow_control(t_end=30e-3)
    pid = next(c for c in ex.controllers if c.inputs)
    seen, _ = hp.watch_with_work(lambda p: ex.run(progress=p), pid, 1e-2, ex.t_end, delay=0.06)
    assert len(seen) >= 5
    check_against_work(seen)


def test_progress_does_not_jump_ahead_when_the_controller_takes_time_engine():
    """必ず出る条件 2: 同じことを、エンジンの口で（3 周期、60 万ステップ）。"""
    run, ctl, period, t_end = slow_engine_case(3)
    seen, _ = hp.watch_with_work(run, ctl, period, t_end, delay=0.06)
    assert len(seen) >= 5
    check_against_work(seen)


def test_progress_never_runs_ahead_when_the_setup_block_is_small():
    """必ず出る条件 3: 準備の配列を埋める単位を 64 ステップにして、どの制御周期（1000 ステップ）でも単位を
    またぐようにする。30 万ステップ。区切りは既定のまま（時間で決まる）。"""
    period, n_periods, h = 1e-3, 300, 1e-6
    ctl = hp.Lookup([0.2 + 0.001 * (j % 7) for j in range(n_periods)], period=period)
    with hp.chunk_steps(setup_block=64):
        seen, _ = hp.watch_with_work(
            lambda p: simulate(commanded_rc(), n_periods * period, h, controllers=[ctl], progress=p),
            ctl, period, n_periods * period)
    assert len(seen) >= 5
    check_against_work(seen)


# ------------------------------------------------------------------ 的2: CSV の形（D-075）

def run_cli(tmp_path, spec: Spec, capsys, name="out.csv"):
    path = tmp_path / "spec.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    target = tmp_path / name
    assert cli_main([str(path), "--csv", str(target)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return target, captured.out


def notes_files(csv_path: Path) -> list[Path]:
    """同じフォルダにある、.notes.txt で終わるファイルの全部（D-076 の名前のもの 1 つだけのはず）。"""
    return sorted(p for p in csv_path.parent.iterdir() if p.name.endswith(".notes.txt"))


def notes_file_of(csv_path: Path) -> Path:
    """D-076 の名前: CSV のファイル名の後ろに .notes.txt（拡張子を取り替えない）。"""
    return csv_path.with_name(csv_path.name + ".notes.txt")


SPEC_WITH_NOTES = lambda: hp.buck_spec("average", sim={"t_end": 3e-4})          # noqa: E731
# 接頭語の無い単位で書いた RLC（単位の換算の注意も、ほかの注意も出ない）
SPEC_WITHOUT_NOTES = lambda: hp.t0_spec("rlc_series", {                         # noqa: E731
    "Vin": q(10, "V"), "R": q(5, "ohm"), "L": q(1e-3, "H"), "C": q(1e-5, "F")})


def test_cli_csv_is_a_header_and_sample_rows_only(tmp_path, capsys):
    spec = SPEC_WITH_NOTES()
    res = expand(spec).run()
    assert len(res.notes) >= 5                                    # 注意の付く結果で確かめる
    target, _ = run_cli(tmp_path, spec, capsys)
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert len(lines) == 1 + len(res.t)                           # 見出し 1 行 ＋ サンプルの数
    assert not any(line.startswith("#") for line in lines)        # 注意の行が無い
    assert lines[0] == "t [s],v(out) [V],i(L1) [A]"               # 見出しに単位（D-042）
    # 改行は、どの行も CR LF（最後の行も）
    assert raw.count(b"\r\n") == len(lines) and raw.count(b"\n") == len(lines) and raw.endswith(b"\r\n")
    # 標準の csv で、全部の行が同じ列数の数値として読める
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["t [s]", "v(out) [V]", "i(L1) [A]"]
    data = np.array([[float(cell) for cell in row] for row in rows[1:]])
    assert data.shape == (len(res.t), 3)
    assert np.array_equal(data[:, 0], res.t) and np.array_equal(data[:, 1], res["v(out)"])
    assert np.array_equal(data[:, 2], res["i(L1)"])               # 値は、倍精度のまま往復する


def test_cli_writes_the_notes_next_to_the_csv_with_codes(tmp_path, capsys):
    spec = SPEC_WITH_NOTES()
    res = expand(spec).run()
    target, out = run_cli(tmp_path, spec, capsys)
    found = notes_files(target)
    assert found == [notes_file_of(target)], [p.name for p in tmp_path.iterdir()]    # out.csv.notes.txt だけ
    assert found[0].name == "out.csv.notes.txt"
    lines = [line for line in found[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(res.notes)                           # 1 行 1 件
    for line, note in zip(lines, res.notes, strict=True):
        assert f"[{note.code}]" in line and str(note) in line     # 番号つき。文は端末と同じ
    assert "ESR_OMITTED" in "\n".join(lines) and "AVERAGE_MODEL_USED" in "\n".join(lines)
    assert str(found[0]) in out                                   # 書いた場所を端末に出す
    assert str(target) in out
    # 端末にも、これまでどおり番号つきで出る
    assert re.findall(r"^\s+\[([A-Z0-9_]+)\] 注意: ", out, flags=re.M) == [n.code for n in res.notes]


def test_cli_does_not_write_a_notes_file_when_there_are_no_notes(tmp_path, capsys):
    spec = SPEC_WITHOUT_NOTES()
    res = expand(spec).run()
    assert res.notes == []
    target, out = run_cli(tmp_path, spec, capsys)
    assert notes_files(target) == []
    assert ".notes.txt" not in out and "注意:" not in out
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + len(res.t)


def test_cli_without_csv_writes_no_file(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text(SPEC_WITH_NOTES().model_dump_json(), encoding="utf-8")
    assert cli_main([str(path)]) == 0
    capsys.readouterr()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spec.json"]


def test_to_csv_never_writes_note_rows_and_lets_the_caller_choose_the_line_ending(tmp_path):
    res = expand(SPEC_WITH_NOTES()).run()
    assert res.notes
    # 既定: CR LF、注意の行なし
    res.to_csv(tmp_path / "default.csv", ["v(out)"], units=True)
    raw = (tmp_path / "default.csv").read_bytes()
    assert raw.count(b"\r\n") == 1 + len(res.t) == raw.count(b"\n") and b"#" not in raw
    # 改行を選ぶ引数の名前は newline（D-076）。既定は CR LF
    assert inspect.signature(Result.to_csv).parameters["newline"].default == "\r\n"
    res.to_csv(tmp_path / "lf.csv", ["v(out)"], units=True, newline="\n")
    raw_lf = (tmp_path / "lf.csv").read_bytes()
    assert b"\r" not in raw_lf and raw_lf.count(b"\n") == 1 + len(res.t)
    assert raw_lf == raw.replace(b"\r\n", b"\n")                  # 中身は同じで、改行だけが違う


# ------------------------------------------------------------------ 的3: 極端な値で番号の無い例外を出さない

def test_extreme_values_do_not_break_the_divergence_scale_estimate():
    """レビューの再現: rlc_series で Vin 1e-30 V、R 1e-9 Ω、L 1e-30 H、C 1e300 F。

    √(L/C) が 0 に下あふれして、大きさの見積もりが 0 で割る（d9e5040 では ZeroDivisionError）。E1g の前の
    main では計算できていた（2 万ステップ）。計算できるか、番号つきのエラーで断るかの、どちらかであること。"""
    spec = hp.t0_spec("rlc_series", {"Vin": q(1e-30, "V"), "R": q(1e-9, "ohm"), "L": q(1e-30, "H"),
                                     "C": q(1e300, "F")})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")           # 展開の中のあふれの警告は、この的の対象ではない
        try:
            res = expand(spec).run()
        except Exception as exc:  # noqa: BLE001
            assert isinstance(exc, Coded), f"番号の無い例外 {type(exc).__name__}: {exc}"
        else:
            assert np.isfinite(res.x).all()


# ------------------------------------------------------------------ 的4: あふれても警告を出さない

def overflowing_run():
    """電源の目安が 1e300 V の正帰還。発散とみなす大きさが倍精度の上限を超えるので、値があふれるまで進む。"""
    netlist = hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=1e300)),
                     hp.comp("R1", "R", "in", "out", 1e3), hp.comp("C1", "C", "out", "0", 1e-6))
    return simulate(netlist, 0.5, 1e-5, controllers=[hp.Feedback(3e-300, period=1e-3)])


def test_overflow_gives_the_divergence_error_even_when_warnings_are_errors():
    """python -W error で動かしても（警告を例外にしても）、発散のエラーが返る。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(DivergenceError) as info:
            overflowing_run()
    assert info.value.code == "RUN_DIVERGED_NOT_FINITE"


def test_overflow_does_not_emit_warnings():
    """あふれたときに、numpy の警告（番号の無い英語の文）が標準エラーに出ない。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(DivergenceError):
            overflowing_run()
    assert [str(w.message) for w in caught] == []
