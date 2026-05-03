# GitHub運用ルール

このプロジェクトは、初心者エンジニアでも安全に開発できるように、必ず Issue と Pull Request（PR）ベースで進めます。

## 基本ルール

- `main` ブランチへ直接 push しない
- 作業前に必ず Issue を作成する
- Issue に書かれていない作業を勝手に広げない
- 作業ごとにブランチを作る
- 変更は小さく分ける
- PR には対応する Issue を必ず紐付ける
- PR を作ってからレビューを受ける
- レビューで OK が出てから `main` にマージする
- わからないまま大きく直さず、早めに相談する

## Issue作成

作業を始める前に、必ず Issue を作成します。小さな修正でも Issue を作ってから着手してください。

Issue に書くこと:

- 何をしたいか
- なぜ必要か
- 完了条件
- 確認方法
- 不明点や相談したいこと

Issue の例:

```markdown
## やりたいこと
ログイン画面で、社員コードが空欄のまま送信されたときにエラーを表示する。

## 背景
現状は何も入力しなくても次の画面に進めてしまい、デモ時にわかりにくい。

## 完了条件
- 社員コードが空欄の場合にエラーが表示される
- パスワードが空欄の場合にエラーが表示される
- 正しい入力時はこれまで通りログインできる

## 確認方法
- `uv run uvicorn app.main:app --reload` で起動
- 空欄でログインしてエラー表示を確認
- `ADMIN` でログインできることを確認

## 相談事項
- エラーメッセージの文言は仮でよいか
```

Issue が作成できたら、Issue 番号を確認します。例: `#12`

## ブランチ名

ブランチ名は、Issue 番号と作業内容がわかる名前にします。

```bash
feature/12-login-page
feature/15-chat-ui
fix/18-login-error
docs/21-update-readme
refactor/24-static-js
```

使い分け:

| 種別 | 用途 | 例 |
|------|------|----|
| `feature/` | 新機能追加 | `feature/12-source-upload` |
| `fix/` | バグ修正 | `fix/18-admin-login` |
| `docs/` | ドキュメント修正 | `docs/21-github-workflow` |
| `refactor/` | 挙動を変えない整理 | `refactor/24-chat-js` |

## 作業開始の流れ

```bash
git checkout main
git pull origin main
git checkout -b feature/Issue番号-作業名
uv sync
```

作業前に必ず Issue を作成し、最新の `main` を取り込みます。

## 開発中の確認

アプリを起動して、変更した画面や機能を自分で確認します。

```bash
uv run uvicorn app.main:app --reload
```

確認すること:

- 画面が表示される
- 変更した操作が期待通りに動く
- ブラウザのコンソールに明らかなエラーがない
- Python 側のエラーがターミナルに出ていない
- 関係ない画面が壊れていない

UI変更時の詳しい確認方法は `docs/ui-review.md` を参照してください。

## コミット

コミットは「何をしたか」がわかる単位で作ります。

よい例:

```bash
git add static/js/login.js static/css/style.css
git commit -m "ログイン画面のバリデーションを追加"
```

避ける例:

```bash
git commit -m "修正"
git commit -m "いろいろ変更"
git commit -m "途中"
```

## PR作成前チェック

PR を作る前に、必ず以下を確認します。

```bash
git status
uv sync
uv run uvicorn app.main:app --reload
```

チェック項目:

- 不要なファイル（`.DS_Store`, `.env`, `.venv` など）が入っていない
- 対応する Issue がある
- PR から Issue へリンクしている
- `README.md` や `CLAUDE.md` の手順と実装がズレていない
- 変更内容を自分の言葉で説明できる
- 動作確認した内容を PR に書ける
- UI を変更した場合は `docs/ui-review.md` のチェックを実施している

## PRの書き方

PR には次の内容を書きます。

```markdown
Closes #12

## 変更内容
- ログイン画面に入力チェックを追加
- エラー表示の文言を調整

## 動作確認
- `uv run uvicorn app.main:app --reload` で起動
- ADMIN でログインできることを確認
- 空欄の場合にエラーが表示されることを確認

## 相談事項
- エラーメッセージの文言は仮です
```

`Closes #12` のように書くと、PR がマージされたときに Issue も自動で閉じます。番号は実際の Issue 番号に置き換えてください。

## レビュー対応

レビューコメントをもらったら、同じブランチで修正して push します。

```bash
git add .
git commit -m "レビュー指摘を反映"
git push origin ブランチ名
```

対応が終わったら、PR 上で「修正しました」と返信します。

## マージ後

PR がマージされたら、ローカルの `main` を更新します。

```bash
git checkout main
git pull origin main
```

使い終わったブランチは削除して構いません。

```bash
git branch -d ブランチ名
```

## やってはいけないこと

- `main` へ直接 push する
- Issue を作らずに作業を始める
- Issue と関係ない変更を同じ PR に入れる
- `.env` や API キーを commit する
- よくわからないまま `git reset --hard` や `git push --force` を使う
- 他の人の変更を勝手に消す
- 動作確認せずに PR を出す
- 1つの PR に無関係な変更をたくさん入れる

## 困ったとき

困ったときは、作業を止めて以下を共有して相談します。

```bash
git status
git branch
git log --oneline -5
```

エラーが出ている場合は、エラーメッセージ全文と「何をしようとしたか」を一緒に共有してください。
