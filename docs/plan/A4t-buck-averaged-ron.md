---
id: A4t
owner: ux-designer
needs_ux: false
gates: implement, check
---

# A4t: 付録A.4 の平均モデルにオン抵抗を反映する

担当: ux-designer ／ 前提: E1b の修正（平均モデルのレグを Vleg と Rleg の直列にした）／ 次: A0 の修正の取り込み、A1

## 目的

E1b の修正で、平均モデルのレグが「Vleg = duty × Vin」から「Vleg と Rleg（スイッチのオン抵抗 r_on）の直列」になった。v(sw) = duty × Vin − r_on × i(L1)。付録A.4 の図、信号の説明、内部の信号の扱いを、これに合わせる。

## 成果物

- `docs/design/screens-v0.1.md` 付録A.4: 平均モデルの回路図（Rleg を描くか、描かずに説明で示すかを決める）、`v(sw)` の説明（日英）、内部の信号 `v(sw_avg)`・`i(Rleg)` を画面の信号の一覧に出すかどうか

## 受け入れ基準

1. 付録A.4 の平均モデルの図と信号の説明が、E1b の実装（`circuit_sim/templates/buck.py`、`circuit_sim/templates/legs.py` ほか、ブランチ E1b）と食い違わない。
2. 内部の信号の扱い（出すか、出さないか、出すならどの名前と説明か）が決まっている。
3. 変えた文言が日英のキーの表にあり、辞書の生成が拾える形になっている。
4. `python scripts/check.py` が通る。
