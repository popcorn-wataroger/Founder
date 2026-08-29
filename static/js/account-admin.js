// Founder - アカウント管理画面（#screen-account-admin）専用（Issue #123 段階4）
//
// 誰が使う画面か:
//     システム管理者（admin）だけ。ログイン時に login.js がこの画面へ送る。
//
// この画面が持つ3つの区画:
//     1. アカウント一覧          GET  /api/admin/accounts
//     2. アカウント追加          POST /api/admin/accounts
//     3. パスワードの強制上書き  PUT  /api/admin/accounts/{user_id}/password
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
 *     elementId … #account-list-status / #account-create-status / #account-reset-status
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
    if (!res.ok) throw new Error("一覧の取得に失敗しました");
    const accounts = await res.json();

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
    setAccountStatus("account-list-status", e.message, "error");
  }
}

/**
 * アカウント1件分の行（<tr>）を組み立てて返す。
 *
 * 入力: account … { user_id, employee_code, name, role }
 * 出力: 組み立てた <tr>（呼び出し側が表に追加する）
 *
 * innerHTML を使わない理由:
 *     氏名や社員コードは登録した人が入力した値で、タグを含むこともあり得る。
 *     textContent で入れれば文字として表示される（XSSにならない）。
 *
 * ロールについて:
 *     GET /api/admin/accounts が返すのは実効ロール（user_roles の上書きを反映した値）。
 *     画面ではこれを日本語のラベルに直して出す。
 */
function buildAccountRow(account) {
  const row = document.createElement("tr");

  row.appendChild(buildAccountCell(account.employee_code));
  row.appendChild(buildAccountCell(account.name));
  row.appendChild(buildAccountCell(formatAccountRole(account.role)));

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
 */
function formatAccountRole(role) {
  if (!role) return "-";
  return ACCOUNT_ROLE_LABELS[role] || role;
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
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "アカウントの追加に失敗しました");

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
    setAccountStatus("account-create-status", e.message, "error");
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
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "パスワードの上書きに失敗しました");

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
    setAccountStatus("account-reset-status", e.message, "error");
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
