"""RAG（検索拡張生成）のまとめ役モジュール。

「質問 → 関連チャンク検索 → Geminiで回答生成」という一連の流れをまとめている。

回答の返し方は2種類ある:
    answer_question        … 全文が生成されるまで待ち、完成した文字列を返す（一括）
    answer_question_stream … 生成された端から少しずつ返す（ストリーミング／SSE用）
"""

import logging
from collections.abc import Iterator

from google import genai

from app.config import GEMINI_API_KEY
from app.vector_store import search

logger = logging.getLogger(__name__)

# 回答の文章生成に使う Gemini のモデル名。
# "gemini-flash-latest" は常に現行の flash モデルを指すエイリアスで、
# 特定バージョンが提供終了(404)しても影響を受けにくい。
GENERATION_MODEL = "gemini-flash-latest"

# 検索で取ってくる関連チャンクの件数（多すぎるとノイズ、少なすぎると情報不足）
TOP_K = 3

# 関連する社内文書が1件も見つからなかったときに返す定型メッセージ
NO_CONTEXT_MESSAGE = (
    "申し訳ありません。社内の資料には、その質問に関する情報が見つかりませんでした。"
)

# 資料は見つかったが、Geminiが回答本文を返さなかったときの定型メッセージ。
# NO_CONTEXT_MESSAGE とは意図的に文言を分けている。
# 「資料が無い」と伝えてしまうと、実際には登録済みのソースを疑わせ、
# ソース管理側の調査が空振りするため。
GENERATION_FAILED_MESSAGE = (
    "申し訳ありません。うまく回答を生成できませんでした。"
    "お手数ですが、質問を少し変えてもう一度お試しください。"
)

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


def answer_question(question: str, role: str) -> tuple[str, list[str]]:
    """質問を受け取り、社内文書に基づいたAIの回答文と参照ソースIDを返す（RAGの中核）。

    入力:
        question … ユーザーからの質問テキスト
        role     … 質問した人の役割（'admin' か 'employee'）。検索範囲の権限判定に使う

    出力:
        (回答文, 参照した source_id のリスト) のタプル
        例: ("週3日までです。", ["12", "15"])
        関連チャンクが0件のときは (定型メッセージ, []) を返す。

    処理:
        1. vector_store.search に role を渡し、権限に応じた範囲で関連チャンクを取得
        2. 関連チャンクが0件なら、定型メッセージと空リストを返して終了
        3. ヒットしたチャンクから参照ソースIDを重複を除いて集める
        4. 関連チャンクと質問からプロンプトを組み立てる
        5. Gemini の文章生成モデルに渡して回答を生成
        6. 回答テキストと参照ソースIDのリストを返す

    なぜ role を検索まで渡すか:
        社員には共通ソースだけを検索させるため。ここで絞らないと、
        他人の個別ソース（評価・給与など）が回答の根拠に混ざってしまう。
    """
    # 1. 質問に意味が近い社内文書のチャンクを、権限に応じた範囲から検索する
    #    戻り値は [{"text": 本文, "source_id": ソースID}, ...] の形
    hits = search(question, role=role, top_k=TOP_K)

    # 2. 関連する文書が1件も無ければ、無理に生成せず定型文を返す
    #    （根拠が無いのにAIが答えると、誤情報を生みやすいため）
    #    参照ソースも無いので空リストを返す
    if not hits:
        return NO_CONTEXT_MESSAGE, []

    # 3. 回答の根拠になったソースIDを集める（同じソースの複数チャンクがヒットするため重複を除く）
    #    dict.fromkeys を使うと、重複を除きつつ元の順番（関連度が高い順）を保てる
    referenced_sources = list(dict.fromkeys(hit["source_id"] for hit in hits))

    # 4. 参考文書＋質問をまとめたプロンプトを作る（プロンプトには本文だけを渡す）
    chunks = [hit["text"] for hit in hits]
    prompt = _build_prompt(question, chunks)

    # 5. Gemini にプロンプトを渡し、回答文を生成してもらう
    response = _client.models.generate_content(
        model=GENERATION_MODEL,
        contents=prompt,
    )

    # 6. 回答本文が返らないことがある（安全性フィルタでブロックされた、上限で生成されなかった等）。
    #    これは障害ではなく想定内の結果なので、例外にせず定型メッセージを返す。
    #    ただし頻発したときに気づけるよう警告ログだけは残す（本文は出さない）
    if response.text is None:
        logger.warning(
            "Gemini が回答本文を返しませんでした model=%s 参照ソース数=%d",
            GENERATION_MODEL,
            len(referenced_sources),
        )
        # 検索自体は成功しているので、参照ソースIDはそのまま返す
        return GENERATION_FAILED_MESSAGE, referenced_sources

    # 7. 回答テキストと、その根拠になったソースIDのリストを返す
    return response.text, referenced_sources


def answer_question_stream(question: str, role: str) -> tuple[list[str], Iterator[str]]:
    """質問を受け取り、参照ソースIDと「回答を少しずつ生み出す入れ物」を返す（ストリーミング版）。

    入力:
        question … ユーザーからの質問テキスト
        role     … 質問した人の役割（'admin' か 'employee'）。検索範囲の権限判定に使う

    出力:
        (参照した source_id のリスト, 回答テキストの断片を順に取り出せるイテレータ)

        1つ目はすぐに確定する（検索は一瞬で終わるため）。
        2つ目は「これから生成される文章の断片が順に出てくる入れ物」で、
        呼び出し元が for で回した瞬間にGeminiが少しずつ文章を作って返してくる。

    answer_question との違い:
        answer_question は全文が完成するまで待ってから1つの文字列を返す。
        こちらは generate_content_stream を使い、生成された端から断片を渡す。
        画面に「文字が少しずつ出てくる」表示ができるようになり、体感の待ち時間が減る。

    処理:
        1. vector_store.search に role を渡し、権限に応じた範囲で関連チャンクを取得
        2. 関連チャンクが0件なら、定型メッセージを1回だけ流して終わる（生成はしない）
        3. ヒットしたチャンクから参照ソースIDを重複を除いて集める
        4. 関連チャンクと質問からプロンプトを組み立てる
        5. Gemini のストリーミング生成を回し、断片が届くたびに1つずつ渡す

    なぜ (ソースID, イテレータ) のタプルで返すか:
        SSEでは「参照ソースは最初に1回」「本文は断片を連続で」送りたい。
        ソースIDは検索の時点で確定しているので先に確定値として返し、
        本文は「あとで少しずつ取り出せる形」で渡すことで、呼び出し元が
        送信順（sources → token → done）を自由に組み立てられる。
    """
    # 1. 質問に意味が近い社内文書のチャンクを、権限に応じた範囲から検索する
    hits = search(question, role=role, top_k=TOP_K)

    # 2. 関連する文書が1件も無ければ、生成せず定型文を1回だけ流して終わる
    #    参照ソースも無いので空リストを返す
    if not hits:
        return [], iter([NO_CONTEXT_MESSAGE])

    # 3. 回答の根拠になったソースIDを集める（重複を除きつつ、関連度が高い順を保つ）
    referenced_sources = list(dict.fromkeys(hit["source_id"] for hit in hits))

    # 4. 参考文書＋質問をまとめたプロンプトを作る（プロンプトには本文だけを渡す）
    chunks = [hit["text"] for hit in hits]
    prompt = _build_prompt(question, chunks)

    def generate_chunks() -> Iterator[str]:
        # Gemini のストリーミング生成。for で回すたびに、生成された断片が順に届く
        for chunk in _client.models.generate_content_stream(
            model=GENERATION_MODEL,
            contents=prompt,
        ):
            # 断片には本文が入らないこともある（安全性の判定情報だけ等）ので、
            # 中身がある断片だけを呼び出し元へ渡す
            if chunk.text:
                yield chunk.text

    # 5. 参照ソースIDは確定値として、本文は「これから少しずつ出てくる入れ物」として返す
    return referenced_sources, generate_chunks()
