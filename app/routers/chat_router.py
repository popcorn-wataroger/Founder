from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.rag import answer_question
from app.routers.auth_router import verify_token

# チャット関連のAPIをまとめるルーター。URLは /api/chat から始まる
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """チャットAPIが受け取るリクエストボディ。

    question … 社員からの質問文（必須）
    """

    question: str


@router.post("")
async def chat(req: ChatRequest, token: dict = Depends(verify_token)):
    """質問を受け取り、RAGで生成したAIの回答を返す。

    入力:
        req   … ChatRequest（question を含むJSONボディ）
        token … verify_token が返すログイン情報（社員・社長どちらでも可）

    出力:
        {"reply": AIの回答文} のJSON（フロント chat.js が data.reply を読むため）

    処理:
        1. 質問が空でないか確認する（空なら400）
        2. rag.answer_question に質問を渡して回答を生成する
        3. 回答をJSONで返す

    権限について:
        verify_token だけを付けているので、ログイン済みなら社員・社長どちらも利用できる。
        （require_admin は付けない。ソース管理と違い、質問は社員も行うため）
    """
    # 1. 前後の空白を除いて、質問が実質空でないか確認する
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="質問を入力してください")

    # 2. RAGの中核関数に質問を渡し、社内文書に基づいた回答を生成してもらう
    answer = answer_question(question)

    # 3. 生成した回答をJSONで返す（キー名はフロント chat.js の data.reply に合わせる）
    return {"reply": answer}
