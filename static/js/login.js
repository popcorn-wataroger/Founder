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

  // ログイン成功 → roleに応じて画面を切り替える
  if (data.role === "admin") {
    showScreen("screen-admin");
  } else {
    showScreen("screen-chat");
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