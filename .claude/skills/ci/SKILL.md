---
name: ci
description: "ローカルでCIと同じチェック（ruff lint / ruff format / mypy / pytest）を順番に実行し、GitHub Actions で落ちる前に問題を見つける。"
when_to_use: "ユーザーが「CIチェックして」「CI回して」「テスト回して」「テストして」「lintかけて」「型チェックして」などと頼んだとき。コードを変更したあと、PRを出す前に確認したいときも含む。"
---

ローカルでCIと同じチェックを順番に実行してください。

以下のコマンドを順番に実行し、各ステップの結果を日本語で報告してください。
1ステップでもエラーが出たら停止して、エラーの内容と修正方法を教えてください。

## ステップ1: Lint（ルール違反チェック）
```
uv run ruff check .
```

## ステップ2: Format（フォーマットチェック）
```
uv run ruff format --check .
```

## ステップ3: Type Check（型チェック）
```
uv run mypy app/
```

## ステップ4: Test（テスト実行）
```
GEMINI_API_KEY=dummy-key-for-ci APP_ENV=test uv run pytest -q --tb=short
```

**ダミーのAPIキーを渡している理由:**

`app/vectorizer.py` は import された時点で Gemini のクライアントを生成します。
そのため `GEMINI_API_KEY` が空だと、テストが始まる前に次のエラーで止まります。

```
ValueError: No API key was provided.
Interrupted: 4 errors during collection
```

これは**テストが壊れているわけでも、環境構築に失敗しているわけでもありません。**
`.env.example` の `GEMINI_API_KEY` は空欄なので、環境を作ったばかりだと必ずこの状態になります。

テストは実際の Gemini API を呼ばないので、ダミーの値で問題ありません。
`.github/workflows/ci.yml` も同じ方法でテストを実行しています。
本物のAPIキーを取得する必要はありません。

## 完了報告

全ステップが通ったら「✓ CIチェック全通過。PRを出せます。」と報告してください。

PRを作る場合は、続けて `/pr` の手順（PR作成前の必須確認）に進んでください。
