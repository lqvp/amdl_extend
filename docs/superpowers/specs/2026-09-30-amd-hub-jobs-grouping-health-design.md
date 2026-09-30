# amd-hub: ジョブグループ操作・Scheduler 抽出・エクスポート・健康バナー 設計

日付: 2026-09-30
ステータス: 承認待ち

## 目的

amd-hub の運用性を 4 点で引き上げる。実行順序は **C → A → E → F**
（C が app.py の構造を先に片付け、A/E が同じ jobs 群を安全に触れるようにするため）。

- **C**: 1240 行の `hub/hub/app.py` からスケジューリング関心を `hub/scheduler.py` へ抽出する（挙動不変）
- **A**: バッチ（親 URL = `parent_url`）単位のグループ操作 — 一括 cancel / 部分 requeue / 部分削除
- **E**: ジョブ履歴とアクティブキューのエクスポート（CSV/JSON、UI ダウンロードリンク付き）
- **F**: `/queue` と `/library` に常設健康バナー（既存 `/api/status` のデータのみ使用、新規 API なし）

## 全体制約（全作業共通）

- 単一プロセス／単一イベントループ前提の不変条件を変えない（`workers=1`、`HubState`）
- seam rule: `src.*` import は `ripper_host.py` と `vendor.py` の 2 ファイルのみ。
  新規モジュール（`scheduler.py`）も AST 走査テストが自動的に対象へ含む
- `HubState` の slots フィールドを増減させない（`test_state.py:122` の配線検査）
- 既存 655 テストの回帰ゼロ。テストは `cd hub && uv run pytest`（Python 3.13、
  実行前に `__pycache__` 一掃）
- ルート追加時は `test_api_jobs.py` の認証ルート表と `test_web_contract.py`
  （セキュリティヘッダ・CSP・affordance・コントラスト）へ同期する

---

## C — Scheduler 抽出（挙動不変リファクタ）

### 新規ファイル

`hub/hub/scheduler.py` に `Scheduler` クラス 1 つ。`state: HubState` を 1 つ持つ。

### 移設範囲（app.py の行番号は移設前）

| 現 app.py | 移設後 |
|---|---|
| `scheduler_loop` (846) / `_sleep_or_stop` (968) / `_has_actionable` (930) | `Scheduler.run()` 以下 private |
| `run_pool` (240) / `_worker` (171) / `_in_flight_table` (302) / `_publish_pool` (312) | `Scheduler._run_pool/_worker` private |
| `_execute` (454) / `_run_with_wrapper_guard` (413) / `_WrapperInterrupted` (375) / `_wait_for_wrapper_loss` (386) | `Scheduler._execute` 以下 private |
| `_park_for` (523) / `_park_reason` (558) / `_park_reason_from_readiness` (606) / `PARK_MESSAGES` (631) | `Scheduler` park 群 private |
| `_on_progress` (650) / `_apply_progress` (672) | `Scheduler.forward_progress`（public） |
| `_leaf_for` (706) / `_filesystem_duplicate` (747) / `_find_duplicate` (758) / `_skip_reason` (783) / `_mark` (831) / `_warn_degraded` (805) | `Scheduler` private 補助 |
| `_announce_if_idle` (332) / `_post_notification` (357) / `_wrapper_problem` (953) | `Scheduler` private |
| `_drain` (1154) | `Scheduler.drain()` |

### app.py に残す

`download_root_from_format` / `validate_download_root` / `vendor_config_path` /
`create_app` / `_jobs_counts` / `_log` / `main` と lifespan。
結果として app.py は約 600 行になる。

### 進捗通知の責任（設計判断）

ワーカースレッドからの進捗
（`on_progress` → `call_soon_threadsafe` → WS 配信）は **Scheduler の責任** に含める。
`Scheduler.forward_progress` が `state.loop` への `call_soon_threadsafe` 封じ手を公開し、
lifespan の ripper 注入箇所は `_on_progress(state)` 相当を `Scheduler` 経由に付け替える。

### 定数

`IDLE_POLL_SECONDS` / `IDLE_READINESS_POLL_SECONDS` / `POST_JOB_POLL_SECONDS` /
`DRAIN_TIMEOUT_SECONDS` / `WRAPPER_GUARD_POLL_SECONDS` は `scheduler.py` へ移す。
`test_deployment.py:1104`（healthcheck `start_period` ≥ `startup_timeout`）が
コードから読む対象が変わらないか確認する。

### テスト方針

characterization test が主。移設前にスケジューラ系テスト
（`test_api_jobs.py:3312 行以降`）を緑で固定 → 移設 → 同一テストがそのまま緑。
新規テストは移設後 `test_api_jobs.py` / `test_state.py` の既存検証で欠ける分のみ追加。
「app.py にスケジューリング関数が残っていないこと」の静的検査は行わない。

### 完了条件

移設前後で `cd hub && uv run pytest` が全件緑。app.py に移設範囲の関数が存在しない。

---

## A — バッチ（parent_url）単位のグループ操作

### 設計判断

- **グループキーは `parent_url`**（全ジョブに既に書き込まれている列を公式化）。
  寝ている `parent_id` 列には触らない。
- 一括操作の雛形は既存の `POST /api/jobs/requeue`
  （`refused` 一覧を返す契約）の流儀に従う。
- TOCTOU 対策は既存パターン（UPDATE 文内ガード）をそのまま使う。

### ストレージ層（hub/hub/jobs.py）

- `JobStore.cancel_pending(parent_url: str) -> CancelResult{cancelled: list[int],
  refused: list[int]}` を追加。
  `UPDATE job SET status='cancelled' WHERE parent_url = ? AND status IN ('queued','waiting')`
  を行使し、`running` 行は `refused` として列挙して返す。read-then-write なし。
- 既存 `requeue` / `delete_finished` にオプション引数 `parent_url: str | None = None`
  を追加（`None` = 従来動作のまま。WHERE 節を条件付きで 1 つ足す）。

### API 層（hub/hub/api/jobs.py）

- `POST /api/jobs/cancel`（新規・セッション必須）。
  body `{"parent_url": "<str>"}` → `{"cancelled": [...], "refused": [...]}`。
  `refused` には走行中の `running` 行の id が入る
  （per-job cancel が running を 409 で拒否するのと同じ「走行中は触らない」哲学）。
- `POST /api/jobs/requeue` の body に `parent_url`（省略可）を追加。
  省略時は従来通り全件で後方互換。`refused` 契約は不変。
- `DELETE /api/jobs/finished?parent_url=`（省略可）。削除された id は
  `LeafRegistry.forget(job_id)` と `deleted` WS フレーム送信を従来通り実行。
- 変更された id は既存の per-id publish / `batch` フレームで WS 配信。
- `jobs.py` と `api/jobs.py` の「`parent_id` は nothing writes」コメントを、
  **グループは `parent_url` で問う**と明記する文に更新する。

### エラー処理

- `parent_url` 未提供／空文字は 400。
- 存在しない親 URL の cancel / requeue / 削除は HTTP 200 + 空リスト
  （「消すものがなかった」は失敗ではない。`refused` と同じ報告思想）。

### UI（/queue）

- 親 URL が表示されているグループに操作列を追加:「保留をキャンセル」「失敗を再キュー」。
  `app.js` の `data-action` ハンドラ対応として実装し、
  `test_web_contract.py` の affordance 対応検査へ追記する。

### テスト

- `test_jobs.py`: `cancel_pending` の原子性と `refused` 報告、
  `requeue`/`delete_finished` の `parent_url` フィルタ。
- `test_api_jobs.py`: 認証ルート表の更新（ルート 1 本増）、cancel → WS `batch` フレーム、
  400 系バリデーション。
- `test_web_contract.py`: affordance 追記。

---

## E — 履歴 + キューのエクスポート

### API（hub/hub/api/jobs.py）

- `GET /api/jobs/export?kind=history|queue&format=csv|json`（新規・セッション必須）
  - `kind=queue`: アクティブ（queued/waiting/running）全件、id 昇順（投入順）
  - `kind=history`: ターミナル（done/failed/skipped/cancelled）全件、id 降順
  - `format=csv`: BOM 付き UTF-8（Excel の日本語 album 名対策）。
    `Content-Disposition: attachment; filename="amd-hub-<kind>-<UTCタイムスタンプ>.<ext>"`
  - `format=json`: オブジェクト配列
  - 不正な `kind` / `format` は 400
- 上限なし（全量）。1 文の SELECT で取得する（ライブラリスキャンと同じ
  「キャッシュなし・毎回の実際読み取り」思想）。
- カラムはスキーマ全列。`skip_reason` は解決済みパスを文字列としてそのまま含む
  （パス変換は hub の仕事ではない）。
- 読み取り専用。書き込み経路には一切触れない。

### UI（/queue）

- 履歴表示ヘッダに「履歴ダウンロード (CSV / JSON)」、
  キューに「キューをダウンロード」の静的リンク（`<a href>`、JS 不要、
  セッション cookie がそのまま効く）。
- 静的リンクなので `data-action` 契約には乗らないが、
  `test_web_contract.py` の affordance 表へ追記する。

### テスト

- `test_api_jobs.py`: 認証ルート表更新（E でルート 1 本増）、kind/format バリデーション、
  CSV の BOM とヘッダ行、出力内容と実 DB 状態の一致（本物 DB パターン）。
- `test_web_contract.py`: ルート追加と CSP / セキュリティヘッダ自動追随の確認。

### やらないこと

ストリーミング、NDJSON、差分エクスポート、ファイル選択 UI（YAGNI）。

---

## F — 常設健康バナー

### 方針

新規 API なし。既存 `/api/status`（`problem` / `degraded_roots` / `per_root` / `queue` /
`pool`）をそのまま消費する。

### 表示

`/queue` ページ上部（`base.html` の共通リージョンに配置し `/library` にも表示）。
常時パネルは出さず、**異常があるときだけ**表示されるバナー群:

| 条件 | バナー |
|---|---|
| `problem == "no-account"` | 「Apple にログインしていません」→ ログイン panel へのリンク |
| `problem == "unavailable"` | 「wrapper が応答しません」→ 状態再読込ボタン |
| `degraded_roots` 非空 | 「読めないライブラリルート: <path>」 |
| `per_root` に 0 アルバムの root | 「<path> は空です（マウント抜けの可能性）」 |

正常時（`problem == null` かつ degraded 空かつ 0 album root なし）は描画しない。

### 更新タイミング

ページロード時に 1 回 `/api/status` を取得し、以後は WS 再接続時と
既存 `wrapper` WS フレーム受信時に再取得。キャッシュ期間・自動リトライは設けない
（1 発ずつの実状態）。

### アクセシビリティ／契約

- バナーは `role="status"`、重大なもののみ `role="alert"`
- コントラストは WCAG AA（`test_web_contract.py` のコントラスト検査に乗る）
- `app.js` に表示ロジックを追加し、既存 `data-action` / affordance 契約テストへ追記

### テスト

- `test_web_contract.py`: バナー markup と表示条件の静的検査
- API 変更ゼロのため `test_api_jobs.py` のルート表は変わらない

### やらないこと

診断 API 拡張、専用ページ、監視連携（Webhook 等）。

---

## 実装順序と各段階の完了条件

| 段階 | 完了条件 |
|---|---|
| C | 移設前後で全テスト緑。app.py に移設範囲の関数なし。app.py ≒ 600 行 |
| A | `POST /api/jobs/cancel` 実装・テスト緑。requeue/finished に parent_url フィルタ。UI 操作列 |
| E | `GET /api/jobs/export` 実装・テスト緑。UI ダウンロードリンク |
| F | 健康バナー表示・契約テスト緑 |

各段階の終了時に `cd hub && uv run pytest` 全件緑を確認してから次へ進む。
