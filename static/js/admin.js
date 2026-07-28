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
    if (!res.ok) throw new Error("取得失敗");
    const users = await res.json();

    // カードを生成して表示する
    // innerHTML の文字列にデータを埋め込まず、要素を組み立ててから textContent で入れる。
    // 名前に「'」や「<」が入っていても、onclick が壊れたりHTMLとして解釈されたりしない
    grid.textContent = "";
    users.forEach((user) => {
      const card = document.createElement("div");
      card.className = "staff-card";
      // クリック時に渡すのは user_id だけ。名前や部署は詳細APIから取り直す
      card.addEventListener("click", () => showStaffDetail(user.user_id));

      const avatar = document.createElement("div");
      avatar.className = "staff-avatar";
      avatar.textContent = firstChar(user.name);

      const name = document.createElement("div");
      name.className = "staff-card-name";
      name.textContent = user.name;

      const dept = document.createElement("div");
      dept.className = "staff-card-dept";
      dept.textContent = user.department;

      card.appendChild(avatar);
      card.appendChild(name);
      card.appendChild(dept);
      grid.appendChild(card);
    });
  } catch (e) {
    grid.textContent = "スタッフ情報の取得に失敗しました。";
  }
}

// ===== 社員データ画面（スタッフ詳細） =====

// いま開いている社員。トーク全文モーダルの見出しや、ソース追加の宛先に使う
let currentStaffUserId = null;
let currentStaffName = "";

// 値が空のときに画面へ出す代替文字
const EMPTY_VALUE = "—";

// アバターに出す先頭1文字を取り出す（名前が空でも落ちないようにする）
function firstChar(text) {
  return text ? String(text).charAt(0) : "";
}

// "1995-08-12" を "1995.8.12" に整形する
// Date に通さない理由: "YYYY-MM-DD" はUTCの0時として解釈されるため、
// 時差によっては前日の日付が表示されてしまう。文字列のまま組み替える
function formatDateOnly(value) {
  if (!value) return EMPTY_VALUE;
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return value;
  return `${m[1]}.${Number(m[2])}.${Number(m[3])}`;
}

// ISO日時を "2025.5.3 14:22" に整形する（最終ログイン用。空なら「未記録」）
function formatLastLogin(value) {
  if (!value) return "未記録";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()} ${time}`;
}

// ISO日時を "5/3 14:20" に整形する（トーク一覧の日時用）
function formatChatLogTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "";
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${time}`;
}

// 指定IDの要素にテキストを入れる（空なら代替文字）
function setDetailText(id, value) {
  document.getElementById(id).textContent = value || EMPTY_VALUE;
}

// 一覧欄に1行だけメッセージを表示する（読み込み中・0件・エラー用）
function showPlaceholder(container, message) {
  container.textContent = "";
  const p = document.createElement("div");
  p.style.fontSize = "13px";
  p.style.color = "var(--text-secondary)";
  p.textContent = message;
  container.appendChild(p);
}

/**
 * スタッフカードのクリックで、その社員の社員データ画面を開く。
 *
 * 入力: userId … 表示する社員の user_id
 * 出力: なし（画面を書き換える）
 *
 * 処理:
 *   1. 画面を詳細タブに切り替え、前の社員の表示内容を消す
 *   2. 基本情報・トーク一覧・ソース一覧の3つを同時に取りにいく
 *   3. 届いたものから順に描画する（1つ失敗しても他は表示する）
 *
 * 3つを Promise.all で並行に投げる理由:
 *   順番に await すると3回分の待ち時間が積み上がる。互いに依存しないので同時に投げる。
 */
async function showStaffDetail(userId) {
  currentStaffUserId = userId;
  currentStaffName = "";

  showAdminTab("detail");

  // 前に開いた社員の内容が残らないよう、いったん空にする
  clearStaffDetail();

  await Promise.all([
    loadStaffProfile(userId),
    loadStaffChatLogs(userId),
    loadStaffSources(userId),
  ]);
}

// 詳細画面の表示内容をすべて初期状態（読み込み中）に戻す
function clearStaffDetail() {
  document.getElementById("detail-avatar").textContent = "";
  document.getElementById("detail-name").textContent = "";
  document.getElementById("detail-dept").textContent = "";
  [
    "detail-employee-code",
    "detail-hire-date",
    "detail-employment-type",
    "detail-gender",
    "detail-birth-date",
    "detail-family",
    "detail-last-login",
  ].forEach((id) => {
    document.getElementById(id).textContent = "";
  });

  setStaffSourceStatus("", null);
  showPlaceholder(document.getElementById("detail-chat-logs"), "読み込み中...");
  showPlaceholder(document.getElementById("detail-sources"), "読み込み中...");
}

// 表示中の社員が切り替わっていないか確認する
// （社員Aを開いた直後に社員Bを開くと、遅れて届いたAの結果でBの画面が上書きされてしまうため）
function isStaffStillOpen(userId) {
  return currentStaffUserId === userId;
}

// 基本情報を取得して左側のプロフィール欄に反映する
async function loadStaffProfile(userId) {
  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error("取得失敗");
    const user = await res.json();

    if (!isStaffStillOpen(userId)) return;
    renderStaffProfile(user);
  } catch (e) {
    if (!isStaffStillOpen(userId)) return;
    document.getElementById("detail-name").textContent = "基本情報を取得できませんでした";
  }
}

// 基本情報を画面に流し込む（すべて textContent。家族構成などに記号が入っても安全）
function renderStaffProfile(user) {
  currentStaffName = user.name || "";

  document.getElementById("detail-avatar").textContent = firstChar(user.name);
  document.getElementById("detail-name").textContent = user.name || "";
  document.getElementById("detail-dept").textContent = user.department || "";

  setDetailText("detail-employee-code", user.employee_code);
  setDetailText("detail-hire-date", formatDateOnly(user.hire_date));
  setDetailText("detail-employment-type", user.employment_type);
  setDetailText("detail-gender", user.gender);
  setDetailText("detail-birth-date", formatDateOnly(user.birth_date));
  setDetailText("detail-family", user.family);
  // 最終ログインは未記録（空）でも「—」ではなく「未記録」と出す
  document.getElementById("detail-last-login").textContent = formatLastLogin(user.last_login_at);
}

// その社員のチャットセッション一覧を取得して「最近のトーク内容」に表示する
async function loadStaffChatLogs(userId) {
  const container = document.getElementById("detail-chat-logs");

  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}/chat-sessions`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error("取得失敗");
    const sessions = await res.json();

    if (!isStaffStillOpen(userId)) return;
    renderStaffChatLogs(sessions);
  } catch (e) {
    if (!isStaffStillOpen(userId)) return;
    showPlaceholder(container, "トーク履歴を取得できませんでした。");
  }
}

// セッション一覧を行として描画する。行のクリックでトーク全文モーダルを開く
function renderStaffChatLogs(sessions) {
  const container = document.getElementById("detail-chat-logs");

  if (sessions.length === 0) {
    showPlaceholder(container, "トーク履歴はありません。");
    return;
  }

  container.textContent = "";
  sessions.forEach((session) => {
    const item = document.createElement("div");
    item.className = "chat-log-item";
    item.addEventListener("click", () => openChatLog(session.session_id));

    const time = document.createElement("span");
    time.className = "chat-log-time";
    time.textContent = formatChatLogTime(session.started_at);

    const text = document.createElement("span");
    text.className = "chat-log-text";
    // preview は社員が打った質問文そのもの。textContent で入れることでHTMLとして解釈されない
    text.textContent = session.preview || "（メッセージなし）";

    item.appendChild(time);
    item.appendChild(text);
    container.appendChild(item);
  });
}

// その社員の個別ソース一覧を取得して「過去のソース」に表示する
async function loadStaffSources(userId) {
  const container = document.getElementById("detail-sources");

  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}/sources`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error("取得失敗");
    const sources = await res.json();

    if (!isStaffStillOpen(userId)) return;
    renderStaffSources(sources);
  } catch (e) {
    if (!isStaffStillOpen(userId)) return;
    showPlaceholder(container, "ソース一覧を取得できませんでした。");
  }
}

// 個別ソースを1件ずつ行として描画する（ダウンロードボタンは今回スコープ外のため出さない）
function renderStaffSources(sources) {
  const container = document.getElementById("detail-sources");

  if (sources.length === 0) {
    showPlaceholder(container, "登録された個別ソースはありません。");
    return;
  }

  container.textContent = "";
  sources.forEach((source) => {
    const tag = SOURCE_TYPE_TAGS[source.file_type] || {
      cls: "tag-common",
      label: source.file_type,
    };

    const item = document.createElement("div");
    item.className = "source-item";

    const info = document.createElement("div");
    info.className = "source-item-info";

    const icon = document.createElement("div");
    icon.className = `source-item-icon ${tag.cls}`;
    icon.textContent = tag.label;

    const texts = document.createElement("div");

    const name = document.createElement("div");
    name.className = "source-item-name";
    // ファイル名はアップロードした人が決めた文字列。textContent で入れる
    name.textContent = source.file_name;

    const date = document.createElement("div");
    date.className = "source-item-date";
    date.textContent = formatSourceDate(source.uploaded_at);

    texts.appendChild(name);
    texts.appendChild(date);
    info.appendChild(icon);
    info.appendChild(texts);
    item.appendChild(info);
    container.appendChild(item);
  });
}

/**
 * トーク全文モーダルを開き、そのセッションのメッセージを時系列で表示する。
 *
 * 入力: sessionId … 見たいセッションのID
 * 出力: なし（モーダルを開いて中身を描画する）
 *
 * 使うAPI:
 *   GET /api/chat/sessions/{session_id}/messages
 *   このAPIは「本人か社長」なら見られる作りなので、社長はそのまま流用できる
 */
async function openChatLog(sessionId) {
  const modal = document.getElementById("modal-chatlog");
  const body = document.getElementById("modal-chatlog-body");

  document.getElementById("modal-chatlog-title").textContent = currentStaffName
    ? `${currentStaffName} — トーク全文`
    : "トーク全文";

  showPlaceholder(body, "読み込み中...");
  modal.classList.add("active");

  try {
    const res = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error("取得失敗");
    const messages = await res.json();

    renderChatLogMessages(body, messages);
  } catch (e) {
    showPlaceholder(body, "トーク全文を取得できませんでした。");
  }
}

// メッセージ一覧を吹き出しとして描画する
function renderChatLogMessages(container, messages) {
  if (messages.length === 0) {
    showPlaceholder(container, "このトークにメッセージはありません。");
    return;
  }

  container.textContent = "";
  messages.forEach((message) => {
    // DBの role は 'user' / 'assistant' だが、CSSのクラスは 'user' / 'ai'。ここで変換する
    // （assistant のまま class に入れるとスタイルが当たらず、吹き出しが崩れる）
    const cssRole = message.role === "user" ? "user" : "ai";
    container.appendChild(buildChatLogMessage(cssRole, message.content));
  });
  container.scrollTop = 0;
}

// 吹き出し1件分の要素を組み立てる（社員＝名前の頭文字、AI＝F）
function buildChatLogMessage(cssRole, content) {
  const msg = document.createElement("div");
  msg.className = `msg ${cssRole}`;

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.style.width = "28px";
  avatar.style.height = "28px";
  avatar.style.fontSize = "10px";
  avatar.textContent = cssRole === "user" ? firstChar(currentStaffName) : "F";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.style.fontSize = "13px";
  // チャット本文は社員が打った文字列とAIの回答。textContent で入れてHTMLとして解釈させない
  bubble.textContent = content;

  msg.appendChild(avatar);
  msg.appendChild(bubble);
  return msg;
}

// 「+ 追加」ボタン：隠しファイル選択欄を開く
function openStaffSourcePicker() {
  document.getElementById("detail-source-file-input").click();
}

// 社員データ画面のアップロード状況を表示する
function setStaffSourceStatus(message, type) {
  const el = document.getElementById("detail-source-status");
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

/**
 * 選んだファイルを、その社員の「個別ソース」として登録する。
 *
 * 入力: file … ファイル選択欄で選ばれたファイル
 * 出力: なし（成功したらソース一覧を取り直す）
 *
 * ソース管理画面のアップロードとの違い:
 *   送り先のAPI（POST /api/sources/upload）は同じ。
 *   違うのは scope を必ず 'individual'、owner_user_id を「いま開いている社員」に固定する点。
 *   画面から種別を選ばせないので、他人のソースとして登録される余地がない。
 */
async function uploadStaffSource(file) {
  const input = document.getElementById("detail-source-file-input");
  if (!file) return;

  // どの社員の画面か分からない状態では送らない（宛先のない登録を防ぐ）
  const userId = currentStaffUserId;
  if (!userId) {
    setStaffSourceStatus("対象の社員が特定できません", "error");
    input.value = "";
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("scope", "individual");
  formData.append("owner_user_id", userId);

  setStaffSourceStatus(`「${file.name}」をアップロード中...`, "loading");
  try {
    const res = await fetch("/api/sources/upload", {
      method: "POST",
      headers: authHeaders(),
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "アップロードに失敗しました");

    setStaffSourceStatus(`「${data.file_name}」を登録しました`, "success");
    // 登録した1件が一覧に出るよう取り直す（アップロード中に別の社員を開いていたら何もしない）
    if (isStaffStillOpen(userId)) await loadStaffSources(userId);
  } catch (e) {
    setStaffSourceStatus(e.message, "error");
  } finally {
    // 同じファイルを続けて選べるようにinputをリセットする
    input.value = "";
  }
}
