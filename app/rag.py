"""RAG（検索拡張生成）のまとめ役モジュール。

「質問 → 関連チャンク検索 → Geminiで回答生成」という一連の流れを、
1つの関数 answer_question にまとめている。
チャットAPI（ルーター）は後続ステップで、この関数を呼び出す形で実装する。
"""

from google import genai

from app.config import GEMINI_API_KEY
from app.vector_store import search

# 回答の文章生成に使う Gemini のモデル名。
# "gemini-flash-latest" は常に現行の flash モデルを指すエイリアスで、
# 特定バージョンが提供終了(404)しても影響を受けにくい。
GENERATION_MODEL = "gemini-flash-latest"

# 検索で取ってくる関連チャンクの件数（多すぎるとノイズ、少なすぎると情報不足）
TOP_K = 3

# 関連する社内文書が1件も見つからなかったときに返す定型メッセージ
NO_CONTEXT_MESSAGE = "申し訳ありません。社内の資料には、その質問に関する情報が見つかりませんでした。"

# Gemini APIクライアント。APIキーは config 経由で読み込む（コードに直書きしない）
_client = genai.Client(api_key=GEMINI_API_KEY)


def _build_prompt(question: str, chunks: list[str]) -> str:
    """検索で得た関連チャンクと質問を組み合わせ、Geminiに渡すプロンプト文を作る。

    入力:
        question … ユーザーからの質問
        chunks   … 質問に関連する社内文書のチャンク（1件以上ある前提）

    出力:
        Geminiに渡す1つの文字列プロンプト

    ねらい:
        「渡した社内文書の範囲で答えて」と明示することで、
        AIが勝手な推測（ハルシネーション）で答えるのを抑える。
    """
    # 各チャンクに番号を振って読みやすく並べる
    context = "\n\n".join(f"【文書{i + 1}】\n{c}" for i, c in enumerate(chunks))

    # 「参考資料 → 質問 → 指示」の順に組み立てる
    return (
        "あなたは社内向けのAIアシスタントです。"
        "以下の社内文書だけを参考にして、質問に日本語で答えてください。"
        "文書に書かれていない内容は、推測せず「資料には記載がありません」と伝えてください。\n\n"
        f"=== 社内文書 ===\n{context}\n\n"
        f"=== 質問 ===\n{question}\n\n"
        "=== 回答 ==="
    )


def answer_question(question: str) -> str:
    """質問を受け取り、社内文書に基づいたAIの回答文を返す（RAGの中核）。

    入力:
        question … ユーザーからの質問テキスト

    出力:
        AIが生成した回答の文章（文字列）

    処理:
        1. vector_store.search で質問に関連するチャンクを取得
        2. 関連チャンクが0件なら、定型メッセージを返して終了
        3. 関連チャンクと質問からプロンプトを組み立てる
        4. Gemini の文章生成モデルに渡して回答を生成
        5. 生成された回答テキストを返す
    """
    # 1. 質問に意味が近い社内文書のチャンクを検索する
    chunks = search(question, top_k=TOP_K)

    # 2. 関連する文書が1件も無ければ、無理に生成せず定型文を返す
    #    （根拠が無いのにAIが答えると、誤情報を生みやすいため）
    if not chunks:
        return NO_CONTEXT_MESSAGE

    # 3. 参考文書＋質問をまとめたプロンプトを作る
    prompt = _build_prompt(question, chunks)

    # 4. Gemini にプロンプトを渡し、回答文を生成してもらう
    response = _client.models.generate_content(
        model=GENERATION_MODEL,
        contents=prompt,
    )

    # 5. 生成された回答テキストを返す
    return response.text
