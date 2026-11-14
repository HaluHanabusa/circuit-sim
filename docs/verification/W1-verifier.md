# W1（Windowsで動くこと）独立検証の記録

- 検証した日: 2026-11-15 ／ 担当: verifier（実装は tooling-engineer。作業は見ていない）
- 対象: ブランチ `W1`。実装の先頭 `7e0a647`、受け入れ試験を足したコミット `f7f0a12`
- 仕様: `docs/plan/W1-windows.md`（受け入れ基準1〜5）
- 手元: Linux、Python 3.13。Windows は GitHub 上の自動テスト（実物）でだけ確かめた
- 本物の `claude` は一度も起動していない（偽の `claude` を使った）

## 判定: 不合格

基準1〜4は満たす。**基準5（後退が無いこと）を満たさない**。理由は2つ。

1. Linux で `scripts/cycle.py run` の動作が main から変わっている（問題1。打ち切りが効かなくなる場合がある）。
2. 「1か所ずつ壊す確認で生き残りが無い」が成り立っていない（下の「壊す確認」）。

## 基準ごとの結果

| # | 基準 | 実測値 | 測り方 | 実物 / 模擬 | 合否 |
|---|---|---|---|---|---|
| 1 | 自動テスト | `7e0a647` の実行 43620492261: 5ジョブ成功。Linux 3.11 / 3.12 / 3.13 は各 744 passed、2 skipped、9 xfailed。Windows 3.11 / 3.13 は各 743 passed、3 skipped、9 xfailed（集めた数はどちらも 755）。`f7f0a12` の実行 43625661000: 5ジョブ成功。Linux 784 / 2 / 10、Windows 784 / 3 / 9（どちらも 796） | `gh run view --log` でジョブごとのログを読んだ | 実物 | 合格 |
| 2 | Windowsでのハーネスの試験 | `tests/test_harness.py` の 172 件のうち、Windows でスキップは3件、実行は 169 件。3件は `test_merge_is_undone_when_interrupted_during_check_on_main` の SIGINT、SIGTERM、SIGHUP | Windows のログの `-rs` の一覧。加えて、スキップの印を数える受け入れ試験を足し、Windows で通した | 実物 | 合格 |
| 3 | cp932 の端末 | Windows と Linux のログで、2つの実行が終了コード 0。出力は `R: 1 kohm … 1000 ohm`、`C: 10 uF … 1e-05 F`。手元では 9 通りの文字コード × 2 仕様、エラーの経路 15 通り × 4 文字コードで、例外なし | 自動テストのログと、手元での実行 | 実物（Windows）と手元（Linux） | 合格 |
| 4 | 環境の確認スクリプト | Linux と Windows のログで終了コード 0、`"ok": true`。結果に os、python、encoding、git、claude、check、headless がある。偽の `claude` での確認は下の「基準4」 | 自動テストのログ、手元での実行、足した受け入れ試験（Windows でも通過） | 実物（`--headless` なし）と模擬（`--headless`） | 合格 |
| 5 | 後退が無いこと | 同じ手順の比較は 33 場面中 29 が一致、4 が意図した違い。ただし、打ち切りの後に戻らない場合がある（問題1）。壊す確認は 78 通り中、生き残り 10、止まらなくなるもの 1 | 下の「基準5」 | 手元（Linux）。一部は実物（Windows） | **不合格** |

許容の根拠: 1〜5 は二値。時間の許容は、足した試験の「打ち切りの後に戻るまでの猶予」だけ（Linux 3 秒、Windows 10 秒。Linux は止める操作が一瞬、Windows は taskkill の起動を含む。実装者の試験は 1 秒の打ち切りに 15 秒まで許している）。

## 見つけた問題

### 問題1（重大、基準5）: 打ち切られた claude が子プロセスを残すと、Linux で `run` が戻らない

- 再現（Linux）: 一時リポジトリで `start` した後、次の偽の `claude` を PATH の先頭に置いて `python scripts/cycle.py run U1 review --timeout 2` を実行する。

  ```sh
  #!/bin/sh
  sleep 12 &
  sleep 60
  ```

- 実測: main 版は 2.1 秒で `review: error（2 秒で終わらなかったので打ち切った）` を記録して戻る。W1 版は 40 秒たっても戻らず（こちらで止めた）、記録も残らない。残った子プロセスが終わるまで待ち続ける。
- 原因: `run_captured` が、打ち切りの後に `proc.communicate()` で出力の終わりを待つ。Linux では、止めるのは起動したプロセスだけで、その子は出力の口を持ったまま残る。この1行を外した版では、すぐに戻ることを確かめた。main の `subprocess.run` は、Linux ではこの待ちをしない。
- 影響: `--timeout`（既定 3600 秒）は、人が見ていない実行の最後の歯止め。本物の CLI は道具の実行などで子プロセスを作る。合格が誤って記録されることは無い（何も記録されない）が、実行が止まり続ける。`doctor.py --headless` も同じ関数を使う。
- Windows: `taskkill /T` が子も止めるので戻る（足した試験が Windows の実物で通過）。
- 試験: `test_headless_timeout_returns_even_when_the_cli_left_a_child_holding_its_output`（Linux では xfail）。

### 問題2（重大、基準5）: 壊しても落ちない分岐が残っている

下の「壊す確認」のとおり。決まりに関わる2つ（C05、C23）は、足した受け入れ試験で落ちるようにした。実装者が報告した「機械的に作った 1165 通りのうち 600 通りを実行、38 が残る」は、一覧がリポジトリに無く、確かめられなかった（未確認）。`docs/design/harness.md` 5章は、Windows の9通りだけを書いていて、この残りに触れていない。

### 軽微

- `python scripts/doctor.py` は、`--headless` を付けなくても `claude --version` を1回実行する（版を調べるため。仕様の範囲6のとおり）。`claude -p` は実行しない。「claude を一切実行しない」わけではない。
- 記録に残らないのは値。名前の形をした文字列は残る: 最上位と `structured_output` の項目の名前、`type` と `subtype` の英小文字の語、`--version` の1行目。偽の `claude` にキーの名前を `sk_ant_SECRETTOKEN123` にして返させると、結果のファイルに残った。設計どおりだが、実装者の試験の目印は `SECRET-7f3a` の1通りで、この境界を試していない。
- `tests/test_dev_tools.py::test_check_runs_at_the_top_of_the_repository` は、pytest をリポジトリの直下で起動すると、`cwd=ROOT` を外しても通る。その壊し方（K05）では、写した `check.py` がリポジトリ全体の試験を入れ子で起動し続け、止まらなくなった。
- 画面の置き換えは、単位でない文字にも掛かる（cp932 で、ファイル名 `無いµ.json` が `無いu.json` と表示される）。
- `scripts/check.py` は `python -m ruff` を使う。ruff を Python の環境の外に入れている場合（pipx など）は、main では通っていた確認が通らなくなる。
- W1 を統合した後、古い main から切った作業ツリーは、main を取り込むまで `cycle.py check` が `error` になる（`scripts/check.py` が無い）。
- 英語 Windows（cp1252）の画面では、`doctor.py` の結果の行（問題なし / 問題あり）も `?` になる。合否は終了コードと JSON で分かる。

## 基準2: スキップの理由と、代わりの試験

| スキップ（Windows） | 理由 | 当たるか | 同じ決まりを確かめる試験 |
|---|---|---|---|
| SIGINT | 他のプロセスへ SIGINT を送る手段が無い | 当たる | `test_merge_is_undone_when_interrupted_from_the_keyboard`（どのOSでも実行。試験の中で KeyboardInterrupt を起こす）と、Ctrl-Break の試験（Windows で実行） |
| SIGTERM | Windows の SIGTERM は強制終了で、後始末の機会が無い | 当たる | `test_merge_killed_during_check_is_undone_by_the_next_merge`（どのOSでも実行） |
| SIGHUP | Windows に SIGHUP が無い | 当たる | Ctrl-Break の試験 |

Linux でのスキップは2件（Ctrl-Break、開いたままの状態ファイル）。試験の中からのスキップや、OSを見て途中で戻る試験は無い（`WINDOWS` を見るのは補助の関数だけ）。

## 基準3: 置き換えた後の意味

- 数値は、UTF-8 の出力と1文字も違わない（cp932、cp1252、ascii）。
- `µ`（U+00B5）と `μ`（U+03BC）は `u`、`Ω`（U+03A9、U+2126）は `ohm`。その文字コードで表せる文字は置き換えない。
- `500 mΩ` は `mohm`、`2 MΩ` は `Mohm` で、取り違えない。画面の単位を文字どおりに読んだ大きさが、同じ行の換算後の値と合うことを、受け入れ試験で確かめている。
- 表に無い文字は `?`（読めない単位 `㎌` のエラーの文面など）。

## 基準4: `scripts/doctor.py`（偽の claude）

| 場面 | claude の起動 | 終了コード | 記録（headless） |
|---|---|---|---|
| `--headless` なし | `--version` の1回だけ | 0 | `{"ran": false, "reason": "not_requested"}` |
| 正常 | `--version`、`-p` が1回ずつ | 0 | `unexpected: []`、`harness_verdict: pass` |
| `is_error` が真 | 同じ | 1 | `unexpected` に `is_error`、`harness_verdict` |
| JSON でない | 同じ | 1 | `form: not_json`、`fields: {}` |
| 終了コード 3 | 同じ | 1 | `returncode: 3`、`unexpected` に `returncode` |
| `structured_output` が無い | 同じ | 1 | `present: false`、`unexpected` に `structured_output` |
| 時間切れ | 同じ | 1 | `timed_out: true`、`unexpected: ["timed_out"]` |
| APIキーの環境変数あり | `--version` の1回だけ | 1 | `reason: api_key_set`（鍵の値は出ない） |

値の位置（応答の本文、要約、基準と指摘の文面、セッションのID、標準エラー、環境変数）に入れた3通りの形の目印は、どの場面でも、結果のファイルにも画面にも出なかった。`.cycle` に書かれるのは `doctor-result.json` だけ。`-p` の組み立ては `cycle.py run review` と同じ並び。

## 基準5: main 版と W1 版に同じ手順を流した比較（Linux）

一時リポジトリに、決まり1〜7の 33 場面（記録した手順と観測は 348 件）を、main 版と W1 版の `cycle.py` に流した。git の日時を固定して、出力とコミットIDをそのまま比べた。

- 29 場面が一致: 必要なゲート、見出しの誤り、見出しの変更と `--reset`、ツリーIDでの記録、例外の場所（追加、書き換え、削除、名前の変更、移動）、check の失敗と古さ、未コミット、切り離された HEAD、実装者と検証者、ヘッドレスの結果 21 通り、取り込み後の確認の失敗、競合、SIGINT / SIGTERM / SIGHUP での取り消し、強制終了からの復旧。
- 4 場面が意図した違い: `check.sh` の実行ビットを落とす（main は error、W1 は影響なし）、`check.py` を消す（main は fail、W1 は error。どちらも統合しない）、実装の指示の文面、main 側に `check.py` が無いときの取り消しの理由（fail と error）。
- `scripts/check.sh`: 6 場面（通る、lint の誤り、試験の失敗、スキップ、import の誤り、試験なし）で、終了コードが main と同じ（0、1、1、0、1、5）。
- 計算: `examples/rlc_series.json` の画面の出力と CSV（3185 行）が、main と1バイトも違わない。`circuit_sim/` の差分は `__main__.py` だけ（画面への出力と BOM）。

## 壊す確認（W1 で変わった箇所。実装者の試験だけを流した結果）

78 通り（`cycle.py` 45、`doctor.py` 26、`check.py` 7）。落ちた 67、生き残り 10、止まらなくなった 1（K05、上の「軽微」）。`doctor.py` と `check.py` の残り 32 通りは、すべて落ちた。

| 番号 | 壊し方 | 決まり | 種類 | 足した試験での結果 |
|---|---|---|---|---|
| C05 | 確認を起動できない（OSError）ときに合格にする | 4、7 | 本当の抜け | 落ちる |
| C23 | claude の出力に UTF-8 でないバイト列があると例外で落ちる | 6 | 本当の抜け | 落ちる |
| C24 | claude の出力を既定の文字コードで読む | 6 | Windows でだけ違いが出る | Windows で落ちるはず（壊した版は流していない） |
| C34 | 状態ファイルの改行を LF に固定しない | 記録 | Windows でだけ違いが出る | 同上 |
| C37、C38 | 記録するパスを OS の区切りのまま残す | 記録 | Windows でだけ違いが出る | 実装者が Windows の実物で落としている（実行 43613733787） |
| C48 | Windows でも、拒まれたら待たない | 8 | Windows でだけ違いが出る | 同上 |
| C49 | `--dry-run` の表示で引数を引用符で囲まない | 表示 | 本当の抜け（決まりの外） | 残る |
| C20 | 打ち切りの後、残りの出力を読み切らない | 6 | Linux では問題1を直す変更 | 問題1の試験が XPASS で落ちる |
| C36 | ロックを明示的に外さない | 8 | 同値（閉じれば外れる） | 残る |

実装者の Windows での9通り（実行 43613733787、コミット `5d0eadd`）は、ログを読んで確かめた: 8通りが落ち、1通りが生き残り。その後の2コミットでは流し直されていない。

## 足した試験の実効性

製品（と、対象の試験）を 20 通り壊して、足した受け入れ試験を流した。18 通りで落ちた（µ を消す、µ を m にする、Ω を消す、置き換えをしない、`-p` を2回実行する、`--headless` なしで `-p` を実行する、要約や環境変数を記録する、ハーネスの試験にスキップを足す、`check.sh` が失敗を握りつぶす、など）。残る2通り（C24、C34）は、Linux では違いが出ない。

## 未確認

- 本物の `claude -p`、日本語 Windows の実機、macOS。
- Windows での `claude` の見つけ方の順。Python の `shutil.which` は、Windows ではいまいる場所を PATH より先に探す。`cycle.py` を実行する場所に `claude.cmd` があれば、そちらが使われるはず（`harness.md` 7章は「PATH の上の」と書いている）。Windows では試していない。
- 実装者の「38 が残る」の中身。

## 追加した試験

`tests/acceptance/test_w1_windows.py`（41 件。Linux では 40 passed、1 xfailed。Windows では 41 passed。手元で約 15 秒）。

- 基準2: スキップの印の数え上げ（3件）
- 基準3: 数値の一致、単位の意味、接頭語の取り違え（14 件）
- 基準4: `claude` の起動の回数と引数、形の記録、値が出ないこと（10 件）
- 基準5: UTF-8 でない出力、既定の文字コード、改行、起動できない確認、打ち切り、`check.sh`、チェックアウトの改行（14 件）

`tests/acceptance/test_e0.py` への実装者の変更（Windows でだけ `SYSTEMROOT` を足す）は、Linux では渡す環境変数が変わらず、比べる内容も同じ。試験を弱めていない。
