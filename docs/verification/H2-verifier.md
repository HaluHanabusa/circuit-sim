# H2 独立検証（verifier）

- 対象: ブランチ `H2`、コミット a818f01、ツリー `53e9779f399f1a3fac1b6b3eb2f1b6921c79c38b`
- 仕様: `docs/plan/H2-harness-followups.md`（範囲1〜25、受け入れ基準1〜3）、設計: `docs/design/harness.md`
- 日付: 2026-11-15。Linux（2コア、Python 3.13.18、git 2.43.0）。ほかの作業単位の検証が同じ機械で並行して動いていた（負荷 6 前後）
- 測り方の共通: すべて `PATH=/usr/local/bin:/usr/bin:/bin`（claude の無い PATH）。作業は `/tmp/h2-verify/` の、
  H2 の写し（`git clone` してツリーが 53e9779f であることを確かめたもの）と一時リポジトリで行った。
  本物の `.cycle/` と `/home/claude/wt/H2` の既存のファイルには触っていない。

## 判定: 不合格

重大な問題が2つある（基準1の付け替えの、別の手口が2つ通る）。基準3は保留（下の「基準3」）。

| # | 基準 | 実測値 | 測り方 | 合否 |
|---|---|---|---|---|
| 1 | 土台の付け替え: 再レビューの再現手順で merge が拒まれ、遅れているだけなら通る | 再現手順2つは拒まれた（main の他の成果は残る）。遅れているだけは通った。自分で考えた別の手口9つのうち、範囲1の中の**2つが通り、main の他の成果が消えた**（下のF、I）。ほかに、文書で守れないとされている記録の手での書き換え（G、R2）でも通る | 一時リポジトリで start → 実装 → main に他の成果 → ゲート → 付け替え → merge。merge の終了コードと、取り込んだ後の main のツリーに他の成果のファイルが残るかを見た | 不合格 |
| 2 | 範囲2〜5: それぞれ、壊したら落ちるテストがある | 自分で作った19通り（範囲2: 6、範囲3: 5、範囲4: 5、範囲5: 3）がすべて落ちた | 写しの `scripts/cycle.py`、`scripts/doctor.py` を1か所ずつ書き換え、`tests/test_harness.py` と `tests/test_dev_tools.py` を `-x` で流した（実装の mutants.py は使っていない） | 合格 |
| 3 | `tests/test_harness.py` が通る。1か所ずつ壊す確認で生き残りが無い | 前半: 通った。後半: **保留**（全件はオーケストレーターの指示で止めた。下の「基準3」） | 前半は `scripts/check.py` の全体の中で確かめた | 保留 |

ほかに頼まれた確認:

| 確認 | 実測値 | 測り方 | 合否 |
|---|---|---|---|
| 4 試験の遮断（範囲19・20・24） | 9つの置き方のすべてで、身代わりの claude は起動されなかった。同じ場所の道具（リンク、普通のファイル）は残った。見張り役を起動できないときは理由を表示して終了コード71。並列実行の子でも遮断が効き、見張り役を起動した試験の名前が出た | 下の「4」 | 合格 |
| 5 ヘッドレスの確認（範囲15・16・17）。範囲7は本物のCLIが要るので未確認 | 役割ごとの判定、失敗の5分類、レビューの引数は仕様どおり。分類から漏れる文面がある（軽微） | 偽の claude（引数を控え、決まった出力を返す）を PATH の先頭に置いて `doctor.py --headless`、`cycle.py run` | 合格（範囲7は未確認） |
| 6 後始末（範囲25）が他人のプロセスを止めないか | 印の無いプロセス、別の回の印、似た文字列を別の変数に持つプロセス、同じ機械のほかの検証のプロセス9個は、どれも止められなかった | 下の「6」 | 合格 |
| 7 `PATH=/usr/local/bin:/usr/bin:/bin python scripts/check.py` | 終了コード 0（ruff 通過、1283 passed、9 skipped、10 xfailed、626秒） | H2 の写し（ツリー 53e9779f、私の追加ファイルなし）で実行。追加した3ファイルは、ruff と単独の実行（3回とも 9 passed、2 xfailed）で確かめた | 合格 |

## 1. 基準1（土台の付け替え）

場面（`/tmp/h2-verify/gates/scen.py`。各場面とも、U1 は main の M0 から始め、実装の後に main に `other.txt` が入る）:

| 場面 | merge | main に other.txt | 期待 |
|---|---|---|---|
| A ゲートの後に `git reset --soft main && git commit` | 拒んだ（check、review、verify が「分かれ目が変わっている」） | 残る | 合う |
| B ゲートの後に `git merge -s ours main` | 拒んだ | 残る | 合う |
| C 遅れているだけ（ゲートの後に main が進む） | 取り込んだ | 残る | 合う |
| C' 遅れているだけ（main がゲートの前に1回、後に2回進む） | 取り込んだ | 残る | 合う（受け入れ試験） |
| D ゲートの後に正直に `git merge main`（ツリーが変わる） | 拒んだ | 残る | 合う（やり直しが要る） |
| E 付け替えの後に check だけやり直す | 拒んだ（review、verify） | 残る | 合う |
| **F snapshot の後、record の前に `reset --soft main`（check もやり直す）** | **取り込んだ** | **消えた** | **合わない（重大1）** |
| G 記録ファイル（`.cycle/state`）の base を手で書き換える | 取り込んだ | 消えた | 文書の「スクリプトでは守れないこと」の範囲 |
| H main の途中のコミット（M1）を、main が M2 に進んだ後に `-s ours` で取り込む | 拒んだ | 残る | 合う |
| **I 分かれ目が2つになる取り込み（下）** | **取り込んだ** | **A の成果が消えた** | **合わない（重大2）** |
| R1 Windows の結果の後に付け替え、ほかのゲートはやり直す | 拒んだ（windows が古い） | 残る | 合う |
| R2 結果のファイルの commit を、付け替えた後のコミットに書き換える | 取り込んだ | 消えた | 結果のファイルの手での書き換え（文書で守れないとされている） |
| R3 結果のファイルの commit を main の先端（ツリーが違う）にする | record-ci が受け付けた | — | 軽微3 |

R2、R3 で main の成果が消えたのは、ほかのゲートを付け替えの**後**に記録し直したため（確認する側が、付け替えた土台の上で見た内容）。範囲1の対象（ゲートの後の付け替え）の外として扱った。

## 2. 基準2（範囲2〜5を壊す）

`/tmp/h2-verify/mut2/run.py`。壊す前の写しで同じ試験は 377 passed、2 skipped。

| 番号 | 壊し方 | 落とした試験 |
|---|---|---|
| 2a | `tests/acceptance/` の名前を `*.py` に広げる | test_exception_paths_take_only_test_files_and_records |
| 2b | `docs/verification/` の名前を `*` に広げる | 同上 |
| 2c | 名前を見ない（場所だけ） | 同上 |
| 2d | 名前を、最下層でなく場所の直下の階層で取る | 同上 |
| 2e | `test_*.py` を `test_*` に広げる | 同上 |
| 2f | 追加（A）以外の変更も許す | test_exception_paths_allow_only_files_that_did_not_exist_at_the_gate[rewrite-…] |
| 3a | 基準ゼロ件の検査を外す | test_verification_pass_without_any_criterion_is_not_a_pass[verify-fail] |
| 3b | 検査をレビューに向ける | 同上 |
| 3c | `cmd_run` がゲートを渡さない | 同上 |
| 3d | `doctor.py` がゲートを渡さない | test_each_role_is_judged_by_the_gate_it_is_used_for |
| 3e | 検査の条件を当たらない形に変える | test_verification_pass_without_any_criterion_is_not_a_pass[verify-fail] |
| 4a | 見出しの変更をすべて「緩める」と見なす | test_header_that_only_adds_gates_keeps_the_records[needs_windows…] |
| 4b | 減ったゲートの求め方を対称差にする | 同上 |
| 4c | ゲートが増えたとき記録を消す | 同上 |
| 4d | 履歴の印を `gates_added` から `reset` に変える | 同上 |
| 4e | merge が見出しの変更を見ない | test_start_refuses_to_change_gates_once_records_exist |
| 5a | 案内からコミットIDを外す | test_recovery_leaves_main_alone_when_it_changed_after_the_kill[commit_on_top] |
| 5b | 取り込み前のコミットを7桁に縮める | 同上 |
| 5c | 取り込もうとしたコミットを外す | 同上 |

## 基準3（保留）

- `tests/test_harness.py`: `scripts/check.py` の全体（上の7）で通った。
- **全件は修正後の再検証で流す。** 全件（`nice -n 19 python scripts/mutants.py run`、対象 772 通り = 782 − 同値 10）を起動したが、
  コードレビューで mutants.py 自体に生き残りを少なく数える経路が見つかり、cycle.py も直すことになったため、オーケストレーターの指示で止めた。
  止めた時点の進み: 少なくとも 25/772 を終え、そこまでに生き残りの表示は無し（結果のファイルは最後にしか書かれないので、件数はそれ以上は分からない）。
- 最初の起動（`nohup` 付き）は、壊す前の試験が落ちて止まった（軽微1）。
- 個別に流したもの: `cycle:kill_tree:del:2`、`cycle:run_captured:del:1` は落ちた。**`cycle:kill_tree:del:3`（`proc.kill()` を消す）は `--confirm`（道具の試験の全体）でも生き残り、一覧に無い。**
  Linux では、`kill_tree` の呼び出し元（`run_captured`、`local_ci.run_streaming`）がどちらも新しいセッションで起動するので、`killpg` が本人も止め、`proc.kill()` は動作を変えない（Linux では同値）。
  Windows では、`taskkill` が失敗したときの最後の歯止めなので、Windows の分岐として扱うのが妥当。一覧に理由を書くか、Windows で落とす試験を足す。
- 一覧の `[[equivalent]]` 10件の判断（全件の再実行の前に直すべきもの）:

| 番号 | 判断 |
|---|---|
| `cycle:<module>:elem:35`（GIT_COMMON_DIR） | **同値ではない。** 理由の「GIT_DIR が無いときは読まない」は、git 2.43.0 で成り立たない。リポジトリ A の作業ツリーで `GIT_COMMON_DIR=<B>/.git git -C <A> rev-parse --git-common-dir` は B を返し、`cat-file` は B の中身を読み、`merge-base main HEAD` は失敗した（参照の名前は A のまま）。外側の環境に置けば試験で落とせる（軽微4） |
| `cycle:<module>:elem:39`（GIT_NAMESPACE） | 同値（Linux で確かめた）。名前空間に別のコミットを置いても、`GIT_NAMESPACE=foo` の `rev-parse main`、`merge-base` は変わらない |
| `cycle:lock_handle:del:3`、`cycle:unlock_handle:del:2`、`cycle:kill_tree:del:1` | 同値ではなく、Windows でだけ通る分岐。Linux では判断できない。PO の Windows で確かめる（末尾） |
| `cycle:run_captured:const:1` | 同値（`encoding` を渡すと、`text` によらず文字列で受け取る） |
| `cycle:recover_merge:const:1`、`cycle:merge_unit:const:2` | 同値（直前に読めた控えを、統合のロックを持ったまま消す。ほかに消す者がいない） |
| `cycle:gate_problems:del:6` | 統合の可否は同値（理由の行が増えるだけ。記録の対象がツリーIDでない場合は、次の検査で先に抜ける） |
| `doctor:main:const:4` | JSON の値としては同値（ファイルのバイト列だけが変わる） |

## 3. 見つけた問題

### 重大

1. **snapshot と record の間に土台を付け替えると、ゲートが有効なまま main の成果が消える。**
   - 再現（受け入れ試験 `test_c1_rebase_between_snapshot_and_record_is_refused`）: main に他の成果 → `TREE=$(cycle.py snapshot U1)` →
     作業ツリーで `git reset --soft main && git commit` → `record U1 review/verify --tree $TREE` → `check U1` → `merge U1` が通り、`other.txt` が main から消える。
   - 原因: `cmd_record` は分かれ目を **record の時点の HEAD** で求める（`main_base(worktree)`）。確認した内容（`--tree`）は snapshot の時点のもの。
     ヘッドレス（`cmd_run`）と `cmd_check` は、実行の前にツリーと分かれ目を一緒に控えるので、食い違わない。
   - 直し方の例: snapshot が分かれ目も控え（状態に「ツリー → 分かれ目」を残す、または表示する値に含める）、record は snapshot の時点の分かれ目を記録する。
     控えが無ければ、record の時点の分かれ目が snapshot の時点と同じことを確かめられないので拒む。
2. **分かれ目が2つになる取り込みで、`git merge-base` の1つだけが控えと一致し、取り込みが他の成果を消す。**
   - 再現（受け入れ試験 `test_c1_second_merge_base_added_by_merging_an_older_unit_without_its_content_is_refused`）: 別の作業単位 A が M0 から分かれて A1 をコミット
     （日時が M1 より古い）。main に M1 が入り、U1 が M1 から始まり、ゲートを通す（分かれ目 M1）。main が A を取り込む（M2）。U1 が `git merge -s ours <A1>`。
     `git merge-base --all main HEAD` は M1 と A1 の2つ、`git merge-base main HEAD` は M1 を返して控えと一致し、merge が通る。取り込み（ort）は2つの分かれ目から作った仮の土台を使うので、A の成果（`a_unit.txt`）が main から消える。
     A1 の日時が新しいと、git は A1 を返し、拒まれる（日時しだいで通る）。
   - 原因: `main_base` が `git merge-base`（1つだけ）を比べている。
   - 直し方の例: `git merge-base --all` の集合を控えて比べる。または、分かれ目が2つ以上ある取り込みを拒む。

### 軽微

1. **試験が `nohup` の下で落ちる。** `nohup python -m pytest tests/test_harness.py::test_merge_is_undone_when_interrupted_during_check_on_main` の `[SIGHUP]` が30秒で落ちる
   （`nohup` なしでは通る）。`nohup python scripts/mutants.py run` は、壊す前の試験がこれで落ちて止まる。原因: 範囲22で、起動したときに無視の合図は差し替えないので、
   無視を引き継いだ `cycle.py merge` は SIGHUP で止まらない。試験の `start_cycle` は SIGINT だけを既定に戻している。SIGHUP と SIGTERM も既定に戻す（試験の側の直し）。
   2時間かかる全件を `nohup` で流したくなるので、9章にも書いておくとよい。
2. **例外の場所に足せる `test_*.py` で、試験の全体を外せる。** `tests/acceptance/test_zz.py` に `import os; os._exit(0)` と書くと、pytest は試験を流さずに終了コード 0 で終わる
   （失敗する試験を一緒に置いて確かめた。ruff も通る）。`cycle.py check` は終了コードだけで合格にするので、check も、取り込んだ後の確認も通る。
   設計書と `AFTER_GATE_FILES` の説明の「名前を絞るのは conftest.py ひとつで受け入れ試験を全部外せるため」は、名前を絞っても同じことができるので言い過ぎ。
   範囲2の文面（名前を絞る）は満たしている。対策の例: `check.py` の合否に、試験の件数（通過が1件以上）を含める（local_ci.py と同じ）か、文書で残る経路として書く。
3. **record-ci は、結果の commit と tree が対応しているかを確かめない。** commit を main の先端（ツリーが違う）に書き換えた結果も受け付けた（分かれ目はその commit から求める）。
   結果のファイルの手での書き換えは守れないと文書にあるが、`commit^{tree} == tree` の検査は安く、取り違えたファイルを拒める。
4. 同値の一覧の `cycle:<module>:elem:35` の理由が誤り（基準3の表）。
5. ヘッドレスの失敗の分類から漏れる文面: `You've hit your limit · resets 3am`、`API Error: 429 … rate_limit_error`（下線）、
   `subtype: error_max_budget_usd` だけで文面の無い終わり方は、どれも `other`。分類は文面だけを見て `subtype` を見ない。本物のCLIの文面は確かめていない（範囲7）。
   誤った種類にはならず `other` に倒れるので、害は情報が減るだけ。
6. レビューのゲートは `--allowedTools` に `Bash` を許すので、シェル経由の書き込みは起動の側では禁じていない（範囲17の「編集の道具」は Edit、Write などの名前）。
   書き換えればツリーが変わってレビューは古くなるので、合格を通す経路ではない（例外の場所へのファイルの追加だけは、ゲートの後でも残る）。
7. （所見）`tests/acceptance/test_e0.py:759` は PATH を `/usr/bin:/bin` と直書きしている（決まり11）。claude を起動する試験ではないので、経路にはならない。

## 4. 試験の遮断の確かめ方と結果

`/tmp/h2-verify/guard/`。本物の代わりに、起動されたら印を書く身代わりの `claude` を置いた。同じ場所に `git`、`bash`（別の場所へのリンク）、
`mytool`（普通のファイル）、`claude-code`（身代わりへのリンク）。写した H2 の中に、`CIRCUIT_SIM_SAFE_PATH` で探してシェルから `claude` を起動する試験を置いて流した。

| 置き方 | 結果 |
|---|---|
| 身代わりの場所を PATH の先頭 / 末尾 | 起動されない。`git`、`bash`、`mytool` は影の場所から見つかる。シェルの `claude` は 127 |
| 相対パス（`.`、`realbin`）と空の項目 | 取り除いた（理由つきで表示） |
| 身代わりの場所へのリンクの場所（末尾に `/`）と、同じ場所の2回目（`//`） | どちらも置き換えた |
| claude がリンク（先は別名の身代わり）だけの場所 | 取り除き、同じ場所の `env2` は残った |
| 試験が名前で claude を起動 | 見張り役に当たり、終了コード 71、試験の名前を表示。身代わりは起動されない |
| 一時フォルダが実行不可（`noexec` の tmpfs を `TMPDIR` に） | 試験を始めずに終了コード 71。理由と、`TMPDIR` を変える案内を表示 |
| 並列実行（pytest-xdist 3.8.0、`-n 2`。検証用の仮想環境に入れた） | 子でも同じ遮断。子で見張り役を起動すると 71 と「起動した試験: …（見張り役の記録から）」 |

`claude-code` のように claude 以外の名前で本物を指すリンクは残る（名前で claude を探す経路ではないので、問題にしない）。

## 5. ヘッドレスの確認（偽の claude）

- `doctor.py --headless`（役割9つ）: すべて合格の出力なら終了コード 0、`unexpected` 空。verifier だけ基準ゼロ件 → verifier は `fail`、ux-designer だけ `structured_output` なし → `error`、
  どちらも `unexpected` に「役割: 項目」で出て、終了コード 1。応答の本文（印の文字列）は記録に入らない。`stop_reason`、`terminal_reason` は値まで記録された。
- 失敗の分類: `Not logged in · Please run /login` → not_logged_in、`Claude AI usage limit reached|…`、`Credit balance is too low` → usage_limit、
  `There's an issue with the selected model …` → model_unavailable、`Exceeded USD budget` → budget_exceeded、その他 → other。標準エラーだけの `Invalid API key` → not_logged_in。漏れは軽微5。
- 引数: code-reviewer は `--permission-mode default --disallowedTools Edit,Write,NotebookEdit,MultiEdit`、ほかは `acceptEdits` で禁止なし。`doctor.py` の `--allowedTools` はどれも `Read`。
  `cycle.py run review --dry-run` と実際の起動の引数も同じ（`--allowedTools Read,Bash,Grep,Glob`）。
- 役割の定義に `tools:` を足すと、実行の前に注意が出て、記録に `restricts_tools: true`。
- `--headless` だけなら確認は省かれ（`check: skipped`）、`--check` を付けると流れた（通過、585秒）。
- `cycle.py run verify` で基準ゼロ件の合格 → `fail` を記録、終了コード 1。

## 6. 後始末

- 4種の印の無いプロセス（`claude` という名前の sleep、別の回の印、空の印、印と無関係の変数）と、同じ機械で動いていたほかの検証のプロセス9個を置いたまま、
  `mutants.py run --only cycle:kill_tree:del:2 cycle:run_captured:del:1 cycle:kill_tree:del:3` を流した。どれも生き残った。
- `stop_leftovers(<値>)` を直接呼んだ: その値の印をそのまま持つものだけを止め、`<値>x`（長い値）、`X=CIRCUIT_SIM_MUTANT_RUN=<値>`（別の変数）は止めなかった。
  環境を空にして起動し直した子（`env -i`）は印を失うので、後始末から漏れる（止めすぎではなく、止め残しの側）。
- 受け入れ試験 `test_h2_mutant_cleanup.py` にした。

## 7. テストの実効性についての所見

- 範囲2〜5の試験は、自分で作った19通りをすべて落とした。期待値は仕様の文面から作られていて、実装の式の写しではない。
- 土台の試験（`test_gates_go_stale_when_the_branch_is_moved_onto_main_without_its_changes` など）は、再現手順の2つと遅れているだけの場合を確かめているが、
  snapshot と record の間、分かれ目が2つの場合が無い（重大1、2がすり抜けた）。
- 追加した受け入れ試験は、写しで「分かれ目を比べない」「record-ci の分かれ目を HEAD で求める」「record の分かれ目を main で求める」に壊すと、それぞれ 4、1、2 件が落ちた。

## 追加したファイル

- `tests/acceptance/test_h2_base_rebase.py`（基準1。8件。重大1、2の2件は `xfail(strict=True)`。直ったら XPASS で落ちるので印を外す）
- `tests/acceptance/test_h2_claude_guard.py`（範囲19・24。2件。Windows では飛ばす）
- `tests/acceptance/test_h2_mutant_cleanup.py`（範囲25。1件。/proc の無いOSでは飛ばす）
- この記録

## PO に頼む Windows での確認

修正後のコミットで行う。

- `python scripts\local_ci.py --ref origin/H2` が合格する（`tests\test_harness_os.py` の `NoDefaultCurrentDirectoryInExePath` の組の試験、`tests\test_error_dialogs.py`、Ctrl-Break と置き換えの待ちの試験、追加した受け入れ試験を含む）。止まる画面が出ないこと。
- 見張り役の Windows の形: PATH の先頭に `claude.cmd` を置いた試験のセッションで、見張り役（`claude.cmd`、`claude.bat`）に当たると終了コード 71 で終わる（`local_ci.py` が失敗になる）。
- Windows でだけ通る分岐を壊して落ちるか: `python scripts\mutants.py run --only cycle:lock_handle:del:3 cycle:unlock_handle:del:2 cycle:kill_tree:del:1 cycle:kill_tree:del:3 --include-equivalent`。
  Windows では後始末が効かないので、終わった後にタスク マネージャーで `python`、`claude`、`cmd` が残っていないか見る（残っていたら手で止める）。
- 作業ツリーの直下に `node.cmd`、`git.exe`（中身は何でもよい偽物）を置いても、`python scripts\cycle.py status <ID>` と `doctor.py` が、それを起動しない（範囲11、14）。
- サインイン済みのCLIで `python scripts\doctor.py --headless`: 役割ごとに `structured_output` が返り、`headless.unexpected` が空。失敗したら `failure` の種類と `stop_reason`、`terminal_reason` を控える（範囲7、15、16）。
- コマンド プロンプト（cp932）で `python benchmarks\bench_switching.py` が文字コードのエラーで落ちない（範囲10）。
- `merge` の確認の途中で Ctrl-Break を押すと、main が取り込み前に戻る。
