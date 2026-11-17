"""E1g の独立検証（2回目。差し戻しの直し 48def1c の確かめ）: CSV と注意のファイル（D-075、D-076）。

決まったこと（docs/decisions.md）:
  D-075  CSV は、見出し 1 行（単位つき）とサンプルの行だけ。注意は、CSV と同じ場所の <CSV の名前>.notes.txt
         （UTF-8、1 行 1 件、番号つき。注意が無ければ書かない。書いたことを端末に出す）。改行は CR LF。
  D-076  注意のファイルの名前は、CSV のファイル名の後ろに .notes.txt（out.csv なら out.csv.notes.txt）。
         Result.to_csv の改行の引数の名前は newline（既定は CR LF。"\\n" も選べる）。

ここで確かめること（コマンドラインは main() を呼び、できたファイルをバイト単位で読む）:
  1. 注意のファイルの名前と中身（バイト列）。端末に出す場所。言語。
  2. 注意の無い計算で、前の書き出しの注意のファイルが残っているときの動き（実装者が決め、オーケストレーターが
     いまの形で進めると判断した点: 消して、端末に知らせる）。消すのは、その名前のファイル 1 つだけ。
     その名前がフォルダやシンボリックリンクのとき。
  3. 書き出しに失敗したとき: 理由を 1 行で標準エラーに出し、終了コード 2（Python の例外を外に出さない）。
     CSV を書けなかったら、古い注意のファイルに触らない（CSV と注意のファイルを食い違わせない）。
  4. Result.to_csv の引数の誤りは、ファイルに触る前に、番号つきのエラーで断る。
"""
from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path

import pytest

import circuit_sim.__main__ as cli
from circuit_sim import Result, expand
from circuit_sim.messages import Coded

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

q = hp.q
NOTES = ".notes.txt"
OLD_NOTES = "[OLD_CODE] 注意: 前の計算の注意\r\n".encode()     # 前の書き出しで残った注意のファイルの中身


def spec_with_notes(t_end=3e-4):
    """C_esr を省いた降圧の状態平均化モデル（展開の注意と計算の注意が、合わせて 5 件以上）。"""
    return hp.buck_spec("average", sim={"t_end": t_end})


def spec_without_notes():
    """接頭語の無い単位で書いた RLC（単位の換算の注意も、ほかの注意も出ない）。"""
    plant = {"Vin": q(10, "V"), "R": q(5, "ohm"), "L": q(1e-3, "H"), "C": q(1e-5, "F")}
    return hp.t0_spec("rlc_series", plant)


@pytest.fixture(scope="module")
def results():
    with_notes, without = expand(spec_with_notes()).run(), expand(spec_without_notes()).run()
    assert len(with_notes.notes) >= 5 and without.notes == []
    return {"with": with_notes, "without": without}


def run_cli(tmp_path, spec, target, capsys):
    """コマンドラインを呼ぶ。戻り値: （終了コード、標準出力、標準エラー）。例外は外に出ないこと。"""
    path = tmp_path / "spec.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    code = cli.main([str(path), "--csv", str(target)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def assert_one_line_failure(code, err):
    assert code == 2
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err, err


# ------------------------------------------------------------------ 1. 名前と中身

@pytest.mark.parametrize("name", ["out.csv", "out", "run.1.CSV", "波形 その1.csv", ".hidden", "-"])
def test_notes_file_is_the_csv_file_name_plus_notes_txt_and_holds_one_coded_line_per_note(
        name, tmp_path, capsys, results):
    folder = tmp_path / "出力 フォルダ"
    folder.mkdir()
    code, out, err = run_cli(tmp_path, spec_with_notes(), folder / name, capsys)
    assert code == 0 and err == ""
    assert listing(folder) == sorted([name, name + NOTES])         # ほかの名前のファイルを作らない
    raw = (folder / (name + NOTES)).read_bytes()
    notes = results["with"].notes
    expected = "".join(f"[{note.code}] 注意: {note}\r\n" for note in notes)
    assert raw == expected.encode("utf-8")           # BOM なし、CR LF、1 行 1 件、Result.notes の順
    assert any(note.code == "ESR_OMITTED" for note in notes)       # 展開の注意（基準7）も入っている
    # 端末: CSV の場所の次の行に、注意のファイルの場所と件数。注意そのものも、これまでどおり出る
    lines = out.splitlines()
    at = lines.index(f"波形を書き出しました: {folder / name}")
    assert lines[at + 1] == f"注意（{len(notes)} 件）を書き出しました: {folder / (name + NOTES)}"
    shown = [line.strip() for line in lines if "] 注意: " in line]
    assert shown == expected.replace("\r\n", "\n").splitlines()
    # CSV は、見出しとサンプルの行だけ（注意の行なし。CR LF）
    csv_raw = (folder / name).read_bytes()
    assert csv_raw.count(b"\r\n") == csv_raw.count(b"\n") == 1 + len(results["with"].t)
    assert b"#" not in csv_raw
    assert csv_raw.startswith(b"t [s],v(out) [V],i(L1) [A]\r\n")


def test_notes_file_follows_the_language_of_the_command_line(tmp_path, capsys, results, monkeypatch):
    monkeypatch.setattr(cli, "LANG", "en")
    code, out, err = run_cli(tmp_path, spec_with_notes(), tmp_path / "out.csv", capsys)
    assert code == 0 and err == ""
    text = (tmp_path / ("out.csv" + NOTES)).read_bytes().decode("utf-8")
    assert text == "".join(f"[{note.code}] Warning: {note.text('en')}\r\n" for note in results["with"].notes)
    assert not any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in text + out)
    assert str(tmp_path / ("out.csv" + NOTES)) in out


def test_writing_again_replaces_the_notes_file_instead_of_appending(tmp_path, capsys, results):
    target = tmp_path / "out.csv"
    (tmp_path / ("out.csv" + NOTES)).write_bytes(OLD_NOTES * 40)
    assert run_cli(tmp_path, spec_with_notes(), target, capsys)[0] == 0
    first = (tmp_path / ("out.csv" + NOTES)).read_bytes()
    assert b"OLD_CODE" not in first and first.count(b"\r\n") == len(results["with"].notes)
    assert run_cli(tmp_path, spec_with_notes(), target, capsys)[0] == 0
    assert (tmp_path / ("out.csv" + NOTES)).read_bytes() == first


# ------------------------------------------------------------------ 2. 古い注意のファイル

NEIGHBOURS = {
    "out.notes.txt": "拡張子を取り替えた形の名前",
    "out.csv.notes.txt.bak": "後ろに .bak",
    "xout.csv.notes.txt": "前に字が付く",
    "out.csv.notes.txt2": "後ろに字が付く",
    "out.csv.notes": "途中まで",
    "other.csv.notes.txt": "ほかの CSV の注意",
    "out.csv .notes.txt": "空白が入る",
    "notes.txt": "notes.txt",
}


def test_run_without_notes_removes_only_the_stale_notes_file_of_this_csv_and_says_so(
        tmp_path, capsys, results):
    folder = tmp_path / "f"
    (folder / "sub").mkdir(parents=True)
    target, stale = folder / "out.csv", folder / ("out.csv" + NOTES)
    stale.write_bytes(OLD_NOTES)
    for name, body in NEIGHBOURS.items():
        (folder / name).write_text(body, encoding="utf-8")
    (folder / "sub" / ("out.csv" + NOTES)).write_text("下のフォルダの同じ名前", encoding="utf-8")
    code, out, err = run_cli(tmp_path, spec_without_notes(), target, capsys)
    assert code == 0 and err == ""
    assert not stale.exists()                                      # 今回の CSV の注意に見えるものを残さない
    assert listing(folder) == sorted([*NEIGHBOURS, "out.csv", "sub"])
    assert all((folder / name).read_text(encoding="utf-8") == body for name, body in NEIGHBOURS.items())
    assert (folder / "sub" / ("out.csv" + NOTES)).read_text(encoding="utf-8") == "下のフォルダの同じ名前"
    # 消したことと場所を、端末に出す（CSV の場所の後）。「書き出しました」とは言わない
    lines = out.splitlines()
    at = lines.index(f"波形を書き出しました: {target}")
    assert str(stale) in lines[at + 1] and "消しました" in lines[at + 1]
    assert not any("件）を書き出しました" in line for line in lines)
    assert (folder / "out.csv").read_bytes().count(b"\r\n") == 1 + len(results["without"].t)


def test_run_without_notes_and_without_a_stale_file_mentions_no_notes_file(tmp_path, capsys):
    code, out, err = run_cli(tmp_path, spec_without_notes(), tmp_path / "out.csv", capsys)
    assert code == 0 and err == ""
    assert listing(tmp_path) == ["out.csv", "spec.json"]
    assert NOTES not in out and "消しました" not in out


@pytest.mark.parametrize("which", ["without", "with"])
def test_a_folder_with_the_name_of_the_notes_file_is_never_removed(which, tmp_path, capsys):
    """同じ名前がフォルダなら、消さずに（中身も残して）、理由を 1 行で知らせて終了コード 2。CSV は書ける。"""
    folder = tmp_path / ("out.csv" + NOTES)
    folder.mkdir()
    (folder / "keep.txt").write_text("中身", encoding="utf-8")
    spec = spec_with_notes() if which == "with" else spec_without_notes()
    code, out, err = run_cli(tmp_path, spec, tmp_path / "out.csv", capsys)
    assert_one_line_failure(code, err)
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "中身"
    assert (tmp_path / "out.csv").read_bytes().startswith(b"t [s],")
    assert f"波形を書き出しました: {tmp_path / 'out.csv'}" in out
    assert str(folder) in err                                      # どのファイルの話かが分かる


def _symlinks_work(tmp_path) -> bool:
    try:
        (tmp_path / "probe-link").symlink_to(tmp_path / "probe-target")
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "probe-link").unlink()
    return True


def test_a_symlink_with_the_name_of_the_notes_file_is_removed_without_touching_its_target(tmp_path, capsys):
    if not _symlinks_work(tmp_path):
        pytest.skip("この環境ではシンボリックリンクを作れない")
    folder = tmp_path / "f"
    folder.mkdir()
    kept = tmp_path / "kept.txt"
    kept.write_text("リンクの先", encoding="utf-8")
    link = folder / ("out.csv" + NOTES)
    link.symlink_to(kept)
    code, out, err = run_cli(tmp_path, spec_without_notes(), folder / "out.csv", capsys)
    assert code == 0 and err == ""
    assert not link.is_symlink() and kept.read_text(encoding="utf-8") == "リンクの先"
    # 先の無いリンクも、残さない
    link.symlink_to(tmp_path / "nowhere.txt")
    code, out, err = run_cli(tmp_path, spec_without_notes(), folder / "out.csv", capsys)
    assert code == 0 and not link.is_symlink() and not (tmp_path / "nowhere.txt").exists()
    assert listing(folder) == ["out.csv"]


# ------------------------------------------------------------------ 3. 書き出しの失敗

@pytest.mark.parametrize("which", ["without", "with"])
@pytest.mark.parametrize("problem", ["missing_folder", "csv_name_is_a_folder", "write_fails_midway"])
def test_failed_csv_export_reports_one_line_and_leaves_the_old_notes_file_alone(
        problem, which, tmp_path, capsys, monkeypatch):
    """CSV を書けなかったら、古い注意のファイルを消さず、書き換えもしない。

    消したり書き換えたりすると、残っている前の CSV と、注意のファイルが食い違う（注意だけが新しい計算の
    ものになる、または、前の CSV の注意が無くなる）。
    """
    folder = tmp_path / "f"
    folder.mkdir()
    if problem == "missing_folder":
        target = folder / "no_such_folder" / "out.csv"
        old_notes = None
    else:
        target = folder / "out.csv"
        old_notes = folder / ("out.csv" + NOTES)
        old_notes.write_bytes(OLD_NOTES)
        if problem == "csv_name_is_a_folder":
            target.mkdir()
            (target / "keep.txt").write_text("中身", encoding="utf-8")
        else:
            target.write_text("前の CSV", encoding="utf-8")

            def fail(self, path, *args, **kwargs):
                raise OSError(28, "No space left on device")       # 途中で空きが無くなった代わり

            monkeypatch.setattr(Result, "to_csv", fail)
    spec = spec_with_notes() if which == "with" else spec_without_notes()
    code, out, err = run_cli(tmp_path, spec, target, capsys)
    assert_one_line_failure(code, err)
    assert "書き出しました" not in out and "消しました" not in out
    if old_notes is None:
        assert listing(folder) == []                               # 何も作らない
    else:
        assert old_notes.read_bytes() == OLD_NOTES
        assert listing(folder) == ["out.csv", "out.csv" + NOTES]
    if problem == "csv_name_is_a_folder":
        assert (target / "keep.txt").read_text(encoding="utf-8") == "中身"
    if problem == "write_fails_midway":
        assert target.read_text(encoding="utf-8") == "前の CSV"


def test_a_spec_that_cannot_be_computed_touches_no_files(tmp_path, capsys):
    target, notes = tmp_path / "out.csv", tmp_path / ("out.csv" + NOTES)
    target.write_text("前の CSV", encoding="utf-8")
    notes.write_text("前の注意", encoding="utf-8")
    missing = hp.buck_spec("average", plant={"L": q(None, "uH")})
    code, out, err = run_cli(tmp_path, missing, target, capsys)
    assert_one_line_failure(code, err)
    assert target.read_text(encoding="utf-8") == "前の CSV"
    assert notes.read_text(encoding="utf-8") == "前の注意"


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="読み取り専用のフォルダで確かめる（Windows と管理者では、書き込みを止められない）")
def test_a_stale_notes_file_that_cannot_be_removed_is_reported_in_one_line(tmp_path, capsys):
    folder = tmp_path / "f"
    folder.mkdir()
    target, stale = folder / "out.csv", folder / ("out.csv" + NOTES)
    target.write_text("", encoding="utf-8")
    stale.write_bytes(OLD_NOTES)
    folder.chmod(0o555)
    try:
        code, out, err = run_cli(tmp_path, spec_without_notes(), target, capsys)
    finally:
        folder.chmod(0o755)
    assert_one_line_failure(code, err)
    assert stale.exists() and str(stale) in err


# ------------------------------------------------------------------ 4. Result.to_csv の引数

def test_to_csv_takes_the_line_ending_as_the_keyword_newline(tmp_path, results):
    parameter = inspect.signature(Result.to_csv).parameters["newline"]      # 名前は newline（D-076）
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default == "\r\n"
    res = results["with"]
    res.to_csv(tmp_path / "default.csv", ["v(out)", "i(L1)"], units=True)
    res.to_csv(tmp_path / "crlf.csv", ["v(out)", "i(L1)"], units=True, newline="\r\n")
    res.to_csv(tmp_path / "lf.csv", ["v(out)", "i(L1)"], units=True, newline="\n")
    crlf, lf = (tmp_path / "crlf.csv").read_bytes(), (tmp_path / "lf.csv").read_bytes()
    assert (tmp_path / "default.csv").read_bytes() == crlf
    assert crlf.count(b"\r\n") == crlf.count(b"\n") == 1 + len(res.t)
    assert b"\r" not in lf and lf == crlf.replace(b"\r\n", b"\n")
    assert b"#" not in crlf and "注意".encode() not in crlf       # 注意のある結果でも、注意の行は無い


@pytest.mark.parametrize("bad", ["\r", "", "\n\r", "\r\n\r\n", ";", " ", "CRLF", None, 1, b"\n", True])
def test_to_csv_refuses_other_line_endings_before_touching_the_file(bad, tmp_path, results):
    existing, fresh = tmp_path / "existing.csv", tmp_path / "fresh.csv"
    existing.write_text("前の中身", encoding="utf-8")
    for path in (existing, fresh):
        with pytest.raises(ValueError) as info:
            results["with"].to_csv(path, ["v(out)"], newline=bad)
        assert isinstance(info.value, Coded) and info.value.code == "ARG_CSV_NEWLINE"
        assert info.value.text("en") != info.value.text("ja")
    assert existing.read_text(encoding="utf-8") == "前の中身" and not fresh.exists()


@pytest.mark.parametrize("units", [False, True])
def test_to_csv_refuses_an_unknown_signal_before_touching_the_file(units, tmp_path, results):
    existing, fresh = tmp_path / "existing.csv", tmp_path / "fresh.csv"
    existing.write_text("前の中身", encoding="utf-8")
    for path in (existing, fresh):
        with pytest.raises(KeyError) as info:
            results["with"].to_csv(path, ["v(out)", "v(no_such_node)"], units=units)
        assert isinstance(info.value, Coded) and info.value.code == "RESULT_NO_SIGNAL"
    assert existing.read_text(encoding="utf-8") == "前の中身" and not fresh.exists()
