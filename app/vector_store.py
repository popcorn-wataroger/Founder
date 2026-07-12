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
        raise ValueError(
            f"chunks と vectors の件数が一致しません: {len(chunks)} != {len(vectors)}"
        )

    points: list[PointStruct] = []
    ids: list[str] = []

    # チャンクを1件ずつ「保存できる形（ポイント）」に変換する
    for index, (text, vector) in enumerate(zip(chunks, vectors)):
        # source_id とチャンク番号を元に、一意で毎回同じになるIDを生成する
        point_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{index}")
        )
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


def search(question: str, role: str, top_k: int = 3) -> list[dict]:
    """質問に意味が近いチャンクを、権限に応じた範囲から上位順に返す。

    入力:
        question … ユーザーからの質問文
        role     … 質問した人の役割。'admin'（社長）か 'employee'（社員）
        top_k    … 上位何件を返すか（既定は3件）

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
        社員(admin以外)の検索では scope=='common' のチャンクだけを対象にする。
        こうしないと、他人の人事評価や給与などの個別ソース(scope='individual')が
        検索でヒットし、AIの回答に混ざってしまう。ここが情報漏洩を防ぐ最後の砦になる。
        社長(admin)はフィルタを掛けず、共通・個別すべてのソースを検索できる。

    なぜベクトルで検索できるか:
        保存時に各チャンクを同じ方法でベクトル化してある。
        質問も同じ方法でベクトル化し、cosine距離が近いもの＝意味が近いものを探す。
    """
    # 1. 質問を、保存時と同じ embedding モデルでベクトル化する
    query_vector = embed_text(question)

    # 2. 権限に応じた検索範囲の絞り込み条件を作る
    if role == "admin":
        # 社長は全ソースを検索できるので、絞り込みなし
        query_filter = None
    else:
        # 社員は共通ソースのみ。payload の scope が "common" のものだけを検索対象にする
        # （must = すべての条件を満たすもの、の意味）
        query_filter = Filter(
            must=[FieldCondition(key="scope", match=MatchValue(value="common"))]
        )

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
    return [
        {
            "text": point.payload["text"],
            "source_id": point.payload["source_id"],
        }
        for point in response.points
    ]


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
            filter=Filter(
                must=[
                    FieldCondition(
                        key="source_id", match=MatchValue(value=source_id)
                    )
                ]
            )
        ),
        wait=True,  # 削除の完了を待ってから戻る（消える前に次の処理へ進まないため）
    )
