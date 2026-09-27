# amd-hub 設計仕様書

- 日付: 2026-09-26
- 状態: 設計レビュー待ち
- 対象ワークスペース: `/home/m/apple-dl_extend`

---

## 1. 概要

`AppleMusicDecrypt`（TUI ダウンローダ）と `wrapper`（C++ の復号 HTTP バックエンド）を
単一の Web UI に統合する。ユーザーがブラウザから URL を投げるとダウンロードが走り、
ライブラリを管理でき、ブラウザで再生できる。デプロイは `docker compose up` のみ。

`AppleMusicDecrypt/src` はライブラリとして in-process で再利用し、既存コードの再実装は行わない。

---

## 2. ゴール / ノンゴール

### ゴール

1. URL を投げるとダウンロードが走る（アルバム / プレイリスト / アーティスト / 曲 / ミュージックビデオ）
2. **アルバムスコープ内のタイトル一致**で重複ダウンロードをスキップする。
   既存ライブラリは命名が不正規（`[ALAC|]artist/album/`、artist 直下の散在ファイル）
   なので、ダウンロード先を config から導出せず**発見**する（§7.3）
3. 複数のライブラリ root を横断して重複を検出する（本ワークスペースでは 69 GB の
   `downloads/` と 341 GB の外付け NTFS ドライブの 2 つ）
4. キュー内の重複（同じ曲を複数回投入）を 1 件に畳む
5. ライブラリを閲覧・検索・削除できる
6. **既存ライブラリの重複を一覧・グループ表示できる**（読み取り専用。§8.2）
7. ブラウザで再生できる
8. `docker compose up` で完結する

### ノンゴール（明示的に範囲外）

- 複数ユーザー・ロール・共有（単一ユーザー専用）
- Apple Music カタログの直接ストリーミング再生（再生対象はローカルにダウンロード済みファイルのみ）
- トランスコード結果の永続保存（再生時の一時変換のみ）
- アルバムをまたいだ**曲名のみ**による重複スキップ（`intro` が 6 アルバムに存在するため
  不可。§7.2.1 の実測に基づく）
- ISRC / adamId によるグローバル同一性管理
- **ミュージックビデオの重複スキップ** — §7 の重複判定は「アルバムスコープで、その
  中の曲名を照合する」前提の設計であり、ミュージックビデオにはアルバムが
  存在しない。MV は `mv.saveDir`（既定 `downloads/music-videos`）という単一の
  フラットなディレクトリに並ぶので、アルバムスコープの照合が成立しない。
  結果として **MV は常に再ダウンロードされる**。これは仕様どおりであり、この
  設計のユーザーは MV に対して「スキップ」表示を期待してはいけない。MV 向けの
  重複判定は別途の設計（フラットディレクトリ向けの別判定）が要る
- **重複ファイルに対する破壊的整理操作**（削除・hardlink 化・移動・改名）。
  341 GB の実データに対するファイル操作は危険であり、ユーザーはアプリ外で行う。
  検出と提示（§8.2）のみ提供する
- `AppleMusicDecrypt` / `wrapper` のアップストリームへの変更（新規ディレクトリのみを追加する）
- 既存 TUI の改修（`uv run python main.py` は引き続き動作する）

「アルバムをまたいだ曲名一致」と「グローバル同一性管理」を見送る根拠は §7.2.1 に
実測データとともに記載する。整理操作を見送る根拠は §8.2 に記載する。

---

## 3. アーキテクチャ（トポロジ）

単一コンテナ。Web アプリが `wrapper-lite-rootless` を子プロセスとして監督する。

```
docker compose up
  └─ amd-hub  (port 8080 のみ公開)
       │
       ├─ [build stage 1] wrapper/ → NDK r23b + cmake
       │      → /out/wrapper-lite-rootless, /out/rootfs/
       │
       ├─ [build stage 2] Python 3.13 + uv deps + hub/ + AppleMusicDecrypt/src + ffmpeg
       │
       └─ [runtime processes]
            amd-hub (FastAPI + HTMX)
              ├── spawn → wrapper-lite-rootless   (loopback:12340、外部公開しない)
              ├── import → Ripper  (creart ブートストラップ経由)
              └── SQLite: /data/hub.db  (job 状態のみ)
```

**QEMU / KVM / privileged / `/dev/kvm` は不要。** upstream の compose が使うのは rootless
user-namespace 方式のみで、`rootfs/` にチェックインされている Android バイナリをそのまま
実行する。

### 3.1 なぜ単一コンテナか

2FA  때문。upstream の `entrypoint.sh` は 2FA コードを**ファイル**
(`rootfs/data/2fa.txt`) から読むため、素の Web フォームでは入力できない。

**当初の「stdin にコードを書き込む」設計は実装できないので撤回する。** `lite/auth.cpp:64`
は 2FA の stdin 分岐を `isatty(STDIN_FILENO)` で守っており、spawn した子プロセスの
fd 0 はパイプなので `isatty` が false、その分岐には**絶対に入らない**。プロンプトは
そもそも表示されない。認証情報が stdin から読まれることもない（`--login user:pass` の
argv だけが入力口）。

正しくは**バイナリが実際に実装している機構**を使う。`auth.cpp` は `isatty` でない場合に
`<base-dir>/2fa.txt` を確認し、`20 × 3 秒 = 60 秒` ポーリングしてから `exit(1)` する。
よって supervisor は `Enter your 2FA code into <dir>/2fa.txt` のログ行を検出し、
コードが投稿されたら `<wrapper_base_dir>/2fa.txt` に書き込む。子はそれを読んで
**自ら削除する**。

この方式の利点: pty の割り当てや行規律の扱いが不要で、バイナリと compose entrypoint が
すでに用いている経路をそのまま再利用できる。

副次的な利点: 公開ポートが 1 つだけになる。12340 は loopback に留まる。Apple 認証情報が
コンテナ環境変数を通らない。**代償として子プロセスの argv には載る**（§11 参照）。

### 3.2 ビルドキャッシュ

Stage 1 は `COPY wrapper/` より**前**に NDK 取得と cmake 設定を置く。これにより
`hub/` の Python 依存だけが変わっても、NDK（約 1 GB）のダウンロードと cmake は
再実行されない。

---

## 4. リポジトリ構成

> **2026-09-27 改訂 — submodule 化した。** 以下は当初の記録であり、submodule 化の判断を
> 「公開可能性のためだけ」として保留した経緯を残す。撤回した判断は 1 つで、理由も 1 つ:
> 「Dockerfile がネットワークを要するので事前ビルド成果物を COPY する」という主張は
> Docker build にはネットワークがある（何况 NDK 692 MB を取得する）ため成立しない。判断を戻せば image 内の `wrapper-build` stage でビルドできるため、submodule 化と一体で
> 「fresh clone から image が完成する」状態にした。`AppleMusicDecrypt` を `8b609df`(v3)、
> `wrapper` を `c61dea9`(lite) に pin している。

```
/home/m/apple-dl_extend/        ← 既存の構成をそのまま使う（submodule 化しない）
  hub/                          ← 新規: Python web アプリ
  Dockerfile                    ← 新規: multi-stage
  compose.yaml                  ← 新規
  docs/superpowers/specs/       ← 新規: 本ドキュメント
  AppleMusicDecrypt/            ← 既存 clone（変更しない）
  wrapper/                      ← 既存 clone（変更しない）
```

submodule 化の利点は公開可能性のみ。Dockerfile は `COPY AppleMusicDecrypt/ ...` で
動作するため、git 手術ゼロでこの構成が成立する。`~/.config/opencode/AGENTS.md` の
「git 操作は勝手にしない」に従い、submodule 化は将来公開する場合の別タスクとする。

---

## 5. コンポーネント

各モジュールは「1 つの責務・明示的な境界」。**`AppleMusicDecrypt` の `src/` に触れるのは
`ripper_host.py` だけ**。`AppleMusicDecrypt` を submodule 化しても壊れるのはそこ 1 ファイル
で済む。

| モジュール | 責務 | 依存 |
|---|---|---|
| `config.py` | env → 設定 (pydantic) | — |
| `auth.py` | 単一パスワード認証 + セッション cookie | config |
| `wrapper_supervisor.py` | wrapper の起動 / 停止 / 健全性 / 2FA ログイン | — |
| `resolver.py` | URL → リーフ列（adamId / title / codec） | `src.api.WebAPI`, `src.models` |
| `dedup.py` | アルバムスコープ重複判定（FS 走査） | ファイルシステム |
| `jobs.py` | ジョブ保存・スケジューラ・再試行・キャンセル | dedup, ripper_host |
| `ripper_host.py` | **creart 登録と `Ripper` 適応（唯一の seam）** | `src.*` |
| `library.py` | FS ベースのライブラリ列挙・検索 | ファイルシステム, TTL キャッシュ |
| `events.py` | SSE ブローカー（ログ行 + ジョブ状態） | — |
| `api/*.py` | ルータ（リソース別） | 上記 |

### 5.1 ダウンロード制御: コンテナ URL をリーフに平坦化

`rip.py` の album / playlist 反復処理には触らない。オーケストレータが URL を**先に展開**し、
リーフ（曲またはミュージックビデオ）単位のジョブへ分解し、各リーフを
`Ripper.rip_song()` / `MVRipper().rip()` に渡す。

理由:

- キュー重複排除が `job` の一意インデックス 1 つで済む
- 進捗がリーフ単位（UI が欲しがる粒度）
- TUI 既存のツリー表示（親 = アルバム、子 = 曲）と 1:1 で一致
- メタデータ取得の API 呼び出しは `_get_song_info_cached` の in-flight dedupe に乗り、
  `rip_song` 側が同じキャッシュを引く → 二重取得ゼロ

---

## 6. 永続状態

**永続化するのは `job` のみ。** ファイルシステムの状態は永続化しない。

```sql
CREATE TABLE job (
  id           INTEGER PRIMARY KEY,
  url          TEXT    NOT NULL,
  url_type     TEXT    NOT NULL,   -- song|album|artist|playlist|music-video
  adam_id      TEXT,
  title        TEXT,               -- ログ表示用のみ。照合には使わない（§7.3）
  codec        TEXT    NOT NULL,
  language     TEXT,
  force        INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL,   -- queued|waiting|running|done|failed|skipped|cancelled
  skip_reason  TEXT,
  parent_id    INTEGER REFERENCES job(id),
  progress     REAL,
  bytes_done   INTEGER,
  bytes_total  INTEGER,
  error        TEXT,
  created_at   TEXT    NOT NULL,
  started_at   TEXT,
  finished_at  TEXT
);

-- キュー内重複排除。
-- キー = (adam_id, codec)。language と force はキーに含めない。
-- 'waiting'（トークン待ちで一時停止中のジョブ）も対象に含まれるのは、
-- 停止中に同じ曲を二重で走らせないため。これは意図的な挙動。
CREATE UNIQUE INDEX job_active_dedupe
  ON job(adam_id, codec)
  WHERE status IN ('queued','waiting','running');
```

`job` はファイルシステムの写像ではなく運用状態なので、§7 の整合性問題とは無関係。

`pragmas: journal_mode=WAL, busy_timeout=5000`。書き込みは `jobs.py` の 1 タスクに集約し、
競合を低く保つ。

---

## 7. 重複判定（ダウンロード）

### 7.1 原則

**ファイルシステムを唯一の真実とする。ステートレス。永続インデックスを持たない。**

ライブラリ側のフォルダが自由に移動・改名・削除される前提のため、ダウンロード判断に
永続状態を使うと、外部で動いたファイルと DB の不整合（実在しないファイルを
「存在済み」と誤判定してスキップする、またはその逆）が起きる。よって判断は全て
リクエスト時に実 FS を見る。

### 7.1.1 実測: 走査は十分安い

本ワークスペースの实物ライブラリ `/run/media/m/1A5E05A75E057D2F/Music`（外付け NTFS、
341 GB）での実測値:

| 指標 | 実測 |
|---|---|
| ディレクトリ数 | 4,367 |
| オーディオファイル数 | 15,317（うちオーディオ 10,184） |
| アルバムディレクトリ数（直下に音源を持つもの） | 3,670 |
| `os.walk`（`stat` のみ、タグ読みなし） | **0.06 秒** |

したがってリクエストごとに全走査してよい。**構造のキャッシュと陈腐化（staleness）問題は
発生しない。** キャッシュが正当化されるのはタグ読み込み（§8）のみ。

### 7.2 ライブラリ root は複数可、命名は正規形を前提としない

実測した 2 つのライブラリは**規約が異なる**:

| ライブラリ | 構造 | `dirPathFormat` との一致 |
|---|---|---|
| `AppleMusicDecrypt/downloads/`（212 アーティスト） | `artist/album/` | 一致する |
| 外付け NTFS `Music/` | `[ALAC\|Atmos/]artist/album/`。ただしアーティスト直下に散在する音源ファイルがあり、アルバムディレクトリ直下にも子ディレクトリが混在する | **一致しない** |

よって **2 つの設計判断が必要**:

1. `AMD_LIBRARY_ROOTS` は**複数指定可**（既定は上記 2 箇所）
2. ダウンロード先は `dirPathFormat` から**導出する**のでなく、既存ライブラリから
   **発見する**（§7.3）

### 7.2.1 実測: 実ライブラリの重複は「アルバム名重複」と「曲名重複」で性格が異なる

外付け NTFS ライブラリでの実測:

| 指標 | 実測 |
|---|---|
| 同一のアルバムディレクトリ**名**が複数箇所に存在する | **223 名称 / 238 個の余剰ディレクトリ**（Task 3 実測。設計時の 218/233 は下限） |
| 正規化タイトルが 2 つ以上のアルバムディレクトリに存在する | **1,206 / 8,721 タイトル（13.8%）**（Task 3 実測） |

218 件の内訳は 2 種類ある。**両方を「重複」として検出する必要があるが、
片方だけ検出すると実効性が大きく落ちる**:

| 種別 | 実例 | 検出方法 |
|---|---|---|
| **A. 同一リリースの複数配置**（真の重複） | `Hush a by little girl` が `ALAC/鎖那/` と `new-dl/鎖那/` の両方にある | アーティスト + アルバム名で一致 |
| **B. コラボのファンアウト**（同一リリースが参加アーティストの folder にそれぞれ置かれる） | `らぶふぉーゆー - Single` が `ALAC/EmoCosine/`, `ALAC/ころねぽち/`, `ALAC/メガミノウタゲ/` にある | アルバム名だけで一致 |

B は Apple Music がクレジット参加アーティストごとにリリースを分配する仕様によるもので、
論理的な重複ではないがユーザーにとっては「同じ曲ファイルが 3 個ある」状態であり、
再ダウンロードは無駄である。

逆に、**タイトル一致だけでグローバルに潰すのは不可**。実測で以下の問題が確認されている:

| タイトル | 配置数 | 問題 |
|---|---|---|
| `intro` | 6 | 完全不同のアルバムに同名トラック。グローバル一致だと誤って skip する |
| `escapism` | 6 | 同上 |
| `mu` | 6 | 同上 |
| `yoake` | 6 | 同上 |
| 空文字列 | 6 | 同上（アルバムに無題の PARTS のようなトラックを含む） |

したがって**「アルバムをスコープにして、その中でのみ曲名を照合する」**という
2 段構えが必要であり、これが §7.3 の設計の根拠である。

### 7.3 アルゴリズム（ジョブ実行時、リーフ 1 曲ごと）

**2 段構え: ①アルバムディレクトリを発見してスコープを決める ②そのスコープ内で曲名を照合する。**

```
── Step 1: ライブラリ走査（リクエストごと、0.06 秒）────────────────
1a. AMD_LIBRARY_ROOTS 配下を再帰走査（stat のみ、タグは読まない）
1b. 「アルバムディレクトリ」= オーディオファイルを 1 つ以上直接持つディレクトリ
1c. 各アルバム dir について、正規化タイトル集合を作る
      titles[dir] = { normalize(f) | f in listdir(dir), ext.lower() in AUDIO_EXTS }
    ※ 入れ子には降りない。アルバム dir 自身がスコープの単位（§7.6）

── Step 2: 候補アルバムディレクトリの発見 ────────────────────────
2a. normalize(album_name, strip_track_prefix=False) を計算（§7.5。曲名と共通の変換）
    ※ フラグを落とすとアルバム名の先頭数字まで除去され、同一アルバムを束縛する
2b. basename がそれと一致するアルバムディレクトリを全 root から集める
2c. dedup.artist_scope で候補を絞る（§7.4）

── Step 3: 曲名照合 ────────────────────────────────────────────
3a. titles[dir] のうちいずれかに normalize(track_title) が含まれていれば一致
3b. 一致あり → status='skipped'
       skip_reason = 'duplicate:<一致した相対パスの一覧>'
3c. 一致なし → rip_song() を実行
```

一致が 1 つもない場合はそのままダウンロードする。アルバムディレクトリが 1 つも
発見できない場合もダウンロードへ進む（ダウンロード自体で新規作成されるため）。

### 7.4 `artist_scope`: アルバム名だけでよいか、アーティスト名も要求するか

Step 2c の判定モード。config で切り替え可能。

| 値 | 条件 | 捕まえるもの | 漏らすもの | 誤検出 |
|---|---|---|---|---|
| `loose`（**既定**） | アルバム名のみ一致 | A と B の両方 | — | 同一アルバム名の別アーティスト版が存在する場合のみ |
| `strict` | アルバム名 + アーティスト名の一致が必要 | A のみ | B（コラボ 2 つめ以降を再ダウンロードしてしまう） | ほぼなし |

**既定を `loose` とする理由**: §7.2.1 の実測 218 件の大半が A と B であり、
`strict` では B が体系的に漏れる。一方 `loose` の誤検出は
「アルバム名が完全に一致し、かつその中に正規化タイトルが一致するトラックがある」
場合のみであり、同一名称の別アルバムが存在することはかなり稀である。

さらに、誤検出が起きた場合にユーザーが裁定できるよう、**一致した実パスを
`skip_reason` に必ず含める**。UI は「既に存在: `ALAC/星宮とと/POP-AID/…`」と表示する。

### 7.5 正規化（両側を同一変換で揃える）

**比較の基準は曲名タグではなく「ファイル名」であり、アルバム側は「ディレクトリ名」である。**
両段で共通の変換関数を 1 つ定義する。

`normalize(name) -> str`（曲名・アルバム名共通、1 関数）:

**順序が重要。** 文字列の畳み込みをすべて先に済ませてから、構造の除去を行う。
先に構造を除去すると、全角のトラック番号や全角の拡張子を見落とす。

1. Unicode **NFKC** 正規化
   - この関数の目的は「同じタイトルかどうか」の判定であり、日本語のライブラリには
     全角/半角の差が実際に含まれるため。`ＡＢＣ Title` と `ABC Title` は同一視される
     なければならない。
   - `str.casefold()` は全角 `Ａ` を `ａ` に倒すだけで半角 `a` にはしない。幅を畳むのは
     NFKC の役割であり、NFC では畳まらない。
2. `str.casefold()`
   - 全角英字も半角に倒すので、以降 `AUDIO_EXTS`（全て小文字の定数）との membership
     判定が自然に大文字小文字を区別しない。
3. **既知のオーディオ拡張子のみ**を除去する（`AUDIO_EXTS` に含まれる場合だけ）
   - **`pathlib.PurePath` を使ってはいけない。** NFKC が `／`(U+FF0F) を `/` に
     畳むため、PurePath のパス分割がこれを区切りと認識し、
     `01. A／B.m4a` が `b` まで潰れて `01. B.m4a` と衝突する。`／` は日本語で
     普通に使われる記号である。
   - 正しくは `rpartition(".")` で最後のドットを分けて**拡張子名だけ**を
     `AUDIO_EXTS` と比較し、含まれないなら**元の文字列をそのまま返す**。
   - `1-01 Caribbean Blue.m4a` → `1-01 Caribbean Blue`
   - `Song. Pt. 2.m4a` → `Song. Pt. 2`
   - `01. Artist - Title` → `01. Artist - Title`（`m4a` が無いので除去しない）
   - **「最後のドット以降を全部落とす」実装にしてはいけない。**
     `pathlib.PurePath(x).stem` は `01. Artist - Title` を `"01"` まで潰してしまうが、
     これは `playlistSongNameFormat` の既定
     `{playlistSongIndex:02d}. {artist} - {title}` の実際の描画結果であり、
     プレイリストから落としたライブラリの全ファイルがトラック番号だけに潰れる。
4. 冒頭のトラック番号を除去する（最大 2 グループ）
   - パターン: `^(?:\d{1,3}(?:\s*[.\-_]\s*|\s+)){1,2}`
   - **区切りは貪欲な `[\s._-]+` run にしてはいけない。** NFKC が `…`(U+2026) を
     `...` に畳むため、その run はタイトル先頭のドットまで吞噬し、実在する
     `13. …to mo da ti _.m4a` が `to mo da ti _` に化ける。
     `04. ...And Then` は `And Then` と衝突する。
   - 区切りは「1 個の `.`/`-`/`_` を前後の空白で囲んだもの、または連続した空白」にする。
     これならドットが 2 個連続しても食べない。
   - 例: `1-01 Title` → `Title` / `01 Title` → `Title` / `1. Title` → `Title`
   - 3 グループ以上は先頭 2 グループのみ除去する（`1-01-02 Title` → `02 Title`）。
     `songNameFormat` の既定 `{disk}-{tracknum:02d} {title}` は 2 グループ、
     `playlistSongNameFormat` の既定 `{playlistSongIndex:02d}. {artist} - {title}` は
     1 グループなので 2 で足りる。
   - `\d{1,3}` は 4 桁の年（`1979 - Song`）にマッチしないので**除去されない**。
     これは意図的で、`1979 - Song` と `01-1979 - Song` が一致するようになる。
   - **ディレクトリ名にはこの工程を適用しない。** アルバム名に数字が含まれるのは
     日常的にある（`4pi`, `1st EP`）。§7.3 の Step 2a では
     `strip_track_prefix=False` を渡す。
5. 連続する空白を 1 つに圧縮し、前後の空白を除去する
6. 上記の処理後に英数字が 1 つも残っていなければ空文字を返す
   （無題トラックの正規化結果を空文字として表現し、Task 4 が
   空キーでは skip しないことを要求できるようにする）

**除去してはいけないもの**（これらはアルバム識別の一部）:

- ` - Single` — Apple Music のシングル表記。実測で大多数のアルバムディレクトリが
  `<track> - Single` 形式であり、これが外れるとアルバムが束縛される
- ` [Deluxe]` / ` (Deluxe)` / ` (Explicit)` — 別アルバムとして区別する
- ` - EP`, ` - album`, ` (feat. …)` — 実ライブラリで実際に使われている

### 7.6 明示的に受け入れる結果

以下は意図的な設計判断であり、バグではない:

| 事象 | 結果 |
|---|---|
| 別アルバムに同名トラックがある（`intro` × 6 など） | **スキップしない**。Step 1c でアルバムスコープに閉じ込めているため。要件通り |
| 同一アルバムに同名トラックが 2 曲ある | 2 曲目以降がスキップされる。許容 |
| 同一アルバムに codec 違い（ALAC と AAC）が併存 | 拡張子を無視するため重複扱い |
| 同一アルバムに `-l` 言語違いが併存 | 同一 `dirPathFormat` でレンダリングされるため一致し、重複扱い |
| コラボが参加アーティスト folders にファンアウト | `loose`（既定）で skip される |
| 同一アルバムが `ALAC/` と `new-dl/` の両方に存在 | いずれの配置も scope に入るので skip される |
| `dirPathFormat` を変更してライブラリが移動した | 再インデックス不要。毎リクエストで走査するため即追従 |
| ライブラリをアプリ外で改名 / 移動 / 削除 | 判定が即座に追従する（ステートレスのため） |
| タイトルが数字で始まる曲 | 3 桁以下（`01 Title`）なら先頭が除去される。4 桁（`1979 - Song`）は除去されず `1979 - song` のままなので、`01-1979 - Song` と一致する |
| タイトルが `<数字><区切り><数字>` で始まる曲 | 区切りに `_` を含むため先頭が除去される。実測 6 ファイル（`04. 3_00 AM.m4a` → `00 am`）。**照合の両側が同じ 1 回の呼び出しを通るので一致は成立するが、正規化は冪等でない**（2 回呼ぶと結果が変わる）。Task 9 はタグ名ではなくレンダリング済みファイル名を 1 回だけ通すこと |

エスケープハッチ: `force` フラグで §7.3 を完全にバイパスする。

---

## 8. ライブラリ閲覧

永続インデックスなし。§7.1.1 の実測（0.06 秒）により、**構造の列挙はリクエストごとに
行ってよい**。TTL キャッシュも陈腐化も不要。

- **root は複数**（`AMD_LIBRARY_ROOTS`）。各 root を走査して 1 つのリストに束ねる
- 一覧: 各 root から再帰 walk、`stat` のみ（タグは読まない）
- **artist / album は `dirPathFormat` から導出しない。** 実測した NTFS ライブラリは
  `[ALAC|]artist/album/` という別規約であり、導出すると不正になる。
  替わりに以下優先順位で**ディレクトリ構造から**決める:
  1. 親ディレクトリがあれば artist = 親の basename、album = 自身の basename
  2. 親が無ければ（root 自身がアルバム扱いになる場合）artist / album は不明
     （`Unknown`）として表示

  **フォーマットバケット（`ALAC` / `Atmos`）の判定は入れない。** 実測では
  実効的な影響が無く、上の 1 と「bucket かどうかを見ずに親を取る」は常に同じ
  結果を返す。`ALAC/TEMPLIME/POP-AID` でもどちらでも artist は `TEMPLIME` で
  あり、判定が結果を変えるのは `ALAC/Atmos/Album` のような入れ子の bucket
  だけで、そこでは codec ディレクトリを artist として返してしまう。入れ子
  bucket は実測 0 件。
- ISRC / UPC / bit depth / sample rate などパスに含まれない項目は**タグ読み**:
  詳細画面を開いた時、またはバックグラウンドで事前読み込みする時にのみ行う。
  **ここだけキャッシュする**（TTL 300 秒、mtime で失効）
- アルバム横断の重複グループ表示は行わない（§2 ノンゴール）。
  ただし §7 の `loose` 判定がどのパスと一致したかは skip 時に常に提示する
- `.part` ファイル（実測 160 個）は中断DLの残骸なので一覧から除外する

ファイルは ID で配信する。クライアントが生の path を送ることは許さない。
`id` = (root index, 相対 path) の `blake2b` 短縮 hex。サーバ側 walk 結果から
解決する。これにより path traversal が構造的に不可能になる。

### 8.1 付け外しできるドライブ

外付け NTFS ドライブは**マウントされていない可能性がある**。`AMD_LIBRARY_ROOTS` の
各要素について、起動時と `/api/status` の応答時に到達可否を検査し、
到達不能なものは **degraded（縮退）状態**として警告する。承認した root が 1 つも
無い場合のみ、ダウンロードと重複判定を**停止**し、UI に明示的なエラーを出す
（黙って重複判定を無効化しない）。

### 8.2 重複レポート（読み取り専用）

§7.2.1 の実測と同じ走査結果から、既存ライブラリの重複を**提示だけ**する。
**ファイルには一切触らない。**

2 種類のグループを返す:

| グループ | 定義 | 実測個数 |
|---|---|---|
| `duplicate_album_dirs` | 同じアルバムディレクトリ**名**が 2 箇所以上 | 218 名称 / 233 余剰 dir |
| `duplicate_titles` | 同じ正規化タイトルが 2 つ以上のアルバム dir に存在 | 1,200 / 8,732 タイトル |

各グループには**実パスの一覧**を必ず含める。ユーザーが自分で判断できるよう、
UI は次を提供する:

- グループごとの全パスをコピー可能なテキストとして提示
- 重複している容量の概算（グループ内のうち 1 個として数える）
- **「変更しない」ボタンを置く**（削除・hardlink・移動・改名を持たない）

**なぜ操作を提供しないか**: 341 GB の実データに対してユーザーが意図しない
ファイル操作が走る后果が、得られる便利さに対して重大に大きい。
また NTFS は Unix 権限と hardlink セマンティクスが完全ではないため、
`os.link` 前提の実装が挙動を预测しにくい。

検出ロジックは §7.3 Step 1 の走査結果を再利用し、走査を二重に実装しない。

---

## 9. API サーフェス

すべて `/api` 配下。JSON。セッション cookie 認証。

```
POST   /api/auth/login              {password} → セッション cookie
POST   /api/auth/logout
GET    /api/auth/session

GET    /api/health                  監視用。認証不要
GET    /api/status                  wrapper 状態 / regions / キュー概要
POST   /api/wrapper/start           POST /api/wrapper/stop      POST /api/wrapper/restart
POST   /api/wrapper/login           {username, password} → 2FA challenge
POST   /api/wrapper/login/2fa       {code}

POST   /api/jobs                    {urls[], codec, language, force}
                                       → {created[], skipped[], deduplicated[]}
GET    /api/jobs                    ?status=&parent=
GET    /api/jobs/{id}
DELETE /api/jobs/{id}
POST   /api/jobs/{id}/retry
GET    /api/jobs/stream             SSE: job 状態 + ログ行 + 転送速度

GET    /api/library/tracks          ?q=&artist=&album=&codec=
GET    /api/library/albums
GET    /api/library/artists
GET    /api/library/duplicates       ?kind=album_dirs|titles   読み取り専用（§8.2）
GET    /api/library/files/{id}      詳細（タグ読み）
GET    /api/library/files/{id}/stream   byte-range 音声配信
DELETE /api/library/files/{id}
POST   /api/library/scan            walk キャッシュ破棄
GET    /api/cover/{id}              アートワーク proxy
```

---

## 10. エラーハンドリング

| 事象 | 対応 |
|---|---|
| wrapper 停止 | ジョブは即 `failed(error=wrapper_unavailable)`。UI に再接続バナー。supervisor が指数バックオフで 3 回まで自動再起動し、以降は手動 |
| トークン期限切れ | `/status` が regions 空 → UI をログインへ誘導。**走行中ジョブは fail せず `waiting` へ入れ、ログイン後に再開** |
| 2FA タイムアウト | challenge の TTL は**既定 60 秒** — バイナリのポーリング窓（`20 × 3 秒`）に合わせる。60 秒を超えた投稿は子に読まれない |
| ダウンロード失敗 | `AppleMusicDecrypt` 既存の retry 設定（`retryTime` / `maxWaitTime`）を使い、ジョブ単位で再試行。尽きたら `failed` としてエラーを画面に出す |
| 整合性チェック失敗 | 既存 config `failedSongNotPassIntegrityCheck` に対応。専用終端状態 + force 再ダウンロード action を提供 |
| ライブラリ walk 失敗 | サービス提供は止めない。該当画面にエラーを表示し、ダウンロードは継続 |
| SQLite lock | WAL + `busy_timeout=5000`。書き込みは 1 タスクに集約 |
| 子プロセスの異常終了 | supervisor の watcher が**実行時**に検出し、指数バックオフで上限（既定 3）まで自動再起動する。上限に達したら `failed` としてユーザー通知し、自動の無限再起動は行わない。`CLONE_NEWPID` により payload は入れ子 PID 1 になるので、停止はプロセスグループではなく launcher への signal を行う |
| 2FA の投稿が 60 秒窓を過ぎた | 子は 60 秒で `exit(1)` する。UI は 60 秒の期限を表示し、期限切れは「code expired」として再ログインを促す |

---

## 11. セキュリティ

| 項目 | 方針 |
|---|---|
| アクセス制御 | 単一パスワード。`AMD_PASSWORD` 環境変数。ハードコード禁止 |
| パスワード比較 | `secrets.compare_digest`（タイミング攻撃対策） |
| ログイン試行 | レートリミット（既定 10 回 / 5 分） |
| セッション | HMAC 署名済み cookie。`HttpOnly`, `SameSite=Lax`, TLS 時のみ `Secure` |
| Apple 認証情報 | ログインフォームから受け、**メモリ上にのみ保持**する。環境変数にも DB にもログにも残さない。
  **ただし子プロセスの argv（`--login user:pass`）に載る** — バイナリが受け取る入力が
  argv だけだからである。`/proc/<pid>/cmdline` は同一 UID のプロセスから読めるため、
  容認ではなく**意図的な露出**として記録する。コンテナ内のサービスプロセスは本
  アプリだけなので露出は自行に閉じている。pty で argv を避けるには `auth.cpp` の改変が
  必要で、upstream への変更は §2 のノンゴール |
| ファイル配信 | path traversal 対策: クライアント path 不可。ID から解決し、`os.path.realpath` が library root 配下であることを検証する |
| バインド | 既定 `0.0.0.0:8080`（LAN から到達するため）。`AMD_BIND` で変更可 |
| 通信 | wrapper は loopback のみ。`127.0.0.1:12340` は publish しない |
| 入力検証 | URL はスキーム allowlist（`https` のみ）。外部入力をすべて検証する |

---

## 12. テスト戦略

`AppleMusicDecrypt` には Python テストが無く、`.gitignore` が `/tests/` を無視する。
**`hub/` は新規ディレクトリ配下なのでテストを commit できる。**

| 種別 | 内容 | 価値 |
|---|---|---|
| 単体 | `dedup.normalize()` の文字列処理（純関数） | **最高。§7.5 は境界値が多い仕様** |
| 単体 | `dedup` のアルバムディレクトリ発見 + 曲名照合（tmp ディレクトリ + 実ファイル構成） | **最高。アルバムスコープと入れ子の境界** |
| 単体 | `artist_scope` の `loose` / `strict` の分岐 | 高。誤検出と漏えんの両方を固定する |
| 単体 | `resolver.py` の URL → リーフ展開（`WebAPI` を fake） | 高 |
| 単体 | `jobs.py` のキュー重複インデックス（in-memory SQLite） | 高 |
| 統合 | fake `Ripper` でのジョブライフサイクル（ネットワーク・wrapper 不要） | 高 |
| 統合 | `library.py` の複数 root 走査 + タグ読みキャッシュ失効 | 中 |
| **回帰** | **実ライブラリ形状のフィクスチャ**（§7.2.1 の実測を模したツリー） | **最高。設計の根拠が実データなので、形が崩れると検出が壊れる** |
| 契約 | 実バイナリでの supervisor 起動 / 停止 / 2FA（`slow` マーク・任意実行） | 中 |
| 手動 E2E | `compose up` → ログイン → 1 曲 DL → 整合性確認 → 2 曲目が skip → 再生 | 最終確認 |

### 12.1 回帰フィクスチャ（実ライブラリから採ったケース）

`/run/media/m/1A5E05A75E057D2F/Music` の実測で観察された形を、最小の tmp ツリーに
再現する。これらはすべて**実際に存在した形**であり、設計中の観察記録である:

| ケース | 実パス（短縮） | 期待動作 |
|---|---|---|
| 同一リリースの複数配置 | `ALAC/鎖那/Hush a by little girl/` と `new-dl/鎖那/Hush a by little girl/` | skip（種別 A） |
| コラボ ファンアウト | `ALAC/{EmoCosine,ころねぽち,メガミノウタゲ}/らぶふぉーゆー - Single/` | `loose` で skip（種別 B） |
| フォーマットバケット違い | `ALAC/TEMPLIME/POP-AID/` と `TEMPLIME/POP-AID/` | skip。バケット有無に影響されない |
| íl互換の一般名 | `intro` が 6 アルバムに存在 | **skip しない**（アルバムスコープが閉じている） |
| artist 直下の散在ファイル | `Nyarons/A.flac`（artist dir 直下） | アルバム dir として扱う（孫が無くても scope になる） |
| 子ディレクトリ混在 | `TEMPLIME/Escapism/` と `TEMPLIME/HIKO.flac` が同居 | 両方を scope に含める |
| ` - Single` アルバム | `<track> - Single/` | アルバム名の一部なので除去しない |
| 曲名先頭の数字 | `1979 - Song.m4a` | 仕様どおりの正規化結果になる |
| 未マウント root | root が存在しない | degraded。`loose` の誤検出は出ないが、対象 root の分は検出できない |

Phase 1 の受け入れ基準:

- 同一アルバムに 2 度 DL を要求すると、2 回目が `skipped` になり、`skip_reason` に
  一致した実パスが含まれる
- `songNameFormat` を `{title} - {album}` に変えた場合でも自己一致が成立する
- 別アルバムの同名トラック（`intro` 相当）は skip されない
- 2 つの root をまたいで重複が検出される

---

## 13. フェーズ計画

| Phase | 成果物 | 完了条件 |
|---|---|---|
| **1** | 骨格、認証、wrapper 監督、2FA ログイン、resolver、ジョブ（キュー重複 + アルバム重複 skip）、キューの HTMX UI | `compose up` でログインでき、URL を投げると 1 曲 DL され、同一アルバムの再 DL が `skipped` になる |
| **2** | FS ベースのライブラリ閲覧・検索・削除、複数 root 集約、重複レポート（読み取り専用） | 2 つのライブラリ（69 GB + 外付け 341 GB）が横断して一覧・検索でき、削除でき、重複グループを実パス付きで提示できる |
| **3** | 再生（byte-range + カバー proxy + トランスコード）、ライブラリからキュー投入 | ブラウザで再生できる |

各フェーズは個別の spec → plan → 実装サイクルを持つ。**本 spec は全体 architecture と
フェーズ 1 の詳細を規定する。実装プランは §16 の手順 2〜6（フェーズ 1）を対象とする。**

### 13.1 フェーズ 3 の既知の制約（重要）

**ブラウザは ALAC も EC3 / Atmos も再生できない。** Safari のみ ALAC 対応、
Chrome / Firefox は両方非対応、EC3 / Atmos はどのブラウザでも非対応。

したがって再生にはトランスコード層が必須になる:

- ffmpeg を image に追加
- ブラウザが再生できる形式はそのまま配信する（帯域・CPU の節約）
- 再生できない形式のみオンデマンドで ffmpeg → opus へ変換する
- 非対応形式は明示的な「非対応」状態を UI に出す（黙って失敗させない）

繰り返しトランスコードによる劣化を避けるため、変換結果は `/data` の一時領域に置き、
ライブラリ本体は書き換えない。

---

## 14. compose.yaml

```yaml
services:
  amd-hub:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
    volumes:
      - ./AppleMusicDecrypt/downloads:/library/a        # 既存 69 GB（dirPathFormat 準拠）
      - /run/media/m/1A5E05A75E057D2F/Music:/library/b  # 外付け NTFS 341 GB（未マウントでも起動する）
      - hub-data:/data                                  # Apple token DB + hub.db
    environment:
      AMD_PASSWORD: ${AMD_PASSWORD:?set AMD_PASSWORD in .env}
      AMD_BIND: 0.0.0.0
      AMD_LIBRARY_ROOTS: /library/a,/library/b
    security_opt:
      - seccomp=unconfined            # rootless user namespace 用（upstream compose と同様）
      - systempaths=unconfined       # 必須。spike で実証済み（§14.1）
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/api/health"]
      interval: 30s

volumes:
  hub-data:
```

`privileged` / KVM / QEMU / 12340 の publish は不要。`cap_add` も不要（§14.1 の対照実験参照）。

### 14.1 `systempaths=unconfined` は必須（spike で実証）

`docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md` の実測結果。
**§14 を最初に書いた状態（`seccomp=unconfined` のみ）では子プロセス起動は失敗する。**

| 構成 | 結果 |
|---|---|
| `seccomp=unconfined` のみ | `mount proc failed: Operation not permitted` |
| `seccomp=unconfined` + `cap_add: [SYS_ADMIN]` | **同じく失敗**。cap では直らない |
| `seccomp=unconfined` + `systempaths=unconfined` | **成功** |

原因: Docker は既定で `/proc` 配下を 12 パスで over-mount する。launcher は
`unshare(CLONE_NEWUSER|CLONE_NEWNS|CLONE_NEWPID)` 後に自前で `proc` を mount するが、
カーネルの `mount_too_revealing()` チェックが「既存 mount に隠された部分がある」ため
拒否する。`systempaths=unconfined` はその over-mount を解除する。
これは権限不足ではなく**視認性の判定**なので、`cap_add` では直らない（対照実験 B で確認）。

**セキュリティ上のトレード（意図的に受け入れる）**: `systempaths=unconfined` は
hardening を実際に弱める。`/proc/{bus,fs,irq,sys}` の read-only マスクが外れ、
`/proc/{kcore,keys,timer_list,interrupts}` の `/dev/null` bind が解除される。
ただしこのコンテナは `CAP_SYS_RAWIO` を持たないため `/proc/kcore` は実際には読めない。
`privileged` ではなく `cap_add` なしで、`publish` するポートは 8080 のみである点を
相殺とみなす。より強い隔離が要る環境では §3.1 の fallback（2 コンテナ構成）に退避する。

**要検証（実装時に確認）**:

- `healthcheck` の `curl` を runtime image に含める必要がある。
- 起動判定はログ文字列で判定せず、**`GET /status` が 200 を返すこと**で判定する
  （spike では実際の ready まで 9.8〜18.4 秒かかった。20 秒の余裕を確保する）。
- `CLONE_NEWPID` により payload の `lite` は**入れ子 PID namespace の PID 1** になる。
  停止時はプロセスグループではなく **launcher に signal を送る**（§10 参照）。

---

## 15. リスクと未解決事項

| リスク | 影響 | 対処 |
|---|---|---|
| rootless launcher を子プロセスとして起動できない | **解消済み** | spike で実証。`systempaths=unconfined` が要 (§14.1)。不要なら 2 コンテナ構成へフォールバック |
| ライブラリ走査のレイテンシ | 実用上ない | 実測 0.06 秒（4,367 dir / 10,184 file、タグ読みなし）。リクエストごとに走査してよい |
| 外付けドライブが未マウント | root 1 つが縮退 | §8.1 の通り degraded として警告。downloads 側は残るため.downloadは継続できる |
| アルバムディレクトリ発見の誤検出（同名アルバムが別アーティスト版） | 誤 skip | `artist_scope=strict` に切れば消える。`skip_reason` に一致パスを必ず表示してユーザーが裁定できるようにする |
| 同一アルバムに実在する同名の重複タイトルで誤判定 | 1 曲分のダウンロード漏れ | 仕様どおりの許容範囲。`force` で回避できる |
| temari のプラットフォーム不一致 | 起動失敗 | `check_dep()` が `temari._platform_key()` を出力する。`uv sync` を先に実行する |
| library mount が書込不能 | ダウンロード失敗 | 起動時に library root の書込可否を検査し、UI にも表示する |
| ディスク容量 | 枯渇 | ダウンロードは `/library` に書く。空き容量を起動時と UI の双方で警告する |

---

## 16. 実施順序

1. **Spike**: 子プロセスとしての `wrapper-lite-rootless` 起動（rootless userns + seccomp のみ）を確認
2. `hub/` 骨格 + multi-stage Dockerfile + compose
3. 認証 + wrapper supervisor + 2FA ログイン（spike の結果に従う）
4. resolver + jobs + dedup + SSE
5. キューの HTMX UI
6. Phase 1 の受け入れ検証
7. → 実装プラン（writing-plans）へ
