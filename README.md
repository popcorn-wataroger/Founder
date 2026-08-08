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

```bash
uv sync
gcloud auth application-default login  # 初回のみ
uv run python scripts/dev.py
```

http://localhost:8000 にアクセス。

機密情報（`JWT_SECRET_KEY` / `GEMINI_API_KEY` / `QDRANT_URL` / `QDRANT_API_KEY`）は `scripts/dev.py` が Google Secret Manager から取得して環境変数に入れるため、`.env` に手で書く必要はありません。詳細は `docs/secrets.md` を参照。

## ファイルの保存先

アップロードされたファイルは Google Cloud Storage に保存する。保存・読み出し・削除は `app/storage.py` にまとめてあり、呼び出し側（ルーターや本文抽出）は保存先を意識しない。

| `GCS_BUCKET_NAME` | `APP_ENV` | 保存先 |
|---|---|---|
| 設定あり | 何でも | GCS |
| 未設定 | `local` / `test` | ローカルの `uploads/` |
| 未設定 | 上記以外（未設定を含む） | **起動しない** |

GCP の認証情報が無くてもローカル開発を進められるよう、`GCS_BUCKET_NAME` が空なら `uploads/` にフォールバックする。ただし本番相当の環境では起動を止める。Cloud Run のコンテナは停止すると中身が消えるため、フォールバックすると「アップロードは成功したのに次のアクセスでファイルが無い」状態を、エラーも出さずに作ってしまうため。

GCS を使う場合は、バケットへのアクセス権を持つ認証情報が必要になる。上のセットアップ手順にある `gcloud auth application-default login`（ADC）を済ませてあれば、認証情報の追加設定は不要（`GOOGLE_APPLICATION_CREDENTIALS` は空のままでよい）。

`scripts/dev.py` が環境変数へ入れるのは Secret Manager 管理対象の4つだけで、`GCS_BUCKET_NAME` は含まれない。そのためローカルで `uv run python scripts/dev.py` を使う限り、既定ではバケット名が空＝`uploads/` 保存になる。ローカルから実際に GCS へ保存して確かめたいときだけ、バケット名を環境変数で渡す。

```bash
GCS_BUCKET_NAME=<バケット名> uv run python scripts/dev.py
```

ダウンロードは署名付きURLではなく、サーバー経由のストリーミング配信にしている。署名付きURLはURLさえあればアプリの権限チェックを通らずに取得できてしまい、「他人の個別ソースは閲覧不可」という権限ルールと噛み合わないため。

## デモ用ログイン

| コード | ロール |
|--------|--------|
| `ADMIN` | 管理者（社長） |
| `EMP001` 〜 `EMP006` | 社員 |

パスワード: 任意

## ドキュメント

- 詳細な要件定義・データモデル・画面仕様は `CLAUDE.md` を参照。
- GitHub の運用ルールは `docs/github-workflow.md` を参照。
- UI の確認方法は `docs/ui-review.md` を参照。
- 機密情報（シークレット）の扱いは `docs/secrets.md` を参照。
