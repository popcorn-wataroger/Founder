"""Qdrant（ベクトルDB）操作を担うモジュール。

この段階では「① コレクション（棚）を用意する」処理だけを実装している。
ポイント（ベクトル）の保存や検索は後続ステップで追加する。
"""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import QDRANT_API_KEY, QDRANT_URL
from app.vectorizer import embed_text

# コレクション名。Qdrant上で「棚」を識別する名前
# DBの sources テーブルと対になる棚なので "sources" とする
COLLECTION_NAME = "sources"

# ベクトルの次元数。gemini-embedding-001 が返す 3072 次元に合わせる
VECTOR_SIZE = 3072

# ベクトル同士の「近さ」の測り方。意味の近さを測るRAGでは cosine（コサイン類似度）が定番
DISTANCE = Distance.COSINE

# Qdrant Cloud への接続クライアント。接続情報は config 経由で読み込む（コードに直書きしない）
_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def ensure_collection() -> None:
    """コレクション（棚）と、絞り込み用インデックスを用意する。

    入力:
        なし

    出力:
        なし（Qdrant側にコレクションとインデックスが用意された状態になる）

    処理:
        1. 既存コレクション一覧を取得し、"sources" が既にあるか調べる
        2. 無ければ、3072次元・cosine距離のコレクションを新規作成する（あれば作らない）
        3. scope / owner_user_id / source_id に payload インデックスを張る（既にあれば何も起きない）

    なぜ payload インデックスが要るか:
        Qdrant は、インデックスの無いキーでの絞り込み検索を拒否する（400エラーになる）。
        search で scope による権限フィルタを掛けるので、scope にインデックスが必須。
        delete_by_source_id で source_id を指定して消すので、source_id にも必須。
        owner_user_id も、将来「特定社員の個別ソースだけ検索する」際に同じ理由で必要になる。

    なぜ冪等にするか:
        アプリ起動のたびに呼んでも、既存コレクションを壊さない・重複作成しないため。
        インデックス作成も、既存コレクションに対して毎回張り直しにならないよう Qdrant 側が
        同じ設定なら何もしない。そのため return より前に置いて、
        「コレクションは既にあるがインデックスだけ無い」状態も自動で直せるようにしている。
    """
    # 既存コレクション名の一覧を集める
    existing = {c.name for c in _client.get_collections().collections}

    # 無ければ、次元数と距離を指定してコレクションを新規作成する
    if COLLECTION_NAME not in existing:
        _client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
        )

    # 絞り込み（検索・削除）に使うキーへインデックスを張る
    # KEYWORD = 完全一致で絞り込む文字列型（"common" などのラベル向き）
    for field_name in ("scope", "owner_user_id", "source_id"):
        _client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field_name,
            field_schema=PayloadSchemaType.KEYWORD,
        )


def save_chunks(
    chunks: list[str],
    vectors: list[list[float]],
    source_id: str,
    scope: str = "common",
    owner_user_id: str | None = None,
) -> list[str]:
    """1つのソースのチャンク群を、まとめて sources コレクションに保存（upsert）する。

    入力:
        chunks        … 分割済みテキストのリスト（例: ["本文1", "本文2", ...]）
        vectors       … 各チャンクをベクトル化した結果のリスト。chunks と同じ順番・同じ件数
        source_id     … このチャンク群が属する元ソースのID（どのファイル由来かを示す）
        scope         … ソースの公開範囲。'common'（全社共通）か 'individual'（社員個別）
        owner_user_id … 個別ソースの持ち主の user_id。共通ソースなら None

    出力:
        保存した各ポイントのID（文字列）のリスト。後で確認・削除に使える。

    処理:
        1. chunks と vectors の件数が一致するか確認（ズレは検索結果の取り違えの原因になる）
        2. チャンクごとに「id + ベクトル + payload」の1ポイントを組み立てる
           - id     : source_id とチャンク番号から一意に決まるUUID
           - vector : そのチャンクのベクトル
           - payload: 元の文章(text)、source_id、scope、owner_user_id
        3. まとめて upsert（無ければ追加、同じidがあれば上書き）する

    なぜ payload に scope と owner_user_id を持たせるか:
        検索時に権限で絞り込むため。Qdrantは payload の値で検索対象を限定できるので、
        「社員の質問では scope='common' のチャンクだけを検索する」「社長は全部検索できる」
        という権限フィルタを、後続ステップの search 側で掛けられるようにしておく。
        他人の評価・給与などの個別ソースが社員への回答に混ざるのを防ぐための土台になる。
        （権限フィルタ自体はまだ未実装。ここでは保存時に情報を持たせるところまで）

    なぜ id を source_id + 番号から作るか:
        同じソースを再アップロードしても同じidになり、重複ではなく上書きになる（冪等）。
    """
    # 件数がズレていると、どのチャンクがどのベクトルか対応が崩れるので先に弾く
    if len(chunks) != len(vectors):
        raise ValueError(f"chunks と vectors の件数が一致しません: {len(chunks)} != {len(vectors)}")

    points: list[PointStruct] = []
    ids: list[str] = []

    # チャンクを1件ずつ「保存できる形（ポイント）」に変換する
    for index, (text, vector) in enumerate(zip(chunks, vectors)):
        # source_id とチャンク番号を元に、一意で毎回同じになるIDを生成する
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{index}"))
        ids.append(point_id)

        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                # payload = ベクトルに紐づく付帯情報
                #   text          : 検索後にここから元テキストを取り出し、回答生成に使う
                #   source_id     : どのソース由来か（削除や参照元の表示に使う）
                #   scope         : 検索時の権限フィルタ用（社員には common のみ検索させる）
                #   owner_user_id : 個別ソースの持ち主（社長が特定社員のソースだけ見るときに使う）
                payload={
                    "text": text,
                    "source_id": source_id,
                    "scope": scope,
                    "owner_user_id": owner_user_id,
                },
            )
        )

    # 組み立てたポイントをまとめて保存（同じidは上書き＝冪等）
    _client.upsert(collection_name=COLLECTION_NAME, points=points)

    return ids


def _build_staff_filter(target_user_id: str) -> Filter:
    """「全社共通ソース ＋ 指定した社員1人の個別ソース」だけに絞る条件を組み立てる。

    入力:
        target_user_id … 対象社員の user_id（この社員の個別ソースだけを許可する）

    出力:
        Qdrant の Filter オブジェクト

    組み立てる条件（OR）:
        - scope == "common"
        - または（scope == "individual" かつ owner_user_id == target_user_id）

    Qdrantの書き方:
        should = 「並べた条件のどれか1つでも満たせばよい」（＝OR）
        must   = 「並べた条件をすべて満たす必要がある」（＝AND）
        should の中に Filter を入れ子にできるので、
        「共通」または「個別 かつ 本人」という OR の中の AND を表現できる。

    なぜ owner_user_id だけで絞らないか:
        scope も併せて見ることで、万一 owner_user_id が入った共通ソースが
        紛れ込んでも、意図しない経路で個別扱いにならない。
        条件を Issue の文言どおりそのまま書き下しておく方が、後から読んで検証しやすい。

    なぜこの関数が必要か（重要）:
        社長(admin)の通常検索はフィルタなし＝全社員の個別ソースが対象になる。
        その状態で「奥村さんについて」と質問すると、別の社員の評価資料が
        根拠に混ざりうる。社員データ画面からの問い合わせでは、
        対象社員以外の個別ソースを検索の時点で除外する必要がある。
    """
    return Filter(
        should=[
            # 条件A: 全社共通ソース
            FieldCondition(key="scope", match=MatchValue(value="common")),
            # 条件B: 個別ソース かつ 持ち主が対象社員
            Filter(
                must=[
                    FieldCondition(key="scope", match=MatchValue(value="individual")),
                    FieldCondition(key="owner_user_id", match=MatchValue(value=target_user_id)),
                ]
            ),
        ]
    )


def search(
    question: str,
    role: str,
    top_k: int = 3,
    target_user_id: str | None = None,
    self_user_id: str | None = None,
) -> list[dict]:
    """質問に意味が近いチャンクを、権限に応じた範囲から上位順に返す。

    入力:
        question       … ユーザーからの質問文
        role           … 質問した人の役割。'admin'（社長）か 'employee'（社員）
        top_k          … 上位何件を返すか（既定は3件）
        target_user_id … 社員データ画面から「この社員について」質問する場合の対象社員の user_id。
                         省略時（None）は従来どおりの挙動になる
        self_user_id   … 質問した本人の user_id。指定すると、社員の検索範囲に
                         「自分の個別ソース」が加わる。省略時（None）は共通ソースのみ

    出力:
        質問に近い順に並んだ辞書のリスト（最大 top_k 件）
        例: [{"text": "本文...", "source_id": "12"}, ...]
        source_id も返すのは、どのソースを根拠に回答したかを記録・表示するため。

    処理:
        1. 質問を embed_text でベクトル化する（保存時と同じモデルなので比較できる）
        2. role から検索範囲の絞り込み条件（フィルタ）を組み立てる
        3. sources コレクションで、そのベクトルに近いポイントを top_k 件検索する
        4. 各ヒットの payload から text と source_id を取り出して返す

    権限について（重要）:
        社員(admin以外)の検索では、既定で scope=='common' のチャンクだけを対象にする。
        こうしないと、他人の人事評価や給与などの個別ソース(scope='individual')が
        検索でヒットし、AIの回答に混ざってしまう。ここが情報漏洩を防ぐ最後の砦になる。
        self_user_id を渡した場合だけ、そこに「自分の個別ソース」が加わる。
        社長(admin)はフィルタを掛けず、共通・個別すべてのソースを検索できる。

    self_user_id に何を渡してよいか（重要）:
        リクエスト由来の値を渡してはならない。必ず JWT の user_id を渡すこと。
        リクエストボディやクエリの値をそのまま渡すと、社員が他人の user_id を
        送るだけで、その人の個別ソース（評価・給与）を読めてしまう。
        JWTは署名付きで書き換えられないので、そこから取り出した値だけが信用できる。

    なぜ target_user_id と self_user_id を分けているか（重要）:
        社員の経路に「リクエスト由来の値」がフィルタまで到達する道を、
        引数の段階で構造的に断つため。
        target_user_id は社長が画面で選んだ相手を指す引数で、社員の経路では
        一切参照しない。self_user_id は本人自身を指す引数で、JWT からしか来ない。
        1つの引数を両方の意味で使い回すと、呼び出し側のたった1箇所の取り違えで
        他人の個別ソースが開いてしまう。引数を分けておけば、
        「社員の経路で target_user_id を見ない」という規則をこの関数の中だけで守れる。

    判定の順番（この順である理由）:
        1. 社員(admin以外) かつ self_user_id あり → 共通 ＋ 自分の個別のみ
        2. 社員(admin以外) かつ 指定なし          → 共通のみ（従来どおり）
        3. 社長 かつ target_user_id               → 共通 ＋ その社員の個別のみ
        4. 社長 かつ 指定なし                     → 絞り込みなし（従来どおり）

        社員の判定を先に置くことで、万一 target_user_id が社員の経路から
        渡ってきても、社員が他人の個別ソースに届くことはない
        （社員の経路では target_user_id を一切見ない、という既存の安全設計は
        self_user_id を足したあとも変えていない）。
        呼び出し元（/api/chat/staff-inquiry）も require_admin で守っているので、
        入口と検索の二段構えになる。

    なぜベクトルで検索できるか:
        保存時に各チャンクを同じ方法でベクトル化してある。
        質問も同じ方法でベクトル化し、cosine距離が近いもの＝意味が近いものを探す。
    """
    # 1. 質問を、保存時と同じ embedding モデルでベクトル化する
    query_vector = embed_text(question)

    # 2. 権限に応じた検索範囲の絞り込み条件を作る
    query_filter: Filter | None
    if role != "admin":
        # 社員の経路。target_user_id はここでは一切見ない
        # （見てしまうと、他人を指す値が渡ったときに個別ソースが開いてしまう）
        if self_user_id is not None:
            # 本人が指定されている場合だけ、共通ソースに「自分の個別ソース」を足す。
            # self_user_id は JWT 由来の値であることが呼び出し側の責任
            query_filter = _build_staff_filter(self_user_id)
        else:
            # 共通ソースのみ。payload の scope が "common" のものだけを検索対象にする
            # （must = すべての条件を満たすもの、の意味）
            query_filter = Filter(
                must=[FieldCondition(key="scope", match=MatchValue(value="common"))]
            )
    elif target_user_id is not None:
        # 社長が「特定の社員について」質問した場合。
        # 共通ソースと、その社員の個別ソースだけに絞る（他の社員の個別ソースは対象外）
        query_filter = _build_staff_filter(target_user_id)
    else:
        # 社長の通常検索。全ソースを検索できるので、絞り込みなし
        query_filter = None

    # 3. そのベクトルに近いポイントを top_k 件検索する
    #    query_filter を渡すと、条件に合うポイントの中だけで「近いもの」を探す
    #    with_payload=True で、ヒットしたポイントに紐づく元テキスト等も一緒に取得する
    response = _client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )

    # 4. 各ヒットの payload から本文と、その根拠となったソースIDを取り出す
    #    payload が欠けたポイントは黙って読み飛ばさず、その場で止める。
    #    payload の scope は社員向けの権限フィルタ（上の query_filter）の判定材料そのもので、
    #    これが無いデータが混ざっている＝情報漏洩を防ぐ前提が崩れている状態だから。
    #    スキップすると検索は成功したように見え、異常に誰も気づけない。
    results: list[dict] = []
    for point in response.points:
        if point.payload is None:
            raise RuntimeError(
                f"検索結果に payload を持たないポイントがあります point_id={point.id}"
            )

        # 保存経路（save_chunks）は必ず両方を入れる。欠けているのは想定外のデータ
        if "text" not in point.payload or "source_id" not in point.payload:
            raise RuntimeError(
                f"検索結果の payload に必要な項目がありません point_id={point.id} "
                f"keys={sorted(point.payload)}"
            )

        results.append(
            {
                "text": point.payload["text"],
                "source_id": point.payload["source_id"],
            }
        )

    return results


def delete_by_source_id(source_id: str) -> None:
    """指定したソースのチャンクを、sources コレクションからまとめて削除する。

    入力:
        source_id … 削除したいソースのID（DBの sources.source_id を文字列にしたもの）

    出力:
        なし（Qdrant側から該当ポイントが消えた状態になる）

    処理:
        payload の source_id が一致するポイントを、条件（フィルタ）で指定して削除する。
        1つのソースは複数チャンクに分かれて保存されているので、
        ポイントIDを1つずつ指定するのではなく、source_id でまとめて消す。

    なぜ必要か:
        ソースを削除してもベクトルがQdrantに残っていると、
        「消したはずの資料」を根拠にAIが回答し続けてしまう。
        特に個別ソース（評価・給与など）が消し残ると情報漏洩につながる。
        DBの削除とQdrantの削除は必ず連動させる。
    """
    # source_id が一致するポイントだけを対象にする条件を組み立てる
    _client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))])
        ),
        wait=True,  # 削除の完了を待ってから戻る（消える前に次の処理へ進まないため）
    )
