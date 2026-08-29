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

  // 世代番号を1つ進める（Issue #99）。
  //
  // ここで進める理由:
  //     前のログインで始まったアップロードがまだ終わっていない場合、その応答は
  //     このあと返ってくる。世代番号を進めておくと、応答を受け取った側が
  //     「別のログインに変わった」と判定し、前の人のファイル名を表示しない。
  //
  //     ログアウト（index.html の logout()）でも進めているが、ログアウトを
  //     経由せずに画面が切り替わる経路が将来増えても取りこぼさないよう、
  //     ログイン側でも必ず進める。
  //
  // 画面を切り替える前に呼ぶ理由:
  //     このあとの initCommonSourceScreen() や restoreChatHistory() は
  //     通信を始める。それらが控える世代番号は、新しい世代である必要がある。
  beginLoginGeneration();

  // チャット画面にある「＋ 全社共通の資料を追加」ボタンの表示を、ロールで決め直す（Issue #116）。
  //
  // なぜログインのたびに決め直すか:
  //     この表示状態は要素に残り続ける。決め直さないと、source_manager が
  //     チャット画面を開いたあとにログアウトし、次に社員がログインしたとき、
  //     前の人向けのボタンが残って見えてしまう。
  //
  // 押されても権限の穴にはならない:
  //     このボタンが行うのは #screen-source-manager へ移動することだけで、
  //     その画面の操作はすべてサーバー側の require_source_uploader が判定する。
  //     それでも、そのロールに無関係な導線が見えるのは避ける。
  document.getElementById("chat-add-common-source").style.display =
    data.role === "source_manager" ? "" : "none";

  // ロールごとに遷移先の画面を分ける。
  //
  // なぜ source_manager を管理者ホームへ通さないか（重要）:
  //     #screen-admin にはスタッフ一覧・社員データ・AIチャットモーダルが同居しており、
  //     さらに初期化の initSourceManagement() が GET /api/admin/users と
  //     GET /api/sources を呼ぶ。どちらも require_ceo なので source_manager では
  //     403 になり、そのまま流用できない。
  //     「不要な部分を隠して見せる」形にすると、隠し忘れがそのまま権限の穴になるため、
  //     共通ソースの登録だけができる専用画面（#screen-source-manager）へ分けている。
  //
  // なぜ admin を業務画面へ通さないか（Issue #122）:
  //     admin はアカウントの管理だけを担当し、チャットやソースといった
  //     業務データには触れない役割。#screen-chat や #screen-source-manager へ通すと、
  //     押しても403になるだけのボタンが並ぶ画面を見せることになる。
  //     サーバー側も require_business_user が業務APIで admin を403にするため、
  //     画面と権限の線引きを揃えて、専用の #screen-account-admin へ送る。
  //
  // 未知のロールが返った場合:
  //     いちばん権限の狭いチャット画面へ倒す（else 側）。
  //     ロールが増えたときに、知らないロールが管理者画面や
  //     ソース登録画面へ流れ込まないようにするため、危険側に倒さない。
  if (data.role === "ceo") {
    showScreen("screen-admin");
    // 管理者ホームの初期タブ（ソース管理）を描画する
    initSourceManagement();
  } else if (data.role === "source_manager") {
    showScreen("screen-source-manager");
    // 前の人の登録結果が残らないよう、ステータス表示をクリアする
    initCommonSourceScreen();
  } else if (data.role === "admin") {
    showScreen("screen-account-admin");
    // 前の人が打ちかけた社員コードや、選んだ上書き対象の表示が残らないよう
    // 初期状態に戻してから、アカウント一覧を読み込む
    initAccountAdminScreen();
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