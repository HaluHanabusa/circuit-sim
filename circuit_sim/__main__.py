"""使い方: python -m circuit_sim 仕様.json [--csv 出力.csv]

仕様JSON（層A）を読み、テンプレートを展開して計算し、要約を表示する。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import MissingValueError, Spec, expand, simulate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="circuit_sim")
    parser.add_argument("spec", type=Path, help="仕様JSON（層A）")
    parser.add_argument("--csv", type=Path, help="波形をCSVに書き出す")
    args = parser.parse_args(argv)

    spec = Spec.model_validate_json(args.spec.read_text(encoding="utf-8"))
    try:
        ex = expand(spec)
    except MissingValueError as exc:
        print(f"計算できません。{exc}", file=sys.stderr)
        return 2

    res = simulate(ex.netlist, ex.t_end, ex.h)
    print(f"テンプレート: {spec.template}")
    print(f"計算時間: {ex.t_end:.6g} s / 刻み: {ex.h:.6g} s / ステップ数: {len(res.t) - 1}")
    for name in ex.probes:
        y = res[name]
        print(f"  {name}: 最終値 {y[-1]:.6g} / 最大 {y.max():.6g} / 最小 {y.min():.6g}")
    for note in res.notes:
        print(f"  注意: {note}")
    if args.csv:
        res.to_csv(args.csv, ex.probes)
        print(f"波形を書き出しました: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
