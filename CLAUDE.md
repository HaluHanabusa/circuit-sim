# 回路シミュレータ（仮称 circuit-sim）

設計書を読み込んで回路と制御系を組み、設計目標（オーバーシュート、整定時間、位相余裕など）を満たすPIDゲインを検証つきで返すローカルアプリ。
差別化は「設計目標 → ゲイン → 検証まで閉じること」と「使われる道具としての完成度」。

## 体制

- **PO（人）**: 方向づけと、下の「POの判断が必要なこと」だけを決める。日々の判断には関与しない。
- **オーケストレーター（メインのセッション）**: 計画、仕様化、エージェントへの委任、ゲート判定、統合、PO報告。原則として自分では実装しない。
- **エージェント**（定義は `.claude/agents/`）:

| 名前 | 役割 | 主に触る場所 |
|---|---|---|
| solver-engineer | 数値計算の核（素子、行列、時間ステップ、速度） | `circuit_sim/` のエンジン部分 |
| control-engineer | 制御ブロック、テンプレート、ゲイン最適化 | `circuit_sim/control/` `templates/` `tuning/` |
| ingest-engineer | 設計書の取り込みとAI連携 | `circuit_sim/ingest/` `ai/` |
| ui-engineer | アプリ（ローカルサーバーと画面）の実装 | `circuit_sim/app/` `frontend/` |
| ux-designer | 画面と操作の設計、UIの品質判定 | `docs/design/` |
| verifier | 独立検証（実装者と別に受け入れ基準を確かめる） | `tests/acceptance/` `docs/verification/` |
| code-reviewer | 差分のレビュー（読み取り専用） | なし |
| release-manager | 配布、利用者向け文書、ライセンス確認 | `README.md` `CHANGELOG.md` `docs/user/` |
| tooling-engineer | 開発の道具（確認スクリプト、ハーネス、自動テスト、OSの違いへの対応） | `scripts/` `.github/` |

## 進め方

1. 計画は `docs/plan/roadmap.md`。現在地とPO向けの要約は `docs/plan/po-board.md`。
2. 作業単位は `milestone-cycle` スキルの手順で進める（仕様 → 実装 → レビュー → 独立検証 → UX確認 → 統合 → 報告）。
3. 数値モデルを足す・変えるときは `model-validation` スキルに従って検証を付ける。
4. ゲートの記録と統合は `scripts/cycle.py` を通す（`docs/design/harness.md`）。条件を満たさない統合はスクリプトが拒む。手で `git merge` しない。
5. 設計は `docs/design/`、決定は `docs/decisions.md` に追記する。

## コマンド

コマンドは Windows、macOS、Linux で同じ（Linux / macOS では `scripts/check.sh`、`scripts/cycle.py …` の形でもよい）。

```bash
python -m pip install -e ".[dev]"
python scripts/check.py                           # ruff + pytest。統合前に必ず通す
python -m circuit_sim examples/rlc_series.json    # 仕様JSONから計算
python scripts/cycle.py status                    # 作業単位ごとのゲートの状況
python scripts/doctor.py                          # 環境の確認（OS、Python、git、claude、確認の結果）
python scripts/local_ci.py --ref origin/<ブランチ>  # 指定したコミットを取り出して確認し、.cycle/ci/ に記録（Windowsの確認もこれ）
python scripts/cycle.py record-ci <ID> --result <ファイル>  # Windowsでの確認の結果をゲートに取り込む
```

## 守る決まり

1. AIが関わるのは「設計書からの抽出」と「結果の説明」だけ。計算の経路は決定的で、同じ入力なら同じ結果になる。
2. 時間ステップ核（`kernel.py`）の最内ループは配列と数値だけを扱う。辞書やオブジェクトを持ち込まない。
3. 仕様は2層。AIが出力するのは層A（`Spec`）だけ。値は `null` を許し、出典（`source`）を持つ。層B（`Netlist`）はコードが展開する。
4. 不足している値を推測で埋めない。目標未達を達成と表示しない。
5. 数値モデルは検証（解析解・収束次数・モデル間の一致のいずれか）なしに統合しない。
6. モーターの負荷は差し替え式（トルクを出し、角度と速度を受け取る口）。モデルの種類（詳細度）は切り替え式（スイッチング / 平均 / 理想）。
7. 依存ライブラリは商用利用できるライセンスのみ（MIT、BSD、Apache-2.0 など）。GPL / AGPL は使わない。LGPLは事前に決定記録を書く。
8. 画面の文言は直書きしない。日本語と英語を切り替えられる形で持つ。
9. 利用者の設計書とデータは端末の外に出さない。例外はAIへの抽出依頼だけで、送る内容を事前に画面で示す。
10. 速度は測ってから手を入れる。
11. 試験から本物の `claude` を起動しない。試験の中で PATH を組むときは `CIRCUIT_SIM_SAFE_PATH` を使う（`tests/conftest.py` が PATH から claude を外し、見張り役を置く）。エージェントが手元で試験を流すときも、claude の無い PATH で流す。

## ブランチと完了の定義

- `main` は常に `python scripts/check.py`（`scripts/check.sh` も同じもの）が通る状態にする。作業はブランチで行い、ゲートを通ってから統合する。
- 完了 = 仕様の受け入れ基準を満たす ＋ `python scripts/check.py` が通る ＋ code-reviewer の重大な指摘がゼロ ＋ verifier が合格 ＋（画面を含む場合）ux-designer が合格 ＋ `roadmap.md` と `po-board.md` を更新。
- 実装した本人（同じエージェントの同じ実行）は、その成果の検証役を兼ねない。

## POの判断が必要なこと

- リポジトリの公開とリリースの告知
- 製品名、ライセンスの変更、有料機能の線引き
- 費用が発生する変更、外部への送信が増える変更

それ以外はオーケストレーターが決め、`docs/decisions.md` に「既定で進めた判断」として記録する。POはいつでも覆せる。

## 言葉

- 文書、コメント、コミットメッセージは日本語。識別子は英語。
- 利用者に見せる言葉は設計者の用語（オーバーシュート、整定時間、位相余裕）を使い、ソフトウェア都合の言葉を出さない。言い換えて平易にせず、現場で標準の専門用語を使う。用語は `docs/design/glossary.md` に従う。
