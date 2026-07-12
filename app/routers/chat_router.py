import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.chat_history import add_message, create_session
from app.rag import answer_question
from app.routers.auth_router import verify_token

# チャット関連のAPIをまとめるルーター。URLは /api/chat から始まる
router = APIRouter(prefix="/api/chat", tags=["chat"])

# 履歴保存に失敗したときの記録用（回答は返しつつ、サーバーログに残す）
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    """チャットAPIが受け取るリクエストボディ。

    question … 社員からの質問文（必須）
    """

    question: str


@router.post("")
async def chat(req: ChatRequest, token: dict = Depends(verify_token)):
    """質問を受け取り、RAGで生成したAIの回答を返す。あわせて会話をDBに記録する。

    入力:
        req   … ChatRequest（question を含むJSONボディ）
        token … verify_token が返すログイン情報（user_id と role を含む。社員・社長どちらでも可）

    出力:
        {"reply": AIの回答文, "referenced_sources": 参照したsource_idのリスト} のJSON
        reply キーは維持する（フロント chat.js が data.reply を読んでいるため）

    処理:
        1. 質問が空でないか確認する（空なら400）
        2. トークンから user_id と role を取り出し、チャットセッションを1件作る
        3. rag.answer_question に質問と role を渡し、回答と参照ソースIDを生成する
        4. 質問（role="user"）と回答（role="assistant"）を履歴として記録する
        5. 回答と参照ソースIDをJSONで返す

    権限について:
        verify_token だけを付けているので、ログイン済みなら社員・社長どちらも利用できる。
        （require_admin は付けない。ソース管理と違い、質問は社員も行うため）
        ただし role を answer_question → search まで渡すことで、検索範囲を権限で絞る。
        社員は共通ソースのみ、社長は全ソースが対象になる。

    履歴保存の失敗について:
        履歴の記録はあくまで管理用の副次的な処理なので、ここで失敗しても回答は返す。
        DBが一時的に書き込めないだけで社員が回答を受け取れなくなる、という事態を避けるため、
        セッション作成もメッセージ記録も try/except で包み、失敗時はログに残して処理を続ける。
    """
    # 1. 前後の空白を除いて、質問が実質空でないか確認する
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="質問を入力してください")

    # 2. ログイン情報（JWTの中身）から、誰の会話か(user_id)と、どの権限か(role)を取り出す
    #    role は 'admin'（社長）か 'employee'（社員）。検索範囲の絞り込みに使う
    user_id = token["user_id"]
    role = token["role"]

    # チャットセッションを1件作る。失敗しても回答は返したいので session_id は None のままにする
    session_id = None
    try:
        session_id = create_session(user_id=user_id)
    except Exception:
        logger.exception("チャットセッションの作成に失敗しました（回答の生成は続行します）")

    # 3. RAGの中核関数に質問と role を渡し、回答文と参照ソースIDを生成してもらう
    answer, referenced_sources = answer_question(question, role=role)

    # 4. 質問と回答を履歴として記録する（セッションが作れていた場合のみ）
    if session_id is not None:
        try:
            add_message(session_id, "user", question)
            add_message(session_id, "assistant", answer)
        except Exception:
            logger.exception("チャット履歴の保存に失敗しました（回答はそのまま返します）")

    # 5. 回答と参照ソースIDをJSONで返す
    #    reply キーはフロント chat.js が読んでいるので変えない（互換性のため）
    return {"reply": answer, "referenced_sources": referenced_sources}
