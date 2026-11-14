# 回路シミュレータ（仮称 circuit-sim）

設計書から回路と制御系を組み、目標値に対してPIDゲインを最適化するシミュレータ。開発の初期段階。

- 計画: `docs/plan/roadmap.md`
- 設計: `docs/design/architecture.md`、`docs/design/app-design.md`
- 開発の進め方: `CLAUDE.md`
- ライセンス: Apache-2.0（商用利用可）

## いまできること（E0）

- 仕様JSON（層A）とネットリスト（層B）のスキーマ（`ir.py`）
- R / L / C / 電圧源 / 電流源の時間応答（修正節点解析＋固定刻みの台形則）
- 電源波形: 直流、ステップ（途中の時刻でも可）、正弦波
- コンデンサ電圧とインダクタ電流の初期値
- 解けない回路の検出（浮いた部分、電圧源ループ、電圧源とコンデンサの直結など）
- テンプレート T0: `rc_lowpass`、`rlc_series`

## 動かし方

```bash
pip install -e ".[dev]"
pytest                                             # 解析解との照合
python -m circuit_sim examples/rlc_series.json --csv out.csv
```

Pythonから:

```python
from circuit_sim import Spec, expand, simulate

spec = Spec.model_validate_json(open("examples/rlc_series.json", encoding="utf-8").read())
ex = expand(spec)                       # 層A → 層B
res = simulate(ex.netlist, ex.t_end, ex.h)
res["v(out)"], res["i(L1)"], res.t      # 波形（NumPy配列）
```

## 検証結果（受け入れ基準: 誤差1%以内）

| 回路 | 解析解との最大誤差 |
|---|---|
| RCステップ応答（159ステップ） | 入力電圧の0.003% |
| RLCステップ応答、減衰係数0.05（3183ステップ） | 入力電圧の0.06% |

刻みを半分にすると誤差が1/4になる（2次精度）ことも確認している。

## 構成

| ファイル | 役割 |
|---|---|
| `circuit_sim/ir.py` | 層A（仕様）と層B（ネットリスト）のスキーマと検証 |
| `circuit_sim/mna.py` | ネットリスト → 回路方程式 `E x' + G x = B u` |
| `circuit_sim/kernel.py` | 時間ステップの核。配列と数値だけを扱う（Numba / C への載せ替え対象） |
| `circuit_sim/simulate.py` | 入口。電源を配列にして核を呼び、結果をまとめる |
| `circuit_sim/templates/` | 層A → 層B の展開（T0） |
| `tests/test_t0.py` | 解析解との照合、検証、入出力のテスト |

## 次

E1a（スイッチング回路エンジン）。仕様は `docs/plan/E1a-switching.md`。
