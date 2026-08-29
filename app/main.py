from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import init_db
from app.routers import auth_router, chat_router, sources_router
from app.routers.auth_router import (
    STAFF_LIST_EXCLUDED_ROLES,
    VALID_ROLES,
    require_account_manager,
    require_ceo,
    verify_token,
    verify_user_password,
)
from app.user_logins import get_last_login_at
from app.user_passwords import check_password_length, set_password
from app.user_roles import set_role
from app.users import (
    EmployeeCodeAlreadyExistsError,
    create_user,
    get_all_users,
    get_user_by_id,
    next_user_id,
    resolve_role,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Founder", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router.router)
app.include_router(sources_router.router)
# 管理者専用のソース一覧API（/api/admin/users/{user_id}/sources）
app.include_router(sources_router.admin_router)
app.include_router(chat_router.router)
# 管理者専用のチャット履歴API（/api/admin/users/{user_id}/chat-sessions）
app.include_router(chat_router.admin_router)

# 静的ファイル配信
app.mount("/static", StaticFiles(directory="static"), name="static")


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """チャットAPIエンドポイント（モック）"""
    if not req.message:
        return {"success": False, "message": "メッセージを入力してください"}
    return {"success": True, "reply": "（モック返答）AIがここで答えます。"}


@app.get("/api/admin/users")
async def get_admin_users(token: dict = Depends(require_ceo)):
    """管理者用スタッフ一覧APIエンドポイント（社長のみ）。

    権限について:
        require_ceo を付けているので、社員が叩くと403で拒否される。
        以前は verify_token だけだったため、ログインさえしていれば社員でも
        全社員の氏名・部署・雇用形態を取得できてしまっていた。
        スタッフ一覧は社長だけが見る画面なので、管理者チェックが必須。

    誰を一覧から外すか:
        STAFF_LIST_EXCLUDED_ROLES（ceo / admin）のロールを持つ人は並べない。
        ceo は見ている本人で、admin は業務データを持たない役割のため、
        どちらも「資料やトークを追う」というこの画面の目的に当てはまらない。
        除外するロールの一覧は app/routers/auth_router.py に集約している。
    """
    result = []
    for user in get_all_users():
        # users テーブルの role ではなく実効ロール（user_roles の上書きを優先）で判定する。
        # 素の role を見ると、DBで ceo にした社員が一覧に残り、
        # DBで employee にした既定の社長が一覧から消えたままになる
        if resolve_role(user) in STAFF_LIST_EXCLUDED_ROLES:
            continue
        result.append(
            {
                "user_id": user["user_id"],
                "employee_code": user["employee_code"],
                "name": user["name"],
                "department": user["department"],
                "employment_type": user["employment_type"],
            }
        )
    return result


@app.get("/api/admin/users/{user_id}")
async def get_admin_user_detail(user_id: str, token: dict = Depends(require_ceo)) -> dict[str, str]:
    """指定した社員1人分の基本情報を返す（社長専用）。

    入力:
        user_id … 見たい社員の user_id（URLパスで指定）
        token   … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        基本情報の辞書（社員コード・氏名・部署・性別・生年月日・家族構成・
        入社日・雇用形態・最終ログイン・実効ロール）

    処理:
        1. user_id で社員を1件探す。見つからなければ404
        2. 実効ロールを resolve_role で決める（DBの上書きがあればそれ、無ければCSVの値）
        3. 実効ロールが STAFF_LIST_EXCLUDED_ROLES（ceo / admin）なら404（下記の理由）
        4. 最終ログイン日時を user_logins テーブルから取り出す
        5. 画面に出す項目だけを1つずつ書き出して返す

    なぜ最終ログインだけ users テーブルから読まないか:
        users の last_login_at 列は全員空のまま使っていない。
        記録先は user_logins テーブルに分けてある（app/user_logins.py 参照）。
        まだ一度もログインしていない社員は記録が無いので空文字を返し、
        画面側（static/js/admin.js の formatLastLogin）が「未記録」と表示する。

    なぜ dict(user) をそのまま返さないか（重要）:
        users テーブルには password 列がある。user をそのまま返すと
        平文パスワードがAPIレスポンスに丸ごと乗ってしまう。
        「返す項目を1つずつ書き出す（ホワイトリスト方式）」にしておけば、
        将来テーブルに列が増えても、書き出していない列は自動的に外に出ない。

    なぜ role を返すようになったか:
        社員データ画面でその社員のロールを表示し、変更するために必要になったため
        （変更は PUT /api/admin/users/{user_id}/role）。
        現在のロールが分からないと、画面は変更後の値を選ばせようがない。
        このAPIは require_ceo を付けた社長専用なので、
        社員が他人のロールを知る経路にはならない。
        なお、返すのは users テーブルの role ではなく resolve_role の結果（実効ロール）。
        素の値をそのまま返すと、user_roles で上書きしたロールが画面に反映されず、
        「変更したのに変わっていない」ように見えてしまう。

    なぜ ceo と admin を404にするか:
        スタッフ一覧（GET /api/admin/users）が同じ STAFF_LIST_EXCLUDED_ROLES で
        両者を除外しているため、詳細だけ引ける状態にすると一覧と挙動がずれる。
        「一覧に出ない人は詳細も見られない」で揃えておく。
        admin を含めるのは、アカウント管理だけを担当して業務データを持たない役割だから。
        部署や家族構成、最終ログインを社員データ画面で追う対象ではない。
        判定に使う集合を一覧と共有しているので、次にロールが増えても
        「一覧には出ないのに詳細は引ける」というずれは生まれない。
    """
    user = get_user_by_id(user_id)

    # 存在しない user_id はここで打ち切る（このあと user を辿るため）
    if user is None:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # ロールは users の値ではなく user_roles の上書きを優先した「実効ロール」を使う。
    # 404の判定と返す値の両方でこれを使い回すのは、resolve_role が
    # user_roles をDBから引く（＝呼ぶたびに接続を開く）ため。
    # 2回引くと同じ答えのために接続が2回開くうえ、その間にロールが
    # 変わると「一覧に出ない人の詳細が返る」ような食い違いも起こりうる
    role = resolve_role(user)

    # 社長本人とシステム管理者（スタッフ一覧に出ない人）も404で揃える。
    # ここもスタッフ一覧と同じく実効ロールで、同じ集合を見る。素の role で判定すると、
    # 一覧には出ないのに詳細は引ける（またはその逆）というずれが生まれる
    if role in STAFF_LIST_EXCLUDED_ROLES:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 最終ログインは users ではなく user_logins が持つ。記録が無ければ空文字（画面は「未記録」表示）
    last_login_at = get_last_login_at(user_id) or ""

    # 画面に出す項目だけを明示的に書き出す（password は返さない）
    return {
        "user_id": user["user_id"],
        "employee_code": user["employee_code"],
        "name": user["name"],
        "department": user["department"],
        "gender": user["gender"],
        "birth_date": user["birth_date"],
        "family": user["family"],
        "hire_date": user["hire_date"],
        "employment_type": user["employment_type"],
        "last_login_at": last_login_at,
        "role": role,
    }


class RoleUpdateRequest(BaseModel):
    role: str


@app.put("/api/admin/users/{user_id}/role")
async def update_admin_user_role(
    user_id: str, req: RoleUpdateRequest, token: dict = Depends(require_ceo)
) -> dict[str, object]:
    """指定した社員のロールを変更する（社長専用）。

    入力:
        user_id … ロールを変える対象の user_id（URLパスで指定）
        req     … リクエストボディ（RoleUpdateRequest）
            role … 変更後のロール名（employee / source_manager / ceo）
        token   … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        {"success": True, "user_id": 対象のuser_id, "role": 変更後のロール}

    処理:
        1. role が妥当な値か検証する（VALID_ROLES に無ければ400）
        2. 対象社員が実在するか確認する（見つからなければ404）
        3. 自分自身への変更を拒否する（403）
        4. user_roles テーブルに保存する

    エラー:
        400 … 知らないロール名を指定した場合
        403 … 社員が叩いた場合（require_ceo が投げる）／自分自身を対象にした場合
        404 … 対象の user_id が存在しない場合

    権限について:
        require_ceo を付けているので、社員と source_manager が叩くと403で拒否される。
        ロールの付け替えは「誰が何を見られるか」を決める操作なので、
        共通ソースを登録できるだけの source_manager には許さない。

    いつから有効になるか（重要）:
        変更は対象ユーザーの「次回ログインから」有効になる。
        ロールはログイン時に発行するJWTへ焼き付けられ、発行後は書き換えられないため、
        すでにログイン中の相手が持っているトークンには反映されない
        （app/routers/auth_router.py の login を参照）。
        すぐに反映させたい場合は、対象者にログインし直してもらう必要がある。

    なぜロール名の集合を自前で持たないか:
        ロール名の定義は app/routers/auth_router.py の定数に集約している。
        こちらにも書くと、ロールが増えたときに片方だけ直して
        「保存はできるが権限判定が知らないロール」ができてしまう。

    なぜ get_admin_user_detail と違って ceo を404にしないか:
        あちらは「スタッフ一覧に出ない人は詳細も見られない」で揃えるために
        ceo を404にしている。
        こちらは社長が誰かを社長にする／社長から降ろすための操作なので、
        社長を対象にできなければ機能そのものが成り立たない。
        目的が違うので、ceo の扱いも意図的に変えている。
    """
    # 1. 知らないロール名を保存させない。
    #    ここを通してしまうと、権限判定が知らない値がDBに入り、
    #    その社員は「どの権限にも当てはまらない状態」でログインすることになる
    if req.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role は {' / '.join(sorted(VALID_ROLES))} のいずれかを指定してください",
        )

    # 2. 実在しない user_id を保存させない（user_roles に幽霊の行が増えるのを防ぐ）
    if get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 3. 自分自身のロールは変えられない。
    #    最後の社長が自分を employee に落とすと、誰もこのAPIを叩けなくなり、
    #    ロールを戻す手段がDBの直接操作しか無くなる（社長ゼロの事故）。
    #    変更先が ceo であっても一律で拒否するのは、判定を単純に保つため。
    #    「ceo → ceo なら実質何も変わらないので許す」といった例外を作ると、
    #    条件が増えるほど事故を防げているかの確認が難しくなる
    if user_id == token["user_id"]:
        raise HTTPException(status_code=403, detail="自分自身のロールは変更できません")

    # 4. 誰がいつ変えたかを一緒に残す（updated_by は変更した社長の user_id）
    set_role(user_id, req.role, updated_by=token["user_id"])

    return {"success": True, "user_id": user_id, "role": req.role}


def _reject_invalid_password(password: str) -> None:
    """パスワードの長さが規則に反していれば400で中断する。

    入力:
        password … 検査したい平文のパスワード

    処理:
        app/user_passwords.py の check_password_length() に判定を任せ、
        メッセージが返ってきたら（＝規則違反なら）HTTPException を投げる。

    出力:
        なし（問題が無ければ何も起きずに戻る）

    例外:
        HTTPException(400) … 短すぎる、または長すぎるとき

    なぜ判定とHTTPへの変換を分けるのか:
        「何文字以上か」はパスワードの決まりごとなので app/user_passwords.py が持ち、
        「規則違反を何番で返すか」はWeb層の都合なのでこちらが持つ。
        パスワードを扱うAPIは3本（アカウント追加・自分の変更・強制上書き）あり、
        この2行を3回書き写すと、片方だけ直す事故が起きる。

    セキュリティ:
        引数 password の中身はログにも例外メッセージにも出さない。
        detail に入るのは check_password_length() が返す規則の説明だけで、
        利用者が入力した値そのものは含まれない。
    """
    message = check_password_length(password)
    if message is not None:
        raise HTTPException(status_code=400, detail=message)


class AccountCreateRequest(BaseModel):
    employee_code: str
    name: str
    role: str
    password: str


@app.post("/api/admin/accounts")
async def create_account(
    req: AccountCreateRequest, token: dict = Depends(require_account_manager)
) -> dict[str, object]:
    """新しいアカウントを1つ追加する（システム管理者専用）。

    入力:
        req … リクエストボディ（AccountCreateRequest）
            employee_code … 社員コード（ログインに使う。全社で一意）
            name          … 氏名
            role          … ロール名（employee / source_manager / ceo / admin）
            password      … 初期パスワード
        token … require_account_manager が返すログイン情報
                （システム管理者でなければ403で弾かれている）

    出力:
        {"success": True, "user_id": 振られたuser_id, "employee_code": …, "role": …}

    処理:
        1. 社員コードと氏名が空でないか確かめる（400）
        2. role が妥当な値か検証する（VALID_ROLES に無ければ400）
        3. パスワードの長さを検証する（短すぎる／長すぎるなら400）
        4. next_user_id() で user_id を採番する
        5. create_user() で users に1行入れる（社員コードが重複していれば409）
        6. set_password() で初期パスワードをハッシュにして保存する

    エラー:
        400 … 社員コード・氏名が空／知らないロール名／パスワードの長さが規定外
        403 … システム管理者以外が叩いた場合（require_account_manager が投げる）
        409 … 社員コードが既に使われている場合

    権限について:
        require_account_manager を付けているので、社員・共通ソース管理者・社長は403になる。
        アカウントの追加はシステム管理者(admin)だけの仕事として
        app/routers/auth_router.py の can_manage_accounts() に集約されている。
        社長(ceo)も弾かれるのは、CLAUDE.md の権限表どおりの姿。

    なぜ検証を先にまとめて済ませるか（重要）:
        users への INSERT（create_user）と user_passwords への保存（set_password）は
        別々のトランザクションで、まとめて取り消す仕組みを持っていない。
        後半で弾かれると「行はあるがパスワードが無いアカウント」が残る。
        値の検証を全部先に通しておけば、書き込みが始まったあとに
        自分から失敗する経路が無くなる。

    それでも set_password が失敗したらどうなるか:
        users に行だけが残る。そのアカウントはログインできない
        （users.password は空文字で、ログインAPIは空のパスワードを400で弾くため、
        平文フォールバックでも一致しない）。
        入れないより中途半端に見えるが、「入れた覚えのないパスワードで入れてしまう」
        よりは安全側なので、この段階ではこの状態を許容する。
        作り直しは同じ社員コードで409になるため、
        取り消し方（アカウント削除）は後続のIssueで扱う。

    なぜ前後の空白を取り除くか:
        画面やコピー&ペーストで社員コードの末尾に空白が紛れることがある。
        そのまま保存すると、見た目が同じなのにログインでは一致しない
        アカウントができあがり、原因が非常に分かりにくい。
    """
    employee_code = req.employee_code.strip()
    name = req.name.strip()

    # 1. 空のまま作らせない。空の社員コードでは誰もログインできない
    if not employee_code or not name:
        raise HTTPException(status_code=400, detail="社員コードと氏名は必須です")

    # 2. 知らないロール名を保存させない。
    #    ここを通すと、権限判定が知らない値を持った社員ができてしまう
    #    （ロール名の定義は auth_router に集約している。ここでは持たない）
    if req.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role は {' / '.join(sorted(VALID_ROLES))} のいずれかを指定してください",
        )

    # 3. パスワードの長さ。短すぎるものは推測されやすく、
    #    長すぎるものは bcrypt が扱えない（hash_password が ValueError になる）。
    #    規則も文言も app/user_passwords.py に集約してあるので、
    #    パスワードを扱う他のAPIと必ず同じ応答になる
    _reject_invalid_password(req.password)

    # 4-5. 番号を採ってから行を作る。社員コードの重複はDBの一意制約が見つける
    user_id = next_user_id()
    try:
        create_user(
            employee_code=employee_code,
            name=name,
            role=req.role,
            user_id=user_id,
        )
    except EmployeeCodeAlreadyExistsError as error:
        raise HTTPException(
            status_code=409,
            detail=f"社員コード {error.employee_code} は既に使われています",
        ) from error

    # 6. 初期パスワードはハッシュにして user_passwords に持つ（平文はどこにも残さない）
    set_password(user_id, req.password)

    return {
        "success": True,
        "user_id": user_id,
        "employee_code": employee_code,
        "role": req.role,
    }


class MyPasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@app.put("/api/me/password")
async def change_my_password(
    req: MyPasswordChangeRequest, token: dict = Depends(verify_token)
) -> dict[str, object]:
    """ログイン中の本人が自分のパスワードを変更する（全ロール）。

    入力:
        req … リクエストボディ（MyPasswordChangeRequest）
            current_password … 今のパスワード（本人確認のため）
            new_password     … 新しいパスワード
        token … verify_token が検証したJWTの中身（user_id / role を含む）

    出力:
        {"success": True}

    処理:
        1. トークンの user_id から社員を1件引く
        2. current_password を verify_user_password() で照合する
        3. new_password の長さを検証する
        4. set_password() でハッシュにして保存する

    エラー:
        400 … new_password の長さが規定外
        401 … 未ログイン／トークンが無効（verify_token が投げる）、
              または current_password が一致しない

    なぜ verify_token を使うのか（重要）:
        パスワードの変更は、業務機能ではなく自分のアカウントの管理。
        システム管理者(admin)を含め、ログインできる人は全員が自分の分を変えられるべき。
        require_business_user を使うと admin が自分のパスワードを変えられなくなり、
        初期パスワードのまま運用し続けることになる。

    なぜ照合を自前で書かないのか（重要）:
        app/routers/auth_router.py の verify_user_password() は、
        user_passwords にハッシュがあればそれで照合し、
        まだ無ければ users.password（data/users.csv 由来の平文）と比べる、
        という移行中の事情を含んだ判定になっている。
        ここで自前の照合を書くと、まだ一度もログインしていない社員が
        「ログインはできるのにパスワードを変更できない」状態になる。
        判定を1箇所に保つため、ログインと同じ関数をそのまま使う。

        なお verify_user_password() は平文で一致したときハッシュを保存する。
        そのため変更処理の中で「古いパスワードのハッシュ保存 → 新しいパスワードで上書き」
        と2回書き込むことになるが、結果は変わらない。
        判定を1箇所に保つことの方を優先している。

    なぜ新旧が同じパスワードでも許すのか:
        「前と同じものは使えない」を入れるには過去のパスワードを覚えておく必要があり、
        そのぶん保存する情報が増える。Issue #123 では禁止しないと決めている。

    401のメッセージを1種類にしている理由:
        社員が見つからない場合も、パスワードが違う場合も同じ文言を返す。
        分けて返すと「このトークンの持ち主はもう存在しない」という
        内部の状態を外から確かめられるようになる。
        利用者にとっては、どちらの場合も「やり直してください」で行動が変わらない。

    セキュリティ:
        current_password / new_password の中身はログにも応答にも出さない。
    """
    user = get_user_by_id(token["user_id"])

    # 1-2. 本人確認。ここを通らないと誰のパスワードでも変えられてしまう。
    #      社員が見つからない場合（トークン発行後に消えた場合）も同じ扱いにする
    if user is None or not verify_user_password(user, req.current_password):
        raise HTTPException(status_code=401, detail="現在のパスワードが正しくありません")

    # 3. 新しいパスワードの長さ（規則も文言も app/user_passwords.py が持つ）
    _reject_invalid_password(req.new_password)

    # 4. ハッシュにして保存する（平文はどこにも残さない）
    set_password(user["user_id"], req.new_password)

    return {"success": True}


class AccountPasswordResetRequest(BaseModel):
    new_password: str


@app.put("/api/admin/accounts/{user_id}/password")
async def reset_account_password(
    user_id: str,
    req: AccountPasswordResetRequest,
    token: dict = Depends(require_account_manager),
) -> dict[str, object]:
    """指定した社員のパスワードを強制的に上書きする（システム管理者専用）。

    入力:
        user_id … 対象の社員の user_id（URLパスで指定）
        req     … リクエストボディ（AccountPasswordResetRequest）
            new_password … 新しいパスワード
        token   … require_account_manager が返すログイン情報
                  （システム管理者でなければ403で弾かれている）

    出力:
        {"success": True, "user_id": 対象のuser_id}

    処理:
        1. 対象の社員が実在するか確かめる（見つからなければ404）
        2. new_password の長さを検証する
        3. set_password() でハッシュにして保存する

    エラー:
        400 … new_password の長さが規定外
        403 … システム管理者以外が叩いた場合（require_account_manager が投げる）
        404 … 対象の user_id が存在しない場合

    なぜ現在のパスワードを要求しないのか:
        これは本人がパスワードを忘れたときに管理者が復旧させるための入口。
        現在のパスワードを求めると、忘れた人を助けられない。
        「本人でなくても変えられる」ことそのものが目的なので、
        代わりに require_account_manager で入口を絞っている。

    なぜ ceo や admin を404にしないのか:
        社員データ画面（GET /api/admin/users/{user_id}）は
        STAFF_LIST_EXCLUDED_ROLES で ceo / admin を404にしているが、
        あちらは「業務上の社員を見る画面」なので対象から外している。
        こちらはアカウントの管理で、社長やシステム管理者のパスワードも
        復旧できなければ機能として成り立たない。目的が違うので判定も変える。

    自分自身を対象にできる理由:
        禁止する理由がない。パスワードは自分のものを変えるのが正当な操作で、
        ロール変更（自分を降格させると社長ゼロになる）のような事故が起きない。
        本人が変える経路は PUT /api/me/password にもあるが、
        こちらを使っても同じ結果になる。

    セキュリティ:
        new_password の中身はログにも応答にも出さない。
        応答に含めるのは「誰のパスワードを変えたか」までにとどめる。
    """
    # 1. 実在しない user_id に保存させない（user_passwords に幽霊の行が増えるのを防ぐ）
    if get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 2. 新しいパスワードの長さ（アカウント追加・本人による変更と同じ規則）
    _reject_invalid_password(req.new_password)

    # 3. ハッシュにして保存する。既に記録があれば上書きされる（set_password は UPSERT）
    set_password(user_id, req.new_password)

    return {"success": True, "user_id": user_id}


@app.get("/")
async def root():
    return FileResponse("static/index.html")
