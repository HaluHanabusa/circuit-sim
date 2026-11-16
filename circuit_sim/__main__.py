"""使い方: python -m circuit_sim 仕様.json [--csv 出力.csv]

仕様JSON（層A）を読み、テンプレートを展開して計算し、要約を表示する。
計算できないとき（値の不足、読めない単位、解けない回路、壊れたファイルなど）は、
理由を1行で標準エラーに出し、終了コード 2 を返す。
文面は、エンジンの注意とエラーと同じ辞書（messages.MESSAGES）から作る。注意には番号を添える
（「[番号] 注意: 文」）。--csv の見出しには単位を付け（画面と同じ形。D-042）、注意をデータの後ろに足す。

仕様ファイルは UTF-8 で読む（先頭の BOM は無視する）。画面への出力は、UTF-8 でない端末
（日本語 Windows の cp932 など）でも落ちないよう、表せない文字を置き換えて出す。
"""
from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path

from pydantic import ValidationError

from . import Spec, expand
from .messages import Coded, Message, Value
from .simulate import StepLimitError, render_note_line

LANG = "ja"     # 表示の言語（辞書の ja / en）

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


def _say(code: str, **params) -> str:
    return Message(code, **params).text(LANG)


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()
    parser = argparse.ArgumentParser(prog="circuit_sim")
    parser.add_argument("spec", type=Path, help="仕様JSON（層A）")
    parser.add_argument("--csv", type=Path, help="波形をCSVに書き出す（見出しに単位、データの後ろに注意）")
    args = parser.parse_args(argv)

    try:
        text = args.spec.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(_say("CLI_READ_FAILED", detail=str(exc)))
    try:
        spec = Spec.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or _say('CLI_WHOLE')}: {err['msg']}"
            for err in exc.errors()
        )
        return _fail(_say("CLI_SCHEMA_INVALID", problems=problems))
    try:
        ex = expand(spec)
        res = ex.run()
    except StepLimitError as exc:
        # 上限はコマンドや仕様JSONからは変えられないので、simulate() の引数を勧める部分は出さない
        return _fail(_say("CLI_CANNOT_RUN", reason=exc.reason.text(LANG) + _say("CLI_STEP_LIMIT_HINT")))
    except ValueError as exc:
        # 値の不足、読めない単位、非正の値、未対応のテンプレート、解けない回路、発散
        reason = exc.text(LANG) if isinstance(exc, Coded) else str(exc)
        return _fail(_say("CLI_CANNOT_RUN", reason=reason))

    print(_say("CLI_TEMPLATE", template=spec.template))
    print(_say("CLI_SUMMARY", t_end=Value(ex.t_end, "s"), h=Value(ex.h, "s"), steps=len(res.t) - 1))
    for name in ex.probes:
        y = res[name]
        print(_say("CLI_SIGNAL", name=name, last=Value(y[-1]), max=Value(y.max()), min=Value(y.min())))
    for note in res.notes:              # 展開の注意（テンプレート）と計算の注意。番号つき
        print(render_note_line(note, "CLI_NOTE", LANG))
    if args.csv:
        res.to_csv(args.csv, ex.probes, units=True, notes=True)
        print(_say("CLI_CSV_WRITTEN", path=str(args.csv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
