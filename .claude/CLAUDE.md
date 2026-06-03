# Founder プロジェクト — 個人作業ルール

## 絶対に守ること

- main ブランチへ直接 push しない
- Issue を作らずに作業を始めない
- .env や API キーを commit しない
- よくわからないまま `git reset --hard` や `git push --force` を使わない

## 作業開始前チェック

1. 対応する Issue が GitHub に存在するか確認する
2. Issue 番号を控える（例: #3）
3. main を最新にしてからブランチを切る

```bash
git checkout main
git pull origin main
git checkout -b feature/Issue番号-作業名
uv sync
```

## ブランチ名ルール

| 種別 | 用途 | 例 |
|---|---|---|
| feature/ | 新機能追加 | feature/12-login-validation |
| fix/ | バグ修正 | fix/18-login-error |
| docs/ | ドキュメント修正 | docs/21-github-workflow |
| refactor/ | 挙動を変えない整理 | refactor/24-chat-js |

## 開発中の動作確認コマンド

```bash
uv run uvicorn app.main:app --reload
```

ブラウザで http://localhost:8000 を開く。

確認アカウント：
- 社員画面 → EMP001
- 管理者画面 → ADMIN
- パスワードは任意

## PR作成前チェック

- [ ] 不要なファイル（.DS_Store, .env, .venv）が入っていない
- [ ] 対応する Issue がある
- [ ] PR に `Closes #番号` を書いた
- [ ] ローカルで動作確認した
- [ ] UI を変更した場合は社員・管理者両画面を確認した

## PRの書き方テンプレート

```
Closes #番号

## 変更内容
- （何をしたか）

## 動作確認
- `uv run uvicorn app.main:app --reload` で起動
- EMP001 で社員画面を確認
- ADMIN で管理者画面を確認

## 相談事項
- （あれば）
```

## 困ったときに共有する情報

```bash
git status
git branch
git log --oneline -5
```

エラーが出た場合はエラーメッセージ全文と「何をしようとしたか」を一緒に共有する。

## 技術スタック（参照用）

| 領域 | 技術 |
|---|---|
| フロントエンド | HTML / CSS / JavaScript（バニラ） |
| バックエンド | Python / FastAPI |
| AI | OpenAI API（GPT-4o） |
| ベクトルDB | Qdrant |
| ストレージ | Google Cloud Storage |
| ホスティング | GCP（Cloud Run + Cloud SQL） |

## 参照ドキュメント

- チームの GitHub 運用ルール → `docs/github-workflow.md`
- 要件定義書 → `docs/requirements.md`
- UI 確認方法 → `docs/ui-review.md`

## 実装時の理解ルール

- 実装前に前提知識が不足していないか確認する（APIとは？など）
- 既存コードのフォルダ・ファイルが何をしているかざっくり把握する
- 画面を触ったら「どの操作でどのAPIが呼ばれ、画面がどう変わるか」説明できるようにする
- エラー処理を入れたら「どんなときにエラーになるか」説明できるようにする
- 関数を作ったら「入力・処理・出力が何か」説明できるようにする
- なるべくファイルごと・関数ごとに役割を分離する

## Claudeへの指示

- コードを書いた後は必ず「このコードがアプリ全体で何の役割を持つか」を昇悟さんが自分の言葉で説明できるまで解説する
- 昇悟さんが理解できていない場合は次のステップに進まない
- わからない専門用語が出たら都度説明する
- 実装前に必要な前提知識を確認してから作業を始める

## PR作成前の必須確認（順番通りに実施）

PR作成ボタンを押す前に必ず以下を順番に確認すること。

1. Issueの完了条件を一つずつチェック
2. Issueの確認方法に沿って動作確認
3. PR作成前チェックリスト
   - 不要なファイル（.DS_Store, .env, .venv）が入っていない
   - 対応するIssueがある
   - PRに「Closes #番号」を書く
   - ローカルで動作確認した
   - UIを変更した場合は社員・管理者両画面を確認
   - 変更内容を自分の言葉で説明できる
4. 上記が全て完了してからPR作成ボタンを押す

この確認を怠った場合はClaudeが必ずストップをかける。