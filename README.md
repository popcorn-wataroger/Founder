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
