"""ストレージ層（app/storage.py）のテスト。

何を守りたいか:
    1. 保存先の切り替えが、設定どおりに決まること
       （本番相当でバケット未設定なら、ローカルへフォールバックせずに止まる）
    2. 保存名の検証を通らない値が、実際の保存先まで届かないこと
       ローカルなら uploads/ の外、GCSならバケット内の想定外の場所に触れさせない
    3. 保存 → 読み出し → 削除が、どちらの保存先でも同じように振る舞うこと

なぜ実際のGCSに繋がないか:
    テストのたびにネットワークと認証情報が必要になると、CIでも手元でも動かせない。
    確かめたいのは「SDKにどんな名前を渡すか」「戻り値をどう扱うか」なので、
    SDKの代わりに、呼び出しを記録する偽のバケットを差し込む。
"""

import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from google.cloud.exceptions import NotFound

from app import storage, upload_paths

# 保存名の生成規則に合わない、あるいは保存先の外を指そうとする値。
# app/routers/sources_router.py のダウンロードテストと同じ考え方の一覧
危険な保存パス一覧 = [
    "/etc/passwd",  # 絶対パスで保存先の外を指す
    "../../secret.txt",  # 相対パスで上の階層へ抜ける
    "uploads/../../secret.txt",
    "uploads/..\\..\\secret.txt",  # Windows形式の区切り文字
    "uploads/evil.txt",  # 保存先の中だが生成規則に合わない名前
    "gs://another-bucket/secret.txt",  # 別のバケットを指そうとする
    "sources/../../secret.txt",
    "",
]


def _safe_save_name(suffix: str = ".txt") -> str:
    """アップロード時と同じ生成規則で、安全な保存名を1つ作る。"""
    return f"{'2' * 20}_{uuid.uuid4()}{suffix}"


# ---------------------------------------------------------------------------
# 1層目: 保存先の切り替え（_resolve_backend）
# ---------------------------------------------------------------------------


def test_バケット名があればGCSを使う() -> None:
    assert storage._resolve_backend("founder-sources", "local") == "gcs"
    # 本番でもバケットがあればGCS。APP_ENV は判断を変えない
    assert storage._resolve_backend("founder-sources", "production") == "gcs"


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_開発環境ならバケット未設定でもローカルに保存する(app_env: str) -> None:
    """全員がGCPの認証情報を持つとは限らないので、開発は uploads/ で続けられる。"""
    assert storage._resolve_backend("", app_env) == "local"


@pytest.mark.parametrize("app_env", ["", "production", "staging"])
def test_本番相当でバケット未設定なら起動を止める(app_env: str) -> None:
    """危険側（ローカル保存）にフォールバックさせない。

    Cloud Run のコンテナは停止時に中身が消えるため、ローカル保存に倒れると
    「アップロードは成功したのにファイルが無い」状態を、エラーも出さずに作ってしまう。
    設定漏れは起動を止めて気づける形にする（config の JWT_SECRET_KEY と同じ原則）。
    """
    with pytest.raises(RuntimeError):
        storage._resolve_backend("", app_env)


# ---------------------------------------------------------------------------
# 2層目: ローカル保存の経路
# ---------------------------------------------------------------------------


@pytest.fixture
def local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """保存先を「使い捨てディレクトリへのローカル保存」に固定する。"""
    test_upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(upload_paths, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "APP_ENV", "test")
    return test_upload_dir


def test_ローカル_保存して読んで消せる(local_dir: Path) -> None:
    """保存 → 存在確認 → 読み出し → 削除、の一周を確かめる。"""
    save_name = _safe_save_name()

    file_path = storage.save(save_name, "就業規則の本文です".encode())

    # DBに記録される値は uploads/<保存名> の形
    assert file_path == str(local_dir / save_name)
    assert (local_dir / save_name).is_file()

    assert storage.exists(file_path) is True
    assert storage.read_bytes(file_path) == "就業規則の本文です".encode()

    storage.delete(file_path)

    assert storage.exists(file_path) is False
    assert not (local_dir / save_name).exists()


def test_ローカル_保存先ディレクトリが無くても保存できる(local_dir: Path) -> None:
    """初回アップロード時は uploads/ がまだ存在しない。"""
    assert not local_dir.exists()

    storage.save(_safe_save_name(), b"body")

    assert local_dir.is_dir()


def test_ローカル_ストリーミングは分割して読み出す(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中身が欠けずに、かつ一度に全部メモリへ載せずに読める。"""
    # 1MBのファイルを用意するのは重いので、読み出し単位のほうを小さくして確かめる
    monkeypatch.setattr(storage, "STREAM_CHUNK_SIZE", 4)
    file_path = storage.save(_safe_save_name(), b"0123456789")

    chunks = list(storage.open_stream(file_path))

    assert b"".join(chunks) == b"0123456789"
    assert len(chunks) > 1, "分割されずに一括で読み出されている"


def test_ローカル_存在しないファイルの削除は失敗しない(local_dir: Path) -> None:
    """削除は冪等。ロールバックや再削除のたびに呼び出し側が握りつぶさなくてよい。"""
    storage.delete(str(local_dir / _safe_save_name()))


def test_ローカル_存在しないファイルはexistsがFalse(local_dir: Path) -> None:
    assert storage.exists(str(local_dir / _safe_save_name())) is False


@pytest.mark.parametrize("危険な保存パス", 危険な保存パス一覧)
def test_ローカル_危険な保存パスは読み書きできない(
    local_dir: Path, tmp_path: Path, 危険な保存パス: str
) -> None:
    """uploads/ の外にある実ファイルを、読むことも消すこともできない。"""
    # 狙われる位置に本物のファイルを置き、実際に触れてしまわないか確かめる
    local_dir.mkdir(parents=True, exist_ok=True)
    おとり = tmp_path / "secret.txt"
    おとり.write_text("これは漏れてはいけない内容です", encoding="utf-8")
    (local_dir / "evil.txt").write_text("これも漏れてはいけない内容です", encoding="utf-8")

    with pytest.raises(ValueError):
        storage.read_bytes(危険な保存パス)

    with pytest.raises(ValueError):
        storage.exists(危険な保存パス)

    with pytest.raises(ValueError):
        storage.delete(危険な保存パス)

    with pytest.raises(ValueError):
        list(storage.open_stream(危険な保存パス))

    with pytest.raises(ValueError):
        storage.save(危険な保存パス, "上書きを狙う中身".encode())

    # おとりが消えても中身が変わってもいない
    assert おとり.read_text(encoding="utf-8") == "これは漏れてはいけない内容です"
    assert (local_dir / "evil.txt").read_text(encoding="utf-8") == "これも漏れてはいけない内容です"


# ---------------------------------------------------------------------------
# 3層目: GCS保存の経路（偽のバケットを差し込む）
# ---------------------------------------------------------------------------


class FakeBlob:
    """google-cloud-storage の Blob のうち、storage.py が使う操作だけを真似た偽物。"""

    def __init__(self, name: str, objects: dict[str, bytes]) -> None:
        self.name = name
        self._objects = objects

    def upload_from_string(self, data: bytes) -> None:
        self._objects[self.name] = data

    def download_as_bytes(self) -> bytes:
        if self.name not in self._objects:
            raise NotFound(self.name)
        return self._objects[self.name]

    def exists(self) -> bool:
        return self.name in self._objects

    def delete(self) -> None:
        if self.name not in self._objects:
            raise NotFound(self.name)
        del self._objects[self.name]

    def open(self, mode: str) -> io.BytesIO:
        if self.name not in self._objects:
            raise NotFound(self.name)
        return io.BytesIO(self._objects[self.name])


class FakeBucket:
    """偽のバケット。保存した中身と、SDKに渡されたオブジェクト名を記録する。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requested_names: list[str] = []

    def blob(self, name: str) -> FakeBlob:
        # 「どんな名前でSDKを呼んだか」を検証できるよう控えておく
        self.requested_names.append(name)
        return FakeBlob(name, self.objects)


@pytest.fixture
def fake_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBucket]:
    """保存先を「偽のバケットへのGCS保存」に固定する。

    実際のGCSには一切繋がない。_get_bucket ごと差し替えるので、
    認証情報もネットワークも要らない。
    """
    bucket = FakeBucket()
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(storage, "_get_bucket", lambda: bucket)
    yield bucket


def test_GCS_保存して読んで消せる(fake_bucket: FakeBucket) -> None:
    """保存 → 存在確認 → 読み出し → 削除、の一周を確かめる。"""
    save_name = _safe_save_name()

    file_path = storage.save(save_name, "就業規則の本文です".encode())

    # SDKに渡すオブジェクト名は「定数プレフィックス + 検証済みの保存名」
    assert fake_bucket.requested_names == [f"sources/{save_name}"]
    assert fake_bucket.objects[f"sources/{save_name}"] == "就業規則の本文です".encode()

    # DBに記録する値は uploads/<保存名> の形のまま（ローカル保存時と揃える）
    assert file_path == str(Path("uploads") / save_name)

    assert storage.exists(file_path) is True
    assert storage.read_bytes(file_path) == "就業規則の本文です".encode()

    storage.delete(file_path)

    assert storage.exists(file_path) is False


def test_GCS_ストリーミングは分割して読み出す(
    fake_bucket: FakeBucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "STREAM_CHUNK_SIZE", 4)
    file_path = storage.save(_safe_save_name(), b"0123456789")

    chunks = list(storage.open_stream(file_path))

    assert b"".join(chunks) == b"0123456789"
    assert len(chunks) > 1, "分割されずに一括で読み出されている"


def test_GCS_存在しないオブジェクトの削除は失敗しない(fake_bucket: FakeBucket) -> None:
    """既に消えていても目的は達成されているので、例外にしない（冪等）。"""
    storage.delete(str(Path("uploads") / _safe_save_name()))


def test_GCS_存在しないオブジェクトはexistsがFalse(fake_bucket: FakeBucket) -> None:
    assert storage.exists(str(Path("uploads") / _safe_save_name())) is False


@pytest.mark.parametrize("危険な保存パス", 危険な保存パス一覧)
def test_GCS_危険な保存パスはSDKに届かない(fake_bucket: FakeBucket, 危険な保存パス: str) -> None:
    """検証を通らない名前は、blob() を呼ぶ手前で止まる。

    「呼ばれていない」ことまで確かめるのは、名前を無害化して呼ぶのではなく
    そもそも到達させない設計になっているかを固定するため。
    """
    # 狙われる位置に本物のオブジェクトを置いておく
    fake_bucket.objects["secret.txt"] = "これは漏れてはいけない内容です".encode()
    fake_bucket.objects["sources/evil.txt"] = "これも漏れてはいけない内容です".encode()

    for 操作 in (
        lambda: storage.read_bytes(危険な保存パス),
        lambda: storage.exists(危険な保存パス),
        lambda: storage.delete(危険な保存パス),
        lambda: list(storage.open_stream(危険な保存パス)),
        lambda: storage.save(危険な保存パス, "上書きを狙う中身".encode()),
    ):
        with pytest.raises(ValueError):
            操作()

    assert fake_bucket.requested_names == [], "危険な名前でSDKが呼ばれた"
    assert fake_bucket.objects["secret.txt"] == "これは漏れてはいけない内容です".encode()
    assert fake_bucket.objects["sources/evil.txt"] == "これも漏れてはいけない内容です".encode()


def test_GCS_クライアントはimport時に作られない(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報が無い環境でも import できること（起動できなくなるのを防ぐ）。

    import 時にクライアントを作ると、GCSを使わないテストやローカル開発でも
    認証情報が無いだけでアプリ全体が起動しなくなる。
    """
    assert storage._client is None

    # _get_bucket を初めて呼んだときに初めて作られる
    作られた: list[str] = []

    class FakeClient:
        def bucket(self, name: str) -> Any:
            作られた.append(name)
            return FakeBucket()

    monkeypatch.setattr(storage.gcs, "Client", lambda: FakeClient())
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(storage, "_client", None)

    storage._get_bucket()

    assert 作られた == ["test-bucket"]
