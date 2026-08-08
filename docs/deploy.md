# Cloud Run へのデプロイ手順

Founder を GCP の Cloud Run へデプロイし、URL でアクセスできる状態にするための手順書です。

関連 Issue: #66（Cloud Run デプロイ）／#64（GCS 移行）／#65（Cloud SQL 移行）／#67（Secret Manager 導入）

**このドキュメントの目的**
デプロイを特定の一人しかできない状態にしないことです。上から順に実行すれば、リポジトリと GCP プロジェクトへのアクセス権を持つ人なら誰でも同じ結果になるように書いています。判断が要る箇所には「なぜそうするのか」を添えてあるので、状況が変わったときに読み替えられます。

**このドキュメントに機密情報は書きません**
パスワード・APIキー・署名鍵の値は一切載せません。値の受け渡し方だけを扱います。機密情報の管理方針そのものは `docs/secrets.md` を参照してください。

**現在の公開URL**
https://founder-525613033246.asia-northeast1.run.app

Cloud Run は1つのサービスに対して2種類のURLを発行します。下の形式でも同じサービスに繋がります（どちらも動作確認済み）。後述の `gcloud run services describe` で表示されるのはこちらの形式です。

https://founder-nbwdutevlq-an.a.run.app

---

## 目次

1. [構成の全体像](#1-構成の全体像)
2. [作業者に必要な権限](#2-作業者に必要な権限)
3. [初回のみ必要な準備](#3-初回のみ必要な準備)
4. [デプロイ手順](#4-デプロイ手順)
5. [2回目以降の注意](#5-2回目以降の注意)
6. [トラブル時の確認方法](#6-トラブル時の確認方法)
7. [やってはいけないこと](#7-やってはいけないこと)

---

## 1. 構成の全体像

### 使用するリソース

| 項目 | 値 |
|---|---|
| GCP プロジェクトID | `notebooklm-482403` |
| GCP プロジェクト番号 | `525613033246` |
| リージョン | `asia-northeast1`（東京） |
| Cloud Run サービス名 | `founder` |
| Cloud SQL インスタンス | `founder-db`（PostgreSQL 16） |
| Cloud SQL 接続名 | `notebooklm-482403:asia-northeast1:founder-db` |
| データベース名 | `founder`（本番）／ `founder_test`（テスト用） |
| データベースユーザー | `founder` |
| GCS バケット | `founder-sources-482403` |
| Artifact Registry | `asia-northeast1-docker.pkg.dev/notebooklm-482403/founder` |
| サービスアカウント | 既定の compute サービスアカウント<br>`525613033246-compute@developer.gserviceaccount.com` |

**リージョンを3つのサービスで揃えている理由**
Cloud Run・Cloud SQL・GCS をすべて `asia-northeast1` に置いています。別リージョンにすると、リクエストのたびに地域をまたぐ通信が発生して遅くなり、リージョン間のデータ転送に課金も発生します。新しいリソースを足すときも同じリージョンにしてください。

### データがどこに置かれるか

Cloud Run のコンテナは**停止すると中身が消えます**。そのため、消えては困るものはすべてコンテナの外に置いています。

| 種類 | 保存先 | 担当モジュール |
|---|---|---|
| アップロードされたファイル | GCS バケット | `app/storage.py` |
| ソース・チャット履歴・最終ログイン | Cloud SQL | `app/database.py` |
| ベクトル（検索用） | Qdrant Cloud | `app/vector_store.py` |
| 社員マスタ | `data/users.csv`（イメージに同梱・読み取り専用） | `app/users.py` |
| APIキー・署名鍵 | Secret Manager | `app/config.py` |

社員マスタだけコンテナ内にあるのは、Git 管理下の読み取り専用データだからです。ログインのたびに変わる最終ログイン日時は Cloud SQL の `user_logins` テーブルに分けてあります。

### 環境変数の渡り方

アプリは `app/config.py` で `os.getenv()` を呼ぶだけで、値がどこから来たかを知りません。供給元だけが環境ごとに違います。

| 環境変数 | 渡し方 | 機密か |
|---|---|---|
| `JWT_SECRET_KEY` | `--set-secrets`（Secret Manager） | 機密 |
| `GEMINI_API_KEY` | `--set-secrets` | 機密 |
| `QDRANT_URL` | `--set-secrets` | 機密 |
| `QDRANT_API_KEY` | `--set-secrets` | 機密 |
| `DATABASE_URL` | `--set-secrets` | 機密（パスワードを含む） |
| `APP_ENV` | `--set-env-vars` に `production` | 機密でない |
| `GCS_BUCKET_NAME` | `--set-env-vars` に `founder-sources-482403` | 機密でない |
| `PORT` | **Cloud Run が自動で渡す**（既定 8080） | — |

`PORT` を自分で指定しないでください。Cloud Run が起動時に決めて渡します。`Dockerfile` の最終行が `${PORT:-8080}` で受け取ります。

`GOOGLE_APPLICATION_CREDENTIALS` と `GOOGLE_CLOUD_PROJECT` も渡しません。Cloud Run 上ではサービスアカウントとプロジェクトIDがメタデータサーバーから自動で解決されるためです（ローカルの `docker run` で試すときだけ必要になります。後述）。

---

## 2. 作業者に必要な権限

デプロイを実行する人（＝作業者本人）に必要なものです。持っていない場合はリポジトリオーナーに依頼してください。

| 必要なもの | 用途 |
|---|---|
| GCP プロジェクト `notebooklm-482403` への参加 | 全般 |
| `roles/run.admin` | Cloud Run へのデプロイ |
| `roles/artifactregistry.writer` | イメージの push |
| `roles/iam.serviceAccountUser` | サービスアカウントを使うサービスのデプロイ |
| `roles/secretmanager.admin` または相当 | 初回の `DATABASE_URL` 登録と権限付与 |
| Cloud SQL のデータベースパスワード | 初回の `DATABASE_URL` 登録 |

プロジェクトのオーナー（`roles/owner`）を持っていれば、上の4つのロールはすべて含まれます。

ローカルの `gcloud` が正しいアカウント・プロジェクトを向いているか、先に確認してください。

```bash
gcloud auth list
gcloud config get-value project
```

`notebooklm-482403` と表示されない場合は設定します。

```bash
gcloud config set project notebooklm-482403
```

---

## 3. 初回のみ必要な準備

**すでに実施済みの手順です。**新しく環境を作り直す場合や、何が設定されているか確認したい場合に使ってください。各手順の末尾に「確認コマンド」を付けてあるので、実施済みかどうかは実行しなくても調べられます。

### 3-1. API が有効か確認する

```bash
gcloud services list --enabled --project=notebooklm-482403 \
  --format="value(config.name)" \
  | grep -E "^(run|artifactregistry|sqladmin|secretmanager|storage)\."
```

次の5つが出れば足りています。

```text
artifactregistry.googleapis.com
run.googleapis.com
secretmanager.googleapis.com
sqladmin.googleapis.com
storage.googleapis.com
```

足りないものがあれば有効化します（例）。

```bash
gcloud services enable run.googleapis.com --project=notebooklm-482403
```

`compute.googleapis.com` は無効のままで構いません。Cloud Run は使いません。

### 3-2. Artifact Registry のリポジトリを作る

イメージの置き場です。

```bash
gcloud artifacts repositories create founder \
  --repository-format=docker \
  --location=asia-northeast1 \
  --description="Founder 社内AIチャットボットのコンテナイメージ" \
  --project=notebooklm-482403
```

確認:

```bash
gcloud artifacts repositories list --project=notebooklm-482403
```

> 同じプロジェクトに `rag-app` という別リポジトリと、`rag-app` という別の Cloud Run サービスがあります。**これらは Founder とは無関係の別アプリです。触らないでください。**

### 3-3. Secret Manager に `DATABASE_URL` を登録する

`JWT_SECRET_KEY` / `GEMINI_API_KEY` / `QDRANT_URL` / `QDRANT_API_KEY` の4件は Issue #67 で登録済みです。`DATABASE_URL` だけが Cloud Run デプロイ（Issue #66）のタイミングで追加されました。接続先が確定しないと値を作れないためです。

**Cloud Run からの接続は Unix ソケット経由**になります。ローカル開発（`cloud-sql-proxy` 経由で `localhost:5432`）とは URL の形が違う点に注意してください。

```text
ローカル   : postgresql://founder:<パスワード>@localhost:5432/founder
Cloud Run : postgresql://founder:<パスワード>@/founder?host=/cloudsql/<接続名>
```

ホスト名の位置が空になり、`host=` パラメータでソケットのパスを指定する形です。このソケットは、デプロイ時の `--add-cloudsql-instances` によってコンテナ内に作られます。

#### 登録コマンド

```bash
# 1) パスワードを画面に出さずに入力する
read -s "DB_PASSWORD?Cloud SQL のパスワード: "; echo

# 2) Cloud Run 用の接続URLを組み立てる
DB_URL="postgresql://founder:${DB_PASSWORD}@/founder?host=/cloudsql/notebooklm-482403:asia-northeast1:founder-db"

# 3) シークレットの入れ物を作る（値はまだ入れない）
gcloud secrets create DATABASE_URL \
  --replication-policy=automatic \
  --project=notebooklm-482403

# 4) 値をバージョン1として登録する（標準入力から渡す）
printf '%s' "$DB_URL" | gcloud secrets versions add DATABASE_URL \
  --data-file=- --project=notebooklm-482403

# 5) 変数を消す
unset DB_PASSWORD DB_URL
```

> `read -s` は zsh の書き方です。bash を使っている場合は `read -s -p "Cloud SQL のパスワード: " DB_PASSWORD; echo` と書きます。

**なぜこの書き方なのか**

- `read -s` … 入力した文字が画面に表示されず、シェル履歴にも残りません
- 変数経由で渡す … 手順4でシェル履歴に残るのは `printf '%s' "$DB_URL" | ...` という**文字列そのもの**で、展開後の値ではありません。`ps` で他の利用者に見えることもありません（`printf` はシェル組み込みコマンドなので、別プロセスとして起動されません）
- `echo` ではなく `printf '%s'` … `echo` は末尾に改行を付けます。改行が混ざると接続文字列が壊れ、原因の分かりにくい接続エラーになります

#### 登録できたかの確認

パスワード部分を伏せて表示します。

```bash
gcloud secrets versions access latest --secret=DATABASE_URL --project=notebooklm-482403 \
  | sed -E 's#(postgresql://[^:]+:)[^@]+(@.*)#\1********\2#'; echo
```

次のとおり表示されれば正しく登録できています。

```text
postgresql://founder:********@/founder?host=/cloudsql/notebooklm-482403:asia-northeast1:founder-db
```

> **パスワードに記号が含まれる場合の注意**
> `@ : / ? # %` はURLの区切り文字として解釈されるため、そのまま含めると接続文字列が壊れます。上の確認出力が期待どおりにならない場合は、パーセントエンコード（`@` → `%40` など）が必要です。

### 3-4. サービスアカウントにシークレットの読み取り権限を付ける

Cloud Run は既定の compute サービスアカウント `525613033246-compute@developer.gserviceaccount.com` で動きます。

このアカウントはプロジェクトに対して `roles/editor` を持っていますが、**`roles/editor` には `secretmanager.versions.access` が含まれていません。**シークレットの中身を読む権限は、基本ロールからは意図的に除外されています。そのため個別の付与が必要です。

```bash
SA="serviceAccount:525613033246-compute@developer.gserviceaccount.com"

for S in JWT_SECRET_KEY GEMINI_API_KEY QDRANT_URL QDRANT_API_KEY DATABASE_URL; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="$SA" \
    --role="roles/secretmanager.secretAccessor" \
    --project=notebooklm-482403
done
```

**プロジェクト全体ではなくシークレット単位で付けています。**プロジェクト全体に付けると、将来追加される別のシークレットまで自動的に読めてしまうためです。

**この付与はデプロイより先に行ってください。**`--set-secrets` を付けたデプロイは、その時点で各シークレットを読めるかを Cloud Run が検証します。1件でも権限が欠けていると、コンテナが起動しないのではなく**デプロイコマンド自体が失敗**します。

確認（5行すべてに `roles/secretmanager.secretAccessor` が出れば正常）:

```bash
for S in JWT_SECRET_KEY GEMINI_API_KEY QDRANT_URL QDRANT_API_KEY DATABASE_URL; do
  gcloud secrets get-iam-policy "$S" --project=notebooklm-482403 \
    --flatten="bindings[].members" \
    --filter="bindings.members:525613033246-compute@developer.gserviceaccount.com" \
    --format="value(bindings.role)" | sed "s|^|$S : |"
done
```

#### Cloud SQL と GCS の権限は追加不要

参考として、なぜこの2つは付与しなくてよいかを記録しておきます。

| 権限 | 状況 |
|---|---|
| `cloudsql.instances.connect` | `roles/editor` に含まれる |
| `storage.objects.create` / `delete` / `list` | バケットに付いている `projectEditor:` 宛の `roles/storage.legacyBucketOwner` が持つ |
| `storage.objects.get` | 同じく `projectEditor:` 宛の `roles/storage.legacyObjectOwner` が持つ |

これは「既定のサービスアカウントを使っている限り」成り立つ話です。専用サービスアカウントへ移行する場合は、上記3つをすべて明示的に付与する必要があります（→ [7. やってはいけないこと](#7-やってはいけないこと)の後の補足を参照）。

### 3-5. Docker から Artifact Registry へ認証できるようにする

```bash
gcloud auth configure-docker asia-northeast1-docker.pkg.dev
```

`~/.docker/config.json` に認証ヘルパーの設定が書かれ、`docker push` のたびに `gcloud` の認証情報が使われるようになります。作業する PC ごとに1回だけ必要です。

---

## 4. デプロイ手順

ここからが毎回の作業です。**リポジトリのルートディレクトリで実行してください。**

### 4-1. イメージ名を決める

```bash
IMAGE=asia-northeast1-docker.pkg.dev/notebooklm-482403/founder/founder:v1
```

`:v1` の部分（タグ）は毎回変えることを勧めます。同じタグを上書きすると、問題があったときに前のイメージへ戻せなくなるためです。`:v2` `:v3` や、日付（`:20260808`）でも構いません。

### 4-2. `linux/amd64` を明示してビルドする

```bash
docker build --platform linux/amd64 -t "$IMAGE" .
```

**`--platform linux/amd64` は必須です。省略しないでください。**

Apple Silicon（M1/M2/M3）の Mac は CPU アーキテクチャが **arm64** です。`docker build` は何も指定しないとホストと同じ arm64 のイメージを作ります。一方 **Cloud Run が実行できるのは amd64（x86_64）のイメージだけ**です。

arm64 のイメージを push してデプロイすると、次のようになります。

1. `docker push` は**成功する**
2. `gcloud run deploy` も**成功したように見える**
3. しかしコンテナが起動せず、リビジョンの作成に失敗する

「コマンドは全部通ったのにアプリが動かない」という切り分けの難しい状態になるため、ビルドの時点で明示します。Intel Mac や Linux（x86_64）で作業している場合も、付けておいて害はありません。

> エミュレーション経由のビルドになるため、Apple Silicon では**数分かかります**。初回は特に時間がかかります。

### 4-3. アーキテクチャを確認する（push 前に必ず）

```bash
docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}'
```

```text
linux/amd64   ← これなら push してよい
linux/arm64   ← push しない。4-2 からやり直す
```

### 4-4. push する

```bash
docker push "$IMAGE"
```

### 4-5. デプロイする

```bash
gcloud run deploy founder \
  --project=notebooklm-482403 \
  --region=asia-northeast1 \
  --image="$IMAGE" \
  --allow-unauthenticated \
  --add-cloudsql-instances=notebooklm-482403:asia-northeast1:founder-db \
  --set-env-vars=APP_ENV=production,GCS_BUCKET_NAME=founder-sources-482403 \
  --set-secrets=JWT_SECRET_KEY=JWT_SECRET_KEY:1,GEMINI_API_KEY=GEMINI_API_KEY:1,QDRANT_URL=QDRANT_URL:1,QDRANT_API_KEY=QDRANT_API_KEY:1,DATABASE_URL=DATABASE_URL:1
```

#### 各フラグの意味

| フラグ | 意味 |
|---|---|
| `founder` | サービス名。この名前で URL が決まる |
| `--project` | 対象の GCP プロジェクト |
| `--region=asia-northeast1` | Cloud SQL・GCS と同じリージョン |
| `--image` | デプロイするイメージ。4-4 で push したもの |
| `--allow-unauthenticated` | GCP の認証なしで URL を開けるようにする（後述） |
| `--add-cloudsql-instances` | コンテナ内に `/cloudsql/<接続名>` のソケットを作る。`DATABASE_URL` の `host=` が指す先がこれ |
| `--set-env-vars` | 機密でない設定を渡す |
| `--set-secrets` | Secret Manager の値を環境変数として注入する |

#### `--allow-unauthenticated` について

URL を知っていれば誰でもページを開ける状態になります。**素通しではなく、アプリ側のログイン画面が最初に出ます。**

デモで URL を開くだけで見せられるようにするため、この設定を選んでいます。ただし現在の認証は社員コードだけで判定し、パスワードは任意の文字列で通る MVP の実装です。**社内の実運用に移す前に、認証の強化かアクセス制限の追加が必要です。**

#### `--set-secrets` の書式

```text
環境変数名=シークレット名:バージョン番号
```

左がアプリが読む環境変数名、右が Secret Manager 側の名前です。両者は同じ名前に揃えてあります。対応表を持たずに済み、`app/config.py` の `os.getenv()` まで同じ名前で一直線に追えるからです。

**バージョンは番号で固定します。`:latest` は使いません。**理由は `docs/secrets.md` の「バージョン指定の方針」に詳しく書いてあります。要点だけ言うと、`latest` はインスタンスが起動した時点の最新版を読むため、ローテーション中に新旧の鍵を持つインスタンスが混在し、「たまにログアウトされる」という再現しにくい障害になります。

現在参照しているバージョン番号は次で確認できます。

```bash
for S in JWT_SECRET_KEY GEMINI_API_KEY QDRANT_URL QDRANT_API_KEY DATABASE_URL; do
  echo "--- $S"
  gcloud secrets versions list "$S" --project=notebooklm-482403 --format="table(name,state)"
done
```

シークレットを更新した場合は、上のコマンドで新しい番号を確認し、デプロイコマンドの番号を書き換えてください。

#### `APP_ENV=production` の意味

`app/config.py` は `APP_ENV` が `local` / `test` のときだけ開発用の既定値を許します。`production` を渡すことで、開発用の署名鍵が使われる余地が無くなります。

**渡し忘れても危険にはなりません。**未設定も本番相当として扱われる設計になっているためです（設定漏れは安全な側に倒す）。

### 4-6. 動作確認

URL を取得します。

```bash
gcloud run services describe founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --format="value(status.url)"
```

> このコマンドが返すのは `https://founder-nbwdutevlq-an.a.run.app` の形式です。冒頭に記載した `https://founder-525613033246.asia-northeast1.run.app` と文字列は違いますが、**同じサービスを指しています。**どちらを開いても構いません。

ブラウザで開き、次を確認します。

| ログイン | 確認すること |
|---|---|
| `EMP001`（社員） | ログインできる／チャットで質問して回答が返る |
| `ADMIN`（社長） | ソース管理・スタッフ一覧・社員データが表示される／ソースのアップロードと削除ができる |

パスワードは任意の文字列で構いません（MVP の仕様）。

**ファイルが消えないことの確認**もしてください。ソースを1件アップロードしたあと、新しいリビジョンをデプロイするか、しばらく置いてコンテナが入れ替わったあとで、ソース一覧に残っていることを確認します。残っていれば GCS と Cloud SQL に正しく保存されています。

---

## 5. 2回目以降の注意

### `--set-*` ではなく `--update-*` を使う

**既存サービスの設定変更に `--set-env-vars` / `--set-secrets` を使ってはいけません。**

| フラグ | 挙動 |
|---|---|
| `--set-env-vars` | 指定しなかった既存の環境変数を**すべて削除する** |
| `--set-secrets` | 指定しなかった既存のシークレット設定を**すべて削除する** |
| `--update-env-vars` | 指定したものだけ追加・変更する（他は維持） |
| `--update-secrets` | 指定したものだけ追加・変更する（他は維持） |

たとえば `GCS_BUCKET_NAME` だけ変えるつもりで次を実行すると、`APP_ENV` が消えます。

```bash
# 危険な例。APP_ENV が消える
gcloud run services update founder --set-env-vars=GCS_BUCKET_NAME=別のバケット ...
```

正しくは次のようにします。

```bash
gcloud run services update founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --update-env-vars=GCS_BUCKET_NAME=別のバケット
```

シークレットのバージョンを差し替える場合も同じです。

```bash
gcloud run services update founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --update-secrets=GEMINI_API_KEY=GEMINI_API_KEY:2
```

不要になった設定を消したいときは、削除専用の `--remove-env-vars` / `--remove-secrets` を使います。

> 4-5 のデプロイコマンドで `--set-*` を使っているのは、**サービスを新規に作るときだけ**の話です。既存の設定が無いため、消える心配がありません。新しいイメージをデプロイし直すだけなら `--image` の変更だけで済みます。

### 新しいイメージだけを反映する場合

```bash
IMAGE=asia-northeast1-docker.pkg.dev/notebooklm-482403/founder/founder:v2

docker build --platform linux/amd64 -t "$IMAGE" .
docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}'   # linux/amd64 を確認
docker push "$IMAGE"

gcloud run services update founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --image="$IMAGE"
```

環境変数とシークレットの設定はサービスに保持されているので、渡し直す必要はありません。

### 前のリビジョンへ戻す

デプロイ後に問題が見つかったときは、トラフィックを前のリビジョンへ戻せます。

```bash
# リビジョン一覧を見る
gcloud run revisions list --service=founder \
  --project=notebooklm-482403 --region=asia-northeast1

# 指定したリビジョンへ100%戻す
gcloud run services update-traffic founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --to-revisions=<戻したいリビジョン名>=100
```

---

## 6. トラブル時の確認方法

### ログを読む

```bash
gcloud run services logs read founder \
  --project=notebooklm-482403 --region=asia-northeast1 --limit=50
```

`--limit` で件数を変えられます。

`Dockerfile` で `PYTHONUNBUFFERED=1` を設定しているため、Python の出力は溜め込まれずすぐログに現れます。起動に失敗した場合でも、原因のスタックトレースが最後まで残ります。

重大度で絞り込むなど、より細かく見たい場合は Cloud Logging を直接読みます。

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="founder"' \
  --project=notebooklm-482403 --limit=50 \
  --format="value(timestamp,severity,textPayload)"
```

エラーだけを見たい場合は、フィルタに条件を足します。

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="founder" AND severity>=ERROR' \
  --project=notebooklm-482403 --limit=20 \
  --format="value(timestamp,severity,textPayload)"
```

### 現在の設定を確認する

```bash
# サービス全体（URL・リビジョン・トラフィック配分）
gcloud run services describe founder \
  --project=notebooklm-482403 --region=asia-northeast1

# URL だけ
gcloud run services describe founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --format="value(status.url)"

# 使われているサービスアカウント
gcloud run services describe founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --format="value(spec.template.spec.serviceAccountName)"

# 現在デプロイされているイメージ
gcloud run services describe founder \
  --project=notebooklm-482403 --region=asia-northeast1 \
  --format="value(spec.template.spec.containers[0].image)"
```

### よくあるエラー

| 症状 | 原因 | 対処 |
|---|---|---|
| デプロイ時に `Permission denied on secret` | サービスアカウントに `secretAccessor` が無い | [3-4](#3-4-サービスアカウントにシークレットの読み取り権限を付ける) を実施する |
| デプロイは成功するがリビジョンが起動しない | イメージが arm64 | [4-2](#4-2-linuxamd64-を明示してビルドする) からやり直す |
| ログに `JWT_SECRET_KEY が設定されていません` | シークレットが注入されていない | `--set-secrets` の指定と、参照しているバージョン番号が有効か確認する |
| ログに `GCS_BUCKET_NAME が設定されていません` | 環境変数が渡っていない | `--set-env-vars` を確認する。本番相当ではローカル保存にフォールバックしない設計 |
| ログに `DATABASE_URL が設定されていません` | シークレットが注入されていない | 上と同じ。`--add-cloudsql-instances` も付いているか確認する |
| ログに `could not connect to server` 系 | `--add-cloudsql-instances` が無い、または接続名が違う | 接続名 `notebooklm-482403:asia-northeast1:founder-db` を確認する |
| ログに `Memory limit exceeded` | 既定の 512MiB では不足 | `--memory=1Gi` を付けて再デプロイする |
| `--allow-unauthenticated` が拒否される | 組織ポリシー（ドメイン制限共有）が有効 | リポジトリオーナーに相談する |

### ローカルのコンテナで再現させる

Cloud Run で起きた問題を手元で切り分けたいときは、同じイメージをローカルで動かせます。ローカルでは Cloud Run が自動でやってくれる部分を手で補う必要があります。

前提として、別ターミナルで Cloud SQL Auth Proxy を起動しておきます。

```bash
cloud-sql-proxy notebooklm-482403:asia-northeast1:founder-db --port 5432
```

値をシェルの環境変数に入れてから起動します。

```bash
export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=GEMINI_API_KEY --project=notebooklm-482403)
export QDRANT_URL=$(gcloud secrets versions access latest --secret=QDRANT_URL --project=notebooklm-482403)
export QDRANT_API_KEY=$(gcloud secrets versions access latest --secret=QDRANT_API_KEY --project=notebooklm-482403)

read -s "DB_PASSWORD?Cloud SQL のパスワード: "; echo
export DATABASE_URL="postgresql://founder:${DB_PASSWORD}@host.docker.internal:5432/founder"
```

```bash
docker run --rm --name founder-local \
  -p 8080:8080 \
  -e PORT=8080 \
  -e APP_ENV=local \
  -e GCS_BUCKET_NAME=founder-sources-482403 \
  -e GOOGLE_CLOUD_PROJECT=notebooklm-482403 \
  -e DATABASE_URL \
  -e GEMINI_API_KEY \
  -e QDRANT_URL \
  -e QDRANT_API_KEY \
  -v "$HOME/.config/gcloud/application_default_credentials.json":/home/appuser/.config/gcloud/application_default_credentials.json:ro \
  founder:local
```

ローカルだけで必要になるものと、その理由です。

| 項目 | 理由 |
|---|---|
| ADC ファイルのマウント | Cloud Run ではサービスアカウントが自動で使われるが、ローカルには無いため作業者の認証情報を渡す |
| `GOOGLE_CLOUD_PROJECT` | `app/storage.py` の `gcs.Client()` はプロジェクトを指定していない。Cloud Run はメタデータサーバーから解決できるが、ローカルでは解決できず起動に失敗する |
| `host.docker.internal:5432` | Cloud Run の Unix ソケットの代わりに、ホストで動く `cloud-sql-proxy` へ繋ぐ |
| `-e 変数名`（`=` を書かない） | 現在のシェルの値をそのまま渡す書き方。値がコマンドラインに現れないため、`ps` や履歴から漏れない |

> **この ADC マウントは検証専用です。**作業者個人の GCP 認証情報をコンテナへ渡しています。Cloud Run では不要です。

---

## 7. やってはいけないこと

- **`.env` をイメージに含める** … `.dockerignore` で除外しています。外すとイメージを取得できる人全員に鍵が渡ります
- **`Dockerfile` や `docs/` にシークレットの値を書く** … Git に残ります
- **シークレットの値をチャット・Issue・PR に貼る** … 受け渡しはファイルパスかクリップボード経由で
- **`--set-env-vars` / `--set-secrets` を既存サービスに使う** … 指定しなかった設定が消えます（[5章](#5-2回目以降の注意)）
- **`--platform linux/amd64` を省略する** … Apple Silicon では起動しないイメージができます（[4-2](#4-2-linuxamd64-を明示してビルドする)）
- **シークレットのバージョンに `:latest` を使う** … ローテーション時に再現しにくい障害を生みます
- **同じプロジェクトの `rag-app`（Cloud Run サービス／Artifact Registry リポジトリ）を操作する** … 別アプリのものです

---

## 補足：既定のサービスアカウントを使っている件

現在は既定の compute サービスアカウント（`roles/editor` を持つ）を使っています。専用のサービスアカウントを作って権限を絞るほうが望ましい構成ですが、次の理由で今回は見送っています。

- Issue #66 のスコープに含まれていない
- 構成要素を増やすと、把握している人が限られて属人化する

専用サービスアカウントへ移行する場合、必要な権限は次のとおりです（`roles/editor` が暗黙に補っていた分を明示する必要があります）。

| 付与先 | ロール |
|---|---|
| シークレット5件（個別に） | `roles/secretmanager.secretAccessor` |
| プロジェクト `notebooklm-482403` | `roles/cloudsql.client` |
| バケット `gs://founder-sources-482403` | `roles/storage.objectAdmin` |

---

## 関連ドキュメント

- 機密情報の管理方針・ローテーション手順 … `docs/secrets.md`
- ローカル開発の起動手順 … `README.md`
- GitHub の運用ルール … `docs/github-workflow.md`
- UI の確認方法 … `docs/ui-review.md`
