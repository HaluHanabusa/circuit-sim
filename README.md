# 回路シミュレータ（仮称 circuit-sim）

設計書から回路と制御系を組み、目標値に対してPIDゲインを最適化するシミュレータ。開発の初期段階。

- 計画: `docs/plan/roadmap.md`
- 設計: `docs/design/architecture.md`、`docs/design/app-design.md`
- 開発の進め方: `CLAUDE.md`
- ライセンス: Apache-2.0（商用利用可）

## いまできること（E0、E1a）

- 仕様JSON（層A）とネットリスト（層B）のスキーマ（`ir.py`）
- R / L / C / 電圧源 / 電流源の時間応答（修正節点解析＋固定刻みの台形則）
- 電源波形: 直流、ステップ（途中の時刻でも可）、正弦波
- コンデンサ電圧とインダクタ電流の初期値
- 解けない回路の検出（浮いた部分、電圧源ループ、電圧源とコンデンサの直結など）
- テンプレート T0: `rc_lowpass`、`rlc_series`
- スイッチとダイオードを含む回路（降圧・昇圧コンバータ、整流回路など）、PWM、制御器を一定周期で挟む口
- 仕様の単位の読み取り、刻みが粗いときの注意、ステップ数の上限

## 動かし方

Python 3.11 以上。Windows と Linux は、GitHub上の自動テストで毎回確かめている。
コマンドはどのOSでも同じ（macOS も同じコマンドを使う。自動テストには入れていない）。

```bash
python -m pip install -e ".[dev]"
python scripts/check.py                            # ruff と pytest（解析解との照合を含む）
python -m circuit_sim examples/rlc_series.json --csv out.csv
```

Windows（PowerShell）で、仮想環境を作るところから始める例:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python scripts\doctor.py                           # 環境の確認（OS、Python、git、claude、確認の結果）
python -m circuit_sim examples\rlc_series.json --csv out.csv
```

- 仕様ファイルは UTF-8 で保存する（メモ帳が付ける先頭の BOM は、あっても読める）。
- 画面に出せない文字は置き換えて表示する。たとえば日本語 Windows で出力をファイルやパイプに送ると、
  文字コードが cp932 になり、`µF` は `uF`、オーム記号は `ohm` と表示される。計算結果は変わらない。
- `python scripts/doctor.py` は、開発の道具が動く環境かどうかを確かめる。結果は画面と
  `.cycle/doctor-result.json` に出る（`docs/design/harness.md` の7章）。

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

`docs/plan/roadmap.md` と `docs/plan/po-board.md` を参照。
