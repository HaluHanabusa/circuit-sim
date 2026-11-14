---
id: W1
owner: tooling-engineer
needs_ux: false
---

# W1: Windowsで動くこと

担当: tooling-engineer ／ 依存: H1、E1a（完了）

## 目的

アプリ本体と開発の道具（確認スクリプト、ハーネス）が、Windowsの上でそのまま動くようにする。GitHub上の自動テストで、Windowsでの動作を毎回確かめる。POのPC（Windows）で、環境と本物のヘッドレス起動を確かめる手段を用意する。

背景: POの方針で、動作環境はWindowsも対象（D-026）。いまの道具はLinux / macOS 前提の箇所がある（`scripts/check.sh` はシェルスクリプト、`scripts/cycle.py` のロックは `fcntl`、テストの偽コマンドは `#!/bin/sh`）。こちらから操作できるPC側のシェルはLinuxの仮想環境で、Windowsそのものは操作できない。

## 範囲

1. **確認スクリプト**: `scripts/check.py`（ruff と pytest を実行、OSに依らない）。`scripts/check.sh` はそれを呼ぶだけにする。`scripts/cycle.py` は `check.sh` ではなく、Pythonで `scripts/check.py` を実行する。
2. **ハーネスのWindows対応**: ファイルのロック（Windowsでは別の仕組み）、シグナル（Windowsに無いものに頼らない）、`claude` コマンドの見つけ方（Windowsでは `claude.cmd` などになる）、パスの区切り。ゲートの決まり（`docs/design/harness.md` 2章）はどのOSでも同じに強制する。
3. **文字コード**: ファイルの読み書きとサブプロセスの入出力は UTF-8 を明示する。画面への出力は、UTF-8 でない端末（日本語Windowsの cp932 など）でも落ちない。µ、Ω、→ のように表せない文字は置き換える。対象は `python -m circuit_sim` と `scripts/` のスクリプト。
4. **テスト**: `tests/test_harness.py` を含む全テストがWindowsで通る。偽の確認スクリプトと偽の `claude` は、シェルスクリプトでなくOSに依らない形にする。
5. **自動テスト**: `.github/workflows/ci.yml` にWindowsを加える（Windowsは Python 3.11 と 3.13、Linuxは 3.11〜3.13）。作業ブランチへの push でも走るようにする。`.gitattributes` で改行を LF に固定する。
6. **環境の確認スクリプト** `scripts/doctor.py`: POが自分のPCで実行する。
   - 確かめるもの: OS、Python の版、git、`claude` の有無と版、`scripts/check.py` の結果。
   - `--headless` を付けたときだけ、本物の `claude -p` を最小の指示で1回実行し、ハーネスが前提にしている出力の形（終了コード、`is_error`、`subtype`、`structured_output`、`session_id` の有無）を記録する。コマンドは `scripts/cycle.py` と同じ組み立てを使う（`--agent`、`--output-format json`、`--json-schema`）。サブスクの利用枠を少し使うので、既定では実行せず、実行前に何をするかを表示する。
   - 結果は画面と `.cycle/doctor-result.json` に出す。認証情報、環境変数の中身、応答の本文は書き出さない（形と真偽だけ）。
7. **文書**: README の動かし方にWindowsの例、`docs/design/harness.md` と `CLAUDE.md` のコマンド、`milestone-cycle` スキルの該当行。

## 範囲外

アプリの画面（A0）、インストーラ、WSLでの使い方の案内、H2 の指摘（土台の付け替えなど）。計算の中身。

## この作業単位に限って許すこと

Windowsでの動作は GitHub上の自動テストでしか確かめられない。そのため、担当はブランチ `W1` へのコミットと push をしてよい（`main` には触らない）。自動テストの結果は `gh run list` / `gh run view --log-failed` で読む。

## 受け入れ基準

| # | 基準 | 合格の条件 | 確かめ方 |
|---|---|---|---|
| 1 | 自動テスト | ブランチ `W1` の最新のコミットで、Linux（3.11〜3.13）とWindows（3.11、3.13）のすべてが成功 | GitHub上の実行結果 |
| 2 | Windowsでのハーネスのテスト | ゲートの決まりのテストが、Windowsでスキップされずに実行されて通る。スキップしてよいのは、Windowsに存在しない機能そのものを試すテストだけ。件数と理由を報告する | Windowsの実行ログのテスト件数 |
| 3 | cp932 の端末 | `PYTHONIOENCODING=cp932` で、`python -m circuit_sim examples/rlc_series.json` と、µ を含む注意が出る仕様（接頭語つきの単位）の実行が、例外で落ちない | LinuxとWindowsの両方で実行 |
| 4 | 環境の確認スクリプト | `python scripts/doctor.py` がLinuxとWindowsの自動テスト上で終了コード0で動き、結果のJSONが範囲6の項目を持つ。`--headless` の経路は偽の `claude` で試験され、認証情報や応答の本文が結果に入らないことを確かめるテストがある | 自動テストとテストコード |
| 5 | 後退が無いこと | Linuxで `scripts/check.sh` と `scripts/cycle.py` の動作が変わらない。ハーネスのテストがすべて通り、1か所ずつ壊す確認で生き残りが無い | 実行 |

許容の根拠: 1〜5はいずれも合否が二値で、数値の許容は無い。3で cp932 を選ぶのは、日本語Windowsの既定の端末だから。

## 報告に含めること

- 基準ごとの結果。Windowsの実物（GitHub上）で確かめたものと、模擬で確かめたものを分ける
- Windowsでスキップしたテストの件数と理由
- `--headless` の本物の実行は未確認であること（POのPCで実行して確かめる）
