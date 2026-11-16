# 文書の体系

最終更新: 2026-11-16 ／ 責任者: オーケストレーター ／ 作業単位: S1（D-060）

`docs/` にある文書の一覧と、どれが何の「正」か、何を変えたらどれを直すかを書く。
同じことを2か所に書かない。ほかの文書の範囲は、写さずに引く（ファイル名と章、決定番号 D-nnn）。

## 1. 文書の一覧と、何の「正」か

### 1.1 設計（`docs/design/`）

| 文書 | 何の「正」か | 責任者 | 状態 |
|---|---|---|---|
| `requirements.md` | 要求。目的、利用者像、ユースケース（UC-nn）、シナリオ（SC-n）、機能要求（FR-nn）、非機能要求（NFR-nn）、範囲に入れないもの、未決 | ux-designer | S1 で作成 |
| `basic-design.md` | 基本設計。システム構成、機能ブロックとモジュール、データ（層A、層B、結果、ファイル形式）、処理方式、外部インターフェース、エラーと注意、安全、拡張の口、試験の方針、対応表 | オーケストレーター | S1 で作成 |
| `accuracy-and-resolution.md` | 精度と分解能。時間・デューティ・測定・表示の分解能と計算の精度の設計値、根拠、実機の制御器との比較、モデルに入っていないもの | control-engineer | S1 で作成 |
| `glossary.md` | 用語。標準の用語、英語、定義、画面と文書でのいまの表記、識別子。直すべき表記の一覧。用語の決めごとは D-063 | control-engineer | S1 で作成 |
| `architecture.md` | 全体設計の要約（データの流れ、パッケージ構成、方式の要点）。詳細は `basic-design.md` | オーケストレーター | あり |
| `app-design.md` | アプリの骨格と原則。利用者の用事、中心になる流れ、画面の構成、設計の原則、品質の基準 | ux-designer | あり |
| `screens-v0.1.md` | 画面仕様（v0.1）。画面ごとの要素、状態、操作、文言（日英。辞書の元になる表）、受け入れ基準（AC-n-n。初版との対応は付録D）、お知らせの一覧、テンプレートの表示定義。リリースごとに `screens-*.md` を足す | ux-designer | あり。第2版（B 案と、降圧コンバータ1つの範囲。D-071、D-072） |
| `design-system.md` | デザインの基準。色、文字、寸法と密度、基本部品、スコープと回路図の描き方、UX のゲートの見方 | ux-designer | あり。第2版（B 案。D-072） |
| `mockups/direction-b/` | B 案の見本（PO が見て選んだ3枚）。**参考で、正ではない**（正は `design-system.md` と `screens-v0.1.md`） | ux-designer | A0r で追加 |
| `mockups/second-edition-trial/` | 第2版で足した設計目標の群と評価の試し描き（見本を元に ux-designer が描いた2枚。寸法が収まるかの確認用）。**参考で、正ではない。PO が選んだものではない** | ux-designer | A0r で追加 |
| `harness.md` | 開発ハーネス。ゲートの決まり、`scripts/cycle.py` の使い方、OS の違い、手元の確認 | tooling-engineer | あり |
| `dependencies.md` | 依存とライセンスの一覧 | ui-engineer | 統合前（A0） |
| `research-ui-v2.md` | 画面の見た目の調査と方向の案 | ux-designer | 統合前（A0r） |
| `README.md` | この文書。文書の体系 | オーケストレーター | S1 で作成 |

責任者は、文書の先頭か作業単位の仕様に書かれたもの。`architecture.md`、`harness.md`、`dependencies.md` には明記が無いので、`CLAUDE.md` の役割の表と、作った作業単位の担当から当てはめた。

### 1.2 決定、計画、検証

| 文書 | 何の「正」か | 責任者 |
|---|---|---|
| `docs/decisions.md` | 決定の記録（D-nnn）。何を、なぜ、だれが決めたか。覆すときは行を消さず、状態を「取り消し」にして新しい行を足す | オーケストレーター |
| `docs/plan/roadmap.md` | 計画。リリースごとの「利用者ができること」、作業単位の一覧と状態 | オーケストレーター |
| `docs/plan/po-board.md` | PO 向けの現在地、お願い、判断が要ること | オーケストレーター |
| `docs/plan/<ID>-<名前>.md` | 作業単位の仕様。範囲と受け入れ基準。先頭の見出しが、必要なゲートを決める | オーケストレーター（仕様化） |
| `docs/verification/<ID>-<担当>.md` | 検証の記録。基準、実測値、測り方、合否。1回の検証ごとに新しいファイルを足し、既存の記録は書き換えない | verifier、ux-designer |

### 1.3 リポジトリの直下と、その他

| 文書 | 何の「正」か |
|---|---|
| `CLAUDE.md` | 体制、進め方、守る決まり（決まり1〜11）、完了の定義、PO の判断が要ること |
| `README.md`（直下） | 入口。いまできること、動かし方 |
| `CHANGELOG.md` | 利用者から見た変更 |
| `.claude/agents/`、`.claude/skills/` | 役割の定義と、手順の型（`milestone-cycle`、`model-validation`） |
| `docs/user/` | 利用者向けの文書。予定（R1。まだ無い） |

### 1.4 事実の「正」

- **動きの事実はコード。** 設計書とコードが食い違ったら、コードを確かめる。コードが意図どおりなら文書を直し、意図と違うなら不具合として扱う。
- **なぜそうしたかは `docs/decisions.md`。** 決定を変えるときは、先に決定記録に追記してから、設計書を直す。
- **文言の事実は画面仕様の表。** 画面の辞書は表から作る（`frontend/tools/strings_from_spec.py`。統合前（A0））。表に無い文字列を画面に出さない。
- **数値の根拠は `accuracy-and-resolution.md` と `docs/verification/`。** 実測値は検証の記録に、設計値とその理由は精度と分解能の文書に置く。

## 2. 何を変えたら、どの文書を直すか

数値モデルや画面を変える作業単位は、該当する設計書の更新を完了の条件に含める（D-060）。仕様（`docs/plan/<ID>-*.md`）を書くときに、この表から直す文書を拾って、範囲と受け入れ基準に入れる（例: E1g の基準14）。

| 変えたもの | 直す文書 |
|---|---|
| 数値モデル（素子、積分の方式、不連続点の扱い、ダイオード、PWM、制御器のタイミング、量子化、デッドタイム） | `accuracy-and-resolution.md`（モデルに入っていないものは4章）、`basic-design.md` 6章（制御は7章）・15.1。検証の記録を足す。範囲に入れないものが変わるなら `requirements.md` の X-7。要点が変わるなら `architecture.md` |
| 分解能、許容差、既定の時間刻み、上限の値 | `accuracy-and-resolution.md`、`basic-design.md` 6.2・6.6、`docs/decisions.md`。要求の水準が変わるなら `requirements.md` の NFR |
| 層A、層B、結果の形。仕様 JSON の版 | `basic-design.md` 5章、`screens-*.md`（開く処理、付録A）、`requirements.md`（FR-06、FR-07）、`CHANGELOG.md`、`docs/decisions.md` |
| CSV、設定ファイルの形 | `basic-design.md` 5.5、`screens-*.md` 8章、直下の `README.md` |
| テンプレートを足した。項目や信号を変えた | `screens-*.md` 付録A（表示定義と文言）、`basic-design.md` 3章・5章・14章（手順は 12.1）、`accuracy-and-resolution.md`、`glossary.md`、`requirements.md` |
| 制御ブロック、ゲインの設計と最適化 | `basic-design.md` 7章、`accuracy-and-resolution.md`、`glossary.md` |
| 画面の文言 | `screens-*.md` の文言の表（辞書を作り直す）、`glossary.md` |
| 画面の配置、操作、状態 | `screens-*.md`、`basic-design.md` 8章。原則に関わるなら `app-design.md` |
| 色、文字、寸法、部品の見た目 | `design-system.md` |
| エラーや注意を足した。番号を変えた | `screens-*.md` 付録B、`basic-design.md` 10章、`glossary.md` |
| HTTP API、コマンドライン、Python の口 | `basic-design.md` 9章、`screens-*.md` 12章、直下の `README.md` |
| ローカルサーバーの安全の仕組み、設定の場所 | `basic-design.md` 11章・5.5、`docs/decisions.md`、`requirements.md` の NFR-08・NFR-09 |
| 構成、プロセス、配布の形、動作環境 | `basic-design.md` 2章、直下の `README.md`、`requirements.md` の NFR-06 |
| モジュールの追加、分割、依存の向き | `basic-design.md` 4章・14章、`architecture.md` 2章 |
| 依存ライブラリ | `dependencies.md`。LGPL などは先に `docs/decisions.md`（決まり7） |
| 用語 | `glossary.md`（正）。次に、画面仕様の文言、`basic-design.md` 1.5、`requirements.md` 0.3、`accuracy-and-resolution.md` 0.2。決めごとを変えるなら、先に `docs/decisions.md`（D-063 を引いて新しい行を足す） |
| 要求、範囲、利用者像 | `requirements.md`、`docs/plan/roadmap.md`。PO の判断が要るものは `docs/plan/po-board.md` |
| 作業単位を統合した | `docs/plan/roadmap.md`、`docs/plan/po-board.md`、`CHANGELOG.md`。`basic-design.md` と `requirements.md` の状態の表記（統合前・進行中 → 実装済み） |
| 開発の手順、ゲート、確認のスクリプト | `harness.md`、`CLAUDE.md`、`.claude/skills/` |
| 設計上の判断をした。覆した | `docs/decisions.md`（必ず）。その決定を引いている文書 |
| 文書とコード、文書どうしの食い違いを見つけた。直した | `basic-design.md` 15.3（コードとの食い違い）、`requirements.md` 8.3（文書どうし。用語は G-8・G-9）。直したら行を消す |

## 3. 読む順番（はじめて保守する人向け）

1. 直下の `README.md`: 動かしてみる（`python -m circuit_sim examples/rlc_series.json`）。
2. `CLAUDE.md`: 体制と、守る決まり。
3. `requirements.md` の1〜4章: だれの、どんな用事のための道具か。
4. `basic-design.md`: 全体を通して読む。分からない言葉は `glossary.md` で引く。
5. 担当する所の詳しい文書。
   - エンジン、制御: `accuracy-and-resolution.md`、`.claude/skills/model-validation/`、該当する `docs/verification/`。
   - 画面: `app-design.md` → `screens-v0.1.md` → `design-system.md`。
   - 開発の道具: `harness.md`。
6. `docs/decisions.md`: なぜそうなっているか。設計書に出てくる D-nnn を引く。通して読むと、経緯が分かる。
7. `docs/plan/roadmap.md` と `docs/plan/po-board.md`: いまの位置と、次にやること。手を入れる作業単位の仕様（`docs/plan/<ID>-*.md`）。

## 4. 書き方の決まり

- 文書は日本語、識別子は英語。利用者に見せる言葉は設計者の用語を使う（`CLAUDE.md` の「言葉」）。用語は `glossary.md` に合わせる。
- 事実でないことを書かない。実装の状態は、main にあるものを「実装済み」、ブランチにあるものを「統合前（ブランチ名）」、仕様だけのものを「予定（作業単位）」、確かめられなかったことを「未確認」と書き分ける（`requirements.md` は、統合前を「進行中」と書く）。
- 根拠に、決定番号（D-nnn）とファイルの場所を添える。
- 図は、コードブロックの中の文字で描く。
- 文書の先頭に、最終更新の日付と責任者を書く。大きく変えたら、末尾の「変更の記録」に1行足す。
- 番号の付いたもの（D-nnn、UC-nn、FR-nn、NFR-nn、AC-n-n、お知らせの番号）は、やめても番号を使い回さない。
- 直す前の画面の表記を本文で引くときは、「（いまの画面の表記: …）」と添える（D-063）。

## 変更の記録

- 2026-11-16（オーケストレーター、S1）: 初版。
- 2026-11-16（ux-designer、S1 の読み合わせ）: 2章の始めを D-060 の文に合わせ、数値モデル・用語の行を直し、食い違いの行を足した。4章に、いまの画面の表記の引き方を足した。
- 2026-11-16（ux-designer、A0r 範囲3）: 1.1 の `screens-v0.1.md` と `design-system.md` の行を第2版に合わせ、見本のフォルダの行を足した。
