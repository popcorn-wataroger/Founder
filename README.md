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
cp .env.example .env
# .env を編集して GEMINI_API_KEY 等を設定
uv run uvicorn app.main:app --reload
```

http://localhost:8000 にアクセス。

## ファイルの保存先

アップロードされたファイルは Google Cloud Storage に保存する。保存・読み出し・削除は `app/storage.py` にまとめてあり、呼び出し側（ルーターや本文抽出）は保存先を意識しない。

| `GCS_BUCKET_NAME` | `APP_ENV` | 保存先 |
|---|---|---|
| 設定あり | 何でも | GCS |
| 未設定 | `local` / `test` | ローカルの `uploads/` |
| 未設定 | 上記以外（未設定を含む） | **起動しない** |

GCP の認証情報が無くてもローカル開発を進められるよう、`GCS_BUCKET_NAME` が空なら `uploads/` にフォールバックする。ただし本番相当の環境では起動を止める。Cloud Run のコンテナは停止すると中身が消えるため、フォールバックすると「アップロードは成功したのに次のアクセスでファイルが無い」状態を、エラーも出さずに作ってしまうため。

GCS を使う場合は、バケットへのアクセス権を持つ認証情報が必要になる。ローカルでは以下を実行しておけば `.env` の `GOOGLE_APPLICATION_CREDENTIALS` は空のままでよい。

```bash
gcloud auth application-default login
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
