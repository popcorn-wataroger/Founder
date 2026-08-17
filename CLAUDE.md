# Founder — 社内AIチャットボット

## プロジェクト概要

NotebookLM的な社内ナレッジQAボット。社長がソースをアップロードし、社員がLINE風チャットで質問するとAIが回答する。権限制御あり（社員は共通ソースのみ、社長は全ソース参照可能）。

## コマンド

```bash
# GCP の認証情報を用意する（初回のみ。scripts/dev.py が Secret Manager を読むため必要）
gcloud auth application-default login

# 開発サーバー起動（Secret Manager から機密情報を取得してから uvicorn を起動する）
uv run python scripts/dev.py

# 依存パッケージ追加時
uv add <package>
```

機密情報の扱いは `docs/secrets.md` を参照。

## 技術スタック

- フロントエンド: HTML / CSS / JavaScript（バニラ。フレームワーク不使用）
- バックエンド: Python 3.11+ / FastAPI
- AI: Gemini API
- ベクトルDB: Qdrant
- ファイルストレージ: Google Cloud Storage
- ホスティング: GCP（Cloud Run + Cloud SQL）
- フォント: Noto Sans JP + DM Sans（Google Fonts）

## ディレクトリ構成

```
Founder/
├── app/
│   ├── main.py               # FastAPI エントリーポイント。ルーター登録と起動時のDB初期化
│   ├── config.py             # 環境変数の読み込みと検証（未設定なら起動を止める）
│   ├── database.py           # Cloud SQL(PostgreSQL) への接続とテーブル作成
│   ├── users.py              # 社員マスタ(data/users.csv)を起動時に読み込む
│   ├── user_logins.py        # 最終ログイン日時の記録・取得（user_logins テーブル）
│   ├── storage.py            # ファイルの保存・読み出し・削除。保存先(GCS/ローカル)を隠す
│   ├── upload_paths.py       # 保存先パスの安全な組み立て（パストラバーサル対策）
│   ├── vectorizer.py         # ソースの本文抽出とベクトル化
│   ├── vector_store.py       # Qdrant への保存・検索
│   ├── rag.py                # 質問→検索→Geminiで回答生成のまとめ役
│   ├── chat_history.py       # チャットセッション・メッセージのDB操作
│   └── routers/
│       ├── auth_router.py    # ログイン・JWT発行・権限判定
│       ├── chat_router.py    # チャットAPI（一括／SSEストリーミング）
│       └── sources_router.py # ソースのアップロード・一覧・削除・ダウンロード
├── static/
│   ├── css/style.css
│   ├── js/
│   │   ├── login.js
│   │   ├── chat.js
│   │   └── admin.js
│   └── index.html            # 全画面のUIプロトタイプ
├── scripts/
│   └── dev.py                # Secret Manager から機密情報を取得して開発サーバーを起動
├── tests/                    # pytest
├── data/
│   └── users.csv             # 社員マスタ（ダミーデータ）
├── docs/
│   ├── github-workflow.md    # GitHub運用ルール
│   ├── requirements.md       # 要件定義書（詳細版）
│   ├── ui-review.md          # UI確認方法
│   ├── secrets.md            # 機密情報の扱い
│   ├── deploy.md             # Cloud Run へのデプロイ手順
│   ├── ci-guide.md           # CI の構成
│   └── vector-db-research.md # ベクトルDBの選定調査
├── .claude/
│   ├── CLAUDE.md             # 個人の作業ルール
│   └── skills/               # PR作成・CI確認用のスキル
├── .github/
│   └── workflows/            # CI（ruff / mypy / pytest）・CodeQL・Qdrant死活監視
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── README.md
├── .env.example
└── .gitignore
```

## コードスタイル

- Python: PEP 8準拠、型ヒント必須、async/await使用
- JavaScript: バニラJS、ES6+、セミコロンあり、シングルクォート不使用
- CSS: カスタムプロパティ（CSS変数）でテーマ管理、BEM不要
- 命名: Python=snake_case、JS=camelCase、CSS=kebab-case
- コメント: 日本語OK

## GitHub運用

初心者エンジニアが開発に参加する前提のため、`main` への直接 push は禁止。作業前に必ず Issue を作成し、作業ブランチを作成して、PRレビュー後にマージすること。詳細は `docs/github-workflow.md` を参照。

## UI確認

画面やスタイルを変更した場合は、PR前に社員画面と管理者画面の両方を確認すること。確認手順とPRへの記載例は `docs/ui-review.md` を参照。

## 画面構成（6画面 + モーダル2つ）

```
ログイン
├── [社員] → チャット画面（LINE風AI会話）
└── [社長] → 管理者ホーム
                ├── ソース管理（PDF/Word/PPT/テキスト/URL）
                └── スタッフ一覧（カード形式）
                      └── 社員データ（基本情報・ログ・ソース）
                            ├── [モーダル] AIチャット（個別ソース参照）
                            └── [モーダル] トーク全文（スクロール閲覧）
```

IMPORTANT: static/index.html に全画面のUIプロトタイプあり。デザイン・レイアウトはこれを叩き台にすること。

## 認証（MVP）

- CSVファイルで社員マスタ管理（ダミーデータ）
- ログインで社員コード＋パスワード → ロール判定（employee / source_manager / admin）
- ADMIN=管理者、EMP001〜EMP006=社員、EMP007=共通ソース管理者

## データモデル

### users（社員マスタ）
user_id(PK), employee_code, name, department, gender, birth_date, family, hire_date, employment_type, role(employee/source_manager/admin), password_hash, last_login_at

### sources（ソース）
source_id(PK), file_name, file_type(pdf/docx/pptx/txt/url), file_path, scope(common/individual), owner_user_id(FK→users, NULLなら共通), uploaded_at, uploaded_by(FK→users)

### chat_sessions（チャットセッション）
session_id(PK), user_id(FK→users), started_at, context_type(general/staff_inquiry)

### chat_messages（チャットメッセージ）
message_id(PK), session_id(FK→chat_sessions), role(user/assistant), content, created_at, referenced_sources(参照ソースIDリスト)

## 権限ルール

IMPORTANT: これらの権限ルールは絶対に守ること。

| 操作 | 社員 | 共通ソース管理者 | 社長 |
|------|------|------|------|
| 共通ソースでAIに質問 | ○ | ○ | ○ |
| 自分の個別ソースでAIに質問 | ○ | ○ | ○ |
| 他人の個別ソースでAIに質問 | ✕ 絶対不可 | ✕ 絶対不可 | ○ |
| 全社共通ソースのアップロード | ✕ | ○ | ○ |
| 自分の個別ソースのアップロード | ○ | ○ | ○ |
| 他人の個別ソースのアップロード | ✕ | ✕ | ○ |
| ソースの削除 | ✕ | ✕ | ○ |
| ソースのダウンロード | ✕ | ✕ | ○ |
| チャットログ閲覧 | 自分のみ | 自分のみ | 全員分 |
| 社員データ閲覧 | ✕ | ✕ | ○ |

- 自分が上げたソースを本人が削除できるようにするかは別Issueとする（現状は社長のみ削除できる）
- DBにはソースの owner_user_id フィールドを必ず持たせる（誰の資料かを一意に決め、検索と表示の絞り込みに使うため）
- 社員がAIに質問した場合、個別ソース（他人の評価・給与情報）は絶対に回答に含めない
- role は employee / source_manager / admin の3値。判定は app/routers/auth_router.py の can_upload_common_source() に集約している
- 社員が自分の資料を登録する経路は POST /api/sources/my-upload。scope と owner_user_id はリクエストで指定できず、サーバーがJWTの user_id から決める
- 社員のチャットの検索範囲は「共通ソース＋自分の個別ソース」。他人の個別ソースは app/vector_store.py の search() が構造的に除外する

## ソース管理

- 対応形式: PDF, Word(.docx), PowerPoint(.pptx), テキスト(.txt), ウェブURL
- ソース種別: 「全社共通」or「社員個別」（個別の場合は対象社員を紐付け）
- ファイルはGCSに保存、メタデータはDBに保存
- アップロード時にベクトル化してQdrantに格納

## 社員データ画面の仕様

- 基本情報: 名前、社員コード、部署、性別、生年月日、家族構成、入社日、雇用形態、最終ログイン
- 最近のトーク: 直近N件をプレビュー表示、クリックでモーダル（トーク全文、ページスクロール可能）
- 過去のソース: 一覧表示＋ダウンロード機能＋追加アップロード
- 「このスタッフについてチャット」: AIが共通＋その社員の個別ソースを参照して回答
- 給与グラフ: MVPでは不要（Phase 2）

## チャットログ

- 保持期間: 無期限
- 社長は各社員データ画面からログを閲覧する（独立したログ一覧画面は不要）
- 業務用チャットのログは管理目的で記録される旨、利用規約に明記予定

## 想定規模

- 社員数: 100人以下
- 同時接続: 数十人程度
