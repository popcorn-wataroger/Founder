# 機密情報（シークレット）の管理ガイド

APIキーや署名鍵などの機密情報を、ローカル開発と本番でどう扱うかをまとめたドキュメントです。

関連 Issue: #67（Secret Manager 導入）／#66（Cloud Run デプロイ）

---

## 基本の考え方

機密情報の「正解」を1か所に置き、アプリはそれを**環境変数として受け取るだけ**にします。

| 環境 | 値の供給元 | アプリ側の読み方 |
|---|---|---|
| ローカル開発 | `.env` ファイル | `os.getenv()` |
| 本番（Cloud Run） | Google Secret Manager → 環境変数としてマウント | `os.getenv()`（同じ） |

Cloud Run はデプロイ設定で「このシークレットをこの環境変数に入れる」と指定でき、コンテナ起動時にGCP側が値を注入します。そのためアプリ側に Secret Manager のライブラリは不要で、読み込み口は `app/config.py` の1か所だけです。

読み込みを担当しているファイル: `app/config.py`

---

## 管理対象の一覧

| 変数 | 用途 | ローカル | 本番 |
|---|---|---|---|
| `JWT_SECRET_KEY` | ログイントークンの署名鍵 | 任意（未設定なら開発用の既定値） | **必須**（未設定なら起動しない） |
| `GEMINI_API_KEY` | Gemini API の認証 | 必要 | 必要 |
| `QDRANT_URL` | Qdrant の接続先 | 必要 | 必要 |
| `QDRANT_API_KEY` | Qdrant Cloud の認証 | 必要 | 必要 |
| `APP_ENV` | 実行環境の識別子 | `local` | `production` など |
| `GCS_BUCKET_NAME` | GCSバケット名 | — | 機密ではない。環境変数のままでよい |
| `DATABASE_URL` | Cloud SQL の接続情報 | — | Issue #65 で導入予定 |

---

## APP_ENV の扱い（重要）

`APP_ENV` が `local` / `test` のときだけ、開発用のゆるい既定値が許されます。それ以外の値、**および未設定のときは本番相当**として扱います。

未設定を「ローカル扱い」にしないのは、本番で `APP_ENV` を渡し忘れたときに開発用の鍵で起動が通ってしまうからです。設定漏れは安全な側（起動しない）に倒します。

- ローカル: `.env` の `APP_ENV=local`
- CI: `.github/workflows/ci.yml` の `APP_ENV: test`

どちらも明示的に指定しているため、この扱いによる影響は受けません。

---

## JWT_SECRET_KEY にフォールバックを置かない理由

以前 `app/config.py` は、未設定時に `"fallback-secret-key"` という固定値を使っていました。この値は公開リポジトリに書かれているため、そのまま本番に出ると次のことが起きます。

1. 攻撃者が同じ鍵で `role: admin` のトークンを自作できる
2. ログインせずに管理者としてアクセスできる
3. 全社員の個別ソース（評価・給与情報など）が閲覧できる

しかも**起動は成功してしまう**ため、事故が起きるまで誰も気づけません。そこで固定値のフォールバックを廃止し、本番相当の環境で未設定なら `RuntimeError` を投げて起動を止めています。

エラーメッセージには鍵の値を含めません。ログに出してよいのは「設定あり/なし」だけです。

---

## ローカル開発の手順

### 1. `.env` を用意する

```bash
cp .env.example .env
```

`.env` は `.gitignore` に入っているため commit されません。

### 2. 値を入れる

| 変数 | 入手先 |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio で発行 |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant Cloud のダッシュボード |
| `JWT_SECRET_KEY` | 下記コマンドで自分用に生成（空のままでも起動する） |

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`APP_ENV=local` は `.env.example` に入っているので、消さないでください。

### 3. 起動して確認する

```bash
uv run uvicorn app.main:app --reload
```

http://localhost:8000 を開き、EMP001（社員画面）と ADMIN（管理者画面）でログインできることを確認します。

---

## やってはいけないこと

- `.env` や鍵の値を commit する
- 鍵の値をチャット・Issue・PR に貼る（受け渡しはパスかクリップボード経由で）
- 読み込み確認のつもりで値を `print` / ログ出力する
- `app/config.py` に値を直書きする

---

## 本番（Google Secret Manager）

使用するGCPプロジェクト:

| 項目 | 値 |
|---|---|
| プロジェクトID | `notebooklm-482403` |
| プロジェクト番号 | `525613033246` |
| コンソール | https://console.cloud.google.com/security/secret-manager?project=notebooklm-482403 |

### 登録済みのシークレット

| シークレット名 | レプリケーション | 暗号鍵 | 状態 |
|---|---|---|---|
| `JWT_SECRET_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `GEMINI_API_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `QDRANT_API_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `QDRANT_URL` | 自動 | Google管理 | バージョン1が有効 |

対象外にしたもの:

- `DATABASE_URL` … Issue #65 で Cloud SQL を導入するときに追加する
- `GCS_BUCKET_NAME` … 機密ではないため、環境変数のままにする
- `OPENAI_API_KEY` … 本プロジェクトでは未使用のため登録しない

リソースパスは `projects/525613033246/secrets/JWT_SECRET_KEY` の形式になります。プロジェクトIDではなくプロジェクト番号が使われる点に注意してください。

---

### 1. シークレットの登録手順

#### 前提: Googleアカウントの2段階認証（MFA）

**2026年7月20日から Google Cloud で2段階認証が必須化されました。**未設定のアカウントはコンソールに入れません。先に https://myaccount.google.com/security から2段階認証を有効にしてください。

#### コンソールでの操作

1. https://console.cloud.google.com/security/secret-manager?project=notebooklm-482403 を開く
2. 「**シークレットを作成**」をクリック
3. 「名前」に**環境変数名と同じ名前**を入力する（例: `JWT_SECRET_KEY`）
4. 「シークレットの値」に値を貼り付ける
5. それ以外の設定項目（レプリケーション、暗号鍵、ローテーション、有効期限、ラベル）は**すべてデフォルトのままでよい**
6. 「シークレットを作成」をクリック

名前を環境変数名と揃えるのは、Cloud Run のマウント設定と `app/config.py` の `os.getenv()` が同じ名前で一直線につながるようにするためです。名前がずれると、どのシークレットがどの設定に対応するのか追えなくなります。

#### gcloud CLI での操作

```bash
# シークレットの入れ物を作る（値はまだ入れない）
gcloud secrets create JWT_SECRET_KEY \
  --replication-policy=automatic \
  --project=notebooklm-482403

# 値を1つ目のバージョンとして追加する
gcloud secrets versions add JWT_SECRET_KEY \
  --data-file=/path/to/secret.txt \
  --project=notebooklm-482403
```

値はファイル経由で渡します。`echo "値" | gcloud ... --data-file=-` のようにコマンドラインへ直接書くと、シェルの履歴（`~/.zsh_history`）に平文で残ります。渡し終えたファイルは削除してください。

登録できたかの確認は、値を表示せずに次で行えます。

```bash
gcloud secrets list --project=notebooklm-482403
gcloud secrets versions list JWT_SECRET_KEY --project=notebooklm-482403
```

---

### 2. 権限（IAM）の付与

Cloud Run のサービスアカウントに `roles/secretmanager.secretAccessor` を付与します。**プロジェクト全体ではなく、シークレット単位で付けること。**プロジェクト全体に付けると、そのサービスアカウントは将来追加される別のシークレットまで自動的に読めてしまいます。

```bash
gcloud secrets add-iam-policy-binding JWT_SECRET_KEY \
  --member="serviceAccount:サービスアカウント名@notebooklm-482403.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=notebooklm-482403
```

4つのシークレットそれぞれに対して同じコマンドを実行します。

> **この作業は Cloud Run のサービス作成後に行います。**付与先のサービスアカウントは Cloud Run のサービスを作らないと決まらないため、実際の付与は Issue #66（Cloud Run デプロイ）の中で実施します。

---

### 3. Cloud Run へのマウント

デプロイ時に「どのシークレットをどの環境変数に入れるか」を指定します。コンテナ起動時にGCP側が値を注入するので、コンテナに `.env` を含める必要はありません。

```bash
gcloud run deploy founder \
  --project=notebooklm-482403 \
  --set-secrets=JWT_SECRET_KEY=JWT_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,QDRANT_API_KEY=QDRANT_API_KEY:latest,QDRANT_URL=QDRANT_URL:latest \
  --set-env-vars=APP_ENV=production
```

`--set-secrets` の書式は `環境変数名=シークレット名:バージョン` です。左が環境変数名、右がSecret Manager側の名前で、今回は両者を同じ名前に揃えています。

`APP_ENV=production` を `--set-env-vars` で渡すのを忘れないでください。ただし忘れても危険な状態にはなりません。`app/config.py` は `APP_ENV` 未設定を本番相当として扱うため、`JWT_SECRET_KEY` が無ければ起動に失敗します。

#### `latest` とバージョン固定の違い

| 指定 | 挙動 | 向いている場面 |
|---|---|---|
| `JWT_SECRET_KEY:latest` | デプロイのたびに最新バージョンを読む | 通常運用。キーを入れ替えても再デプロイだけで反映される |
| `JWT_SECRET_KEY:1` | 常にバージョン1を読む | 「どのリビジョンがどの値で動いていたか」を厳密に固定したい場合 |

`latest` は「デプロイ時点の最新」を読むだけで、稼働中のコンテナが自動で追随するわけではありません。値を変えたら**必ず再デプロイが必要**です。

---

### 4. キーの入れ替え（ローテーション）手順

漏れたときに差し替えられないと、シークレット管理をしている意味がありません。手順を決めておきます。

#### コンソールでの操作

1. Secret Manager で対象のシークレットを開く
2. 「**バージョン**」タブを選ぶ
3. 「**+ 新しいバージョン**」をクリックし、新しい値を入力して追加する（古いバージョンはこの時点では残す）
4. Cloud Run を再デプロイして、新しいバージョンを読み込ませる
5. 動作確認する（EMP001 と ADMIN の両画面でログイン〜チャット）
6. 問題なければ古いバージョンを「**無効化**」する
7. しばらく様子を見て、戻す必要がないと確認できたら「**破棄**」する

いきなり破棄せず、無効化を挟むのが重要です。無効化は元に戻せますが、破棄は元に戻せません。無効化した状態で問題が起きたら、すぐ有効化して切り戻せます。

#### gcloud CLI での操作

```bash
# 新しいバージョンを追加
gcloud secrets versions add JWT_SECRET_KEY \
  --data-file=/path/to/new-secret.txt \
  --project=notebooklm-482403

# 再デプロイ後、動作確認してから古いバージョンを無効化
gcloud secrets versions disable 1 --secret=JWT_SECRET_KEY --project=notebooklm-482403

# 切り戻しが不要と確認できたら破棄（取り消せない）
gcloud secrets versions destroy 1 --secret=JWT_SECRET_KEY --project=notebooklm-482403
```

#### `JWT_SECRET_KEY` を入れ替えるときの注意

**全員のログインが切れます。**発行済みのトークンは古い鍵で署名されているため、新しい鍵では検証に失敗し、利用者は再ログインを求められます。

`JWT_EXPIRE_HOURS = 8`（`app/config.py`）なので、もともと8時間でログインし直す設計ではありますが、入れ替えた瞬間は全員が同時に切れます。業務時間外に実施するか、事前に周知してください。

他の3つ（`GEMINI_API_KEY` / `QDRANT_API_KEY` / `QDRANT_URL`）にはこの影響はありません。
