// Founder - 管理者画面

// スタッフ一覧をAPIから取得して表示する
async function loadStaffList() {
  const grid = document.getElementById("staff-grid");

  try {
    // APIを呼び出す
    const res = await fetch("/api/admin/users");
    const users = await res.json();

    // カードを生成して表示する
    grid.innerHTML = "";
    users.forEach((user, index) => {
      grid.innerHTML += `
        <div class="staff-card" onclick="showStaffDetail('${user.name}', '${user.department}')">
          <div class="staff-avatar">${user.name.charAt(0)}</div>
          <div class="staff-card-name">${user.name}</div>
          <div class="staff-card-dept">${user.department}</div>
        </div>`;
    });

  } catch (e) {
    grid.innerHTML = "<p>スタッフ情報の取得に失敗しました。</p>";
  }
}