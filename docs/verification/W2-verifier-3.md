# W2 独立検証の記録・3回目（verifier。6ec1b6c）

- 対象: ブランチ `W2`、先頭 `6ec1b6c`（ツリー `de9b44665b7560f9e706481c41fd65fb326065f0`）。修正は `git diff d5b4989 fe3d357`（6ec1b6c で取り込み）
- これまでの記録: `docs/verification/W2-verifier.md`（1回目）、`docs/verification/W2-verifier-2.md`（2回目）。どちらも書き換えていない
- 手元は Linux、Python 3.13.18、git 2.43.0。GitHub上の自動テストは使っていない。push もしていない
- 本物の `claude` は起動していない。PATH は「罠の偽 `claude` の場所、`/usr/local/bin`、`/usr/bin`、`/bin`」だけにした（`/opt/node22/bin` は入れていない）。
  罠は起動されると引数を控えて終了コード 97 で終わる。この回のすべての実行で、罠は1回も起動されなかった

## 判定: 基準1〜4と6は合格。基準5は未確認（POの結果待ち）

重大な問題は見つからなかった。2回目の問題A（端末を閉じたときに claude が止まらない）と、再レビューの問題B（試験から本物の claude に届く）は、
どちらも直っていた。W2 の完了は、POの Windows の結果が下の「Windows の結果で確かめること」を満たしたときに決まる。

## 基準ごとの結果

| # | 基準 | 実測値 | 測り方 | 合否 |
|---|---|---|---|---|
| 1 | 取り出して実行 | 受け入れ試験 `test_w2_local_ci_gate.py`（29件）がすべて通る。2回目の中断と時間切れの再現手順も同じ結果 | 全体の確認 | 合格 |
| 2 | 記録の中身 | 同上（基準2の5件） | 全体の確認 | 合格 |
| 3 | 合否 | 1回目の重大1の再現手順（`-k`、`--deselect`、`PYTEST_PLUGINS`）で、どれも全体が走って不合格になる。後の素の実行も不合格を示す。`doctor.py` も `PYTEST_` の変数を渡さなくなった | 再現手順の流し直し | 合格 |
| 4 | ハーネスとのつなぎ | `record-ci` が `--current` の結果と、dirty・changed の崩れた結果を拒む（2回目と同じ）。**見張り役が働いた確認は不合格になり、record-ci は windows を fail と記録する**（下の5） | 再現手順、一時リポジトリ | 合格 |
| 5 | Windowsの実機 | POの結果（6ec1b6c）待ち | 下の「Windows の結果で確かめること」 | 未確認 |
| 6 | 後退が無いこと | `PATH=<罠>:/usr/local/bin:/usr/bin:/bin python scripts/check.py` が終了コード 0（1040 passed、5 skipped、9 xfailed、224 秒） | 実行 | 合格 |

許容の根拠: すべて合否が二値。所要時間は記録だけ。

## 1. A と B が直ったか（2回目の再現手順）

- **A: 直っていた。** 偽の `claude` を使った `cycle.py run U1 review` に、5通りで合図を送った。まとまりへの SIGHUP（端末を閉じた）、
  まとまりへの SIGINT、まとまりへの SIGTERM、プロセスだけへの SIGTERM、プロセスだけへの SIGHUP。6ec1b6c では、どれでも `claude` が一緒に止まった
  （`cycle.py` の終了コードは 129、-2、143、143、129）。b17b5ff では、プロセスだけへの SIGTERM と SIGHUP で `claude` が残っていた（修正前からの穴。これも直った）。
  `doctor.py --no-check --headless` も、まとまりへの SIGHUP で止まった。
- **B: 直っていた。** 2回目と同じく、試験の側の偽の `claude` から実行の印を外した（起動できない。noexec と同じ）。その複製で
  `tests/test_harness.py`、`tests/test_dev_tools.py`、W1 の受け入れ試験2つを流した。罠は **0 回**（2回目は 111 回）だった。
  見張り役は 112 回起動され、セッションは終了コード **71** で失敗した。起動した試験の名前は 81 件表示された。
  遮断は、罠のある場所（`claude` という名前のファイルがある場所）を PATH から外していた。
- 追加の確かめ（`claude` が python と同じ場所にある配置）: よくある配置（npm の `-g` で `/usr/local/bin` に入れた場合など）を模擬した。
  罠の `claude` と python へのリンクを同じ場所に置き、その python で全体の確認を流した。試験の中には `Path(sys.executable).parent` を PATH の先頭に
  足すもの（`test_w1_windows.py` の check.sh の試験）がある。それでも、罠は 0 回、全体は合格（1040 passed）だった。
  ただしこれは、足し直した PATH で `claude` を名前で探す試験が、いまは無いからにすぎない。遮断の後で、試験が `claude` のある場所を
  PATH に足し直す形は、仕組みでは防いでいない（記録だけ。下の「残っていること」）。

## 2. 受け入れ試験への変更

`git diff d5b4989 6ec1b6c -- tests/acceptance/` の変更は、`tests/acceptance/test_w1_recheck.py` の `run_harness_tests` の1行だけ。
`base_env(...)` に `PATH=os.environ["CIRCUIT_SIM_SAFE_PATH"]` を足している。私の受け入れ試験（`test_w2_local_ci_gate.py`）は変わっていない。

- 期待値、条件、件数は変わっていない。壊さない場合は `returncode == 0` で「4 passed」、壊した場合は `returncode == 1` で「1 failed」のままになっている。
- 写した先には `conftest.py` が無く、遮断が働かない。そこへ、`claude` も見張り役も無い PATH を渡している。壊した試験が `claude` を探すと
  「見つからない」で落ちるようになり、本物に届く経路と、外側の見張り役を起動して外側全体を 71 で落とす経路が無くなった。試験を弱める変更ではない。
- `CIRCUIT_SIM_SAFE_PATH` が無いセッションでは KeyError で落ちる。黙って通ることは無い。この試験は Windows でだけ走るので、Linux では実行を確かめられなかった。

## 3. 壊す確認（3回目の修正で入った分岐）

自分で選んで 25 か所を壊した。内訳は `cycle.py` 7、`doctor.py` 1、`tests/conftest.py` 17。それぞれ、壊した場所に近い試験から `-x` で流した。
罠は1回も起動されなかった。**生き残りは 2**（下の表）。

| ID | 壊し方 | 落とした試験 |
|---|---|---|
| N01 | `run_captured` で終了の合図を例外にしない（A の戻し） | `test_stop_posix.py::test_stop_signal_to_a_headless_run_stops_the_cli_too` |
| N02 | `attr.tree` を固定しない | `test_harness.py::test_tree_id_does_not_depend_on_the_users_git_settings` |
| N03、N06 | 利用者の無視の指定だけで外したファイルを見分けない、`check-ignore` を固定する | `test_harness.py::test_files_only_the_users_git_settings_ignore_are_named_with_the_way_out` |
| N04 | 違うファイルの名前を出さない | `test_harness.py::test_status_gives_the_same_reason_as_merge_for_uncommitted_changes` |
| N05 | HEAD と内容の一致を確かめない（status と merge） | `test_harness.py::test_merge_requires_committed_worktree` |
| N08 | doctor が `PYTEST_` の変数を渡す | `test_dev_tools.py::test_check_run_by_doctor_does_not_get_the_callers_pytest_settings` |
| G01–G05 | 中を確かめられない場所を残す、PATHEXT の拡張子を数えない、拡張子付きの名前を数えない、大文字と小文字を区別する、相対パスの項目を残す | `test_claude_guard.py::test_places_that_hold_anything_launched_as_claude_are_taken_off` |
| G06 | claude と同じ場所の git を残さない | `test_claude_guard.py::test_a_tool_found_only_next_to_claude_stays_reachable_through_a_link` |
| G07 | 見張り役が成功で終わる（POSIX） | `test_claude_guard.py::test_shell_that_searches_on_after_a_fake_that_cannot_start_does_not_reach_the_real_one` |
| G08、G09、G14、G15 | 見張り役を PATH に置かない、PATH を遮断しない、見張り役を先頭に置く、見張り役を含む PATH を安全な PATH として渡す | `test_claude_guard.py::test_this_session_runs_with_claude_taken_off_the_path` |
| G10、G12、G13 | 見張り役が起動されてもセッションを失敗にしない、起動した試験の名前を控えない、見張り役が記録を残さない | `test_claude_guard.py::test_session_fails_when_the_sentinel_is_started_even_if_every_test_passes` |
| G11 | 見張り役の場所を消さない | `test_claude_guard.py::test_session_passes_when_the_sentinel_is_not_started` |
| G16 | Windows の見張り役を `claude.cmd` だけにする | `test_claude_guard.py::test_windows_extension_list_without_cmd_does_not_reach_the_real_exe` |
| **N07** | **違うファイルの名前を、5個と「ほか N 件」ではなく1個で切る** | **生き残り**（表示だけ。統合の可否は変わらない） |
| **G17** | **Windows の見張り役（`.cmd`、`.bat`）が終了コード 0 で終わる** | **生き残り**（Windows でだけ動く。Linux では確かめられない。終了コードが 0 でも、記録が残るのでセッションは 71 で失敗する） |

W1 の受け入れ試験の1行（`CIRCUIT_SIM_SAFE_PATH`）を戻す壊し方は、Windows でだけ走る試験なので確かめていない。

## 5. 見張り役が働いたときの local_ci と record-ci

確かめ方: 一時リポジトリに本物の `tests/conftest.py`（遮断と見張り役）を写し、`/bin/sh -c claude` を実行する試験を足した。
ほかの試験はすべて通る。`needs_windows: true` の作業単位でコミットし、Windows の結果として `local_ci.py --ref <コミット>` を実行した。

- 試験は 4 件すべて通った。確認（`scripts/check.py`）の終了コードは 71。local_ci の結果は `verdict: fail`、`reason: check_failed`、終了コード 1。
- その結果を `record-ci` すると終了コード 1 で、windows のゲートは `fail` と記録された（合格として取り込まない。不合格は不合格として記録する、という決まりのとおり）。統合はできない。
- 罠は 0 回。見張り役を呼んだのは、写した conftest の見張り役だった。

## 残っていること（重大なものは無い）

- 軽微（記録）: 遮断は、試験のセッションが始まったときの PATH にだけ掛かる。試験が `claude` のある場所を PATH に足し直すと遮断は効かない
  （例: `Path(sys.executable).parent` を足す形。python と claude が同じ場所にある配置）。また、PATH を書かずに子プロセスを起動し、
  シェルの既定の PATH で探す形も防げない。いまの試験には、そのうえで `claude` を探すものは無かった（上の模擬で 0 回）。
  新しい試験で PATH を組むときは `CIRCUIT_SIM_SAFE_PATH` を使う、という決まりを文書に残すとよい。
- 生き残り N07（表示だけ）と G17（Windows だけ）。

## Windows の結果（6ec1b6c）で確かめること（基準5）

- `commit` が `6ec1b6c43b89fb0d3d37f6f6e9cbacb928ca0499`、`tree` が `de9b44665b7560f9e706481c41fd65fb326065f0`。
  `mode` が `checkout`、`ref` が `origin/W2`、`dirty` が false、`changed` が空。
- `os.system` が `Windows`、`encoding.locale` が `cp932`、`encoding.utf8_mode` が false。
- `check.returncode` が 0（見張り役が働いた場合は 71 で `check_failed` になる。合格にならない）、`timed_out` が false。
- `tests.total` が 1054（Linux の集め方と同じ。1040 + 5 + 9）。`failed` と `errors` が 0、`failed` の一覧が空、`verdict` が `pass`、`reason` が `ok`。
  `skipped` は Linux の 14 より多い（Windows で飛ばす試験の分）。
- Windows でだけ走る `tests/acceptance/test_w1_recheck.py::test_breaking_a_branch_only_windows_takes_makes_a_harness_test_fail` が通っている（`failed` に無い）。
- POの画面に「見張り役の claude が … 回起動された」と、「試験を始めない」（git が claude と同じ場所にしか無い場合）が出ていない。
  画面の最後が「結果: 合格」。`%TEMP%\circuit-sim-ci-*` と `%TEMP%\circuit-sim-guard-*` が残っておらず、`git worktree list` が1行。
- 結果に、PCの名前、利用者名、`C:\Users\…` のような場所が入っていない。
- ハーネスの側で `python scripts/cycle.py record-ci W2 --result <ファイル>` が終了コード 0。

## 追加した試験と記録

- 試験は足していない（A と B の再現と、修正で入った分岐は、実装側の試験が押さえている。生き残りの2つは表示と Windows だけ）。
- この記録 `docs/verification/W2-verifier-3.md`。試し書きは `/tmp/w2-verify3/`。端末を閉じる模擬は `hup.py`、`hup_doctor.py`。
  壊す確認は `mutate3.py`。見張り役と record-ci の確かめは `scratch/test_sentinel_rc.py`。罠の `claude` は `trap/`、python と同じ場所の配置は `pybin/`。
