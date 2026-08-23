"""RAG（検索拡張生成）のまとめ役モジュール。

「質問 → 関連チャンク検索 → Geminiで回答生成」という一連の流れをまとめている。

回答の返し方は2種類ある:
    answer_question        … 全文が生成されるまで待ち、完成した文字列を返す（一括）
    answer_question_stream … 生成された端から少しずつ返す（ストリーミング／SSE用）
"""

import logging
import time
from collections.abc import Iterator

from google import genai
from google.genai.errors import ServerError

from app.config import GEMINI_API_KEY
from app.vector_store import search

logger = logging.getLogger(__name__)

# Gemini APIが一時的なエラー（503 UNAVAILABLE等）を返したときのリトライ回数と待機秒数。
# 待機秒数は試行のたびに倍にする（1回目→2秒、2回目→4秒、3回目→8秒）。
# 高負荷が原因のエラーなので、間を置かず連打すると余計に混雑を悪化させるため。
STREAM_RETRY_MAX_ATTEMPTS = 3
STREAM_RETRY_BASE_SECONDS = 2

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


def _build_prompt(question: str, chunks: list[str], profile: str | None = None) -> str:
    """参考情報と質問を組み合わせ、Geminiに渡すプロンプト文を作る。

    入力:
        question … ユーザーからの質問
        chunks   … 質問に関連する社内文書のチャンク（0件のこともある）
        profile  … 対象社員の基本情報（app.users.format_user_profile が作った文字列）。
                   社員データ画面からの質問のときだけ渡す。通常のチャットでは None

    出力:
        Geminiに渡す1つの文字列プロンプト

    ねらい:
        「渡した情報の範囲で答えて」と明示することで、
        AIが勝手な推測（ハルシネーション）で答えるのを抑える。

    chunks と profile の両方を空にして呼んではいけない:
        参考情報が1つも無い状態で生成させると、AIは自分の知識で答えを作ってしまう。
        呼び出し元（answer_question / answer_question_stream）が、
        どちらも空のときは生成せず定型メッセージで返すようにしている。

    セクションを分けて渡す理由:
        基本情報は社員マスタの値、社内文書はアップロードされた資料と、出どころが違う。
        「=== 対象社員の基本情報 ===」と「=== 社内文書 ===」に分けておくと、
        AIがどちらを根拠にしたか回答文の中で書き分けやすくなる。
        また、片方しか無いときはそのセクションごと省けるので、
        「空のセクション」を渡してAIを混乱させずに済む。
    """
    # 参考情報のセクションを、ある分だけ組み立てる（無いセクションは丸ごと省く）
    sections = []

    # 基本情報は社員マスタ由来なので、社内文書より先に置く（質問の主語になることが多いため）
    if profile:
        sections.append(f"=== 対象社員の基本情報 ===\n{profile}")

    if chunks:
        # 各チャンクに番号を振って読みやすく並べる
        context = "\n\n".join(f"【文書{i + 1}】\n{c}" for i, c in enumerate(chunks))
        sections.append(f"=== 社内文書 ===\n{context}")

    reference = "\n\n".join(sections)

    # 「参考資料 → 質問 → 指示」の順に組み立てる
    #
    # 出力形式をここで縛る理由:
    #   回答は static/js/chat.js の renderMarkdownInto が解釈して画面に表示する。
    #   フロントが対応していない記法（表・リンク・コードブロックなど）が返ると、
    #   整形されずに記号のまま出てしまう。
    #   そこでサーバー側で使わせる記法を絞り、フロントの対応範囲と一致させる。
    #
    # 番号付きリストの途中に段落を挟ませない理由:
    #   renderMarkdownInto は「同じ種類の行が連続する間」を1つの ol にまとめる。
    #   項目と項目の間に段落が入ると ol が2つに分かれ、
    #   2つ目が 1. から番号を振り直してしまう（1. 1. と並んで見える）。
    #   パーサーを複雑にする代わりに、生成側で連続した形にしてもらう。
    return (
        "あなたは社内向けのAIアシスタントです。"
        "以下の参考情報だけを参考にして、質問に日本語で答えてください。"
        "参考情報に書かれていない内容は、推測せず「資料には記載がありません」と伝えてください。\n\n"
        "回答は次の記法だけを使ってください。"
        "太字は **文字**、箇条書きは行頭に「- 」、番号付きの列挙は行頭に「1. 」、"
        "見出しは行頭に「### 」を付けます。"
        "表・リンク・画像・コードブロックは使わないでください。"
        "番号付きリストを使うときは、項目と項目の間に段落や空行を挟まず、"
        "番号の行を続けて並べてください。"
        "各項目の説明は、その項目の行の中に含めてください。\n\n"
        f"{reference}\n\n"
        f"=== 質問 ===\n{question}\n\n"
        "=== 回答 ==="
    )


def answer_question(
    question: str, role: str, self_user_id: str | None = None
) -> tuple[str, list[str]]:
    """質問を受け取り、社内文書に基づいたAIの回答文と参照ソースIDを返す（RAGの中核）。

    入力:
        question     … ユーザーからの質問テキスト
        role         … 質問した人の役割（'admin' か 'employee'）。検索範囲の権限判定に使う
        self_user_id … 質問した本人の user_id。
                       社員が「自分の個別ソース」も検索対象に含めるために使う。
                       省略時（None）は共通ソースのみ（従来どおり）。

                       必ず JWT の user_id を渡すこと。リクエスト由来の値を
                       渡してはならない。渡すと、社員が他人の user_id を送るだけで
                       その人の個別ソース（評価・給与）を読めてしまう。

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

    target_user_id との違い（answer_question_stream を参照）:
        target_user_id は社長が画面で選んだ「相手」を指す値で、
        require_admin で守られた経路（社員データ画面）からのみ渡る。
        self_user_id が指すのは常に本人自身で、社員の経路からも渡る。
        指し示す相手が違うので、引数も分けている。
    """
    # 1. 質問に意味が近い社内文書のチャンクを、権限に応じた範囲から検索する
    #    戻り値は [{"text": 本文, "source_id": ソースID}, ...] の形
    hits = search(question, role=role, top_k=TOP_K, self_user_id=self_user_id)

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


def answer_question_stream(
    question: str,
    role: str,
    target_user_id: str | None = None,
    profile: str | None = None,
    self_user_id: str | None = None,
) -> tuple[list[str], Iterator[str]]:
    """質問を受け取り、参照ソースIDと「回答を少しずつ生み出す入れ物」を返す（ストリーミング版）。

    入力:
        question       … ユーザーからの質問テキスト
        role           … 質問した人の役割（'admin' か 'employee'）。検索範囲の権限判定に使う
        target_user_id … 社員データ画面から「この社員について」質問する場合の対象社員の user_id。
                         省略時（None）は従来どおり、role だけで検索範囲が決まる
        profile        … 対象社員の基本情報（app.users.format_user_profile が作った文字列）。
                         社員データ画面からの質問のときだけ渡す。通常のチャットでは None
        self_user_id   … 質問した本人の user_id。
                         社員が「自分の個別ソース」も検索対象に含めるために使う。
                         省略時（None）は共通ソースのみ（従来どおり）。

                         必ず JWT の user_id を渡すこと。リクエスト由来の値を
                         渡してはならない。渡すと、社員が他人の user_id を送るだけで
                         その人の個別ソース（評価・給与）を読めてしまう。

                         target_user_id との違い:
                         target_user_id は社長が画面で選んだ「相手」を指す値で、
                         require_admin で守られた経路からのみ渡る。
                         self_user_id が指すのは常に本人自身で、社員の経路からも渡る。
                         指し示す相手が違うので、引数も分けている。

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
        1. vector_store.search に role と target_user_id を渡し、
           権限に応じた範囲で関連チャンクを取得
        2. 関連チャンクが0件で、基本情報も無ければ、
           定型メッセージを1回だけ流して終わる（生成はしない）
        3. ヒットしたチャンクから参照ソースIDを重複を除いて集める
        4. 基本情報＋関連チャンク＋質問からプロンプトを組み立てる
        5. Gemini のストリーミング生成を回し、断片が届くたびに1つずつ渡す

    関連チャンクが0件でも、基本情報があれば生成する理由:
        「奥村さんの家族構成は？」のように、答えが社員マスタ側にしか無い質問がある。
        ここでチャンク0件を理由に打ち切ると、基本情報を渡した意味が無くなり、
        画面には出ている情報なのにAIは「資料がありません」と答えることになる。
        逆に、チャンクも基本情報も無いときは根拠がゼロなので、従来どおり生成しない。

    参照ソースについて:
        基本情報は検索結果ではないので referenced_sources には載せない。
        基本情報だけで答えた場合、参照ソースは空リストのままになる。

    なぜ target_user_id をここで受けるだけで、そのまま search に渡すか:
        「誰の資料を見てよいか」の判断は vector_store.search が一手に引き受けている。
        この関数が独自に絞り込みを足すと、権限のルールが2か所に散らばって
        片方だけ直され、抜けが生まれる。ここは受け渡しに徹する。

    なぜ (ソースID, イテレータ) のタプルで返すか:
        SSEでは「参照ソースは最初に1回」「本文は断片を連続で」送りたい。
        ソースIDは検索の時点で確定しているので先に確定値として返し、
        本文は「あとで少しずつ取り出せる形」で渡すことで、呼び出し元が
        送信順（sources → token → done）を自由に組み立てられる。
    """
    # 1. 質問に意味が近い社内文書のチャンクを、権限に応じた範囲から検索する
    #    target_user_id を渡した場合は「共通 ＋ その社員の個別」だけが対象になる
    hits = search(
        question,
        role=role,
        top_k=TOP_K,
        target_user_id=target_user_id,
        self_user_id=self_user_id,
    )

    # 2. 参考にできる情報が1つも無ければ、生成せず定型文を1回だけ流して終わる
    #    （文書が0件でも、対象社員の基本情報があればそれを根拠に生成できるので続行する）
    if not hits and not profile:
        return [], iter([NO_CONTEXT_MESSAGE])

    # 3. 回答の根拠になったソースIDを集める（重複を除きつつ、関連度が高い順を保つ）
    #    基本情報は検索結果ではないため、ここには含めない（文書が0件なら空リストになる）
    referenced_sources = list(dict.fromkeys(hit["source_id"] for hit in hits))

    # 4. 基本情報＋参考文書＋質問をまとめたプロンプトを作る（プロンプトには本文だけを渡す）
    chunks = [hit["text"] for hit in hits]
    prompt = _build_prompt(question, chunks, profile=profile)

    def generate_chunks() -> Iterator[str]:
        # Gemini のストリーミング生成。for で回すたびに、生成された断片が順に届く
        #
        # リトライについて:
        #   Gemini APIが混雑時に 503 UNAVAILABLE を返すことがある。一時的なエラーなので、
        #   本文をまだ1つも送っていない場合に限りやり直す。
        #   本文を送信済みの状態でやり直すと、呼び出し元（chat_router）が
        #   途中まで送った断片の後ろにもう一度最初から回答をつなげてしまい、
        #   ユーザーには「回答が二重に流れる」ように見えるため、その場合はリトライせず
        #   従来どおり例外を外へ伝えてエラー扱いにする。
        for attempt in range(1, STREAM_RETRY_MAX_ATTEMPTS + 1):
            yielded_any = False
            try:
                for chunk in _client.models.generate_content_stream(
                    model=GENERATION_MODEL,
                    contents=prompt,
                ):
                    # 断片には本文が入らないこともある（安全性の判定情報だけ等）ので、
                    # 中身がある断片だけを呼び出し元へ渡す
                    if chunk.text:
                        yielded_any = True
                        yield chunk.text
                return
            except ServerError as e:
                is_last_attempt = attempt == STREAM_RETRY_MAX_ATTEMPTS
                if yielded_any or is_last_attempt:
                    raise
                wait_seconds = STREAM_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Gemini APIが一時的なエラーを返したためリトライします "
                    "(%d/%d回目、%d秒後に再試行): %s",
                    attempt,
                    STREAM_RETRY_MAX_ATTEMPTS,
                    wait_seconds,
                    e,
                )
                time.sleep(wait_seconds)

    # 5. 参照ソースIDは確定値として、本文は「これから少しずつ出てくる入れ物」として返す
    return referenced_sources, generate_chunks()
