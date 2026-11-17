"""A0 の独立検証（11回目）: main（66c562d）を取り込んだ後の、アプリとエンジンの境目の受け入れ試験。

記録は docs/verification/A0-verifier-11.md。

main（E1b、E1g）で、A0 のサーバー（circuit_sim/app/server.py の check_spec）が使うエンジンの口が変わった:
  - 読める版: SCHEMA_VERSION が 0.1 → 0.2、SUPPORTED_SCHEMA_VERSIONS = ("0.1", "0.2") が公開された。
    版の欄が無いファイルは、中身から版を決める。
  - Spec.control: 何でも入る dict → 値・単位・出典の組（Quantity）。版 0.1 の数値は読むときに組に直し、
    数値でも null でもない値はエンジンが断る。
  - TEMPLATES: buck_voltage_mode が足された。
ここでは、それをサーバーがどう受けるかを確かめる（サーバーの関数をそのまま呼ぶ。ブラウザは使わない）。

期待値の作り方: テンプレートの一覧と読める版の一覧はエンジンが公開するもので、画面仕様（N-OPEN-TEMPLATE、
N-OPEN-VERSION）も「エンジンが使えるもの」を見せると決めている。だから一覧そのものは読むが、確かめるのは
一覧の作り方によらない性質（一覧に出たものは、どれも本当に開ける。テンプレートには両方の言語の名前がある）。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import circuit_sim
from circuit_sim.app.server import check_spec
from circuit_sim.templates import TEMPLATES

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "circuit_sim" / "app" / "samples" / "rlc_series.json"
CJK = re.compile(r"[぀-ヿ㐀-鿿]")
PLANT = {
    "Vin": {"value": 10, "unit": "V", "source": "p.1"},
    "R": {"value": 1, "unit": "ohm", "source": "p.1"},
    "L": {"value": 1e-3, "unit": "H", "source": "p.1"},
    "C": {"value": 1e-5, "unit": "F", "source": "p.1"},
}
I18N = ROOT / "frontend" / "src" / "i18n"


def open_text(data: object) -> tuple[dict | None, dict | None]:
    return check_spec("x.json", json.dumps(data))


def listed_templates() -> list[str]:
    summary, notice = open_text({"template": "no_such_template", "plant": {}})
    assert summary is None and notice is not None and notice["code"] == "N-OPEN-TEMPLATE", notice
    return notice["params"]["list"].split(", ")


# ------------------------------------------------------------------ テンプレート


def test_every_template_named_in_the_notice_opens():
    """開けないテンプレートの案内の「使えるテンプレート」は、どれも本当に開ける（版 0.1 でも 0.2 でも）。"""
    listed = listed_templates()
    assert listed == sorted(listed) and len(set(listed)) == len(listed), listed
    assert set(listed) == set(TEMPLATES)
    assert "rlc_series" in listed          # 同梱のサンプルのテンプレート
    for name in listed:
        for version in ("0.1", "0.2"):
            summary, notice = open_text({"schema_version": version, "template": name, "plant": {}})
            assert notice is None, (name, version, notice)
            assert summary == {"file": "x.json", "template": name}


def test_every_engine_template_has_a_name_in_both_languages():
    """画面（ヘッダーと枠）は template.<名前>.name でテンプレートの名前を出す。エンジンの一覧のどれを
    開いても、辞書の鍵や識別子がそのまま出ない（日本語の名前には仮名・漢字がある。英語の名前には無い）。"""
    dicts = {lang: json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8")) for lang in ("ja", "en")}
    for name in TEMPLATES:
        key = f"template.{name}.name"
        ja, en = dicts["ja"].get(key), dicts["en"].get(key)
        assert ja and en, (key, ja, en)
        assert CJK.search(ja) and not CJK.search(en), (key, ja, en)


def test_buck_example_from_main_opens():
    """main で足された examples/buck_voltage_mode.json（版 0.2、control が組の形）を開ける。"""
    summary, notice = check_spec("buck_voltage_mode.json",
                                 (ROOT / "examples" / "buck_voltage_mode.json").read_text(encoding="utf-8"))
    assert notice is None, notice
    assert summary == {"file": "buck_voltage_mode.json", "template": "buck_voltage_mode"}


# ------------------------------------------------------------------ 版


def test_bundled_sample_version_is_readable_and_the_sample_opens():
    """同梱のサンプル（版 0.1）は、main で現在の版が 0.2 になった後も、読める版に入っていて開ける。"""
    text = SAMPLE.read_text(encoding="utf-8")
    assert json.loads(text)["schema_version"] in circuit_sim.SUPPORTED_SCHEMA_VERSIONS
    summary, notice = check_spec(SAMPLE.name, text)
    assert notice is None, notice
    assert summary == {"file": "rlc_series.json", "template": "rlc_series"}


@pytest.mark.parametrize("data", [
    {"template": "rlc_series", "plant": PLANT},                                   # 版の欄なし → 0.2
    {"template": "rlc_series", "plant": PLANT, "control": {"kp": 1.5}},           # 版の欄なし → 0.1
    {"schema_version": "0.1", "template": "rlc_series", "plant": PLANT, "control": {"kp": 1.5, "ki": None}},
    {"schema_version": "0.2", "template": "rlc_series", "plant": PLANT,
     "control": {"kp": {"value": 1.5, "unit": "", "source": "手入力"}}},
], ids=["no-version", "no-version-bare-control", "v0.1-bare-control", "v0.2-control-quantity"])
def test_files_the_engine_reads_also_open_in_the_app(data):
    """エンジンが読む形（版の欄なし、版 0.1 の数値の control、版 0.2 の組の control）は、アプリでも開ける。
    サーバーが、エンジンと別の版の決まりを持っていない。"""
    circuit_sim.Spec.model_validate(data)        # 前提: エンジンが読む
    summary, notice = open_text(data)
    assert notice is None, notice
    assert summary == {"file": "x.json", "template": "rlc_series"}


# ------------------------------------------------------------------ control の形（main で厳しくなった所）


@pytest.mark.parametrize("value", ["abc", True, [1, 2]], ids=["string", "bool", "list"])
def test_legacy_control_that_is_not_a_number_is_a_schema_notice(value):
    """版 0.1 の control の値が数値でも null でもないと、エンジンは断る（main の Spec）。アプリは落ちずに
    「仕様JSONの形に合わない」（N-OPEN-SCHEMA）として案内する（取り込む前の A0 では、この形は開けていた）。"""
    summary, notice = open_text({"schema_version": "0.1", "template": "rlc_series", "plant": PLANT,
                                 "control": {"kp": value}})
    assert summary is None and notice is not None
    assert notice["code"] == "N-OPEN-SCHEMA" and notice["params"]["n"] >= 1, notice
    assert notice["items"] and all(item["problem"].startswith("schema.") for item in notice["items"])


def test_bare_number_control_in_version_02_names_the_item():
    """版 0.2 では control の値は組（値・単位・出典）。数値だけなら、その項目の場所を示して断る。"""
    summary, notice = open_text({"schema_version": "0.2", "template": "rlc_series", "plant": PLANT,
                                 "control": {"kp": 1.5}})
    assert summary is None and notice["code"] == "N-OPEN-SCHEMA", notice
    assert [item["path"] for item in notice["items"]] == ["control.kp"], notice


@pytest.mark.xfail(strict=True, reason="軽微（A1 へ）: 版 0.1 の control の数値でない値をエンジンが断ると、"
                   "サーバーは場所を「$」（全体）・「形式が違います」として示し、control.kp を示さない。"
                   "エンジンのエラーは番号 SPEC_LEGACY_CONTROL_NOT_NUMBER と項目の名前（name）を持つ")
def test_legacy_control_notice_names_the_item():
    """版 0.1 の control の数値でない値を断るとき、版 0.2 と同じく、その項目（control.kp）を場所として
    示す。"""
    summary, notice = open_text({"schema_version": "0.1", "template": "rlc_series", "plant": PLANT,
                                 "control": {"kp": "abc"}})
    assert summary is None and notice["code"] == "N-OPEN-SCHEMA", notice
    assert [item["path"] for item in notice["items"]] == ["control.kp"], notice
