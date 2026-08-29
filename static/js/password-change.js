// Founder - 自分のパスワードを変更するモーダル（Issue #123 段階4）
//
// どの画面から使うか:
//     チャット（employee）／管理者ホーム（ceo）／共通ソース管理者（source_manager）／
//     アカウント管理（admin）の4画面。全ロールが自分のパスワードを変えられる。
//
// なぜ画面ごとにファイルを分けず、これ1つで4画面をまかなうか:
//     扱うDOMが #modal-password-change の1つしかないため。
//     モーダルは画面の外（index.html の末尾）に1つだけ置いてあり、
//     どの画面から開いても同じ要素を使う。
//     入力欄を画面ごとに複製すると同じIDの要素が複数でき、
//     document.getElementById がどれを返すか不定になる
//     （static/js/source-manager.js 冒頭の判断と同じ）。
//
// なぜ account-admin.js に混ぜないか:
//     あちらは #screen-account-admin だけを見るファイルで、admin しか開かない。
//     こちらは4画面から呼ばれる。既存が chat.js / admin.js / source-manager.js と
//     画面・用途ごとに分かれている流儀に合わせ、役割の違うものを同居させない。
//
// パスワードの扱い:
//     console.log には一切出さない。
//     モーダルを閉じるときに入力欄を必ず空にする（DOMに残さない）。

// 変更リクエストが進行中のログイン世代。連打による二重送信を防ぐ（Issue #99）
//
// 真偽値ではなく世代番号を持つ理由:
//     真偽値だと、前の人の通信が終わるまで true のままになり、
//     ログアウトして次にログインした人が「連打」と判定されて弾かれる。
//     どの世代が使用中かを持てば、同じ世代の連打だけを弾ける。
//     実行していないときは null。
let passwordChangeGeneration = null;

/**
 * 処理中・成功・失敗のメッセージをモーダル内に表示する。
 *
 * 入力: message … 表示する文字列（空なら消す） / type … "loading" | "success" | "error"
 * 出力: なし（#password-change-status の表示が変わる）
 *
 * textContent を使う理由:
 *     ここに出すのはサーバーが返した detail の文言。
 *     innerHTML だと、その中にタグが含まれていた場合にHTMLとして解釈されてしまう。
 */
function setPasswordChangeStatus(message, type) {
  const el = document.getElementById("password-change-status");
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

/**
 * パスワード変更モーダルを開く。
 *
 * 入力: なし
 * 出力: なし（モーダルが表示される）
 *
 * 開くたびに入力欄とメッセージを空にする理由:
 *     前回の操作の結果（「変更しました」など）や打ちかけの値が残っていると、
 *     いま何をしているのか分からなくなる。
 *     とくに共用端末では、前の人が閉じずにログアウトした場合に
 *     打ちかけのパスワードが次の人に見えることになる。
 */
function openPasswordChange() {
  document.getElementById("password-current").value = "";
  document.getElementById("password-new").value = "";
  setPasswordChangeStatus("", null);
  document.getElementById("modal-password-change").classList.add("active");
  // 開いた直後に入力を始められるようにする
  document.getElementById("password-current").focus();
}

/**
 * パスワード変更モーダルを閉じる。
 *
 * 入力: なし
 * 出力: なし（モーダルが隠れる）
 *
 * closeModal() を使わず専用の関数にしている理由:
 *     閉じるときに入力欄を空にする必要があるため。
 *     モーダルは画面の外に1つだけ置かれていて作り直されないので、
 *     消さないとログアウトしてもDOMにパスワードが残る。
 */
function closePasswordChange() {
  document.getElementById("password-current").value = "";
  document.getElementById("password-new").value = "";
  setPasswordChangeStatus("", null);
  document.getElementById("modal-password-change").classList.remove("active");
}

/**
 * 自分のパスワードを変更する。
 *
 * 入力: なし（#password-current と #password-new の値を読む）
 * 処理: PUT /api/me/password に送る
 * 出力: なし（#password-change-status に結果を表示する）
 *
 * エラーになる場面:
 *     400 … 新しいパスワードの長さが規定外（8文字未満、または72バイト超）
 *     401 … 現在のパスワードが違う、またはログインしていない
 *
 * 長さの検査を画面側でしない理由:
 *     規則はサーバー側（app/user_passwords.py の check_password_length）が持つ。
 *     画面にも同じ数字を書くと、規則を変えたときに片方だけ直す事故が起きる。
 *     APIが返す detail をそのまま出せば、必ず同じ文言になる。
 *     空欄チェックだけは、通信せずに済むのでここで行う。
 */
async function changeMyPassword() {
  const currentInput = document.getElementById("password-current");
  const newInput = document.getElementById("password-new");
  const currentPassword = currentInput.value;
  const newPassword = newInput.value;

  // 空欄チェックは世代を取る前に行う。
  // 通信しないので、ここで取ると解放する場所が増えるだけになる
  if (!currentPassword || !newPassword) {
    setPasswordChangeStatus("現在のパスワードと新しいパスワードを入力してください", "error");
    return;
  }

  // 通信を始めた時点のログイン世代を控える（Issue #99）。
  // 応答が返るまでにログアウトして別の人がログインしていた場合、
  // その結果は前の人のものなので画面には出さない。
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない（連打での二重送信を防ぐ）。
  // 別のログインに変わっていれば、前の人の通信が続いていても受け付ける。
  if (passwordChangeGeneration === generation) return;
  passwordChangeGeneration = generation;

  setPasswordChangeStatus("変更中...", "loading");
  try {
    const token = localStorage.getItem("token");
    const res = await fetch("/api/me/password", {
      method: "PUT",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    const data = await parseJsonOrNull(res);
    if (!res.ok) {
      // サーバーが detail を返していればその文言を、
      // 返していない（HTMLが返ってきた等）なら定型文を出す
      throw new ApiError((data && data.detail) || "パスワードの変更に失敗しました");
    }

    if (!isSameLoginGeneration(generation)) return;

    setPasswordChangeStatus("パスワードを変更しました", "success");
    // 入力欄のクリアも照合の内側に置く。
    // 外に出すと、次にログインした人が入力中の値を消してしまう
    currentInput.value = "";
    newInput.value = "";
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setPasswordChangeStatus(toDisplayMessage(e), "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     変更まで「実行していない」ことにしてしまう。
    if (passwordChangeGeneration === generation) {
      passwordChangeGeneration = null;
    }
  }
}
