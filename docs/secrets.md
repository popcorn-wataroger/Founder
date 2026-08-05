# 機密情報（シークレット）の管理ガイド

APIキーや署名鍵などの機密情報を、ローカル開発と本番でどう扱うかをまとめたドキュメントです。

関連 Issue: #67（Secret Manager 導入）／#66（Cloud Run デプロイ）

---

## 基本の考え方

機密情報の「正解」を1か所に置き、アプリはそれを**環境変数として受け取るだけ**にします。

| 環境 | 値の供給元 | アプリ側の読み方 |
|---|---|---|
| ローカル開発 | `.env` ファイル | `os.getenv()` |
| 本番（Cloud Run） | Google Secret Manager → 環境変数としてマウント | `os.getenv()`（同じ） |

Cloud Run はデプロイ設定で「このシークレットをこの環境変数に入れる」と指定でき、コンテナ起動時にGCP側が値を注入します。そのためアプリ側に Secret Manager のライブラリは不要で、読み込み口は `app/config.py` の1か所だけです。

読み込みを担当しているファイル: `app/config.py`

---

## 管理対象の一覧

| 変数 | 用途 | ローカル | 本番 |
|---|---|---|---|
| `JWT_SECRET_KEY` | ログイントークンの署名鍵 | 任意（未設定なら開発用の既定値） | **必須**（未設定なら起動しない） |
| `GEMINI_API_KEY` | Gemini API の認証 | 必要 | 必要 |
| `QDRANT_URL` | Qdrant の接続先 | 必要 | 必要 |
| `QDRANT_API_KEY` | Qdrant Cloud の認証 | 必要 | 必要 |
| `APP_ENV` | 実行環境の識別子 | `local` | `production` など |
| `GCS_BUCKET_NAME` | GCSバケット名 | — | 機密ではない。環境変数のままでよい |
| `DATABASE_URL` | Cloud SQL の接続情報 | — | Issue #65 で導入予定 |

---

## APP_ENV の扱い（重要）

`APP_ENV` が `local` / `test` のときだけ、開発用のゆるい既定値が許されます。それ以外の値、**および未設定のときは本番相当**として扱います。

未設定を「ローカル扱い」にしないのは、本番で `APP_ENV` を渡し忘れたときに開発用の鍵で起動が通ってしまうからです。設定漏れは安全な側（起動しない）に倒します。

- ローカル: `.env` の `APP_ENV=local`
- CI: `.github/workflows/ci.yml` の `APP_ENV: test`

どちらも明示的に指定しているため、この扱いによる影響は受けません。

---

## JWT_SECRET_KEY にフォールバックを置かない理由

以前 `app/config.py` は、未設定時に `"fallback-secret-key"` という固定値を使っていました。この値は公開リポジトリに書かれているため、そのまま本番に出ると次のことが起きます。

1. 攻撃者が同じ鍵で `role: admin` のトークンを自作できる
2. ログインせずに管理者としてアクセスできる
3. 全社員の個別ソース（評価・給与情報など）が閲覧できる

しかも**起動は成功してしまう**ため、事故が起きるまで誰も気づけません。そこで固定値のフォールバックを廃止し、本番相当の環境で未設定なら `RuntimeError` を投げて起動を止めています。

エラーメッセージには鍵の値を含めません。ログに出してよいのは「設定あり/なし」だけです。

---

## ローカル開発の手順

### 1. `.env` を用意する

```bash
cp .env.example .env
```

`.env` は `.gitignore` に入っているため commit されません。

### 2. 値を入れる

| 変数 | 入手先 |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio で発行 |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant Cloud のダッシュボード |
| `JWT_SECRET_KEY` | 下記コマンドで自分用に生成（空のままでも起動する） |

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`APP_ENV=local` は `.env.example` に入っているので、消さないでください。

### 3. 起動して確認する

```bash
uv run uvicorn app.main:app --reload
```

http://localhost:8000 を開き、EMP001（社員画面）と ADMIN（管理者画面）でログインできることを確認します。

---

## やってはいけないこと

- `.env` や鍵の値を commit する
- 鍵の値をチャット・Issue・PR に貼る（受け渡しはパスかクリップボード経由で）
- 読み込み確認のつもりで値を `print` / ログ出力する
- `app/config.py` に値を直書きする

---

## 本番（Google Secret Manager）

使用するGCPプロジェクト:

| 項目 | 値 |
|---|---|
| プロジェクトID | `notebooklm-482403` |
| プロジェクト番号 | `525613033246` |
| コンソール | https://console.cloud.google.com/security/secret-manager?project=notebooklm-482403 |

### 登録済みのシークレット

| シークレット名 | レプリケーション | 暗号鍵 | 状態 |
|---|---|---|---|
| `JWT_SECRET_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `GEMINI_API_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `QDRANT_API_KEY` | 自動 | Google管理 | バージョン1が有効 |
| `QDRANT_URL` | 自動 | Google管理 | バージョン1が有効 |

対象外にしたもの:

- `DATABASE_URL` … Issue #65 で Cloud SQL を導入するときに追加する
- `GCS_BUCKET_NAME` … 機密ではないため、環境変数のままにする
- `OPENAI_API_KEY` … 本プロジェクトでは未使用のため登録しない

リソースパスは `projects/525613033246/secrets/JWT_SECRET_KEY` の形式になります。プロジェクトIDではなくプロジェクト番号が使われる点に注意してください。

---

### 1. シークレットの登録手順

#### 前提: Googleアカウントの2段階認証（MFA）

Google Cloud では多要素認証（MFA）が段階的に必須化されています。**適用時期はアカウントの種別によって異なる**ため、自分のアカウントがいつから対象になるかは公式ドキュメントで確認してください。

- [Google Cloud の MFA 要件](https://cloud.google.com/identity/docs/mfa-requirement)

必須化前でも有効にしておけば影響を受けません。未設定の場合は先に https://myaccount.google.com/security から2段階認証を有効にしておくことを勧めます。

#### コンソールでの操作

1. https://console.cloud.google.com/security/secret-manager?project=notebooklm-482403 を開く
2. 「**シークレットを作成**」をクリック
3. 「名前」に**環境変数名と同じ名前**を入力する（例: `JWT_SECRET_KEY`）
4. 「シークレットの値」に値を貼り付ける
5. それ以外の設定項目（レプリケーション、暗号鍵、ローテーション、有効期限、ラベル）は**すべてデフォルトのままでよい**
6. 「シークレットを作成」をクリック

名前を環境変数名と揃えるのは、Cloud Run のマウント設定と `app/config.py` の `os.getenv()` が同じ名前で一直線につながるようにするためです。名前がずれると、どのシークレットがどの設定に対応するのか追えなくなります。

#### gcloud CLI での操作

```bash
# シークレットの入れ物を作る（値はまだ入れない）
gcloud secrets create JWT_SECRET_KEY \
  --replication-policy=automatic \
  --project=notebooklm-482403

# 値を1つ目のバージョンとして追加する
gcloud secrets versions add JWT_SECRET_KEY \
  --data-file=/path/to/secret.txt \
  --project=notebooklm-482403
```

値はファイル経由で渡します。`echo "値" | gcloud ... --data-file=-` のようにコマンドラインへ直接書くと、シェルの履歴（`~/.zsh_history`）に平文で残ります。渡し終えたファイルは削除してください。

登録できたかの確認は、値を表示せずに次で行えます。

```bash
gcloud secrets list --project=notebooklm-482403
gcloud secrets versions list JWT_SECRET_KEY --project=notebooklm-482403
```

---

### 2. 権限（IAM）の付与

Cloud Run のサービスアカウントに `roles/secretmanager.secretAccessor` を付与します。**プロジェクト全体ではなく、シークレット単位で付けること。**プロジェクト全体に付けると、そのサービスアカウントは将来追加される別のシークレットまで自動的に読めてしまいます。

```bash
gcloud secrets add-iam-policy-binding JWT_SECRET_KEY \
  --member="serviceAccount:サービスアカウント名@notebooklm-482403.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=notebooklm-482403
```

4つのシークレットそれぞれに対して同じコマンドを実行します。

> **この作業は Cloud Run のサービス作成後に行います。**付与先のサービスアカウントは Cloud Run のサービスを作らないと決まらないため、実際の付与は Issue #66（Cloud Run デプロイ）の中で実施します。

---

### 3. Cloud Run へのマウント

デプロイ時に「どのシークレットをどの環境変数に入れるか」を指定します。コンテナ起動時にGCP側が値を注入するので、コンテナに `.env` を含める必要はありません。

`--set-secrets` / `--update-secrets` の書式は `環境変数名=シークレット名:バージョン` です。左が環境変数名、右がSecret Manager側の名前で、今回は両者を同じ名前に揃えています。

**本番ではバージョンを固定します**（`:latest` は使いません）。理由は下記「バージョン指定の方針」を参照してください。

#### 新規デプロイ（サービスを初めて作るとき）

既存の設定が無いため `--set-*` で構いません。

`gcloud run deploy` は**デプロイする中身（コンテナイメージ）の指定が必須**です。`--image` か `--source` のどちらかを必ず付けてください。付けないとコマンドがエラーになります。

**A. ビルド済みのコンテナイメージを使う場合**

```bash
gcloud run deploy founder \
  --project=notebooklm-482403 \
  --image=REGION-docker.pkg.dev/notebooklm-482403/REPOSITORY/IMAGE:TAG \
  --set-secrets=JWT_SECRET_KEY=JWT_SECRET_KEY:1,GEMINI_API_KEY=GEMINI_API_KEY:1,QDRANT_API_KEY=QDRANT_API_KEY:1,QDRANT_URL=QDRANT_URL:1 \
  --set-env-vars=APP_ENV=production
```

`REGION` / `REPOSITORY` / `IMAGE` / `TAG` は Artifact Registry に push した実際の値に置き換えます（例: `asia-northeast1-docker.pkg.dev/notebooklm-482403/founder/founder:v1`）。

**B. ローカルのソースからビルドする場合**

```bash
gcloud run deploy founder \
  --project=notebooklm-482403 \
  --source=. \
  --set-secrets=JWT_SECRET_KEY=JWT_SECRET_KEY:1,GEMINI_API_KEY=GEMINI_API_KEY:1,QDRANT_API_KEY=QDRANT_API_KEY:1,QDRANT_URL=QDRANT_URL:1 \
  --set-env-vars=APP_ENV=production
```

`--source=.` はカレントディレクトリを Cloud Build へ送り、GCP側でイメージをビルドしてからデプロイします。リポジトリに `Dockerfile` があることが前提です。

> **イメージ／`Dockerfile` の作成そのものは Issue #66（Cloud Run デプロイ）の担当範囲です。**本ドキュメント（Issue #67）はシークレットの受け渡し方だけを扱います。上のコマンドは #66 でイメージが用意できた後に実行するものと考えてください。

#### 既存サービスの更新（2回目以降）

**既存サービスに `--set-*` を使ってはいけません。**

- `--set-env-vars` … 指定しなかった既存の環境変数を**すべて削除**する
- `--set-secrets` … 指定しなかった既存のシークレット設定を**すべて削除**する

たとえば `APP_ENV` だけ変えるつもりで `--set-env-vars=APP_ENV=production` を実行すると、他の環境変数（`GCS_BUCKET_NAME` など）が消えます。追加・変更したいものだけを渡す `--update-*` を使ってください。

```bash
# 環境変数を1つだけ変える（他の環境変数は維持される）
gcloud run services update founder \
  --project=notebooklm-482403 \
  --update-env-vars=APP_ENV=production

# シークレットのバージョンを1つだけ差し替える（他のシークレット設定は維持される）
# NEW_VERSION は下記コマンドで確認した番号を毎回入れる。固定の数字を書かない
gcloud secrets versions list JWT_SECRET_KEY --project=notebooklm-482403

gcloud run services update founder \
  --project=notebooklm-482403 \
  --update-secrets=JWT_SECRET_KEY=JWT_SECRET_KEY:$NEW_VERSION
```

バージョンを差し替えるときは、トラフィックの切り替えと旧バージョンの無効化までがセットです。手順は「4. キーの入れ替え（ローテーション）手順」を参照してください。

不要になった設定を消したいときは、削除専用の `--remove-env-vars` / `--remove-secrets` を使います。

`APP_ENV=production` を渡すのを忘れないでください。ただし忘れても危険な状態にはなりません。`app/config.py` は `APP_ENV` 未設定を本番相当として扱うため、`JWT_SECRET_KEY` が無ければ起動に失敗します。

#### バージョン指定の方針

| 指定 | 挙動 | 使う場面 |
|---|---|---|
| `JWT_SECRET_KEY:1` | 常にバージョン1を読む | **本番の既定。これを使う** |
| `JWT_SECRET_KEY:latest` | インスタンス起動時点の最新を読む | 本番では使わない |

**本番で `latest` を使わない理由**

Cloud Run は環境変数にマウントしたシークレットを**インスタンスの起動時に解決**します。値を読み直すタイミングはインスタンスごとにバラバラです。

`latest` のままローテーションすると、こうなります。

1. Secret Manager にバージョン2を追加する
2. すでに動いているインスタンスは、起動時に読んだバージョン1を持ったまま
3. その後スケールアウトで増えたインスタンスは、起動時にバージョン2を読む
4. **バージョン1とバージョン2が同時に稼働する**

`JWT_SECRET_KEY` でこれが起きると、バージョン1の鍵で署名されたトークンをバージョン2のインスタンスが検証して失敗し、利用者が不定期にログアウトされます。しかもどのインスタンスに当たるかは運次第なので、「たまにログインが切れる」という再現しにくい障害になります。

バージョンを固定しておけば、値が変わるのは明示的に再デプロイしたときだけです。**どのバージョンを読むかはリビジョン単位で決まる**ため、1つのリビジョンの中では鍵が混ざりません。

ただし「システム全体で常に1つの鍵しか動いていない」という意味ではありません。Cloud Run は複数のリビジョンにトラフィックを分割できるため、**新旧のリビジョンが併存している間は、旧バージョンの鍵と新バージョンの鍵が同時に稼働します**。`latest` のときと違うのは、それが運任せではなく、トラフィック配分として自分で制御できる点です。

この性質から、ローテーション時は次の順序を守る必要があります。

- 旧リビジョンが動いている間は、旧バージョンを無効化してはいけない（そのリビジョンのインスタンスが起動できなくなる）
- 旧リビジョンのトラフィックを 0% にし、切り戻しが不要と確認してから、旧バージョンを無効化・破棄する

---

### 4. キーの入れ替え（ローテーション）手順

漏れたときに差し替えられないと、シークレット管理をしている意味がありません。手順を決めておきます。

Cloud Run はバージョンを固定して参照しているため、**新しいバージョンを追加しただけでは反映されません。**新しいバージョン番号を明示して再デプロイし、**新しいリビジョンへトラフィックを寄せ切る**ところまでが1セットです。

流れは次の5段階です。

1. Secret Manager に新しいバージョンを追加する
2. Cloud Run を新しいバージョン番号で更新する（＝新しいリビジョンが作られる）
3. 新しいリビジョンへトラフィックを100%移し、旧リビジョンを0%にする
4. 動作確認したら、旧バージョンを**無効化**する
5. 切り戻しが不要と確認できたら、旧バージョンを**破棄**する

順序を入れ替えないでください。旧リビジョンにトラフィックが残ったまま旧バージョンを無効化すると、そのリビジョンのインスタンスが起動できなくなります。

#### コンソールでの操作

1. Secret Manager で対象のシークレットを開く
2. 「**バージョン**」タブを選ぶ
3. 「**+ 新しいバージョン**」をクリックし、新しい値を入力して追加する（古いバージョンはこの時点では残す）
4. 追加されたバージョン番号を控える（**毎回この画面で確認する。前回の番号を覚えて使わない**）
5. Cloud Run のマウント設定を**手順4で控えた番号に書き換えて**再デプロイする（`:latest` にはしない）
6. Cloud Run サービスの「**リビジョン**」タブを開き、新しいリビジョンのトラフィックが100%、旧リビジョンが0%になっていることを確認する
7. 動作確認する（EMP001 と ADMIN の両画面でログイン〜チャット）
8. 問題なければ古いバージョンを「**無効化**」する
9. しばらく様子を見て、戻す必要がないと確認できたら「**破棄**」する

いきなり破棄せず、無効化を挟むのが重要です。無効化は元に戻せますが、破棄は元に戻せません。無効化した状態で問題が起きたら、すぐ有効化して切り戻せます。

#### gcloud CLI での操作

**バージョン番号は毎回必ず確認して入れてください。**下のコマンドは番号を直接書かず、`OLD_VERSION` / `NEW_VERSION` という変数に入れる形にしています。数字を固定で書いた例をコピーして2回目以降のローテーションでそのまま流すと、**いま稼働中の新しいバージョンを無効化・破棄してしまい、Cloud Run が起動できなくなります**（`disable` は有効化で戻せますが、`destroy` は戻せません）。

```bash
# 0. 対象のシークレットと共通設定
SECRET=JWT_SECRET_KEY
PROJECT=notebooklm-482403
SERVICE=founder

# 1. 新しいバージョンを追加する
gcloud secrets versions add "$SECRET" \
  --data-file=/path/to/new-secret.txt \
  --project="$PROJECT"

# 2. 現在のバージョン一覧を確認する（ここで見た番号だけを使う）
gcloud secrets versions list "$SECRET" --project="$PROJECT"

# 3. 手順2の出力を見て、自分で番号を入れる
#    NEW_VERSION = 手順1で追加された番号
#    OLD_VERSION = いま Cloud Run が参照している番号（次のコマンドで確認できる）
gcloud run services describe "$SERVICE" --project="$PROJECT" \
  --format="value(spec.template.spec.containers[0].env)"

NEW_VERSION=  # 例: 2（必ず手順2の出力を見て入れる）
OLD_VERSION=  # 例: 1（必ず現在の参照先を確認して入れる）

# 4. 新しいバージョン番号を明示して更新する（他の設定を消さないため --update-secrets を使う）
#    このコマンドで新しいリビジョンが作られる
gcloud run services update "$SERVICE" \
  --project="$PROJECT" \
  --update-secrets="$SECRET=$SECRET:$NEW_VERSION"

# 5. トラフィックを最新リビジョンへ100%寄せる（旧リビジョンを0%にする）
gcloud run services update-traffic "$SERVICE" \
  --project="$PROJECT" \
  --to-latest

# 6. 旧リビジョンが0%になったことを確認する
gcloud run revisions list --service="$SERVICE" --project="$PROJECT"

# 7. 動作確認してから旧バージョンを無効化する
gcloud secrets versions disable "$OLD_VERSION" --secret="$SECRET" --project="$PROJECT"

# 8. 切り戻しが不要と確認できたら破棄する（取り消せない）
gcloud secrets versions destroy "$OLD_VERSION" --secret="$SECRET" --project="$PROJECT"
```

手順7・8で使う `OLD_VERSION` は、**手順6で0%になったリビジョンが参照していた番号**です。`NEW_VERSION` と取り違えると、稼働中のリビジョンが読んでいるバージョンを止めることになり、インスタンスの起動に失敗します（新規インスタンスが立ち上がらず、スケールアウトやリビジョン再起動のタイミングで障害になります）。

手順5でトラフィックを寄せ切らずに手順7へ進むのも同じ理由で危険です。旧リビジョンが少しでもトラフィックを持っている状態で旧バージョンを無効化すると、そのリビジョンのインスタンスが起動できなくなります。

`--to-latest` は「常に最新リビジョンへ100%」という設定です。段階的に移したい場合は `--to-revisions=新リビジョン名=100` のようにリビジョン名を指定します。

#### `JWT_SECRET_KEY` を入れ替えるときの注意

**全員のログインが切れます。**発行済みのトークンは古い鍵で署名されているため、新しい鍵では検証に失敗し、利用者は再ログインを求められます。

`JWT_EXPIRE_HOURS = 8`（`app/config.py`）なので、もともと8時間でログインし直す設計ではありますが、入れ替えた瞬間は全員が同時に切れます。業務時間外に実施するか、事前に周知してください。

他の3つ（`GEMINI_API_KEY` / `QDRANT_API_KEY` / `QDRANT_URL`）にはこの影響はありません。
