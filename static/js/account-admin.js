// Founder - アカウント管理画面（#screen-account-admin）専用（Issue #123 段階4）
//
// 誰が使う画面か:
//     システム管理者（admin）だけ。ログイン時に login.js がこの画面へ送る。
//
// この画面が持つ3つの区画:
//     1. アカウント一覧          GET  /api/admin/accounts
//        （行ごとのロール変更     PUT  /api/admin/users/{user_id}/role）
//     2. アカウント追加          POST /api/admin/accounts
//     3. パスワードの強制上書き  PUT  /api/admin/accounts/{user_id}/password
//
// ロール変更のAPIだけ /api/admin/users/... なのはなぜか:
//     あれは社長が社員データ画面から使っている既存のAPI（Issue #91）で、
//     Issue #124 で admin も通るようになった（サーバー側の require_role_manager）。
//     同じ操作にAPIを2本用意すると、片方だけ直す事故が起きるため、
//     この画面も既存のAPIをそのまま使う。
//
// なぜ admin.js を流用しないか:
//     admin.js は管理者ホーム（#screen-admin）のDOM（#source-tbody / #staff-grid など）を
//     直接参照していて、1300行ある。共有するには引数化のリファクタが必要で、
//     社長の画面の回帰リスクを負うことになる。
//     source-manager.js と同じく、入口ごとにファイルを分け、
//     それぞれが1つの画面だけを見る形にしておく。
//
// なぜ一覧に GET /api/admin/users を使わないか（重要）:
//     あちらは社長のスタッフ一覧で、require_ceo のため admin では403になる。
//     さらに STAFF_LIST_EXCLUDED_ROLES で ceo / admin を除外し、role も返さない。
//     アカウント管理は社長やシステム管理者自身も対象にする必要があり、
//     ロールも表示するため、専用の GET /api/admin/accounts を使う。
//
// 権限について:
//     3つのAPIはいずれもサーバー側の require_account_manager が判定する。
//     画面を出し分けているのは利便性のためで、権限の砦はサーバー側。

// ロール名を画面に出すときの日本語ラベル。
//
// なぜ画面側で持つか:
//     APIが返すのは "employee" のような内部の値。そのまま並べると、
//     どれがどの権限なのか画面からは読み取れない。
//     判定には一切使わず、表示のためだけに使う。
//     知らないロールが返ってきた場合は、値をそのまま出す（隠さない）。
const ACCOUNT_ROLE_LABELS = {
  employee: "社員",
  source_manager: "共通ソース管理者",
  ceo: "社長",
  admin: "システム管理者",
};

// ロールのプルダウンに並べる順番。
//
// なぜラベルの辞書とは別に持つか:
//     ACCOUNT_ROLE_LABELS はオブジェクトなので、キーの並び順に頼ると
//     書き足した位置で画面の並びが変わる。並びは表示の一部なので明示する。
//     権限の広さ順（社員 → 共通ソース管理者 → 社長 → システム管理者）に並べており、
//     アカウント追加フォーム（index.html の #account-new-role）とも同じ順。
const ACCOUNT_ROLE_ORDER = ["employee", "source_manager", "ceo", "admin"];

// 一覧取得リクエストの通し番号。最新のリクエストだけがDOMを更新するために使う
//
// なぜ必要か:
//     loadAccounts() は画面初期化時と、アカウント追加の成功後の2箇所から呼ばれ、
//     これらは並行して走りうる。先に投げた古いリクエストが後から完了すると、
//     追加直後の一覧を古い結果で上書きし、追加したはずのアカウントが
//     一覧から一時的に消える。番号が最新でなければ描画しないことで防ぐ。
let accountListRequestId = 0;

// 追加リクエストが進行中のログイン世代。連打による二重登録を防ぐ（Issue #99）
//
// 真偽値ではなく世代番号を持つ理由:
//     真偽値だと、前の人の通信が終わるまで true のままになり、
//     ログアウトして次にログインした人が「連打」と判定されて弾かれる。
//     実行していないときは null。
let accountCreateGeneration = null;

// 上書きリクエストが進行中のログイン世代。追加とは別に持つ
//
// 分けている理由:
//     2つは別の区画にあり、別のメッセージ欄（#account-create-status /
//     #account-reset-status）に結果を出す。1つの変数を共有すると、
//     追加の最中に上書きが押せなくなるうえ、何も表示されないまま
//     無視されたように見える。
let accountResetGeneration = null;

// ロール変更が進行中のログイン世代。追加・上書きとは別に持つ（Issue #124）
//
// 別に持つ理由:
//     追加（#account-create-status）・上書き（#account-reset-status）と
//     メッセージの出し先が違い、区画も違う。1つの変数を共有すると、
//     ロールを変更している間はアカウントの追加が押せなくなる。
let accountRoleGeneration = null;

// パスワードを上書きする対象。一覧のボタンで選ぶまでは null
//
// user_id だけでなく氏名と社員コードも持つ理由:
//     「誰のパスワードを変えようとしているか」を画面に出し続けるため。
//     user_id だけだと、画面には数字しか出せない。
let accountResetTarget = null;

/**
 * ログイン時に保存したトークンを認証ヘッダとして組み立てる。
 *
 * 入力: extra … 追加したいヘッダ（省略可）
 * 出力: fetch の headers に渡すオブジェクト
 *
 * admin.js の authHeaders() と同じ処理だが、あちらは管理者ホーム用のファイルなので
 * この画面から呼ぶと依存関係が逆流する（source-manager.js と同じ理由）。
 */
function accountAuthHeaders(extra) {
  const token = localStorage.getItem("token");
  return Object.assign({ Authorization: `Bearer ${token}` }, extra || {});
}

/**
 * 3つの区画のどれかにメッセージを表示する。
 *
 * 入力:
 *     elementId … #account-list-status / #account-role-status /
 *                 #account-create-status / #account-reset-status
 *     message   … 表示する文字列（空なら消す）
 *     type      … "loading" | "success" | "error"
 * 出力: なし
 *
 * textContent を使う理由:
 *     ここに出すのはサーバーが返した detail の文言や社員コード。
 *     innerHTML だと、その中にタグが含まれていた場合にHTMLとして解釈されてしまう。
 */
function setAccountStatus(elementId, message, type) {
  const el = document.getElementById(elementId);
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

/**
 * アカウント管理画面を初期状態にして、一覧を読み込む。
 *
 * 入力: なし
 * 出力: なし
 *
 * ログイン直後に login.js から呼ぶ。
 *
 * 前の人の状態を消す理由:
 *     画面は作り直されず、表示を切り替えているだけ。
 *     消さないと、前にログインしたシステム管理者が打ちかけた社員コードや、
 *     「◯◯さんのパスワードを上書きします」という対象の表示が
 *     次の人の画面に残る。
 */
function initAccountAdminScreen() {
  document.getElementById("account-new-code").value = "";
  document.getElementById("account-new-name").value = "";
  document.getElementById("account-new-role").value = "employee";
  document.getElementById("account-new-password").value = "";
  setAccountStatus("account-create-status", "", null);
  // 前の人が変更したロールの結果が残らないようにする
  setAccountStatus("account-role-status", "", null);

  clearAccountResetTarget();
  loadAccounts();
}

/**
 * アカウント一覧を取得して表に並べる。
 *
 * 入力: なし
 * 処理: GET /api/admin/accounts を叩き、返ってきた配列を #account-tbody に並べる
 * 出力: なし（一覧の表示が変わる）
 *
 * エラーになる場面:
 *     403 … システム管理者以外で叩いた場合
 *     通信失敗 … オフラインやサーバー停止
 *
 * この一覧には全員が並ぶ:
 *     社長もシステム管理者も除外されないので、
 *     どのアカウントのパスワードもこの画面から上書きできる。
 */
async function loadAccounts() {
  // このリクエストの番号を採番する。以降、最新かどうかをこの番号で判定する
  const requestId = ++accountListRequestId;
  const tbody = document.getElementById("account-tbody");
  setAccountStatus("account-list-status", "読み込み中...", "loading");

  try {
    const res = await fetch("/api/admin/accounts", { headers: accountAuthHeaders() });
    const accounts = await parseJsonOrNull(res);
    if (!res.ok) {
      // サーバーが detail を返していればその文言を、
      // 返していない（HTMLが返ってきた等）なら定型文を出す
      throw new ApiError((accounts && accounts.detail) || "一覧の取得に失敗しました");
    }
    // 200 なのに本文がJSONでない場合（プロキシがHTMLを返した等）もここで止める
    if (!Array.isArray(accounts)) throw new ApiError("一覧の取得に失敗しました");

    // 待っている間に新しいリクエストが始まっていたら、この結果は捨てる
    if (requestId !== accountListRequestId) return;

    tbody.textContent = "";
    if (accounts.length === 0) {
      setAccountStatus("account-list-status", "表示できるアカウントがありません。", null);
      return;
    }

    accounts.forEach((account) => {
      tbody.appendChild(buildAccountRow(account));
    });
    setAccountStatus("account-list-status", "", null);
  } catch (e) {
    // 失敗の表示も同じ理由で、最新のリクエストのときだけ出す
    if (requestId !== accountListRequestId) return;
    tbody.textContent = "";
    setAccountStatus("account-list-status", toDisplayMessage(e), "error");
  }
}

/**
 * アカウント1件分の行（<tr>）を組み立てて返す。
 *
 * 入力: account … { user_id, employee_code, name, role, updated_at, updated_by }
 * 出力: 組み立てた <tr>（呼び出し側が表に追加する）
 *
 * 列の並び:
 *     社員コード / 氏名 / ロール（変更できる）/ 変更日時 / 変更した人 /
 *     パスワードを上書き
 *
 * innerHTML を使わない理由:
 *     氏名や社員コードは登録した人が入力した値で、タグを含むこともあり得る。
 *     textContent で入れれば文字として表示される（XSSにならない）。
 *
 * ロールについて:
 *     GET /api/admin/accounts が返すのは実効ロール（user_roles の上書きを反映した値）。
 *     画面ではこれをプルダウンの選択状態として出し、変更もここから行う。
 *
 * 変更日時・変更した人について:
 *     どちらも user_roles に記録されている値（誰がいつロールを変えたか）。
 *     一度もロールを変更されていない社員では、APIが両方とも空文字を返すので
 *     セルは空欄になる。
 */
function buildAccountRow(account) {
  const row = document.createElement("tr");

  row.appendChild(buildAccountCell(account.employee_code));
  row.appendChild(buildAccountCell(account.name));
  row.appendChild(buildAccountRoleCell(account));
  row.appendChild(buildAccountCell(formatAccountUpdatedAt(account.updated_at)));
  // updated_by はサーバー側で氏名に変換済み（app/main.py の get_admin_accounts）。
  // 画面側では変換せず、返ってきた文字列をそのまま出す
  row.appendChild(buildAccountCell(account.updated_by));

  const actionCell = document.createElement("td");
  // div + onclick にしない理由（Issue #114）:
  //     div は標準ではフォーカスを受け取れないため、Tabキーで到達できず、
  //     EnterやSpaceでも押せない。button ならフォーカス・キー操作・
  //     スクリーンリーダーへの「ボタンである」という伝達をブラウザが用意してくれる。
  // type="button" を付ける理由:
  //     button の type は省略すると submit になる。将来この画面に form を置いたとき、
  //     押すたびにページが送信・再読み込みされてしまう。
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-outline";
  button.style.padding = "6px 14px";
  button.style.fontSize = "12px";
  button.textContent = "パスワードを上書き";
  // onclick 属性の文字列ではなく関数を直接渡す。
  // 属性に埋めると、氏名に引用符が含まれていた場合にJSとして壊れる
  button.addEventListener("click", () => {
    selectAccountForPasswordReset(account.user_id, account.employee_code, account.name);
  });
  actionCell.appendChild(button);
  row.appendChild(actionCell);

  return row;
}

/**
 * 表のセル（<td>）を1つ作って返す。
 *
 * 入力: text … 表示する文字列
 * 出力: 組み立てた <td>
 */
function buildAccountCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text || "";
  return cell;
}

/**
 * ロール名を画面表示用の文言にする。
 *
 * 入力: role … APIが返したロール名（返らない場合は undefined）
 * 出力: 日本語のラベル。知らない値ならその値のまま。値が無ければ "-"
 *
 * 知らないロールを隠さない理由:
 *     ラベルの追加漏れを空欄で隠すと、画面上は正常に見えてしまう。
 *     内部の値がそのまま出れば、追加し忘れに気づける。
 *
 * どこで使うか:
 *     一覧のプルダウンの選択肢の文言（buildAccountRoleOption）と、
 *     変更に成功したときのメッセージ。
 */
function formatAccountRole(role) {
  if (!role) return "-";
  return ACCOUNT_ROLE_LABELS[role] || role;
}

/**
 * ロールの列（<td>）を組み立てて返す。プルダウンと「変更」ボタンが入る。
 *
 * 入力: account … 一覧の1件分（user_id / employee_code / name / role）
 * 出力: 組み立てた <td>
 *
 * 選んだ瞬間に送らず、ボタンを押させる理由（重要）:
 *     ロールの変更は「誰が何を見られるか」を決める操作で、取り消しても
 *     その間に見られた情報は戻らない。プルダウンを1つ隣に押し間違えただけで
 *     即座に権限が変わる形は、この操作には強すぎる。
 *     パスワードの強制上書きが「対象を選ぶまで入力欄を使えない」形になっているのと
 *     同じ考え方で、実行までに一手間置く。
 *
 * 変わっていないときにボタンを押せなくする理由:
 *     変わっていないのに送れると、押した側は何が起きたのか分からない。
 *     「選び直してから押す」という順番を、押せる・押せないで示す。
 *     社員データ画面の onRoleSelectChange() と同じ考え方。
 *
 * 自分自身の行について:
 *     サーバーは自分自身のロール変更を403で拒否する（app/main.py の
 *     update_admin_user_role）。403を見てからエラーを出すより、
 *     最初から操作できない方が「できない操作」だと分かる。
 *     判定に使うのはログイン時に控えた社員コード（static/js/session.js）。
 *     これは表示のための判定で、権限の砦はあくまでサーバー側。
 *     画面側の disabled を外されても、サーバーが403で拒否する構造は変わらない。
 *
 * 知らないロールが返ってきた場合:
 *     その値の選択肢を足してから選ぶ。足さないとブラウザは「何も選ばれていない」
 *     状態にするため、画面に出ている値と送られる値が食い違う
 *     （社員データ画面の renderStaffRole() が employee に倒しているのと同じ問題への対処）。
 */
function buildAccountRoleCell(account) {
  const cell = document.createElement("td");
  const control = document.createElement("span");
  control.className = "account-role-control";

  // APIが role を返さなかった場合も「未選択」にせず、その値のまま扱う
  const currentRole = account.role || "";

  const select = document.createElement("select");
  // <label> を置けない位置（表のセル）なので、読み上げ用の名前を属性で付ける
  select.setAttribute("aria-label", `${account.name} のロール`);
  ACCOUNT_ROLE_ORDER.forEach((role) => {
    select.appendChild(buildAccountRoleOption(role));
  });
  if (!ACCOUNT_ROLE_ORDER.includes(currentRole)) {
    select.appendChild(buildAccountRoleOption(currentRole));
  }
  select.value = currentRole;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-outline";
  button.textContent = "変更";
  // 選び直すまでは押せない
  button.disabled = true;

  control.appendChild(select);
  control.appendChild(button);

  // ログイン中の本人の行かどうか。employee_code は users テーブルで一意なので、
  // ログイン画面に入力された社員コードと突き合わせれば本人を特定できる
  if (account.employee_code === currentEmployeeCode()) {
    select.disabled = true;
    const note = document.createElement("span");
    // 既存の補足文言と同じ見た目（小さめ・淡い色）にする
    note.className = "upload-hint";
    note.textContent = "自分のロールは変更できません";
    control.appendChild(note);
  } else {
    select.addEventListener("change", () => {
      button.disabled = select.value === currentRole;
    });
    button.addEventListener("click", () => {
      saveAccountRole(account, select, button, currentRole);
    });
  }

  cell.appendChild(control);
  return cell;
}

/**
 * ロールのプルダウンの選択肢（<option>）を1つ作って返す。
 *
 * 入力: role … ロール名（APIが返す内部の値）
 * 出力: 組み立てた <option>（value は内部の値、表示は日本語のラベル）
 */
function buildAccountRoleOption(role) {
  const option = document.createElement("option");
  option.value = role;
  option.textContent = formatAccountRole(role);
  return option;
}

/**
 * 選んだロールを保存する（PUT /api/admin/users/{user_id}/role）。
 *
 * 入力:
 *     account     … 対象の1件（user_id / employee_code / name を使う）
 *     select      … その行のプルダウン
 *     button      … その行の「変更」ボタン
 *     currentRole … 変更前のロール（失敗したときに戻す先）
 * 処理: 選択値を送り、成功したら一覧を取り直す
 * 出力: なし（#account-role-status に結果を表示する）
 *
 * エラーになる場面:
 *     400 … 知らないロール名（画面のプルダウンからは起きない）
 *     403 … システム管理者・社長以外が叩いた場合／自分自身を対象にした場合
 *     404 … 対象の user_id が存在しない場合（一覧を開いたまま消えた場合など）
 *     いずれもAPIが detail に理由を入れて返すので、その文言をそのまま表示する。
 *
 * 送信中に行の操作を止める理由:
 *     応答が返る前にもう一度押せると、同じ社員に2回送ることになる。
 *     ボタンだけでなくプルダウンも止めるのは、送った値と画面の表示が
 *     途中でずれないようにするため。
 *
 * 成功したら一覧を取り直す理由:
 *     変更日時と変更した人（updated_at / updated_by）はサーバーが記録する値で、
 *     画面側では作れない。取り直せば、いま行った変更が記録として行に出る。
 *
 * メッセージを一覧の状態表示（#account-list-status）と分けている理由:
 *     成功後に呼ぶ loadAccounts() があの欄を「読み込み中...」で上書きするため、
 *     同じ欄に出すと結果が一瞬で消える。
 */
async function saveAccountRole(account, select, button, currentRole) {
  // 連打対策。処理中のボタンからの再実行は受け付けない
  if (button.disabled) return;

  const role = select.value;

  // 通信を始めた時点のログイン世代を控える（Issue #99）。
  // 応答が返るまでにログアウトして別の人がログインしていた場合、
  // その結果は前の人のものなので画面には出さない
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない
  if (accountRoleGeneration === generation) return;
  accountRoleGeneration = generation;

  select.disabled = true;
  button.disabled = true;
  setAccountStatus("account-role-status", "ロールを変更中...", "loading");

  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(account.user_id)}/role`, {
      method: "PUT",
      headers: accountAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ role: role }),
    });
    const data = await parseJsonOrNull(res);
    if (!res.ok || !data) {
      throw new ApiError((data && data.detail) || "ロールの変更に失敗しました");
    }

    if (!isSameLoginGeneration(generation)) return;

    // 「いつから有効か」を必ず添える。
    // ロールはログイン時に発行するJWTへ焼き付けられ、発行後は書き換えられないため、
    // すでにログイン中の相手には反映されない。
    // 黙っていると「変更したのに権限が変わらない」と受け取られる
    setAccountStatus(
      "account-role-status",
      `${account.name}（${account.employee_code}）のロールを` +
        `${formatAccountRole(data.role || role)}に変更しました。` +
        "対象の社員が次にログインしたときから有効になります。",
      "success"
    );
    // 一覧を取り直して、変更日時と変更した人を反映する。
    // 行は作り直されるので、この行の select / button の状態は戻さなくてよい
    loadAccounts();
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setAccountStatus("account-role-status", toDisplayMessage(e), "error");
    // 送る前の状態に戻す。
    // 選択を変更後の値のままにすると、画面には変わったように見えるのに
    // サーバーには保存されていない、という食い違いが残る
    select.value = currentRole;
    select.disabled = false;
    button.disabled = true;
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     変更まで「実行していない」ことにしてしまう。
    if (accountRoleGeneration === generation) {
      accountRoleGeneration = null;
    }
  }
}

/**
 * ロールを変更した日時を画面表示用の文字列にする。
 *
 * 入力: value … APIが返す updated_at（UTC・ISO形式の文字列。変更が無ければ空文字）
 * 出力: "2026.8.30 14:22" の形の文字列。空文字なら空文字、日時として読めなければ元の値
 *
 * ブラウザのローカル時刻に直す理由:
 *     保存されているのはUTC（例: 2026-08-30T05:22:10.123456+00:00）。
 *     そのまま出すと日本時間から9時間ずれた時刻を読ませることになり、
 *     文字列も長くて表の幅を取る。
 *
 * 空文字を空文字のまま返す理由:
 *     一度もロールを変更されていない社員は「記録が無い」だけで、
 *     日時が壊れているわけではない。空欄にしておけば、
 *     変更されたことがある社員の行だけが目に留まる。
 *
 * 読めない値をそのまま返す理由:
 *     整形できないことを空欄で隠すと、画面上は正常に見えてしまう。
 *     値がそのまま出れば、おかしな記録が入っていることに気づける
 *     （formatAccountRole が知らないロールを隠さないのと同じ）。
 *
 * admin.js の formatLastLogin() を呼ばない理由:
 *     出力の形式は同じだが、あちらは管理者ホーム用のファイルで、
 *     この画面から呼ぶと依存関係が逆流する
 *     （accountAuthHeaders() が authHeaders() を複製しているのと同じ判断）。
 */
function formatAccountUpdatedAt(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()} ${time}`;
}

/**
 * 新しいアカウントを追加する。
 *
 * 入力: なし（追加フォームの4つの入力欄を読む）
 * 処理: POST /api/admin/accounts に送る
 * 出力: なし（#account-create-status に結果を表示し、成功時は一覧を取り直す）
 *
 * エラーになる場面:
 *     400 … 社員コード・氏名が空／知らないロール名／パスワードの長さが規定外
 *     403 … システム管理者以外が叩いた場合
 *     409 … 社員コードが既に使われている場合
 *     いずれもAPIが detail に理由を入れて返すので、その文言をそのまま表示する。
 *
 * 画面側で長さや形式を検査しない理由:
 *     規則はサーバー側が持つ（app/user_passwords.py の check_password_length など）。
 *     画面にも同じ規則を書くと、変えたときに片方だけ直す事故が起きる。
 *     APIの detail をそのまま出せば、必ず同じ文言になる。
 */
async function createAccount() {
  const codeInput = document.getElementById("account-new-code");
  const nameInput = document.getElementById("account-new-name");
  const roleInput = document.getElementById("account-new-role");
  const passwordInput = document.getElementById("account-new-password");

  // 通信を始めた時点のログイン世代を控える（Issue #99）。
  // 応答が返るまでにログアウトして別の人がログインしていた場合、
  // その結果は前の人のものなので画面には出さない。
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない（連打での二重登録を防ぐ）。
  // 別のログインに変わっていれば、前の人の通信が続いていても受け付ける。
  if (accountCreateGeneration === generation) return;
  accountCreateGeneration = generation;

  setAccountStatus("account-create-status", "アカウントを追加中...", "loading");
  try {
    const res = await fetch("/api/admin/accounts", {
      method: "POST",
      headers: accountAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        employee_code: codeInput.value,
        name: nameInput.value,
        role: roleInput.value,
        password: passwordInput.value,
      }),
    });
    const data = await parseJsonOrNull(res);
    if (!res.ok || !data) {
      throw new ApiError((data && data.detail) || "アカウントの追加に失敗しました");
    }

    if (!isSameLoginGeneration(generation)) return;

    setAccountStatus(
      "account-create-status",
      `${data.employee_code} のアカウントを追加しました`,
      "success"
    );
    // 入力欄のクリアも照合の内側に置く。
    // 外に出すと、次にログインした人が入力中の値を消してしまう
    codeInput.value = "";
    nameInput.value = "";
    roleInput.value = "employee";
    passwordInput.value = "";
    // 追加した1件を一覧に反映する。
    // 世代が変わっている場合はここまで来ないので、
    // 別の人の画面で一覧を取り直すこともない
    loadAccounts();
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setAccountStatus("account-create-status", toDisplayMessage(e), "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     追加まで「実行していない」ことにしてしまう。
    if (accountCreateGeneration === generation) {
      accountCreateGeneration = null;
    }
  }
}

/**
 * パスワードを上書きする対象を選ぶ（一覧のボタンから呼ばれる）。
 *
 * 入力:
 *     userId       … 対象の user_id
 *     employeeCode … 対象の社員コード（画面表示用）
 *     name         … 対象の氏名（画面表示用）
 * 出力: なし（3つ目の区画が入力できる状態になる）
 *
 * 対象を選ぶまで入力欄を使えなくしている理由:
 *     パスワードの上書きは、誰に対する操作かを間違えると
 *     関係のない社員がログインできなくなる。
 *     「誰のものか分からないまま実行できる」状態を作らない。
 */
function selectAccountForPasswordReset(userId, employeeCode, name) {
  accountResetTarget = { userId: userId, employeeCode: employeeCode, name: name };

  const target = document.getElementById("account-reset-target");
  target.className = "account-reset-target selected";
  // textContent で入れる（氏名にタグが含まれていても文字として表示される）
  target.textContent = `${name}（${employeeCode}）のパスワードを上書きします`;

  document.getElementById("account-reset-password").disabled = false;
  document.getElementById("account-reset-button").disabled = false;
  document.getElementById("account-reset-password").value = "";
  setAccountStatus("account-reset-status", "", null);
  document.getElementById("account-reset-password").focus();
}

/**
 * パスワード上書きの対象の選択を解除し、区画を初期状態に戻す。
 *
 * 入力: なし
 * 出力: なし
 *
 * 上書きの成功後にも呼ぶ理由:
 *     対象と入力値が残っていると、続けてボタンを押したときに
 *     同じ人のパスワードをもう一度変えてしまう。
 *     1回ごとに選び直させる。
 */
function clearAccountResetTarget() {
  accountResetTarget = null;

  const target = document.getElementById("account-reset-target");
  target.className = "account-reset-target";
  target.textContent = "一覧の「パスワードを上書き」から対象を選んでください";

  document.getElementById("account-reset-password").value = "";
  document.getElementById("account-reset-password").disabled = true;
  document.getElementById("account-reset-button").disabled = true;
}

/**
 * 選んだ社員のパスワードを強制的に上書きする。
 *
 * 入力: なし（accountResetTarget と #account-reset-password の値を読む）
 * 処理: PUT /api/admin/accounts/{user_id}/password に送る
 * 出力: なし（#account-reset-status に結果を表示する）
 *
 * エラーになる場面:
 *     400 … 新しいパスワードの長さが規定外
 *     403 … システム管理者以外が叩いた場合
 *     404 … 対象の user_id が存在しない場合
 *
 * 成功メッセージに氏名を入れる理由:
 *     誰のパスワードを変えたのかが記録として画面に残る。
 *     「押したつもりの相手と違った」に、その場で気づけるようにする。
 */
async function resetAccountPassword() {
  // ボタンは対象が選ばれるまで disabled だが、
  // 選択が解除された直後などに備えて念のため確かめる
  if (!accountResetTarget) {
    setAccountStatus("account-reset-status", "対象を選んでください", "error");
    return;
  }

  const passwordInput = document.getElementById("account-reset-password");

  // 空欄チェックは世代を取る前に行う。
  // 通信しないので、ここで取ると解放する場所が増えるだけになる
  if (!passwordInput.value) {
    setAccountStatus("account-reset-status", "新しいパスワードを入力してください", "error");
    return;
  }

  // 対象を控えておく。応答を待つ間に別の行が選ばれても、
  // メッセージに出すのは「いま送った相手」でなければならない
  const target = accountResetTarget;

  // 通信を始めた時点のログイン世代を控える（Issue #99）
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない（連打を防ぐ）
  if (accountResetGeneration === generation) return;
  accountResetGeneration = generation;

  setAccountStatus("account-reset-status", "上書き中...", "loading");
  try {
    const res = await fetch(
      `/api/admin/accounts/${encodeURIComponent(target.userId)}/password`,
      {
        method: "PUT",
        headers: accountAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ new_password: passwordInput.value }),
      }
    );
    const data = await parseJsonOrNull(res);
    if (!res.ok) {
      throw new ApiError((data && data.detail) || "パスワードの上書きに失敗しました");
    }

    if (!isSameLoginGeneration(generation)) return;

    setAccountStatus(
      "account-reset-status",
      `${target.name}（${target.employeeCode}）のパスワードを上書きしました`,
      "success"
    );
    // 対象と入力値を消して、続けて同じ人を書き換えてしまうのを防ぐ。
    // 照合の内側に置くのは、次にログインした人の画面を触らないため
    const message = document.getElementById("account-reset-status").textContent;
    clearAccountResetTarget();
    // clearAccountResetTarget() は入力欄だけを初期化する。
    // 結果のメッセージは残したいので、消えた分を戻す
    setAccountStatus("account-reset-status", message, "success");
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setAccountStatus("account-reset-status", toDisplayMessage(e), "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     上書きまで「実行していない」ことにしてしまう。
    if (accountResetGeneration === generation) {
      accountResetGeneration = null;
    }
  }
}
