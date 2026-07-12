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


# 会話の種類。general=通常のチャット、staff_inquiry=社長が特定社員について質問する会話
# （staff_inquiry の中身の実装は #24 の範囲。ここでは値として受け取れるようにするだけ）
VALID_CONTEXT_TYPES = {"general", "staff_inquiry"}


class ChatRequest(BaseModel):
    """チャットAPIが受け取るリクエストボディ。

    question   … 社員からの質問文（必須）
    session_id … どの会話の続きか（任意）。省略すると新しいセッションを自動で作る
    """

    question: str
    session_id: int | None = None


class SessionRequest(BaseModel):
    """セッション作成APIが受け取るリクエストボディ。

    context_type … 会話の種類（任意）。省略すると 'general'
    """

    context_type: str = "general"


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
        2. トークンから user_id と role を取り出す
        3. 会話をどのセッションに記録するかを決める
           - session_id が指定されていれば、そのセッションの続きとして記録する
           - 指定が無ければ、従来どおり新しいセッションを自動で作る
        4. rag.answer_question に質問と role を渡し、回答と参照ソースIDを生成する
        5. 質問（role="user"）と回答（role="assistant"）を履歴として記録する
        6. 回答と参照ソースIDをJSONで返す

    権限について:
        verify_token だけを付けているので、ログイン済みなら社員・社長どちらも利用できる。
        （require_admin は付けない。ソース管理と違い、質問は社員も行うため）
        ただし role を answer_question → search まで渡すことで、検索範囲を権限で絞る。
        社員は共通ソースのみ、社長は全ソースが対象になる。

    session_id を受け取るときの権限チェック（重要）:
        session_id は連番なので、他人のIDを推測して指定するのは簡単。
        検証せずに追記すると、他人の会話に自分の発言を勝手に挿入できてしまう。
        （社長のチャット履歴に、社員が偽の発言を紛れ込ませる等）
        そのため「そのセッションの持ち主 == 自分」でなければ 403 で拒否する。
        閲覧（GET）と違い書き込みなので、社長であっても他人のセッションには追記させない。

    履歴保存の失敗について:
        履歴の記録はあくまで管理用の副次的な処理なので、ここで失敗しても回答は返す。
        DBが一時的に書き込めないだけで社員が回答を受け取れなくなる、という事態を避けるため、
        セッション作成もメッセージ記録も try/except で包み、失敗時はログに残して処理を続ける。
        ただし session_id の権限チェックは別で、これは失敗させる（不正な書き込みを通さないため）。
    """
    # 1. 前後の空白を除いて、質問が実質空でないか確認する
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="質問を入力してください")

    # 2. ログイン情報（JWTの中身）から、誰の会話か(user_id)と、どの権限か(role)を取り出す
    #    role は 'admin'（社長）か 'employee'（社員）。検索範囲の絞り込みに使う
    user_id = token["user_id"]
    role = token["role"]

    # 3. どのセッションに記録するかを決める
    if req.session_id is not None:
        # 3-a. 継続する会話が指定された場合。まず「本当に自分のセッションか」を確認する
        owner_user_id = get_session_owner(req.session_id)

        # 存在しないセッションを指定された場合は404
        if owner_user_id is None:
            raise HTTPException(status_code=404, detail="セッションが見つかりません")

        # 他人のセッションへの書き込みは拒否する（社長であっても他人の会話には追記させない）
        if owner_user_id != user_id:
            raise HTTPException(status_code=403, detail="このセッションには投稿できません")

        session_id = req.session_id
    else:
        # 3-b. 指定が無ければ、従来どおり新しいセッションを自動で作る（既存動作の維持）
        #      失敗しても回答は返したいので session_id は None のままにする
        session_id = None
        try:
            session_id = create_session(user_id=user_id)
        except Exception:
            logger.exception("チャットセッションの作成に失敗しました（回答の生成は続行します）")

    # 4. RAGの中核関数に質問と role を渡し、回答文と参照ソースIDを生成してもらう
    answer, referenced_sources = answer_question(question, role=role)

    # 5. 質問と回答を履歴として記録する（セッションが用意できていた場合のみ）
    if session_id is not None:
        try:
            add_message(session_id, "user", question)
            add_message(session_id, "assistant", answer)
        except Exception:
            logger.exception("チャット履歴の保存に失敗しました（回答はそのまま返します）")

    # 6. 回答と参照ソースIDをJSONで返す
    #    reply キーはフロント chat.js が読んでいるので変えない（互換性のため）
    return {"reply": answer, "referenced_sources": referenced_sources}


@router.post("/sessions")
async def create_chat_session(
    req: SessionRequest, token: dict = Depends(verify_token)
):
    """新しいチャットセッションを作り、その session_id を返す。

    入力:
        req   … SessionRequest（context_type を含む。省略時は 'general'）
        token … verify_token が返すログイン情報（user_id を含む）

    出力:
        {"session_id": 採番されたセッションID}

    使いどころ:
        フロントが会話を始めるときに1回だけ呼び、返ってきた session_id を保持する。
        以降 POST /api/chat に同じ session_id を付けて送れば、
        一連のやり取りが1つの会話としてまとまる。
        （現在の chat.js は session_id を送っていないため、質問ごとに
          セッションが自動作成される。フロント対応は別途）

    処理:
        1. context_type が正しい値か確認する（不正なら400）
        2. トークンの user_id でセッションを作り、session_id を返す

    権限について:
        誰のセッションを作るかは、リクエストではなくトークンから決める。
        user_id をリクエストで受け取る設計にすると、他人のIDを送りつけて
        他人名義のセッションを作れてしまう。GET /api/chat/sessions と同じ考え方。
    """
    # 1. 会話の種類が想定内の値かを確認する（想定外の値がDBに入るのを防ぐ）
    if req.context_type not in VALID_CONTEXT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="context_type は general または staff_inquiry を指定してください",
        )

    # 2. セッションを作る。持ち主は必ずトークンの user_id（なりすまし防止）
    session_id = create_session(
        user_id=token["user_id"],
        context_type=req.context_type,
    )

    return {"session_id": session_id}


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
