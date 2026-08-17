// Founder - ログイン処理

async function handleLogin() {
  const employeeCode = document.getElementById("login-code").value.trim();
  const password = document.getElementById("login-pass").value.trim();

  // 空欄チェック
  if (!employeeCode || !password) {
    showLoginError("社員コードとパスワードを入力してください");
    return;
  }

  // ログインAPIを呼び出す
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ employee_code: employeeCode, password: password }),
  });
  const data = await res.json();

  // ログイン失敗
  if (!data.success) {
    showLoginError(data.message);
    return;
  }

  // ログイン成功 → トークンを保存し、roleに応じて画面を切り替える
  // 保存したトークンは、以降のadmin API呼び出しで認証ヘッダに付ける
  localStorage.setItem("token", data.token);

  // ロールごとに遷移先の画面を分ける。
  //
  // なぜ source_manager を管理者ホームへ通さないか（重要）:
  //     #screen-admin にはスタッフ一覧・社員データ・AIチャットモーダルが同居しており、
  //     さらに初期化の initSourceManagement() が GET /api/admin/users と
  //     GET /api/sources を呼ぶ。どちらも require_admin なので source_manager では
  //     403 になり、そのまま流用できない。
  //     「不要な部分を隠して見せる」形にすると、隠し忘れがそのまま権限の穴になるため、
  //     共通ソースの登録だけができる専用画面（#screen-source-manager）へ分けている。
  //
  // 未知のロールが返った場合:
  //     いちばん権限の狭いチャット画面へ倒す（else 側）。
  //     ロールが増えたときに、知らないロールが管理者画面や
  //     ソース登録画面へ流れ込まないようにするため、危険側に倒さない。
  if (data.role === "admin") {
    showScreen("screen-admin");
    // 管理者ホームの初期タブ（ソース管理）を描画する
    initSourceManagement();
  } else if (data.role === "source_manager") {
    showScreen("screen-source-manager");
    // 前の人の登録結果が残らないよう、ステータス表示をクリアする
    initCommonSourceScreen();
  } else {
    showScreen("screen-chat");
    // 前回までの会話をサーバーから取り直して吹き出しを並べ直す。
    // session_id をブラウザに保存していないため、ここで毎回取得する
    restoreChatHistory();
  }
}

function showLoginError(message) {
  let errorEl = document.getElementById("login-error");
  if (!errorEl) {
    errorEl = document.createElement("p");
    errorEl.id = "login-error";
    errorEl.style.cssText = "color: #E8593C; font-size: 13px; margin-top: 12px; text-align: center;";
    document.querySelector(".login-btn").after(errorEl);
  }
  errorEl.textContent = message;
}