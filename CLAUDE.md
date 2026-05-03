# Founder — 社内AIチャットボット

## プロジェクト概要

NotebookLM的な社内ナレッジQAボット。社長がソースをアップロードし、社員がLINE風チャットで質問するとAIが回答する。権限制御あり（社員は共通ソースのみ、社長は全ソース参照可能）。

## コマンド

```bash
# 開発サーバー起動
uvicorn app.main:app --reload

# 依存パッケージ追加時
pip install <package> && pip freeze > requirements.txt
```

## 技術スタック

- フロントエンド: HTML / CSS / JavaScript（バニラ。フレームワーク不使用）
- バックエンド: Python 3.11+ / FastAPI
- AI: OpenAI API（GPT-4o）
- ベクトルDB: Qdrant
- ファイルストレージ: Google Cloud Storage
- ホスティング: GCP（Cloud Run + Cloud SQL）
- フォント: Noto Sans JP + DM Sans（Google Fonts）

## ディレクトリ構成

```
Founder/
├── app/
│   └── main.py              # FastAPI エントリーポイント
├── static/
│   ├── css/style.css
│   ├── js/
│   │   ├── login.js
│   │   ├── chat.js
│   │   └── admin.js
│   └── index.html            # UIプロトタイプ（叩き台）
├── docs/
│   └── requirements.md       # 要件定義書（詳細版）
├── requirements.txt
├── .env.example
└── .gitignore
```

## コードスタイル

- Python: PEP 8準拠、型ヒント必須、async/await使用
- JavaScript: バニラJS、ES6+、セミコロンあり、シングルクォート不使用
- CSS: カスタムプロパティ（CSS変数）でテーマ管理、BEM不要
- 命名: Python=snake_case、JS=camelCase、CSS=kebab-case
- コメント: 日本語OK

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
- ログインで社員コード＋パスワード → ロール判定（employee / admin）
- ADMIN=管理者、EMP001〜EMP006=社員

## データモデル

### users（社員マスタ）
user_id(PK), employee_code, name, department, gender, birth_date, family, hire_date, employment_type, role(employee/admin), password_hash, last_login_at

### sources（ソース）
source_id(PK), file_name, file_type(pdf/docx/pptx/txt/url), file_path, scope(common/individual), owner_user_id(FK→users, NULLなら共通), uploaded_at, uploaded_by(FK→users)

### chat_sessions（チャットセッション）
session_id(PK), user_id(FK→users), started_at, context_type(general/staff_inquiry)

### chat_messages（チャットメッセージ）
message_id(PK), session_id(FK→chat_sessions), role(user/assistant), content, created_at, referenced_sources(参照ソースIDリスト)

## 権限ルール

IMPORTANT: これらの権限ルールは絶対に守ること。

| 操作 | 社員 | 社長 |
|------|------|------|
| 共通ソースでAIに質問 | ○ | ○ |
| 自分の個別ソースでAIに質問 | △（将来） | ○ |
| 他人の個別ソースでAIに質問 | ✕ 絶対不可 | ○ |
| ソースのアップロード・削除 | ✕ | ○ |
| チャットログ閲覧 | 自分のみ | 全員分 |
| 社員データ閲覧 | ✕ | ○ |

- DBにはソースの owner_user_id フィールドを必ず持たせる（将来の本人閲覧対応のため）
- 社員がAIに質問した場合、個別ソース（他人の評価・給与情報）は絶対に回答に含めない

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
