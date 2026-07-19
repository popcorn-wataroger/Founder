import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.chat_history import (
    add_message,
    create_session,
    get_messages,
    get_session_owner,
    get_sessions,
)
from app.rag import answer_question, answer_question_stream
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


def _resolve_session(requested_session_id: int | None, user_id: str) -> int | None:
    """会話をどのセッションに記録するかを決める（POST /api/chat と /api/chat/stream で共用）。

    入力:
        requested_session_id … リクエストで指定された session_id（未指定なら None）
        user_id              … トークンから取り出した、質問した本人の user_id

    出力:
        記録先の session_id。セッションを用意できなかった場合のみ None

    処理:
        - session_id が指定されている場合:
            持ち主を確認し、存在しなければ404、自分のものでなければ403にする
        - 指定が無い場合:
            新しいセッションを自動で作る（既存の動作を維持）
            作成に失敗しても回答は返したいので、例外は握って None を返す

    権限について（重要）:
        session_id は連番なので、他人のIDを推測して指定するのは簡単。
        検証せずに追記すると、他人の会話に自分の発言を勝手に挿入できてしまう。
        （社長のチャット履歴に、社員が偽の発言を紛れ込ませる等）
        そのため「そのセッションの持ち主 == 自分」でなければ403で拒否する。
        閲覧（GET）と違い書き込みなので、社長であっても他人のセッションには追記させない。
    """
    # 継続する会話が指定された場合。まず「本当に自分のセッションか」を確認する
    if requested_session_id is not None:
        owner_user_id = get_session_owner(requested_session_id)

        # 存在しないセッションを指定された場合は404
        if owner_user_id is None:
            raise HTTPException(status_code=404, detail="セッションが見つかりません")

        # 他人のセッションへの書き込みは拒否する（社長であっても他人の会話には追記させない）
        if owner_user_id != user_id:
            raise HTTPException(status_code=403, detail="このセッションには投稿できません")

        return requested_session_id

    # 指定が無ければ、新しいセッションを自動で作る（既存動作の維持）
    # 失敗しても回答は返したいので、例外は握って None を返す（履歴が残らないだけ）
    try:
        return create_session(user_id=user_id)
    except Exception:
        logger.exception("チャットセッションの作成に失敗しました（回答の生成は続行します）")
        return None


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

    session_id を受け取るときの権限チェック:
        他人のセッションに書き込めないよう、_resolve_session が持ち主を検証する（詳細はそちら）。

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

    # 3. どのセッションに記録するかを決める（権限チェック込み）
    session_id = _resolve_session(req.session_id, user_id)

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


def _sse_event(event: str, data: dict) -> str:
    """SSE（Server-Sent Events）の1イベントを、送信できる文字列に組み立てる。

    入力:
        event … イベント名（'sources' / 'token' / 'done' / 'error'）
        data  … そのイベントで送る中身（辞書）

    出力:
        SSE形式の文字列。例:
            event: token\\ndata: {"text": "こんにちは"}\\n\\n

    SSEの決まりごと:
        - "event: 名前" と "data: 中身" を改行で並べ、最後に空行を1つ入れて1イベントの区切りとする
        - data の中に生の改行があると、そこで別の行と解釈されて壊れる
          → JSON文字列にすることで改行が \\n にエスケープされ、1行に収まる
        - ensure_ascii=False で日本語をそのまま送る（読みやすさのため）
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/stream")
async def chat_stream(req: ChatRequest, token: dict = Depends(verify_token)):
    """質問を受け取り、AIの回答を「生成された端から少しずつ」流して返す（SSE版）。

    入力:
        req   … ChatRequest（question と、任意の session_id）
        token … verify_token が返すログイン情報（user_id と role を含む）

    出力:
        text/event-stream 形式のストリーム。次の順でイベントが流れる:
            1. event: sources … 参照した source_id のリスト（最初に1回）
            2. event: token   … 回答本文の断片（生成されるたびに連続で何度も）
            3. event: done    … 生成完了の合図（最後に1回）
            ※ 途中でエラーが起きた場合は event: error を流して終わる

    POST /api/chat との違い:
        /api/chat は全文が完成するまで待ってからJSONを1回返す。
        こちらは断片が届くたびに送るので、画面に文字が少しずつ出てくる表示ができる。
        既存の /api/chat はそのまま残してあるので、フロントは好きな方を使える。

    履歴保存について（B案・完走時のみ保存）:
        本文を最後まで送り終えたときだけ、質問と回答をDBに記録する。
        途中でユーザーがブラウザを閉じたり、生成がエラーで止まった場合は保存しない。
        中途半端な回答が「AIの回答」として履歴に残ると、後から読んだ社長が
        誤った内容を正しい回答だと受け取ってしまうため、記録しない方を選んでいる。

    権限について:
        検索範囲の絞り込み（社員は共通ソースのみ）も、
        session_id の持ち主チェックも、非ストリーミング版とまったく同じ。
    """
    # 1. 前後の空白を除いて、質問が実質空でないか確認する
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="質問を入力してください")

    # 2. ログイン情報から user_id と role を取り出す
    user_id = token["user_id"]
    role = token["role"]

    # 3. どのセッションに記録するかを決める（権限チェック込み。/api/chat と同じ処理）
    #    ここで403や404になる場合は、ストリームを始める前に通常のHTTPエラーとして返る
    session_id = _resolve_session(req.session_id, user_id)

    # 4. 参照ソースID（確定値）と、本文の断片を順に取り出せる入れ物を受け取る
    #    この時点ではまだ生成は始まっていない（for で回した瞬間に生成が走る）
    referenced_sources, chunk_iterator = answer_question_stream(question, role=role)

    def event_stream():
        """SSEのイベントを順に送り出す。送り終わったら履歴を保存する。"""
        # 送信済みの本文を貯めていく入れ物（最後に履歴として保存するため）
        collected: list[str] = []

        try:
            # 4-a. 最初に「どの資料を参照したか」を1回送る
            yield _sse_event("sources", {"referenced_sources": referenced_sources})

            # 4-b. 本文の断片が届くたびに、1つずつ送る（ここが「少しずつ出てくる」部分）
            for chunk in chunk_iterator:
                collected.append(chunk)
                yield _sse_event("token", {"text": chunk})

            # 4-c. 最後まで届いたので、完了の合図を送る
            yield _sse_event("done", {})

        except Exception:
            # 生成の途中でエラーが起きた場合。履歴は保存せず、エラーを伝えて終わる
            # （途中までの回答を「AIの回答」として残さないため）
            logger.exception("ストリーミング生成中にエラーが発生しました")
            yield _sse_event("error", {"message": "回答の生成中にエラーが発生しました"})
            return

        # 5. ここに到達＝最後まで送り切った（完走した）場合のみ、履歴を保存する
        #    途中でブラウザを閉じられた場合、この行には到達しない（＝保存されない）
        if session_id is not None:
            try:
                add_message(session_id, "user", question)
                add_message(session_id, "assistant", "".join(collected))
            except Exception:
                logger.exception("チャット履歴の保存に失敗しました（回答は送信済み）")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/sessions")
async def create_chat_session(req: SessionRequest, token: dict = Depends(verify_token)):
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
