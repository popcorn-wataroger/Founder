"""Qdrant（ベクトルDB）操作を担うモジュール。

この段階では「① コレクション（棚）を用意する」処理だけを実装している。
ポイント（ベクトル）の保存や検索は後続ステップで追加する。
"""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

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
    """コレクション（棚）が無ければ作る。あれば何もしない。

    入力:
        なし

    出力:
        なし（Qdrant側にコレクションが用意された状態になる）

    処理:
        1. 既存コレクション一覧を取得し、"sources" が既にあるか調べる
        2. 無ければ、3072次元・cosine距離のコレクションを新規作成する
        3. あれば作らない（何度呼んでも安全に動く＝冪等）

    なぜ冪等にするか:
        アプリ起動のたびに呼んでも、既存コレクションを壊さない・重複作成しないため。
    """
    # 既存コレクション名の一覧を集める
    existing = {c.name for c in _client.get_collections().collections}

    # 既に "sources" があれば、何もせず終了
    if COLLECTION_NAME in existing:
        return

    # 無ければ、次元数と距離を指定してコレクションを新規作成する
    _client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
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


def search(question: str, top_k: int = 3) -> list[str]:
    """質問に意味が近いチャンクの元テキストを、上位から返す。

    入力:
        question … ユーザーからの質問文
        top_k    … 上位何件を返すか（既定は3件）

    出力:
        質問に近い順に並んだ「元テキスト」のリスト（最大 top_k 件）

    処理:
        1. 質問を embed_text でベクトル化する（保存時と同じモデルなので比較できる）
        2. sources コレクションで、そのベクトルに近いポイントを top_k 件検索する
        3. 各ヒットの payload から元テキスト(text)を取り出してリストで返す

    権限について:
        現時点では全チャンクが検索対象。payload に scope / owner_user_id を持たせたので、
        後続ステップでここに権限フィルタ（社員なら scope='common' のみ）を追加する。

    なぜベクトルで検索できるか:
        保存時に各チャンクを同じ方法でベクトル化してある。
        質問も同じ方法でベクトル化し、cosine距離が近いもの＝意味が近いものを探す。
    """
    # 1. 質問を、保存時と同じ embedding モデルでベクトル化する
    query_vector = embed_text(question)

    # 2. そのベクトルに近いポイントを top_k 件検索する
    #    with_payload=True で、ヒットしたポイントに紐づく元テキスト等も一緒に取得する
    response = _client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=top_k,
        with_payload=True,
    )

    # 3. 各ヒットの payload から元テキストを取り出す（保存時に "text" キーで入れてある）
    return [point.payload["text"] for point in response.points]
