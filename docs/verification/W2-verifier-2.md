# W2 独立検証の記録・2回目（verifier。修正後の d5b4989）

- 対象: ブランチ `W2`、先頭 `d5b4989`（ツリー `4719b2a5692c5e7def40633b84774a56b81d249e`）。修正は `git diff b17b5ff d5b4989`
- 1回目の記録: `docs/verification/W2-verifier.md`（`fee52d2`、判定は不合格）。この記録は書き換えていない
- 手元は Linux、Python 3.13.18。GitHub上の自動テストは使っていない。push もしていない
- 本物の `claude` は起動していない。どの実行でも PATH は「罠の偽 `claude` の場所、`/usr/local/bin`（python）、`/usr/bin`（git）」だけにした
  （`/opt/node22/bin` は入れていない。`/usr/bin`、`/usr/local/bin`、`/bin` に `claude` が無いことは確かめた）。
  罠の `claude` は、起動されると引数を控えて終了コード 97 で終わる

## 判定: 不合格

重大な問題が1つある（問題A）。修正で入った「新しいプロセスのまとまりで起動する」変更のために、Linux / macOS で
端末を閉じると（前面のまとまりへの SIGHUP）、`cycle.py run` と `doctor.py --headless` が起動した `claude` が
止まらずに動き続けるようになった（修正前は一緒に止まった）。1回目の問題（重大1、軽微2〜4、C21）は、すべて直っていた。

並行の再レビューの重大な指摘（試験の PATH の組み立て）は W2b で直している最中なので、ここでは確かめた結果だけを書く（問題B）。

## 基準ごとの結果

| # | 基準 | 実測値 | 測り方 | 合否 |
|---|---|---|---|---|
| 1 | 取り出して実行 | 壊した作業フォルダ（`circuit_sim/__init__.py` に `raise ImportError`、落ちる新しい試験、壊れたファイル、`.venv/circuit_sim/`）から `--ref origin/W2` で、結果は `d5b4989…`／`4719b2a…`。呼び出し元に `PYTEST_ADDOPTS="-k test_nothing_matches"` と、壊れた作業フォルダを指す `PYTHONPATH` を置いても合格（試験 1027 件、通過 1014、スキップと xfail 13、201 秒。一時フォルダに `circuit-sim-ci-*` 無し、`git worktree list` は1行）。中断（SIGINT、SIGTERM、SIGHUP、まとまりへの SIGINT と SIGHUP）と時間切れの後に、作業用の場所で動くプロセスは 0（修正前の版の同じ手順では 1） | W2 の複製、1回目の一時リポジトリ | 合格 |
| 2 | 記録の中身 | 1回目の受け入れ試験（基準2の5件）が通る | `python scripts/check.py` | 合格 |
| 3 | 合否 | 1回目の重大1の再現手順で、`-k`、`--deselect`、`PYTEST_PLUGINS` のどれでも、全体の試験が走って不合格（終了コード 1、通過 3、失敗 1）。後の素の実行も不合格を示す。`cycle.py check` も pytest の変数を渡さない | 1回目の再現手順を修正後のスクリプトで | 合格 |
| 4 | ハーネスとのつなぎ | `record-ci` が `mode: current` の結果を拒む（1回目の軽微3の再現手順）。`dirty` が true か欠け、`changed` が空でないか欠け、の結果も拒む。`gates:` と `needs_windows: true` を一緒に書いても windows が要る | 一時リポジトリ | 合格 |
| 5 | Windowsの実機 | POの2回目の結果待ち。1回目（`fee52d2`）の要点は、ツリーが Linux の `fee52d2^{tree}` と一致、件数 964 が Linux と一致、失敗 1（試験の側の前提の誤り） | 下の「Windows の2回目の結果で確かめること」 | 未確認 |
| 6 | 後退が無いこと | `python scripts/check.py` は終了コード 0（1014 passed、4 skipped、9 xfailed、200 秒）。ツリーIDの求め方を固定しても、既存のゲートは緩んでいない（下の表）。**ただし問題Aの後退がある** | 実行、3つの版の比較、端末を閉じたときの模擬 | **不合格（問題A）** |

許容の根拠: すべて合否が二値。所要時間は記録だけ。

## 1回目の問題が直ったか

| 1回目の問題 | 再現手順（1回目と同じ） | 修正後の結果 |
|---|---|---|
| 重大1 `PYTEST_ADDOPTS` | 全体では落ちるコミットを、`-k test_value`、`--deselect`、`PYTEST_PLUGINS`（`PYTHONPATH` に置いた試験を外す差し込み）で実行 | どれも全体が走り、不合格（通過 3、失敗 1）。後の素の実行は不合格の結果を示す。受け入れ試験 `test_c3_a_run_with_only_some_tests_selected_does_not_make_a_pass` が通る |
| 軽微2 時間切れの後の pytest | プロセスへの SIGINT、SIGTERM、SIGHUP、まとまりへの SIGINT と SIGHUP、`--timeout 2` | 作業用の場所で動くプロセスは、どれも 0。修正前の版では、同じ数え方で SIGTERM の後に 1 が残る（数え方が正しく働くことの確かめ）。`test_c1_the_tests_do_not_keep_running_after_a_timeout` が通る |
| 軽微3 `--current` の取り込み | コミットに無い `data.csv` に助けられた `--current` の合格を `record-ci` | 終了コード 2「コミットを取り出して確かめた結果ではない」。windows は記録されない |
| 軽微4 文字コード | `PYTHONUTF8=0` で合格した後、`PYTHONUTF8=1` を `--force` 無しで実行 | 使い回さずに実行した。同じ設定でもう一度実行すると使い回した |
| C21 `gates:` と `needs_windows` | 受け入れ試験 `test_c4_needs_windows_is_required_also_when_the_gates_are_listed` と、壊し方 K32 | 試験が通る。壊すと、実装側の新しい試験が落ちる |

私の受け入れ試験の変更は、xfail の印2つを外したことだけ（`git diff b17b5ff d5b4989 -- tests/acceptance/` で 10 行の削除。ほかの変更は無い）。

## 基準6: ツリーIDの求め方の固定（main 版、b17b5ff、d5b4989）

一時リポジトリ2種（A: `.gitattributes` に `* text=auto eol=lf`、B: `.gitattributes` 無しで CRLF のファイルをコミット）を、
9通りの利用者・システムの設定（なし、`core.autocrlf` が true／input／false、システムの `core.autocrlf=true`（Git for Windows と同じ）、
`safecrlf=true`、`core.excludesFile`、`core.attributesFile`、`core.hooksPath` に post-checkout）で取り出した。それぞれに7通りの状態
（変更なし、変更、LF→CRLF、CRLF→LF、LF の新規、CRLF の新規、利用者だけが無視する新規ファイル `x.local`）を当てた。全部で 126 通り。
期待値は、ディスクのバイト列を `git cat-file` の HEAD のバイト列と直接比べたもの（改行の変換と属性を通さない）。

- d5b4989 が「HEAD と同じ」とし、main 版か b17b5ff が「変わった」とした組み合わせは2つ。B の `autocrlf=input` の「変更なし」と「CRLF→LF」で、
  どちらもバイト列は HEAD と同じだった（main 版が、空の索引からの組み立てで、コミット済みの CRLF を変換して食い違えていた）。
  **d5b4989 が変更を隠した組み合わせは無い。** A の「LF→CRLF」は3つの版すべてが同じIDになる（`eol=lf` の正規化。git の決まり）。
- d5b4989 の方が厳しくなったもの:
  - 利用者の `core.excludesFile` だけが無視していた新規ファイル。main 版と b17b5ff は内容に含めず「HEAD と同じ」、d5b4989 は「変わった」。
  - `safecrlf=true` のとき。main 版と b17b5ff は git がエラーで止まり、d5b4989 は求められる。
- 同じバイト列なら、d5b4989 のIDは、設定（9通り）に関係なく同じだった（126 通りすべて）。
- 記録だけ: `.gitattributes` の無いリポジトリを `autocrlf=true` で取り出すと、d5b4989 は「変更なし」の状態を「変わった」とする
  （ディスクのバイト列は CRLF に変わっていて、main 版も同じ扱い。b17b5ff だけが「HEAD と同じ」）。このリポジトリには
  `.gitattributes` があるので当たらない。
- 取り出し（`local_ci`）: B のリポジトリを、利用者の設定 `autocrlf=true` と、post-checkout のフック（ファイルを1つ作る）がある環境で確かめた。
  b17b5ff はフックが取り出した場所で動き `content_mismatch` の error になった。d5b4989 はフックが動かず合格した（コミットとツリーが一致）。

## 見つけた問題

- **A（重大）: Linux / macOS で端末を閉じると、`cycle.py run` と `doctor.py --headless` の `claude` が止まらなくなった**
  - 再現: 一時リポジトリで、偽の `claude`（0.2 秒ごとに印を書き、12 秒で終わる）を使い、`cycle.py run U1 review` を
    自分のまとまりの先頭として起動した（端末の前面のジョブと同じ形）。偽が動き始めてから、そのまとまりへ SIGHUP を送った
    （端末を閉じたときや SSH が切れたときと同じ）。b17b5ff では `cycle.py` と偽の `claude` が一緒に止まった。d5b4989 では
    `cycle.py` だけが止まり（終了コード -1）、偽の `claude` は印を書き続けた。`doctor.py --no-check --headless` でも同じ。
    まとまりへの SIGINT（端末の Ctrl-C）では、どちらの版も止まる。
  - 原因: `run_captured` が `claude` を新しいセッション（`start_new_session`）で起動するようになり、端末からの SIGHUP が `claude` に
    届かない。`cmd_run` と doctor の headless の確認は `termination_as_exception` を使っていないので、SIGHUP で `cycle.py` が
    そのまま終わり、`kill_tree` が呼ばれない。
  - 影響: 記録の残らないヘッドレスの実行が、`--timeout` の歯止めも無く、利用枠を使いながら作業ツリーを書き換え続ける
    （上限は `--max-budget-usd`）。Windows には当たらない（`group_options` が何も指定しない）。
  - 直し方の案: `cmd_run` と doctor の headless の確認を `termination_as_exception()` の中で動かす（SIGHUP と SIGTERM が例外になり、
    `run_captured` の `except BaseException` がまとまりごと止める）。SIGTERM も、修正前から `claude` に届いていなかったので、一緒に良くなる。
- **B（再レビューの重大な指摘。W2b で修正中）: 確かめた。** 試験の側の偽の `claude` が起動できない状態（`install_command` の実行の印を外す。
  一時フォルダが noexec の場合と同じ）にして、`tests/test_harness.py`、`tests/test_dev_tools.py`、W1 の受け入れ試験2つを流すと、
  PATH の上の罠の `claude`（本物の場所の代わり）が 111 回起動された。うち 75 回は `claude -p`（本物なら、利用枠を使うヘッドレス実行）、36 回は `--version`。
  ふだんの状態（偽が起動できる）では、全体の確認（1014 件）の間に罠は1回も起動されなかった。
  Windows でだけ走る壊す確認（`no_cmd_by_default`）の経路は、Linux では確かめられなかった。
  - 私の受け入れ試験（`test_w2_local_ci_gate.py`）は偽の `claude` を置かず、`claude` を探す処理も通らない
    （`run U1 windows --dry-run` は、引数の検査で `claude` を探す前に止まる）。ただし PATH は呼び出し元のものをそのまま引き継いでいる。
- 記録だけ: `local_ci.py` は、呼び出し元の `PYTHONPATH` を、確かめる場所の後ろに続けて渡す。そこに pytest の差し込み（`pytest11` の
  エントリーポイントを持つ配布物）を置けば、自動で読み込まれて試験を選び直せる。わざと仕込む場合だけで、手で結果のファイルを書き換えるのと同じ扱い。

## 壊す確認（修正で入った分岐）

修正で入った分岐を自分で選んで 34 か所壊した（`cycle.py` 25、`local_ci.py` 9。構文が壊れてしまった1つは、構文を保つ形に作り直した）。
道具の試験3ファイルと、W2 の受け入れ試験を、壊した場所に近いものから `-x` で流した（PATH は罠と git・python の場所だけ）。
**34 か所すべてで試験が落ちた。生き残りは無い。** 壊している間に罠は1回も起動されなかった。

| ID | 壊し方 | 落とした試験 |
|---|---|---|
| K01–K03 | `check_env` が何も捨てない、`PYTEST_ADDOPTS` だけを捨てる、`cycle.py check` が捨てない | `test_harness.py::test_check_does_not_hand_the_callers_pytest_settings_to_the_tests` |
| K04、K33 | `local_ci` が `PYTEST_` の変数を捨てない、呼び出し元の `PYTEST_ADDOPTS` を戻す | `test_local_ci.py::test_callers_pytest_settings_do_not_choose_the_tests` |
| K05、K06 | `PYTHONPATH` に確かめる場所を置かない、呼び出し元の値を先に置く | `test_local_ci.py::test_python_started_elsewhere_reads_the_checked_content_first` |
| K07、K08 | 使い回しの条件から文字コードを外す、既定の文字コードだけにする | `test_local_ci.py::test_result_that_is_not_for_the_same_check_is_not_reused` |
| K09–K13 | `record-ci` が mode を見ない、dirty を見ない、dirty の欠けを許す、changed を見ない、changed の欠けを許す | `test_harness.py::test_result_that_is_not_a_consistent_windows_result_is_not_recorded` |
| K14、K17 | 新しいまとまりで起動しない（全体、`run_captured`） | `test_harness.py::test_timeout_stops_the_processes_the_command_started` |
| K15b | まとまりごと止めない | 同上 |
| K16 | まとまりが無いときの例外を抑えない | `test_harness.py::test_stopping_a_command_that_has_already_ended_is_not_an_error` |
| K18 | 確認を新しいまとまりで起動しない | `test_local_ci.py::test_check_that_does_not_finish_is_stopped_and_is_not_a_pass` |
| K19–K22、K25、K27 | autocrlf、safecrlf、外の属性、利用者の無視の指定、`GIT_ATTR_SOURCE` を固定しない、`add -A` を固定しない | `test_harness.py::test_tree_id_does_not_depend_on_the_users_git_settings` |
| K23、K29 | フックを空にしない、強制追加分の `update-index` を固定しない | `test_harness.py::test_tree_id_does_not_run_the_hooks_of_the_working_folder` |
| K24、K26、K28、K30 | システムの属性を外さない、`read-tree`、本物の索引の一覧、`write-tree` を固定しない | `test_harness.py::test_git_for_the_content_does_not_read_the_attributes_of_the_system` |
| K31 | 取り出しを固定しない | `test_local_ci.py::test_checkout_is_the_commit_whatever_git_settings_and_hooks_the_worki…` |
| K32 | `gates:` があると windows を要求しない | `test_harness.py::test_needs_windows_adds_the_windows_gate_also_to_the_gates_listed_in_t…` |

問題Aの経路（`cmd_run` と doctor が SIGHUP を例外にしないこと）は、修正前からあったもので、壊す対象にしていない。試験も無い。

## Windows の2回目の結果（d5b4989）で確かめること（基準5）

- `commit` が `d5b4989b125ca77656541f88e3306412f4dc8497`、`tree` が `4719b2a5692c5e7def40633b84774a56b81d249e`（Linux の `d5b4989^{tree}`）。
  `mode` が `checkout`、`ref` が `origin/W2`、`dirty` が false、`changed` が空。
- `os.system` が `Windows`、`encoding.locale` が `cp932`、`encoding.utf8_mode` が false。
- `check.returncode` が 0、`check.timed_out` が false。
- `tests.total` が 1027（Linux と同じ集め方なら一致する。1回目は 964 で一致した）。`failed` と `errors` が 0、`failed` の一覧が空、
  `verdict` が `pass`、`reason` が `ok`。`skipped` は Windows では Linux（13）より多い。1回目は 15。この版では、私の受け入れ試験のうち
  Windows で飛ばす4件（中断2件、時間切れの後のプロセス、別のOSの結果）の分も増える。
- 1回目に落ちた `tests/test_local_ci.py::test_current_checks_the_working_folder_as_it_is` が通っている（`failed` に無い）。
- POの画面の最後が「結果: 合格」で、「注意: 作業用の場所を片付けられなかった」が出ていない。実行の後に `%TEMP%\circuit-sim-ci-*` が無く、
  `git worktree list` が1行。
- 結果に、PCの名前、利用者名、`C:\Users\…` のような場所が入っていない。
- ハーネスの側で `python scripts/cycle.py record-ci W2 --result <ファイル>` が終了コード 0。
  統合する内容が `d5b4989` に `tests/acceptance/` と `docs/verification/` の新しいファイルを足しただけなら取り込める。それ以外の変更があれば拒まれる（正しい動き）。

## 追加した試験と記録

- 試験は足していない（1回目の受け入れ試験が、1回目の問題をすべて押さえていた。問題Aの試験は、直す側で足すのがよい）。
- この記録 `docs/verification/W2-verifier-2.md`。試し書きは `/tmp/w2-verify2/`（比較 `treecmp3.py`、端末を閉じる模擬 `hup.py`・`hup_doctor.py`、
  壊す確認 `mutate2.py`、罠の `claude`）。
