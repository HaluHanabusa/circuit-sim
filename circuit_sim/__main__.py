"""使い方: python -m circuit_sim 仕様.json [--csv 出力.csv]

仕様JSON（層A）を読み、テンプレートを展開して計算し、要約を表示する。
計算できないとき（値の不足、読めない単位、解けない回路、壊れたファイルなど）は、
理由を1行で標準エラーに出し、終了コード 2 を返す。

仕様ファイルは UTF-8 で読む（先頭の BOM は無視する）。画面への出力は、UTF-8 でない端末
（日本語 Windows の cp932 など）でも落ちないよう、表せない文字を置き換えて出す。
"""
from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path

from pydantic import ValidationError

from . import MissingValueError, Spec, expand
from .simulate import StepLimitError

# 端末の文字コードで表せない文字の置き換え。単位の文字は、同じ意味の英数字にする（表に無い文字は ?）。
# µ は U+00B5 と U+03BC、Ω は U+03A9 と U+2126 の2通りがある（units.py が受け付ける書き方と同じ）
_PLAIN_TEXT = {"\u00b5": "u", "\u03bc": "u", "\u03a9": "ohm", "\u2126": "ohm", "\u2192": "->"}
_FALLBACK = "circuit_sim.plain"


def _plain(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeEncodeError):
        raise error
    text = error.object[error.start:error.end]
    return "".join(_PLAIN_TEXT.get(ch, "?") for ch in text), error.end


codecs.register_error(_FALLBACK, _plain)


def _make_output_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors=_FALLBACK)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()
    parser = argparse.ArgumentParser(prog="circuit_sim")
    parser.add_argument("spec", type=Path, help="仕様JSON（層A）")
    parser.add_argument("--csv", type=Path, help="波形をCSVに書き出す")
    args = parser.parse_args(argv)

    try:
        text = args.spec.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"仕様ファイルを読めません: {exc}")
    try:
        spec = Spec.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '全体'}: {err['msg']}" for err in exc.errors()
        )
        return _fail(f"仕様JSONの形が正しくありません。{problems}")
    try:
        ex = expand(spec)
        res = ex.run()
    except MissingValueError as exc:
        return _fail(f"計算できません。{exc}")
    except StepLimitError as exc:
        # 上限はコマンドや仕様JSONからは変えられないので、simulate() の引数を勧める部分は出さない
        return _fail(
            f"計算できません。{exc.without_api_hint}"
            "（仕様JSONの simulation.h と simulation.t_end で指定する）"
        )
    except ValueError as exc:
        # 読めない単位、非正の値、未対応のテンプレート、解けない回路、大きすぎる計算
        return _fail(f"計算できません。{exc}")

    print(f"テンプレート: {spec.template}")
    print(f"計算時間: {ex.t_end:.6g} s / 刻み: {ex.h:.6g} s / ステップ数: {len(res.t) - 1}")
    for name in ex.probes:
        y = res[name]
        print(f"  {name}: 最終値 {y[-1]:.6g} / 最大 {y.max():.6g} / 最小 {y.min():.6g}")
    for note in ex.notes + res.notes:
        print(f"  注意: {note}")
    if args.csv:
        res.to_csv(args.csv, ex.probes)
        print(f"波形を書き出しました: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
