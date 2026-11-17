"""E1g の範囲3・4・6・7・8・9・10・12（受け入れ基準7〜11、13）。

- 信号の情報（基準9、10）: すべての回路で、結果の名前ごとに単位と正の向きが返り、
名前が「表示する」と「内部」に
  漏れなく分かれる。テンプレートが決めていない名前（内部とみなす名前）は無い。表示する信号は画面仕様の付録A の
  信号の表（A.2、A.3、A.4）と1対1。テンプレートが決めた情報は、エンジンが回路から作る情報と食い違わない。
- CSV（範囲4、基準7、D-075、D-076）: コマンドラインの --csv の見出しに単位（画面と同じ形）。CSV は見出し
  1行とサンプルの行だけ（注意の行を入れない）。行の終わりは CR LF で、Result.to_csv の newline で選べる。
- テンプレートの注意（基準7）: C_esr を省いた降圧で、Result.notes・コマンドラインの表示・CSV と一緒に書き出す
  注意のファイル（<CSV の名前>.notes.txt）に番号つきで出る。注意が無ければ注意のファイルを書かず、前の
  書き出しのものが残っていたら消す。
- 表示に使う値（基準11）: 降圧の r_on がテンプレートの表示定義から取れ、計算に使った値と一致する。
- 出典（基準8）: 手入力 と manual が同じ結果になり、書き出すと 手入力。
- 層A の欄（基準13）: targets と missing は予約で、読まない（結果を変えない）。
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Spec, expand
from circuit_sim.__main__ import main as cli_main
from circuit_sim.__main__ import notes_path
from circuit_sim.signals import KIND_UNIT, describe_signals
from circuit_sim.templates import TEMPLATE_DISPLAY

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "docs" / "design" / "screens-v0.1.md"
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"


def buck(fidelity="switching", esr=None, load_step=False, ref_step=False, t_end=0.3e-3):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    if esr is not None:
        d["plant"]["C_esr"] = {"value": esr, "unit": "ohm", "source": "手入力"}
    if load_step:
        d["plant"]["R_load_step"] = {"value": 3, "unit": "ohm"}
        d["plant"]["t_load_step"] = {"value": 0.1, "unit": "ms"}
    if ref_step:
        d["control"]["Vref_step"] = {"value": 10, "unit": "V"}
        d["control"]["t_ref_step"] = {"value": 0.15, "unit": "ms"}
    return expand(Spec.model_validate(d))


def t0(template, **plant):
    return expand(Spec.model_validate({"template": template, "plant": {
        k: {"value": v, "unit": u} for k, (v, u) in plant.items()}}))


RC = dict(Vin=(5, "V"), R=(1e3, "ohm"), C=(1e-6, "F"))
RLC = dict(Vin=(10, "V"), R=(1, "ohm"), L=(1e-3, "H"), C=(10e-6, "F"))

ALL_CIRCUITS = {"rc_lowpass": lambda: t0("rc_lowpass", **RC), "rlc_series": lambda: t0("rlc_series", **RLC)}
COMBINATIONS = itertools.product(("switching", "average"), (None, 0.0, 0.02), (False, True), (False, True))
for f, esr, ls, rs in COMBINATIONS:
    ALL_CIRCUITS[f"buck_{f}_esr{esr}_load{ls}_ref{rs}"] = (
        lambda f=f, esr=esr, ls=ls, rs=rs: buck(f, esr, ls, rs))


@pytest.mark.parametrize("case", list(ALL_CIRCUITS))
def test_every_result_name_is_either_shown_or_internal_and_declared_by_the_template(case):
    ex = ALL_CIRCUITS[case]()
    res = ex.run()
    names = set(res.names)
    assert set(res.signal_info) == names
    display, internal = set(res.display_names), set(res.internal_names)
    assert display | internal == names and not (display & internal)
    declared = {s.name for s in ex.signals}
    assert names - declared == set(), "テンプレートが決めていない名前（内部とみなされる）"
    gates = {c.gate: [c.id] for c in ex.netlist.components if c.type == "S"}
    engine = describe_signals(ex.netlist, res.names, gates)
    for name, info in res.signal_info.items():
        assert info.unit == KIND_UNIT[info.kind] and info.unit in ("V", "A", "")
        e = engine[name]
        assert (info.kind, info.unit, info.node, info.element, info.positive, info.command, info.gate) == (
            e.kind, e.unit, e.node, e.element, e.positive, e.command, e.gate), name
        if info.kind == "element_current":
            assert info.positive == tuple(ex.netlist.component(info.element).nodes)
        assert len(res[name]) == len(res.t)


def table_rows(section_title: str, end: str) -> list[list[str]]:
    text = SCREENS.read_text(encoding="utf-8").split(section_title)[1].split(end)[0]
    table = text.split("**信号。**")[1].split("\n\n")[1]
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in table.splitlines()
            if line.startswith("|") and not line.startswith("|---") and "番号（色）" not in line]


@pytest.mark.parametrize("template, section, end, plant", [
    ("rc_lowpass", "### A.2 RCローパス", "### A.3", RC),
    ("rlc_series", "### A.3 直列RLC", "### A.4", RLC),
])
def test_rc_and_rlc_signals_match_appendix_a_one_to_one(template, section, end, plant):
    """名前と並び、単位、既定で表示するか。正の向きは説明の「a から out へ」「V1 の中を ＋ から −」と同じ。"""
    rows = table_rows(section, end)
    ex = t0(template, **plant)
    res = ex.run()
    assert res.display_names == [r[1].strip("`") for r in rows]
    for name, r in zip(res.display_names, rows, strict=True):
        info = res.signal_info[name]
        assert info.unit == r[2] and info.shown == (r[3] == "する") and not info.internal
        m = re.search(r"(\w+) から (\w+) へ流れる向きが正", r[4])
        if m:
            assert info.positive == m.groups()
        elif info.kind == "element_current":
            assert "の中を ＋ から − へ流れる向きが正" in r[4] and info.positive == ("in", "0")
    assert res.internal_names == []
    assert [s.name for s in TEMPLATE_DISPLAY[template].signals[None]] == res.display_names


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_buck_display_signals_match_a4_and_the_display_definition(fidelity):
    rows = table_rows("### A.4 降圧コンバータ", "### A.5")
    column = {"switching": 4, "average": 5}[fidelity]
    expected = [r[1].strip("`") for r in rows if r[column] == "出る"]
    for esr, ls, rs in itertools.product((None, 0.02), (False, True), (False, True)):
        res = buck(fidelity, esr, ls, rs).run()
        assert res.display_names == expected
    assert [s.name for s in TEMPLATE_DISPLAY["buck_voltage_mode"].signals[fidelity]] == expected


def test_buck_internal_signals_match_the_a4_internal_table():
    """付録A.4 の「内部の信号」の表に挙げた名前と、結果の内部の信号が同じ（i(Rleg) は結果に無い）。"""
    text = SCREENS.read_text(encoding="utf-8").split("| 内部の信号 | モデル | 出さない理由 |")[1]
    text = text.split("\n\n")[0]
    first = [line.split("|")[1] for line in text.splitlines()
             if line.strip().startswith("|") and "---" not in line]
    listed = set(re.findall(r"`([a-z]+\([A-Za-z0-9_]+\))`", "\n".join(first)))
    found = set()
    for f, esr, ls in itertools.product(("switching", "average"), (None, 0.02), (False, True)):
        found |= set(buck(f, esr, ls).run().internal_names)
    assert found == listed - {"i(Rleg)"}


# ------------------------------------------------------------------ CSV とテンプレートの注意


def write_spec(tmp_path, d) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return path


def test_template_note_appears_in_notes_cli_and_the_notes_file_with_its_code(tmp_path, capsys):
    """基準7（D-075 で改訂）: C_esr を省いた降圧（同梱の例）。注意は Result.notes、コマンドラインの表示、
    CSV と一緒に書き出す注意のファイルの3か所に、番号つきで出る。CSV そのものには入れない。"""
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "switching", "t_end": 0.2e-3}
    ex = expand(Spec.model_validate(d))
    res = ex.run()
    assert [n.code for n in res.notes].count("ESR_OMITTED") == 1
    assert res.notes[: len(ex.notes)] == ex.notes                    # 展開の注意が先頭に入る
    csv_path = tmp_path / "out.csv"
    assert cli_main([str(write_spec(tmp_path, d)), "--csv", str(csv_path)]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == ""
    assert re.search(r"^  \[ESR_OMITTED\] 注意: C_esr（出力コンデンサの等価直列抵抗）の値が無い", out, re.M)
    assert out.count("注意:") == len(res.notes)                      # 展開の注意を二重に出さない
    # CSV: 見出し1行（単位つき）とサンプルの行だけ。どの行も CR LF。注意の行（# …）は無い
    raw = csv_path.read_bytes()
    lines = raw.decode("utf-8").split("\r\n")
    assert lines[-1] == "" and len(lines) - 1 == 1 + len(res.t) and raw.count(b"\n") == 1 + len(res.t)
    assert lines[0] == "t [s],v(out) [V],i(L1) [A]" and b"#" not in raw and "注意".encode() not in raw
    data = np.loadtxt(csv_path, delimiter=",", skiprows=1)
    assert data.shape == (len(res.t), 3) and np.array_equal(data[:, 1], res["v(out)"])
    # 注意のファイル: CSV のファイル名の後ろに .notes.txt（D-076）。UTF-8（BOM なし）、1行1件、番号つき、CR LF
    notes_file = tmp_path / "out.csv.notes.txt"
    assert notes_path(csv_path) == notes_file
    expected = "".join(f"[{n.code}] 注意: {n}\r\n" for n in res.notes)
    assert notes_file.read_bytes() == expected.encode("utf-8")
    assert "[ESR_OMITTED] 注意: C_esr（出力コンデンサの等価直列抵抗）の値が無い" in expected
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.csv", "out.csv.notes.txt", "spec.json"]
    # 書いたことと場所を、端末に出す（波形、注意の順）
    assert out.splitlines()[-2:] == [
        f"波形を書き出しました: {csv_path}", f"注意（{len(res.notes)} 件）を書き出しました: {notes_file}"]


def test_notes_file_name_is_the_csv_file_name_plus_notes_txt():
    """D-076: 拡張子を取り替えない（out.csv → out.csv.notes.txt）。拡張子の無い名前、点の多い名前でも同じ。"""
    assert notes_path(Path("a") / "out.csv") == Path("a") / "out.csv.notes.txt"
    assert notes_path(Path("out")) == Path("out.notes.txt")
    assert notes_path(Path("run.1.CSV")) == Path("run.1.CSV.notes.txt")
    assert notes_path(Path("-")) == Path("-.notes.txt")          # 標準出力に出す形は無い（- もファイル名）


def test_csv_header_units_for_dimensionless_and_time(tmp_path):
    res = buck().run()
    path = tmp_path / "all.csv"
    res.to_csv(path, ["v(out)", "i(L1)", "cmd(duty)", "gate(q1)"], units=True)
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "t [s],v(out) [V],i(L1) [A],cmd(duty) [1],gate(q1) [1]")
    res.to_csv(path, ["v(out)"])                                     # 既定は単位なし（E0 の形のまま）
    assert path.read_text(encoding="utf-8").splitlines()[0] == "t,v(out)"


def test_to_csv_writes_only_the_header_and_samples_and_takes_the_line_ending(tmp_path):
    """D-075、D-076: to_csv は注意の行を書かない（注意のある結果でも）。行の終わりは newline で選ぶ
    （既定は CR LF、"\\n" も選べる）。中身は同じで、行の終わりだけが違う。"""
    res = buck("average").run()
    assert len(res.notes) >= 2                                       # 注意のある結果で確かめる
    names = ["v(out)", "cmd(duty)"]
    res.to_csv(tmp_path / "crlf.csv", names, units=True)
    res.to_csv(tmp_path / "same.csv", names, units=True, newline="\r\n")
    res.to_csv(tmp_path / "lf.csv", names, units=True, newline="\n")
    crlf, lf = (tmp_path / "crlf.csv").read_bytes(), (tmp_path / "lf.csv").read_bytes()
    assert (tmp_path / "same.csv").read_bytes() == crlf
    rows = [",".join(["t [s]", "v(out) [V]", "cmd(duty) [1]"])] + [
        ",".join(repr(float(v)) for v in (res.t[k], res["v(out)"][k], res["cmd(duty)"][k]))
        for k in range(len(res.t))]
    assert crlf == "".join(row + "\r\n" for row in rows).encode("utf-8")
    assert lf == "".join(row + "\n" for row in rows).encode("utf-8")
    # 注意を CSV に足す引数は無い（D-075。注意は Result.notes から取る）
    with pytest.raises(TypeError):
        res.to_csv(tmp_path / "x.csv", names, notes=True)
    assert not (tmp_path / "x.csv").exists()


@pytest.mark.parametrize("bad", ["\r", "", "\n\n", ";", None, 2, "\r\n "])
def test_to_csv_refuses_other_line_endings_with_a_coded_error_before_writing(tmp_path, bad):
    res = t0("rc_lowpass", **RC).run()
    path = tmp_path / "keep.csv"
    path.write_bytes(b"old")
    with pytest.raises(ValueError) as info:
        res.to_csv(path, newline=bad)
    assert info.value.code == "ARG_CSV_NEWLINE" and info.value.params == {"value": repr(bad)}
    assert "CR LF" in str(info.value) and "CR LF" in info.value.text("en") and repr(bad) in str(info.value)
    assert path.read_bytes() == b"old"                               # 断るのは、ファイルを開く前
    with pytest.raises(KeyError):
        res.to_csv(path, ["v(nowhere)"], units=True)                 # 無い名前も、開く前に断る
    assert path.read_bytes() == b"old"


def rc_dict():
    plant = {k: {"value": v, "unit": u, "source": "手入力"} for k, (v, u) in RC.items()}
    return {"template": "rc_lowpass", "plant": plant}


def test_rc_cli_csv_has_units_and_no_notes_file_when_there_are_no_notes(tmp_path, capsys):
    csv_path = tmp_path / "rc.csv"
    assert cli_main([str(write_spec(tmp_path, rc_dict())), "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "注意" not in out and out.splitlines()[-1] == f"波形を書き出しました: {csv_path}"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t [s],v(out) [V],i(V1) [A]" and not any(line.startswith("#") for line in lines)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rc.csv", "spec.json"]   # 注意のファイルは無い


def test_cli_removes_the_notes_file_of_an_earlier_export_when_this_run_has_no_notes(tmp_path, capsys):
    """前の書き出しの注意のファイルが同じ名前で残っていると、今回の CSV の注意に見える。注意が無い計算では、
    それを消して、消したことと場所を表示する。ほかの名前のファイルには触らない。"""
    csv_path = tmp_path / "rc.csv"
    stale, other = tmp_path / "rc.csv.notes.txt", tmp_path / "rc.notes.txt"
    stale.write_text("[ESR_OMITTED] 注意: 前の計算の注意\r\n", encoding="utf-8")
    other.write_text("自分で書いたメモ", encoding="utf-8")
    assert cli_main([str(write_spec(tmp_path, rc_dict())), "--csv", str(csv_path)]) == 0
    captured = capsys.readouterr()
    assert not stale.exists() and other.read_text(encoding="utf-8") == "自分で書いたメモ"
    assert captured.err == "" and captured.out.splitlines()[-2:] == [
        f"波形を書き出しました: {csv_path}",
        f"今回の計算に注意は無いので、前の書き出しで残っていた注意のファイルを消しました: {stale}"]
    # もう一度書き出すと、消すものは無いので、何も言わない
    assert cli_main([str(tmp_path / "spec.json"), "--csv", str(csv_path)]) == 0
    assert "注意" not in capsys.readouterr().out


def test_cli_replaces_the_notes_file_of_an_earlier_export_when_this_run_has_notes(tmp_path, capsys):
    """注意のある計算では、前の注意のファイルを今回の注意で置き換える（後ろに足さない）。"""
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "average", "t_end": 0.2e-3}
    res = expand(Spec.model_validate(d)).run()
    csv_path = tmp_path / "out.csv"
    notes_file = notes_path(csv_path)
    notes_file.write_text("[OLD] 注意: 前の計算の注意\r\n" * 50, encoding="utf-8")
    assert cli_main([str(write_spec(tmp_path, d)), "--csv", str(csv_path)]) == 0
    capsys.readouterr()
    assert notes_file.read_bytes() == "".join(f"[{n.code}] 注意: {n}\r\n" for n in res.notes).encode("utf-8")


def test_cli_reports_a_failed_export_in_one_line(tmp_path, capsys):
    """書き出せないとき（場所が無い、注意のファイルの名前が使えない）は、理由を1行で標準エラーに出して、
    終了コード 2（Python の例外の表示にしない）。"""
    spec_path = write_spec(tmp_path, rc_dict())
    missing_dir = tmp_path / "no_such_dir" / "rc.csv"
    assert cli_main([str(spec_path), "--csv", str(missing_dir)]) == 2
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1 and captured.err.startswith("ファイルに書き出せません: ")
    assert "no_such_dir" in captured.err and "書き出しました" not in captured.out
    # 注意のファイルの名前に、フォルダがある: CSV は書けるが、注意のファイルを書けない
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "average", "t_end": 0.2e-3}
    csv_path = tmp_path / "out.csv"
    notes_path(csv_path).mkdir()
    assert cli_main([str(write_spec(tmp_path, d)), "--csv", str(csv_path)]) == 2
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1 and captured.err.startswith("ファイルに書き出せません: ")
    assert "out.csv.notes.txt" in captured.err
    assert captured.out.splitlines()[-1] == f"波形を書き出しました: {csv_path}" and csv_path.exists()


# ------------------------------------------------------------------ 表示に使う値


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_on_resistance_from_the_display_definition_matches_the_netlist(fidelity):
    """基準11: r_on は仕様の値によらず表示定義から取れる。

    計算に使った S1・S2（平均モデルでは Rleg）の値と同じ。"""
    r_on = TEMPLATE_DISPLAY["buck_voltage_mode"].value("r_on")
    assert r_on.unit == "ohm" and set(r_on.elements) == {"S1", "S2", "Rleg"}
    net = buck(fidelity).netlist
    used = [c.r_on for c in net.components if c.id in ("S1", "S2")] + [
        c.value for c in net.components if c.id == "Rleg"]
    assert used and all(v == r_on.value for v in used)


def test_display_definition_has_models_and_their_defaults():
    d = TEMPLATE_DISPLAY["buck_voltage_mode"]
    assert d.fidelities == ("switching", "average") and d.default_fidelity == "switching"
    assert [i.key for i in d.items][:3] == ["Vin", "L", "C"]
    assert TEMPLATE_DISPLAY["rc_lowpass"].fidelities == () and TEMPLATE_DISPLAY["rc_lowpass"].values == ()


# ------------------------------------------------------------------ 層A の欄


def rc_spec(source=None, **extra):
    extra_source = {"source": source} if source is not None else {}
    d = {"template": "rc_lowpass", "plant": {k: {"value": v, "unit": u, **extra_source}
                                            for k, (v, u) in RC.items()}}
    d.update(extra)
    return Spec.model_validate(d)


@pytest.mark.parametrize("written", ["manual", " Manual ", "MANUAL"])
def test_manual_and_tenyuryoku_are_the_same_and_saved_as_tenyuryoku(written):
    """基準8: manual と 手入力 は同じに扱う（計算の結果も同じ）。書き出すと 手入力。"""
    a, b = rc_spec("手入力"), rc_spec(written)
    assert a == b
    assert all(q.source == "手入力" for q in b.plant.values())
    assert json.loads(b.model_dump_json())["plant"]["R"]["source"] == "手入力"
    ra, rb = expand(a).run(), expand(b).run()
    assert np.array_equal(ra.x, rb.x) and [str(n) for n in ra.notes] == [str(n) for n in rb.notes]


def test_other_sources_are_kept_as_written():
    s = rc_spec("p.3 表2")
    assert s.plant["R"].source == "p.3 表2" and rc_spec(None).plant["R"].source is None
    assert rc_spec("manually typed").plant["R"].source == "manually typed"


def test_targets_and_missing_are_reserved_and_not_read():
    """基準13: targets（設計目標）と missing は予約。中身があっても結果は変わらず、注意も出ない。

    不足は実際の値から判断する（missing に名前があっても値があれば計算し、missing が空でも
    値が無ければ不足）。"""
    base = expand(rc_spec("手入力")).run()
    other = expand(rc_spec("手入力", targets={"overshoot": 5.0}, missing=["R", "C"])).run()
    assert np.array_equal(base.x, other.x) and [str(n) for n in base.notes] == [str(n) for n in other.notes]
    d = {"template": "rc_lowpass", "missing": [], "plant": {"Vin": {"value": 5, "unit": "V"},
                                                          "R": {"value": None, "unit": "ohm"}}}
    with pytest.raises(ValueError) as info:
        expand(Spec.model_validate(d))
    assert info.value.code == "MISSING_VALUES" and info.value.names == ["R", "C"]
    assert "予約" in Spec.__doc__ and "targets" in Spec.__doc__ and "missing" in Spec.__doc__
