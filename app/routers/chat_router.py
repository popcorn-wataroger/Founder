import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.chat_history import (
    add_message,
    create_session,
    get_messages,
    get_session_owner,
    get_sessions,
)
from app.rag import answer_question
from app.routers.auth_router import require_admin, verify_token

# チャット関連のAPIをまとめるルーター。URLは /api/chat から始まる
router = APIRouter(prefix="/api/chat", tags=["chat"])

# 管理者専用のチャットAPIをまとめるルーター。URLは /api/admin から始まる
# （社長が社員データ画面から、他人のチャットログを見るために使う）
admin_router = APIRouter(prefix="/api/admin", tags=["chat"])

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


@router.get("/sessions")
async def list_my_sessions(token: dict = Depends(verify_token)):
    """ログイン中の本人のチャットセッション一覧を返す。

    入力:
        token … verify_token が返すログイン情報（user_id を含む）

    出力:
        セッションの一覧（新しい順）。各件に session_id / started_at / preview を含む

    権限について:
        返すのは「トークンに入っている user_id」のセッションだけ。
        user_id をリクエストから受け取らず、必ずトークンから取り出すのがポイント。
        フロントから user_id を送らせる設計にすると、他人のIDを送りつけて
        他人の履歴を見られてしまうため、なりすましの余地を残さない。
    """
    # 誰のセッションを返すかは、リクエストではなくトークンだけで決める
    user_id = token["user_id"]

    return get_sessions(user_id)


@router.get("/sessions/{session_id}/messages")
async def list_session_messages(session_id: int, token: dict = Depends(verify_token)):
    """指定セッションのメッセージ全文を、時系列で返す。

    入力:
        session_id … 見たいセッションのID（URLパスで指定）
        token      … verify_token が返すログイン情報（user_id と role を含む）

    出力:
        メッセージの一覧（古い順）。各件に role / content / created_at を含む

    処理:
        1. セッションの持ち主（user_id）を調べる。存在しなければ404
        2. 見てよい人かを判定する。ダメなら403
        3. メッセージ一覧を返す

    権限について（重要）:
        session_id は連番なので、社員が /api/chat/sessions/3/messages のように
        他人のセッションIDを推測して叩くのは非常に簡単。
        そのままメッセージを返す実装にすると、他人のチャットログが丸見えになる。
        そこで「セッションの持ち主 == 自分」か「社長（admin）」の場合だけ許可する。
        社長は社員データ画面で全員のログを閲覧できる必要があるため、例外として通す。
    """
    # 1. このセッションが誰のものかを調べる
    owner_user_id = get_session_owner(session_id)

    # 存在しないセッションを指定された場合は404
    if owner_user_id is None:
        raise HTTPException(status_code=404, detail="セッションが見つかりません")

    # 2. 見てよい人かを判定する
    #    許可するのは「本人」か「社長」だけ。それ以外（＝他人のセッションを見ようとした社員）は拒否
    is_owner = owner_user_id == token["user_id"]
    is_admin = token.get("role") == "admin"
    if not is_owner and not is_admin:
        raise HTTPException(status_code=403, detail="このチャット履歴は閲覧できません")

    # 3. メッセージ一覧を時系列で返す
    return get_messages(session_id)


@admin_router.get("/users/{user_id}/chat-sessions")
async def list_user_sessions(user_id: str, token: dict = Depends(require_admin)):
    """指定した社員のチャットセッション一覧を返す（社長専用）。

    入力:
        user_id … 履歴を見たい社員の user_id（URLパスで指定）
        token   … require_admin が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        その社員のセッション一覧（新しい順）

    使いどころ:
        社長が社員データ画面で「最近のトーク」を表示するときに使う。
        一覧から session_id を選んで /api/chat/sessions/{session_id}/messages を叩けば、
        トーク全文モーダルの中身を取得できる。

    権限について:
        require_admin を付けているので、社員がこのURLを叩いても403で拒否される。
        他人の user_id を自由に指定できるAPIなので、管理者チェックは必須。
    """
    return get_sessions(user_id)
