// Founder - 管理者画面

// ファイル種別ごとの表示タグ（CSSクラスと表示ラベル）
const SOURCE_TYPE_TAGS = {
  pdf: { cls: "tag-pdf", label: "PDF" },
  docx: { cls: "tag-docx", label: "DOCX" },
  pptx: { cls: "tag-pptx", label: "PPTX" },
  txt: { cls: "tag-txt", label: "TXT" },
  url: { cls: "tag-url", label: "URL" },
};

// owner_user_id から社員名を引くためのキャッシュ（loadSourceOwnersで構築）
let sourceOwnerNames = {};

// ログイン時に保存したトークンを認証ヘッダとして組み立てる
function authHeaders(extra) {
  const token = localStorage.getItem("token");
  return Object.assign({ Authorization: `Bearer ${token}` }, extra || {});
}

// HTMLエスケープ（ファイル名やURLをそのまま埋め込むと壊れるため）
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

// ISO日時を「2025.4.1」形式に整形する
function formatSourceDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()}`;
}

// 処理中・成功・失敗のメッセージを画面に表示する
function setSourceStatus(message, type) {
  const el = document.getElementById("source-status");
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

// 管理者ホームを開いたときの初期化（社員選択肢を読み込んでから一覧を描画）
async function initSourceManagement() {
  await loadSourceOwners();
  await loadSources();
}

// 対象社員のセレクトボックスを /api/admin/users から構築する
async function loadSourceOwners() {
  const select = document.getElementById("source-owner");

  try {
    const res = await fetch("/api/admin/users", { headers: authHeaders() });
    const users = await res.json();

    sourceOwnerNames = {};
    select.innerHTML = "";
    users.forEach((user) => {
      sourceOwnerNames[user.user_id] = user.name;
      select.innerHTML += `<option value="${user.user_id}">${escapeHtml(user.name)}（${escapeHtml(user.department)}）</option>`;
    });
  } catch (e) {
    // 選択肢が空でもアップロード時にエラー表示されるため、ここでは黙認する
  }
}

// 種別（全社共通／社員個別）の切り替えで対象社員セレクトの表示を制御する
function onScopeChange() {
  const scope = document.getElementById("source-scope").value;
  document.getElementById("source-owner-group").style.display =
    scope === "individual" ? "block" : "none";
}

// 現在選択中の種別・対象社員を取り出す（個別なのに未選択ならnull）
function getScopeSelection() {
  const scope = document.getElementById("source-scope").value;
  if (scope === "individual") {
    const ownerUserId = document.getElementById("source-owner").value;
    if (!ownerUserId) return null;
    return { scope: scope, ownerUserId: ownerUserId };
  }
  return { scope: scope, ownerUserId: null };
}

// ソース一覧をAPIから取得して表示する
async function loadSources() {
  const tbody = document.getElementById("source-tbody");
  tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-secondary);">読み込み中...</td></tr>`;

  try {
    const res = await fetch("/api/sources", { headers: authHeaders() });
    if (!res.ok) throw new Error("取得失敗");
    const sources = await res.json();
    renderSources(sources);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;">ソース一覧の取得に失敗しました。</td></tr>`;
  }
}

// 取得したソースデータでテーブルの中身を描画する
function renderSources(sources) {
  const tbody = document.getElementById("source-tbody");

  if (sources.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-secondary);">登録されたソースはありません。</td></tr>`;
    return;
  }

  tbody.innerHTML = "";
  sources.forEach((s) => {
    const tag = SOURCE_TYPE_TAGS[s.file_type] || { cls: "tag-common", label: s.file_type };
    const scopeTag =
      s.scope === "individual"
        ? `<span class="tag tag-individual">個別</span>`
        : `<span class="tag tag-common">共通</span>`;
    const owner = s.owner_user_id
      ? sourceOwnerNames[s.owner_user_id] || s.owner_user_id
      : "—";

    tbody.innerHTML += `
      <tr>
        <td>${escapeHtml(s.file_name)}</td>
        <td><span class="tag ${tag.cls}">${escapeHtml(tag.label)}</span></td>
        <td>${scopeTag}</td>
        <td>${escapeHtml(owner)}</td>
        <td>${formatSourceDate(s.uploaded_at)}</td>
        <td><button class="delete-btn" onclick="deleteSource(${s.source_id})">×</button></td>
      </tr>`;
  });
}

// アップロードゾーンにファイルを乗せたとき：既定動作を止めてハイライトする
function onSourceDragOver(event) {
  event.preventDefault();
  document.getElementById("source-drop-zone").classList.add("dragover");
}

// ゾーンから外れたとき：ハイライトを解除する
function onSourceDragLeave(event) {
  event.preventDefault();
  document.getElementById("source-drop-zone").classList.remove("dragover");
}

// ファイルをドロップしたとき：既定動作（ブラウザがファイルを開く）を止め、先頭ファイルをアップロードする
function onSourceDrop(event) {
  event.preventDefault();
  document.getElementById("source-drop-zone").classList.remove("dragover");
  const file = event.dataTransfer.files[0];
  if (file) uploadSource(file);
}

// ファイルをアップロードする（multipart/form-data）
async function uploadSource(file) {
  if (!file) return;

  const selection = getScopeSelection();
  if (!selection) {
    setSourceStatus("社員個別の場合は対象社員を選択してください", "error");
    document.getElementById("source-file-input").value = "";
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("scope", selection.scope);
  if (selection.ownerUserId) formData.append("owner_user_id", selection.ownerUserId);

  setSourceStatus(`「${file.name}」をアップロード中...`, "loading");
  try {
    const res = await fetch("/api/sources/upload", {
      method: "POST",
      headers: authHeaders(),
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "アップロードに失敗しました");

    setSourceStatus(`「${data.file_name}」を登録しました`, "success");
    await loadSources();
  } catch (e) {
    setSourceStatus(e.message, "error");
  } finally {
    // 同じファイルを連続で選べるようにinputをリセットする
    document.getElementById("source-file-input").value = "";
  }
}

// URLをソースとして登録する（JSON）
async function registerUrl() {
  const input = document.getElementById("source-url-input");
  const url = input.value.trim();

  if (!url) {
    setSourceStatus("URLを入力してください", "error");
    return;
  }

  const selection = getScopeSelection();
  if (!selection) {
    setSourceStatus("社員個別の場合は対象社員を選択してください", "error");
    return;
  }

  setSourceStatus("URLを登録中...", "loading");
  try {
    const res = await fetch("/api/sources/url", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        url: url,
        scope: selection.scope,
        owner_user_id: selection.ownerUserId,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "URL登録に失敗しました");

    setSourceStatus("URLを登録しました", "success");
    input.value = "";
    await loadSources();
  } catch (e) {
    setSourceStatus(e.message, "error");
  }
}

// ソースを削除して一覧を再描画する
async function deleteSource(sourceId) {
  if (!confirm("このソースを削除しますか？")) return;

  setSourceStatus("削除中...", "loading");
  try {
    const res = await fetch(`/api/sources/${sourceId}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "削除に失敗しました");

    setSourceStatus("ソースを削除しました", "success");
    await loadSources();
  } catch (e) {
    setSourceStatus(e.message, "error");
  }
}

// スタッフ一覧をAPIから取得して表示する
async function loadStaffList() {
  const grid = document.getElementById("staff-grid");

  try {
    // APIを呼び出す（管理者トークンを認証ヘッダに付ける）
    const res = await fetch("/api/admin/users", { headers: authHeaders() });
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
