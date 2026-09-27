# 移植可能なライブラリ設定 — design

- 日付: 2026-09-27
- 状態: design レビュー待ち
- 対象ワークスペース: `/home/m/amdl_extend`
- 関連: `2026-09-26-amd-hub-design.md`（§7.2 ライブラリ）、`2026-09-27-deployment-report.md`

---

## 1. 背景

本 spec は「`/home/m/amdl_extend/hub` のようなパスがハードコードされている問題」
から始まった調査の結果である。調査は 3 回の誤発見を経て、当初の問いより広い
問題に着地した。**どの誤発見も、同一の欠陥の別の顔**이었다。

### 1.1 誤発見 1: ディレクトリのリネームが未コミットだった

`hub/hub/config.py` に `/home/m/amdl_extend/AppleMusicDecrypt/downloads` が
ハードコードされていた。調べた結果、これは**存在しないパス**を名指ししていた。
しかもコミット済み状態（`aea016a`）はリポジトリの実際の場所と一致しており、
作業ツリー側に 15 ファイル 77 行の `apple-dl_extend` → `amdl_extend` の一括置換
が**未コミットで入っていた**。その 12 ファイルは
`docs/superpowers/findings/**` の**実行ログの逐語記録**だった。

この置換はプロジェクト名の統一という**意図的な**もので、オペレータが自ら行った。
対処はディレクトリのリネームで、実行済み。結果として**リポジトリ内に存在しない
絶対パスの参照は 0 件**になった。

### 1.2 誤発見 2: `data/` が実データで、`volumes:` ブロックは孤児だった

compose は `./data/hub-data` と `./data/wrapper-data` を **bind mount** しており、
`volumes:` が宣言する `hub-data` / `wrapper-data` という**名前付き volume を
参照していない**。`docker volume ls` に残っていた `apple-dl_extend_*` は旧
compose 時代の孤児だった。

実データは `data/` にあり（`.gitignore:31` で除外、`.dockerignore:70` の
`**/data/` でイメージからも除外）、**ディレクトリとともに移動した**ので
Apple アカウントは無傷である。リネーム前に名前付き volume のデータ移行を
試みたのは**この前提の誤り**であり、結果として no-op であった。

### 1.3 誤発見 3: `/library/b` の `b` は孤児だった

`docs/superpowers/specs/2026-09-26-amd-hub-design.md:647-653` が元の設計を示す。

```yaml
- ./AppleMusicDecrypt/downloads:/library/a          # 既存 69 GB（dirPathFormat 準拠）
- /run/media/m/1A5E05A75E057D2F/Music:/library/b     # 外付け NTFS 341 GB
AMD_LIBRARY_ROOTS: /library/a,/library/b
```

`/library/a`（クライアント自身のツリー）が唯一のルートになった際にマウントだけ
が外れ、`AMD_DOWNLOAD_ROOT` が `/library/b` に移った。**文字 `b` だけが残った。**
`/library/a` は 2026-09-27 のデプロイ後に存在しなくなったが、**コメントだけが
生き残り**、`/library/a` が実在するかのような記述が今も残っている。

### 1.4 本当の問題

以上を踏まえ、オペレータの要求はこうである。

> **`/home/m/Music/HDD_Music` を含むホスト固有のリテラルをなくし、他人のマシン
> （外付け NTFS ドライブ無し）でも動かせるようにしたい。**

技術的にはドライブは不要である。ライブラリ bind は `AMD_LIBRARY_HOST` でパラメータ化
済みで、マシン内の普通のディレクトリでも動作する。可搬性を阻むのは
**ハードコードされた既定値と、存在しない前提に基づく原稿**だけだった。

---

## 2. 目的 / 非目的

### 目的

1. リポジトリ内に**このマシン固有の絶対パスリテラルを 1 件も残さない**
2. **他人のマシン**（外付け NTFS ドライブ無し、ライブラリはマシン内の普通の
   ディレクトリ）でも `cp .env.example .env && docker compose up -d --build` で
   起動できる
3. 誤った設定が**起動時に、変数名つきで**失敗する（最初のトラックの再ダウンロード
   という不可視の失敗ではなく）

### 非目的

- 複数ライブラリのサポートを**追加**すること。`AMD_LIBRARY_ROOTS` は既に
  カンマ区切りの複数対応をしている。**形の変更のみ**とする
- `.env.example:51-56` の TODO（起動時に `dirPathFormat` の root が
  `AMD_LIBRARY_ROOTS` のメンバーであることを検証して拒否する）。**別 spec の
  新機能**であり、§8 に記す
- `docs/superpowers/**` の**履歴**の書き換え。前 spec の決定どおり触らない
- サブモジュール（`AppleMusicDecrypt/`, `wrapper/`）への変更。AGENTS.md により
  永久に不可

---

## 3. 原則: 3 階層、3 つのルール

現状のハードコードは 3 種類あり、混同正是この問題の根本である。

| 階層 | 例 | ルール |
|---|---|---|
| **リポジトリ相対** | `AppleMusicDecrypt`, `AppleMusicDecrypt/downloads` | 導出する（`parents[2]`）。**書かない** |
| **オペレータ固有** | ライブラリのあるホストディレクトリ | **必須。既定値なし。変数名を挙げて起動時失敗** |
| **コンテナ内部** | `/library`, `/data`, `/app`, `/opt/wrapper` | 名前だけを**単一の情報源**で決める |

### 第 1 階層: 規約は既に存在する

`hub/hub/ripper_host.py::_VENDOR_ROOT` と `hub/hub/app.py::vendor_config_path` は
どちらも `Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"` であり、
`hub/deploy/build_gate.py` が `parents[2]` の一致を**ビルド時に**検証している。
**本 spec は新しい導出規約を作らない。既存のものを第 1 階層に適用するだけ。**

### 第 2 階層: ポリシーも既に存在する

`hub/hub/config.py:1-7` のモジュードキュメントがこう明言している。

> a misconfigured deployment must fail at startup with a message naming the
> variable — not at the first request, and not by silently falling back to a
> different mode.

そして `compose.yaml:138` の `AMD_PASSWORD: ${AMD_PASSWORD:?...}` が同型の先例
である。**新しいポリシーを作るのではなく、既存ポリシーを 2 箇所に適用する。**

### 第 3 階層: 「単一の情報源」も既に構造化されている

`AMD_DOWNLOAD_ROOT` がその役を負っている。`Dockerfile:302` の
`ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}` と `dirPathFormat` の sed はそこから
自動的に追従し、`hub/tests/test_deployment.py:1519-1547` がその一箇所性を守る。
**改名 `/library/b` → `/library` は `AMD_DOWNLOAD_ROOT` 一箇所の変更で足りる。**

---

## 4. 決定事項

| # | 決定 | 理由 |
|---|---|---|
| D1 | 対象は**他人のマシン**（外付けドライブ無し） | 要求の明示。技術的には drive は不要 |
| D2 | `AMD_LIBRARY_HOST` は**必須化**し fail-fast | 第 2 階層のルール。`AMD_PASSWORD` と同型 |
| D3 | `AMD_LIBRARY_ROOTS` も**必須化**、`DEFAULT_LIBRARY_ROOTS` を**削除** | D2 と同じ哲学を逸らさない。黙って空をスキャンする footgun を残さない |
| D4 | `/library/b` → **`/library`** | 孤児になった `a` を消す。「`b` は何?」という質問自体を起こさない |
| D5 | **2 コミット**（挙動 → 原稿） | ①はテストが証明し、②は機械的で挙動リスクゼロ |

### D3 で逆転する記録済みの決定

`docs/superpowers/findings/2026-09-26-normalize-report.md:231` に、空の
`AMD_LIBRARY_ROOTS` を**エラーにせず既定値にフォールバックさせる**ことが
§8.1 に基づく**意図的な決定**として記録されている。本 spec はこれを**逆転する**。

理由:

- 既存の既定 1 本目 `AppleMusicDecrypt/downloads` は上流の
  `AppleMusicDecrypt/.gitignore:15` で無視されるため、**新規 clone では存在せず**、
  クライアントが実際に何かダウンロードするまで生成されない。これは AGENTS.md が
  最も注意深い点の一つ（"a root that is a *silently empty* mount point is not
  [reported]"）そのものへの足場になる
- §8.1 のフォールバック判断が量身された「空なら 2 つのホスト library を使う」
  という前提は、**その 2 つのうち 1 つが既に消えている**ため成立しない

この逆転の理由は §9 に記録として残す。

---

## 5. 変更仕様

### 5.1 `hub/hub/config.py`

**削除**: `DEFAULT_LIBRARY_ROOTS` 定数（22-25 行）と、その上の 19-21 行のコメント。

**`_paths()` の変更**: 「必須」モードを追加する。現状は（該当箇所のみ抜粋）:

```python
def _paths(env, key, default):
    raw = _text(env, key, "")
    if not raw:
        return list(default)      # ← 既定値へフォールバック
    # ... 以降は変更しない
```

`AMD_LIBRARY_ROOTS` については未設定時に `RuntimeError` を投げる。メッセージは
**変数名を名指しし、実行可能な形式を示す**（§6）。

**検査順序**（`load_settings` 内）:

```
1. AMD_PASSWORD
2. AMD_LIBRARY_ROOTS   ← 新規の必須検査をここに
3. AMD_BIND, AMD_PORT, ... （既存の順を維持）
```

順序が契約である。`test_config.py:6-16` は `load_settings({"AMD_PASSWORD": ""})`
と `load_settings({})` が `AMD_PASSWORD` の `RuntimeError` を期待している。
`AMD_LIBRARY_ROOTS` の検査を**後ろ**に置くと、この 2 件は別の理由で `pass` して
**password を検証しなくなる**。

**ローカル開発について**: アプリは `.env` を**読まない**（`.env` は compose 専用）。
つまりローカル実行でも `AMD_PASSWORD` と同様に**環境変数を export する**ことが
必要になる。これは現状と同じ要求であり、可搬性のために**新しい**負担ではない。
README に 1 行書く（§5.6）。

### 5.2 `compose.yaml`

```yaml
# 29 行目
        AMD_DOWNLOAD_ROOT: /library

# 110-114 行目
      - type: bind
        source: ${AMD_LIBRARY_HOST:?set AMD_LIBRARY_HOST in .env to the host directory that holds your music library}
        target: /library
        bind:
          create_host_path: false

# 170 行目
      AMD_LIBRARY_ROOTS: /library
```

`create_host_path: false` は**維持する**。これは現在の compose で最も慎重に守られ
ている安全装置であり、可搬性の問題ではない。`AMD_LIBRARY_HOST` が未設定なら compose は
**変数名つきのエラーで停止**し、存在しないパスに空のライブラリを mount する
ことはない。**正しい挙動である。**

### 5.3 `Dockerfile`

```
# 12 行目
ARG AMD_DOWNLOAD_ROOT=/library
```

`ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}`（302 行目）と `dirPathFormat` /
`playlistDirPathFormat` の sed は**変更しない**。既に `${AMD_DOWNLOAD_ROOT}` から
導出されているため、自動的に `/library` になる。**これを書かないことが第 3 階層
のルールの中核である。**

### 5.4 `hub/spike/task4_real_library_check.py`

`DEFAULT_LIBRARY_ROOTS` の import が壊れる。`ROOTS` を環境変数から読むように変更する。

```python
ROOTS = [
    Path(p)
    for p in os.environ.get("AMD_LIBRARY_ROOTS", "").split(",")
    if p.strip()
]
if not ROOTS:
    raise SystemExit(
        "set AMD_LIBRARY_ROOTS to the roots to check, e.g. AMD_LIBRARY_ROOTS=/library"
    )
```

**理由**: この spike は「このマシンで今動いている設定に対する検証」であり
（`docs/superpowers/findings/2026-09-26-dedup-report.md` に証拠が残っている）、
ハードコードされた値ではなく実際の設定を読むのが正しい。`hub/spike/` は
**git 追跡されており**（`git ls-files` で確認）、`pyproject.toml:108` の
`per-file-ignores` で lint からも外されている。

### 5.5 テスト

#### `hub/tests/test_config.py`

新規 3 件。**TDD: 最初に赤くする。**

```python
def test_requires_library_roots():
    with pytest.raises(RuntimeError, match="AMD_LIBRARY_ROOTS"):
        load_settings({"AMD_PASSWORD": "x"})


def test_the_missing_library_roots_message_says_how_to_set_it():
    # A message that only names the variable leaves the operator to guess the format, and
    # the format is a comma-separated list of *container* paths -- not the host path they
    # put in AMD_LIBRARY_HOST. Both halves are asserted because the point of this change is that a
    # new user can get it right without reading the source.
    with pytest.raises(RuntimeError) as excinfo:
        load_settings({"AMD_PASSWORD": "x"})
    message = str(excinfo.value)
    assert "AMD_LIBRARY_ROOTS" in message
    assert "comma-separated" in message


def test_the_password_is_still_checked_before_the_library_roots():
    # Both required settings fail this way, so the order is a contract: a caller who has
    # set neither must be told about the one it is most likely to have meant. This is what
    # keeps the two existing password tests honest -- without it they would pass for the
    # wrong reason and stop testing the password.
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({})
```

既存 17 箇所に `AMD_LIBRARY_ROOTS` を追加する。実測の内訳:

- `load_settings` の呼び出しは `tests/` 全体で **22 箇所**、うち **17 箇所**が
  未指定で、**全て `test_config.py` に集中**している
- **`Settings(` の直接構築は 0 箇所**。`test_api_jobs.py:324` の `settings`
  fixture は 329 行で `AMD_LIBRARY_ROOTS` を渡しており、アプリ層のテストは
  全て無傷
- **`create_app()` を引数なしで呼ぶ箇所も 0**（実測）。したがって
  `hub/hub/app.py:789` の `load_settings()` フォールバック経路は
  テストで実行されておらず、本変更で新たに落ちるテストはない
- `:46` の素の `load_settings()` は `monkeypatch.setenv` で
  `AMD_LIBRARY_ROOTS` を設定する必要がある

**`:6-16` の password テスト 2 件は意図的に渡さない。** ここへ渡すと、password が
未設定でも library roots が設定済みなら password の検査を飛ばすことになり、
`match="AMD_PASSWORD"` が別の理由で成立して**password を検証しなくなる**。
順序は新規の `test_the_password_is_still_checked_before_the_library_roots` が守る。

**`:30-40` の `test_default_library_roots_are_the_two_spec_libraries` は削除する。**
固定する価値のある既定値がなくなるため。このテストが固定していた**意図**
（「コンテナのマウントポイント `/library/a`, `/library/b` が既定値にすり入って
はいけない」）は、`test_deployment.py` 側へ受け継ぐ（`/library/b` を持たない
ことを assert する既存の 1075 行目がその役目を既に担っている）。

#### `hub/tests/test_deployment.py`

**現行 1566-1571 を置き換える。**

```python
match = re.search(r"AMD_LIBRARY_HOST:-([^}]+)\}", binds[0]["source"])
assert match, f"the source should be an overridable host path, got {binds[0]['source']!r}"
assert match.group(1).endswith("HDD_Music"), (
    "the host path is the symlink, not the /run/media/<UUID> target it points at: ..."
)
```

**2 つ目の assert はこのマシンの個人名をリテラルとして固定している。削除する。**
このテストの名前 `test_the_external_drive_is_mounted_by_the_base_compose_and_cannot_be_invented`
も「外付けドライブ」という前提を含んでいるため、`test_the_library_is_required_and_cannot_default_to_this_host`
へ改名する。置き換え:

```python
match = re.search(r"AMD_LIBRARY_HOST:\?([^}]+)\}", raw)
assert match, (
    f"AMD_LIBRARY_HOST must be required (:?) rather than defaulted: a default is a host-specific "
    f"path baked into the file, and it is wrong on every machine but this one. "
    f"got {binds[0]['source']!r}"
)
assert "HDD_Music" not in raw, (
    "no host-specific default may appear in the compose file; the operator's library path "
    "belongs in .env"
)
```

**現行 1085-1112 `test_the_library_root_is_mounted_through_the_symlink_and_not_resolved`
は前提が成立しなくなる。** このテストは compose から**このマシンの実パス**を
抜き出し、シンボリックリンクか否かを**実行環境に問い合わせ**、そうでなければ
`skip` する。`AMD_LIBRARY_HOST` に既定値がなくなったことで成立しなくなる。

置き換え:

- **残す**: `assert "/run/media/" not in code`（出荷ファイルが udev の UUID を
  名指ししないこと。`AMD_LIBRARY_HOST` を経由する形でも同じ）
- **削除する**: 「既定値がこのマシンでシンボリックリンクか」の判定部分。パスは
  `.env` から来るものであり、compose ファイルが検査できる対象ではない
- **追加する**: `AMD_LIBRARY_HOST` が `:?` 構文であること（上記と同じ assert）
- ガイダンスは **原稿**（`.env.example` / `README`）へ移す:
  「`/run/media/<UUID>/` ではなく、自分で管理する安定したパスを指せ」

これは**改善**である。現状のテストは**ランナーが動いているマシン**に依存し、
シンボリックリンクが存在しない環境では `skip` して**何も検査していない**。

`/library/b` → `/library` に伴う literal 更新: 550, 604, 1072, 1073, 1075,
1112, 1139, 1560, 1582 の 9 箇所。166, 180, 1499 は docstring / コメント内の
言及なので併せて更新する。

#### `hub/deploy/mutation_check.py`

79-83 行の 2 つの mutation が `.env.example` の文字列を標的にしている。

```python
"the /library/a containment TODO removed from .env.example": (
    ENV, lambda t: t.replace("TODO(spec \u00a78.1, Phase 2)", "later")),
"the false 'KEEP /library/a FIRST' rationale reinstated": (
    ENV, lambda t: t.replace("*** /library/a MUST BE IN THE LIST. ***", "*** KEEP /library/a FIRST. ***")),
```

2 つめは `*** /library/a MUST BE IN THE LIST. ***` という**まさに §5.6 で消す
文字列**を標的にしている。消した後、この mutation は**標的が存在しない no-op** に
なる。これは既に 1 度起きている（`2026-09-27-deployment-report.md:727`:
「the stale `KEEP /library/a FIRST` mutation | its target string no longer existed,
so the mutation did not apply」）。**標的が消える前に置き換える。**

- 79 行めの TODO mutation は**そのまま**（§8 で TODO はスコープ外として残るため、
  標的文字列は生存する）
- 81 行めの mutation を、**`/library/a` を復活させる**形の新しいものに置き換える
  （削除した散文を再導入する mutation）

### 5.6 原稿（Commit 2）

| ファイル | 変更 |
|---|---|
| `.env.example` | 43-56 行（「`/library/a` MUST BE IN THE LIST」）と 58-69 行（「the only root」）の**矛盾した 14 行**を統合する。新人がすることは「ライブラリのあるディレクトリを 1 つ指定するだけ」。`AMD_LIBRARY_HOST` が必須であることも常务。58-61 行の「ドライブ必須」という記述を外し、「マシン内の任意のディレクトリでよい」と書く |
| `.env.example` | compose を経由しないローカル実行の手順を追記する（§5.1） |
| `README.md:39` | `ls -L /home/m/Music/HDD_Music` を汎用手順へ置き換える |
| `README.md:96` | 「`/library/a`」を `/library` へ |
| `Dockerfile:196-199` | `/library/a` 前提のコメントを削除する（`${AMD_DOWNLOAD_ROOT}` へ） |
| `build_gate.py:172-179` | 同上。**コードは変更しない** — `settings.library_roots[0]` を使う実装は正しい。コメントだけが古い |
| `acceptance_check.py:10, 87` | docstring とコメントの `/library/a`, `/library/b` を更新する |
| `config.py:19-21` | `DEFAULT_LIBRARY_ROOTS` ごと削除する（§5.1） |
| `AGENTS.md` | **このマシンの事実としての記述は残す**（`/home/m/Music/HDD_Music` はこのドライブだと読める）。他の環境向けの設計根拠として書かれた文だけを普遍化する。現状 `/home/m/` は 3 箇所で、**3 箇所が生存していることをレビューで確認する** |

**触らない**: `docs/superpowers/**`（履歴）、`AppleMusicDecrypt/`、
`wrapper/`（サブモジュール）、既存の未コミット 77 行のリネーム。

### 5.7 テスト戦略

1. **TDD**: §5.5 の新規 3 件を**先に赤くして**から実装に入る
2. `cd hub && uv run pytest -q` — 645 green を確認
3. `cd hub && uv run ruff check .` — clean
4. **`build_gate.py` を 1 回実測する**: Dockerfile が
   `ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}` を持つため `load_settings()` は
   `/library` を得るはずだが、**推測で通さない**。`docker compose build` の最終段
   で実際に走らせる
5. **コンテナ内での `acceptance_check.py`**: イメージ内で
   `AMD_LIBRARY_ROOTS=/library` が設定されていることを確認する

---

## 6. エラーメッセージ

以下は**実測済み**であり、推測で書いていない。

### `AMD_LIBRARY_HOST` 未設定時

compose が変数名つきで停止する。実測した形式:

```
error while interpolating services.amd-hub.volumes.0.source: required variable
AMD_LIBRARY_HOST is missing a value: set AMD_LIBRARY_HOST in .env to the host directory that holds your music library
```

一時ディレクトリに `compose.yaml` を書いて `${BAR:?set BAR in .env}` を
`docker compose config` にかけて**実測した**。`:?` のメッセージは compose が
`required variable <名前> is missing a value: <メッセージ>` と連結する。
したがって `:?` のメッセージには変数名を含めない（含めると 2 度出る）。

### `AMD_LIBRARY_ROOTS` 未設定時

アプリが起動時に投げる:

```
AMD_LIBRARY_ROOTS is unset. Name every host directory that holds your music
library, comma-separated, e.g. AMD_LIBRARY_ROOTS=/library
```

**コンテナ内のパス**である `/library` を例として示す点が重要で、`AMD_LIBRARY_HOST` には
**ホストの**パスが入る。2 つの変数が別物であることがメッセージだけで分かるように
するためである。

---

## 7. ロールアウト

既存デプロイへの影響:

1. **`.env` で `AMD_LIBRARY_HOST` を有効にする必要がある。** 実測で現在の `.env` は
   95 行目に `# AMD_LIBRARY_HOST=/home/m/Music/HDD_Music` と**コメントアウト**した状態で
   持つだけで、**変数は未設定**である。これまでの起動は compose の既定値に
   依存していた。必須化により、`AMD_LIBRARY_HOST` を有効にするまで `docker compose up`
   は失敗する
2. これを回避するため、実装時に**このマシンの `.env` の 95 行目をコメントアウト
   解除する**（gitignore 済み、オペレータ所有）
3. イメージには影響しない。`AMD_DOWNLOAD_ROOT` の `/library/b` → `/library` は
   イメージ内のパスなので再ビルドが必要だが、**データの移行は不要**
4. `data/` 内の Apple アカウントと `hub.db` は**無傷**。ここが bind mount である
   ことの意義（§1.2）

---

## 8. スコープ外（明示）

- **`.env.example:51-56` の TODO**（起動時に `dirPathFormat` の root が
  `AMD_LIBRARY_ROOTS` のメンバーであることを検証して拒否する）。AGENTS.md が
  「最後にチェックされていない設定の誤り方」と名指ししている。**同じ精神だが別
  spec の新機能**であり、本 spec の主題は「黙って壊れる設定を起動時に落とす」
  ことなので親和性が高いにもかかわらず、**このスコープには入れない**。
  **次の候補**
- `docs/superpowers/**` の履歴（§2 非目的）
- サブモジュールへの変更（AGENTS.md により永久に不可）
- 既存の未コミット 77 行のリネーム（意図されたものとして保持）

---

## 9. リスクと記録

| リスク | 評価 |
|---|---|
| `build_gate.py` が `AMD_LIBRARY_ROOTS` を要求するため壊れる | **低**。Dockerfile:302 の `ENV` が値を供給する。§5.7-4 で実測する |
| 既存デプロイが `.env` の更新を忘れる | **中**。§7 で緩和する。忘れた場合、他マシンでは「`AMD_LIBRARY_HOST` が必須」というエラーが出るので**静かには壊れない** |
| 17 箇所の `test_config.py` 修正漏れ | **低**。`pytest` が全てを報告する |
| `/library` への改名でイメージ内の `dirPathFormat` が変わる | **低**。`AMD_DOWNLOAD_ROOT` 経由なので自動的に一貫する。`build_gate.py:169-185` の包含チェックがこれを検証する |
| `AGENTS.md` の書き換えが不徹底で、このマシン固有の情報が失われる | **低**。方向は §5.6 で決まっている。**判定はレビューで行う** — §10 の grep で 3 箇所が生きているか確認する |

**記録として残すべきこと**:

- §1 の誤発見 3 連（未コミットの置換 → `data/` が実データ → `/library/b` が
  孤児）は、**どれも同じ欠陥の別の顔**であり、「このマシン固有の値がファイルに
  残っている」という一点に集約される。**3 階層ルール（§3）でまとめて治療した**
  のは、症状ごとに直したわけではないからである
- D3 は**記録済みの意図的な決定の逆転**であり、その理由（§4）が spec に残る
  こと自体が記録として機能する
- `/library/a` のコメントが**生き残っていた**のは、AGENTS.md が「設計の根拠が
  載っているコメントは最も注意深く読むべき場所であり、だからこそ最も腐りやすい」
  という性質を持つため。**「重要なコメント」は「最も早く古くなる」ことを意味
  する。** 今回の 3 件はすべてこの性質の結果である

---

## 10. 完了条件

**実行されるファイルにこのマシン固有の絶対パスが 1 件も残っていないこと。**
これが本 spec の主目標であり、他の条件はこれを支える。

- [ ] `git grep -nE '(/home/m/|/run/media/)' -- hub compose.yaml Dockerfile
      README.md .env.example` が **0 件**を返す
  - `hub/.venv/` は git 管理外なので `git grep` の対象外
  - サブモジュール（`AppleMusicDecrypt/`, `wrapper/`）は対象外
  - `docs/superpowers/**` は**対象外**（履歴。§2 非目的）
  - **`AGENTS.md` は対象外** — このドライブについての事実として
    `/home/m/Music/HDD_Music` を**残す**ことを §5.6 で決定済み。3 箇所が
    生きていることをレビューで確認する
  - **この spec ファイル自体**も対象外（設計の記述として
    `/home/m/Music/HDD_Music` を含む）
- [ ] `grep -rn 'DEFAULT_LIBRARY_ROOTS' hub/` が 0 件（削除済み）
- [ ] `cd hub && uv run pytest -q` が green
- [ ] `cd hub && uv run ruff check .` が clean
- [ ] `docker compose build` が `BUILD GATE OK` で通る（§5.7-4 の実測）
- [ ] このマシンの `.env` で `AMD_LIBRARY_HOST` がコメントアウト解除されている
- [ ] `docker compose up -d` が `AMD_LIBRARY_HOST` 必須のエラーを出さずに起動する
- [ ] `/api/status` が**期待するアルバム数**を per-root で報告する
  （空ライブラリが「健全」に見えないこと。§3 の第 2 階層の存在理由）
