# Founder

社内AIチャットボット — NotebookLM的なナレッジQAシステム

## 概要

社長がアップロードしたソース（マニュアル、規定、社員個別資料など）をもとに、AIが社員の質問に回答する社内チャットボット。

## 技術スタック

| 領域 | 技術 |
|------|------|
| フロントエンド | HTML / CSS / JavaScript |
| バックエンド | Python（FastAPI） |
| AI基盤 | OpenAI API |
| ベクトルDB | Qdrant |
| ファイルストレージ | Google Cloud Storage |
| ホスティング | Google Cloud Platform |

## セットアップ

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# 環境変数の設定
cp .env.example .env
# .env を編集して OPENAI_API_KEY 等を設定

# 開発サーバー起動
uvicorn app.main:app --reload
```

ブラウザで http://localhost:8000 にアクセス。

## ログイン（デモ）

| コード | ロール |
|--------|--------|
| `ADMIN` | 管理者（社長） |
| `EMP001` 〜 `EMP006` | 社員 |

パスワード: 任意

## ディレクトリ構成

```
Founder/
├── app/
│   └── main.py              # FastAPI アプリケーション
├── static/
│   ├── css/
│   │   └── style.css         # スタイルシート
│   ├── js/
│   │   ├── login.js          # ログイン画面
│   │   ├── chat.js           # 社員用チャット
│   │   └── admin.js          # 管理者画面
│   └── index.html            # エントリーポイント
├── docs/
│   └── requirements.md       # 要件定義書
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## ライセンス

Private
