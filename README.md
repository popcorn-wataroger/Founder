# Founder

社内AIチャットボット — NotebookLM的なナレッジQAシステム

## 概要

社長がアップロードしたソース（マニュアル、規定、社員個別資料など）をもとに、AIが社員の質問に回答する社内Webアプリ。

- **社員向け：** LINE風チャットUIで社内ナレッジに即座にアクセス
- **社長向け：** ソース管理、チャットログ閲覧、スタッフ管理を一元化

## 技術スタック

| 領域 | 技術 |
|------|------|
| フロントエンド | HTML / CSS / JavaScript（バニラ） |
| バックエンド | Python / FastAPI |
| AI | Gemini API |
| ベクトルDB | Qdrant |
| ストレージ | Google Cloud Storage |
| ホスティング | GCP（Cloud Run + Cloud SQL） |

## セットアップ

初回だけ実行するもの。

```bash
uv sync
gcloud auth application-default login  # GCPの認証情報を用意する
brew install cloud-sql-proxy           # Cloud SQL への接続に使う
```

機密情報（`JWT_SECRET_KEY` / `GEMINI_API_KEY` / `QDRANT_URL` / `QDRANT_API_KEY` / `DATABASE_URL`）は `scripts/dev.py` が Google Secret Manager から取得して環境変数に入れるため、`.env` に手で書く必要はありません。`DATABASE_URL` だけは Cloud Run 用（Unix ソケット指定）の形式で登録されているため、`scripts/dev.py` が取得後に proxy 経由（`localhost:5432`）で繋がる形へ変換してから設定します。詳細は `docs/secrets.md` を参照。

## ローカル開発の起動手順

データベースは Cloud SQL（PostgreSQL）にあります。ローカルからは **Cloud SQL Auth Proxy** 経由で接続するため、ターミナルを2つ使います。

### 1. proxy を起動する（ターミナルA・起動したままにする）

```bash
cloud-sql-proxy notebooklm-482403:asia-northeast1:founder-db --port 5432
```

このコマンドは動かしっぱなしにします。`localhost:5432` へ来た接続を Cloud SQL のインスタンスへ中継する役割で、止めるとアプリはDBに繋がらなくなります。

proxy を使う理由は、Cloud SQL がインターネットへ直接ポートを開けていないためです。認証は `gcloud auth application-default login` で用意した認証情報を proxy が使うので、DBのIPを公開したりファイアウォールを開けたりする必要がありません。

### 2. アプリを起動する（ターミナルB）

```bash
uv run python scripts/dev.py
```

`DATABASE_URL` を手で設定する必要はありません。`scripts/dev.py` が Secret Manager から取得し、proxy 経由で繋がる形に変換して環境変数に入れます。DBのパスワードを人が目にすることなく開発できます。

http://localhost:8000 にアクセス。

### 3. テストを実行するとき

テストは本番と同じ PostgreSQL に対して実行します。**初回だけ**、テスト用の `founder_test` データベースを作成してください。

```bash
gcloud sql databases create founder_test --instance=founder-db --project=notebooklm-482403
```

作成済みかどうかは次で確認できます（一覧に `founder_test` があれば作成済み）。

```bash
gcloud sql databases list --instance=founder-db --project=notebooklm-482403
```

2回目以降は、次のコマンドだけで実行できます。

```bash
uv run python scripts/test.py
```

`scripts/test.py` が接続情報を取得し、接続先を `founder_test` に差し替えてから pytest を起動します。引数はそのまま pytest に渡るため、一部だけ実行することもできます。

```bash
uv run python scripts/test.py -k dev_script
```

> **注意: 接続先を `founder`（開発用）にしないでください。**
> `tests/conftest.py` はテスト1件ごとに `sources` / `chat_sessions` / `chat_messages` / `user_logins` の4テーブルを `TRUNCATE` します。接続先を間違えると、**開発中に入れたデータが消えます。**
>
> `scripts/test.py` は、接続先が `founder_test` であることを確かめてから pytest を起動します。違う場合は pytest を起動せずにエラーで止まるため、開発用DBを指したまま実行されることはありません。`tests/conftest.py` 側でも、接続先のデータベース名に `test` が含まれない場合はテーブルを空にせずに中断します。

CI（GitHub Actions）では `.github/workflows/ci.yml` が PostgreSQL の service コンテナを立て、`DATABASE_URL` を渡しています。proxy は使いません。

## ファイルの保存先

アップロードされたファイルは Google Cloud Storage に保存する。保存・読み出し・削除は `app/storage.py` にまとめてあり、呼び出し側（ルーターや本文抽出）は保存先を意識しない。

| `GCS_BUCKET_NAME` | `APP_ENV` | 保存先 |
|---|---|---|
| 設定あり | 何でも | GCS |
| 未設定 | `local` / `test` | ローカルの `uploads/` |
| 未設定 | 上記以外（未設定を含む） | **起動しない** |

GCP の認証情報が無くてもローカル開発を進められるよう、`GCS_BUCKET_NAME` が空なら `uploads/` にフォールバックする。ただし本番相当の環境では起動を止める。Cloud Run のコンテナは停止すると中身が消えるため、フォールバックすると「アップロードは成功したのに次のアクセスでファイルが無い」状態を、エラーも出さずに作ってしまうため。

GCS を使う場合は、バケットへのアクセス権を持つ認証情報が必要になる。上のセットアップ手順にある `gcloud auth application-default login`（ADC）を済ませてあれば、認証情報の追加設定は不要（`GOOGLE_APPLICATION_CREDENTIALS` は空のままでよい）。

`scripts/dev.py` が環境変数へ入れるのは Secret Manager 管理対象の5つ（`JWT_SECRET_KEY` / `GEMINI_API_KEY` / `QDRANT_URL` / `QDRANT_API_KEY` / `DATABASE_URL`）だけで、`GCS_BUCKET_NAME` は含まれない。そのためローカルで `uv run python scripts/dev.py` を使う限り、既定ではバケット名が空＝`uploads/` 保存になる。ローカルから実際に GCS へ保存して確かめたいときだけ、バケット名を環境変数で渡す。

```bash
GCS_BUCKET_NAME=<バケット名> uv run python scripts/dev.py
```

ダウンロードは署名付きURLではなく、サーバー経由のストリーミング配信にしている。署名付きURLはURLさえあればアプリの権限チェックを通らずに取得できてしまい、「他人の個別ソースは閲覧不可」という権限ルールと噛み合わないため。

## デモ用ログイン

| コード | ロール |
|--------|--------|
| `ADMIN` | 管理者（社長） |
| `EMP007` | 共通ソース管理者 |
| `EMP001` 〜 `EMP006` | 社員 |

パスワード: `password`

MVPの検証用に、全ユーザー共通の値を `data/users.csv` に平文で持たせている。本番運用時はパスワードのハッシュ化と個別設定を行い、ログイン画面の案内表示とこの記載を削除する。

## ドキュメント

- 詳細な要件定義・データモデル・画面仕様は `CLAUDE.md` を参照。
- GitHub の運用ルールは `docs/github-workflow.md` を参照。
- UI の確認方法は `docs/ui-review.md` を参照。
- 機密情報（シークレット）の扱いは `docs/secrets.md` を参照。
- Cloud Run へのデプロイ手順は `docs/deploy.md` を参照。
