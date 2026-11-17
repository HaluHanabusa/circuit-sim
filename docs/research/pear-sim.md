# PEAR-sim と Pulsim（緩いライセンスの区分線形のパワエレ向けエンジン）の調査

調べた日: 2026-11-17 ／ 調べた人: 調査担当（general-purpose） ／ きっかけ: 差別化のスレッドの持ち込みメモの事実1
**コードは入れず、動かしていない**（PO の指示 (b)）。公開のファイル（raw.githubusercontent.com）、PyPI と crates.io の API、`git ls-remote` とコミットの情報だけを読んだ。PEAR-sim は main `ff790cb`（2026-11-17）、Pulsim は main `2d414ed`（2026-11-12）。

**前回の調査の誤り**: 2026-11-16 の調査（`devices-and-solver.md`、`landscape.md`）は「緩いライセンスの区分線形のパワエレ向けのエンジンは見つからなかった」と書いたが、Pulsim は PyPI に 2025-12 からあった（見落とし）。PEAR-sim は 2026-10-17 の公開。以後、「見つからなかった」と書くときは日付と検索の範囲を添え、PyPI のキーワード検索と MCP Registry を範囲に入れる。

## 1. 結論

- **PEAR-sim**: 本体と同梱物は **○**（Apache-2.0。NOTICE の同梱物は MIT と Apache-2.0 のデータだけ）。必須の依存は numpy、scipy、PyYAML、jsonschema（推移的に attrs、referencing、rpds-py ほか）。NumPy・SciPy を除けば ○ で、**依存込みでは NumPy・SciPy の判定に従う**（いまの circuit-sim と同じ位置。PEAR-sim が新しく持ち込む問題は無い）。条件: rpds-py ≥ 0.28（0.27 以前は MPL-2.0 の crate を同梱）。任意の Rust の核は PyPI のホイールに入っておらず、組むなら △。ngspice と motulator は試験だけで、別のプロセスとして呼ぶ（結合しない）。名前とロゴは許諾の外（Apache-2.0 §6 と同じ）。ファイルの見出しに出所の印（GUID）がある。
- **Pulsim**: 本体は MIT だが、配布物に **Eigen（MPL-2.0。LGPL の部分も含みうる）を組み込む**ので **×**。任意の依存にも LGPL（CairoSVG）。任意の依存の anthropic は、入れると外部へ送るのが既定と読める。
- ほかに同じ種類のもの（緩いライセンス、区分線形、パワエレ向け）は見つからなかった（WebSearch 7 回、PyPI と crates.io の API。網羅ではない）。PESLite（AGPL-3.0-only、2026-09、「LLM の推論のための決定的なシミュレーションの裏方」）は狙いが同じだが ×。

## 2. PEAR-sim の中身（ファイルで確かめたもの）

- 回路: 降圧・昇圧・反転昇降圧（CCM、BCM、DCM）、DAB、LLC、整流（半波〜12 パルス、サイリスタ）、三相インバータ（SPWM、SVPWM、PLL）、NPC、電動機（PMSM、SynRM、DC。experimental）、結合インダクタ、飽和インダクタ、ESR・DCR。スイッチとダイオードは理想か区分線形。同期整流の降圧の例がある。
- 解法: 事象駆動の区分線形・時不変の状態空間（修正節点解析、ダイオードの有効集合）。ゲートは予定の事象として正確な時刻に、自然転流は根を探して時刻を決める。**事象の間は SciPy の `solve_ivp`（既定 RK45）の可変刻み**（固定刻みでも台形則でもない）。解けない回路は名前つきのエラー。
- 制御: 周期の頭で標本化するサンプル値の PI・PWM（時間領域では計算遅れ 0）。信号ブロックの層（gain、sum、saturate、unit_delay、Park 変換、量子化など）。制御の周期は1つ。D 項の部品は見当たらない。
- 安定余裕: 1つの脚・CCM の状態平均化で、**利用者が宣言した補償器**のループゲインを、ZOH と計算遅れ込みで求め、最悪の交差を採り、宣言した PM・GM の最小値を合否の門にする。**目標からゲインを求める機能は無い**。
- 判定: `expectations` が指標ごとに pass / fail / not_applicable / not_comparable / not_run を返す。target 型は符号つきの差、min / max 型は測った値だけ。オーバーシュートと整定時間の判定は見当たらない。設計目標の層（`DesignSpec`）はトポロジーを選ぶ水準で、足りない値は既定値でなく「問い」にする。
- 検証: 解析解（RC、RL、RLC、降圧・昇圧の比、DCM の周期の解、SPWM のデッドタイムの閉じた解）。ngspice との照合は線形の部分だけ（4件）。スイッチングの挙動を別のシミュレータと照合したものは無い。試験は約 1,200（127 ファイル）。実行ごとに全ファイルの SHA-256 を記録（可変刻みなので OS 間はバイト一致でなく許容差で比べる、と README）。
- MCP サーバー（任意、`pear-sim-mcp`）。Windows はそのまま動く方針で、CI に Windows・macOS・Linux（実際の結果は未確認）。
- 成熟度: 公開から1か月（非公開の開発は 7 月から）。人は1名で、PR のコミットに Claude の共著。v0.1.0（11-09）、v0.1.1（11-13）、PyPI は「3 - Alpha」、README は「設計の承認に使うな」。機能の登録 57 件のうち supported 15、experimental 35、unsupported 7。0.x の間はモデルの形式が壊れうる。Python 約 59,000 行、試験約 32,000 行。星 0。10 日で 35 の PR。

## 3. circuit-sim にとっての意味

| 役割 | 判断 |
|---|---|
| (c) 照合の相手 | **いま最も合う**。独立の実装で方式も違う（可変刻みで事象の位置を決める ↔ 固定刻みの台形則と抵抗のスイッチ）。別の環境・別のプロセスで、試験のときだけ動かせば配布物に入らない。スイッチングの照合の相手として ngspice より適する見込み（推測） |
| (d) 競合 | **はっきり当たる**。規律がほぼ同じ（決定的、推測せずに拒む、足りない値は問いにする、証拠を残す、緩いライセンス、ローカル、Windows、AI 連携）。最初の例の依頼も同期整流の降圧 |
| (b) コードを表示つきで取り込む | ライセンス上は可。ただし内部の型に依存するので、文書の式から書き直すほうが小さい見込み。核は SciPy のオブジェクトを使い、決まり2 と合わない |
| (a) 依存として入れる土台 | ライセンス上は可、**いまは不可**（公開1か月、作者1名、形式が壊れうる、6 割が experimental）。凍結した拡張（損失、熱、多くのトポロジー、定常解析）を再開するときの第一候補 |

v0.1 との重なり: 同期整流の降圧のスイッチングの時間領域、位相余裕の判定、目標ごとの判定の形は、PEAR-sim にもある。**PEAR-sim に無いもの**: 位相余裕の目標から初期ゲインを求めること、オーバーシュート・整定時間などの時間領域の制御の目標と判定、離散 PID（D 項）、画面、日本語と設計者の用語、設計書の取り込み、固定刻みの決まった刻み列。**circuit-sim に無いもの**: 多くのトポロジー、結合と飽和、デッドタイム、損失と熱、定常解析、フロケ、掃引と batch、感度、MCP、ngspice・motulator との照合、能力の登録、3 OS の CI、約 1,200 の試験。

**独自と言えなくなったもの**: `landscape.md` 4.1 の 2〜4（規律、AI の扱い、ローカルで緩いライセンス）。**残る独自の部分**: 4.1 の 1（制御の設計目標 → ゲインの合成 → スイッチングモデルで確認 → 目標ごとの未達の量を一続きに）と、画面・日本語・設計書・教育の方向。PEAR-sim は設計を選ぶ層へ進んでいるので、この窓は狭い（推測）。

## 4. 動かさないと分からないこと

同じ同期整流の降圧での数値の一致と許容差、速さ、決定性（同じ環境でのビット一致）、Python の API を部品として呼べるか、CI が通っているか、ホイールの中の同梱物（mcp の依存、pydantic-core）、PEAR Power の法人性と商用の意図、Pulsim が Eigen の LGPL の部分を使っているか。

## 5. 出典

- PEAR-sim（`ff790cb`）: https://github.com/PEAR-power/PEAR-sim の LICENSE、NOTICE、README.md、pyproject.toml、uv.lock、CHANGELOG.md、AGENTS.md、rust/pear-sim-core/Cargo.toml・Cargo.lock、docs/adr/0001〜0003、docs/design-spec.md、docs/stability-analysis.md、src/pear_sim/core/solver.py、analysis/small_signal.py、spec/controls.py、evidence/expectations.py、oracle/ngspice.py、validation/status.json、.github/workflows/ci.yml。PyPI: https://pypi.org/pypi/pear-sim/json
- rpds-py の版ごとの Cargo.lock: https://raw.githubusercontent.com/crate-py/rpds/v0.27.1/Cargo.lock ほか
- Pulsim（`2d414ed`）: https://github.com/lgili/Pulsim の LICENSE、README.md、pyproject.toml、CMakeLists.txt
- Eigen: https://gitlab.com/libeigen/eigen/-/raw/3.4.0/COPYING.README 、https://eigen.tuxfamily.org/
- PESLite: https://pypi.org/project/peslite/
