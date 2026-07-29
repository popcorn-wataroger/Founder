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

/**
 * 吹き出し1件分の要素を組み立てる（社員＝名前の頭文字、AI＝F）。
 *
 * AIの回答だけ renderMarkdownInto を通す理由:
 *   AIの回答には "###" や "- " などのMarkdown記法が含まれる。
 *   textContent で入れると記号がそのまま画面に出てしまうため、
 *   社員のチャット画面（chat.js の appendMessage）と同じ整形を通す。
 *   社員の発言を整形しないのも同じ理由で、打ち込んだ "**" は記法ではなく
 *   打った通りに表示されるべき文字だから（PR #52 と同じ方針）。
 *
 * renderMarkdownInto は chat.js のグローバル関数:
 *   index.html が chat.js → admin.js の順で読み込んでいるので、そのまま呼べる。
 *   整形の中身も createElement で組み立てる作りなので、
 *   textContent と同じくHTMLとしては解釈されない（XSSにならない）。
 */
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
  if (cssRole === "ai") {
    // AIの回答はMarkdown記法を含むので、chat.js と同じ整形を通してDOMに組み立てる
    renderMarkdownInto(bubble, content);
  } else {
    // 社員の発言は打った通りに出す。textContent で入れてHTMLとして解釈させない
    bubble.textContent = content;
  }

  msg.appendChild(avatar);
  msg.appendChild(bubble);
  return msg;
}

// ===== このスタッフについてチャット（AIチャットモーダル） =====
//
// 社長が社員データ画面から「この社員について」質問する画面。
// 社員用チャット（chat.js）との違いは次の2点だけで、表示の仕組みは同じものを使う。
//   - 叩くAPIが POST /api/chat/staff-inquiry（対象社員を target_user_id で指定する）
//   - モーダルを閉じると会話が終わる（次に開くと新しいセッションになる）

// このモーダルで今つないでいる会話（セッション）のID。会話を始めていなければ null。
// 閉じるときに null に戻すので、開き直すと必ず新しい会話として始まる
let staffChatSessionId = null;

// 送信中かどうか。連打で複数のストリームが同時に走り、吹き出しが混ざるのを防ぐ
let isStaffChatSending = false;

/**
 * 「このスタッフについてチャット」ボタンで、AIチャットモーダルを開く。
 *
 * 入力: なし（currentStaffUserId / currentStaffName を見る）
 * 出力: なし（モーダルを開き、中身を初期状態にする）
 *
 * 処理:
 *   1. 見出しとアバターを、いま開いている社員に合わせて書き換える
 *   2. 前回の会話の吹き出しを消し、案内文だけを置く
 *   3. セッションと送信中フラグを初期化する（＝新しい会話として始まる）
 *
 * 毎回中身を作り直す理由:
 *   別の社員の画面から開き直したときに、前の社員についての会話が残っていると、
 *   どの社員の話か分からなくなる。会話がモーダル1回分で完結する作りに揃えている。
 */
function openAiChat() {
  const name = currentStaffName || "このスタッフ";

  document.getElementById("modal-aichat-avatar").textContent = firstChar(currentStaffName);
  document.getElementById("modal-aichat-title").textContent = `${name} についてチャット`;

  // 会話をリセットする（閉じ忘れても、開いた時点で必ず新しい会話になる）
  staffChatSessionId = null;
  isStaffChatSending = false;
  setStaffChatSending(false);

  const container = document.getElementById("ai-chat-body");
  container.textContent = "";
  container.appendChild(
    buildAiChatMessage(
      "ai",
      `${name} の個別ソースと全社共通ソースの両方を参照してお答えします。何を知りたいですか？`
    )
  );

  document.getElementById("ai-chat-input").value = "";
  document.getElementById("modal-aichat").classList.add("active");
}

/**
 * AIチャットモーダルを閉じ、会話を終了させる。
 *
 * 入力: なし
 * 出力: なし（モーダルを閉じ、セッションを捨てる）
 *
 * closeModal（index.html の共通関数）を使わない理由:
 *   closeModal はモーダルを見えなくするだけで、会話の状態には手を触れない。
 *   このモーダルは「閉じたら会話が終わる」仕様なので、
 *   セッションIDを捨てる後始末をここで行う。
 *   捨てないと、次に開いたとき前回の続きとして同じセッションに書き込まれてしまう。
 */
function closeAiChat() {
  document.getElementById("modal-aichat").classList.remove("active");
  staffChatSessionId = null;
  isStaffChatSending = false;
  setStaffChatSending(false);
}

/**
 * 吹き出し1件分の要素を組み立てる（社長＝「社」、AI＝「F」）。
 *
 * 入力:
 *   cssRole … 'user'（質問した社長）か 'ai'（Founder）
 *   text    … 表示する文字列
 * 出力:
 *   組み立てた .msg 要素
 *
 * buildChatLogMessage（トーク全文モーダル用）と分けている理由:
 *   あちらの 'user' は「社員本人」なのでアバターに社員名の頭文字を出す。
 *   こちらの 'user' は「質問している社長」なので、社員の頭文字を出すと誰の発言か誤解される。
 *   共通なのは中身の描画（renderMarkdownInto）だけなので、そこだけを使い回す。
 */
function buildAiChatMessage(cssRole, text) {
  const msg = document.createElement("div");
  msg.className = `msg ${cssRole}`;

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.style.width = "28px";
  avatar.style.height = "28px";
  avatar.style.fontSize = "10px";
  if (cssRole === "user") {
    avatar.style.background = "var(--accent-light)";
    avatar.style.color = "var(--accent)";
  }
  avatar.textContent = cssRole === "user" ? "社" : "F";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.style.fontSize = "13px";
  if (cssRole === "ai") {
    // AIの回答はMarkdown記法を含むので、chat.js と同じ整形を通してDOMに組み立てる
    renderMarkdownInto(bubble, text);
  } else {
    // 社長が打った文字は打った通りに出す（textContent なのでHTMLとして解釈されない）
    bubble.textContent = text;
  }

  msg.appendChild(avatar);
  msg.appendChild(bubble);
  return msg;
}

/**
 * 吹き出しをチャット欄の最後に足し、その吹き出し要素を返す。
 *
 * 返り値を吹き出し（.msg-bubble）にしている理由:
 *   AIの回答はストリーミングで少しずつ届く。
 *   先に空の吹き出しを作っておき、届いた断片をこの要素に追記していくため。
 */
function appendAiChatMessage(cssRole, text) {
  const container = document.getElementById("ai-chat-body");
  const msg = buildAiChatMessage(cssRole, text);
  container.appendChild(msg);
  container.scrollTop = container.scrollHeight;
  return msg.querySelector(".msg-bubble");
}

/**
 * 送信中の見た目（ローディング状態）を切り替える。
 *
 * 入力: sending … 送信中なら true
 * 出力: なし（入力欄と送信ボタンの操作可否を切り替える）
 *
 * 回答が返ってくるまで数秒かかるため、押せる見た目のままだと
 * 利用者が反応が無いと感じて連打してしまう。
 * 「入力中...」の吹き出し（sendAiChat 側）と合わせて、処理中であることを示す。
 */
function setStaffChatSending(sending) {
  document.getElementById("ai-chat-input").disabled = sending;
  document.getElementById("ai-chat-send").disabled = sending;
}

/**
 * このモーダルの会話を記録するセッションを、まだ持っていなければ作る。
 *
 * 入力: なし
 * 出力: なし（成功すれば staffChatSessionId が埋まる）
 *
 * context_type に 'staff_inquiry' を指定する理由:
 *   社長自身の通常チャットと区別するため。社員データ画面から聞いた会話が
 *   社員用チャット画面の履歴として復元されてしまうのを防ぐ。
 *
 * 失敗しても例外は投げない:
 *   session_id が無いままでも、サーバー側が質問ごとにセッションを作って回答は返す。
 *   「履歴が1つにまとまらないこと」より「回答が返ること」を優先する（chat.js と同じ方針）。
 */
async function ensureStaffChatSession() {
  try {
    const res = await fetch("/api/chat/sessions", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ context_type: "staff_inquiry" }),
    });
    if (!res.ok) {
      console.error("チャットセッションの作成に失敗しました:", res.status);
      return;
    }
    const data = await res.json();
    staffChatSessionId = data.session_id;
  } catch (e) {
    console.error("チャットセッションの作成に失敗しました:", e);
  }
}

/**
 * モーダルの入力欄のキー操作を受け取り、確定したEnterのときだけ送信する。
 *
 * 入力: event … input の keydown イベント
 * 出力: なし（条件を満たせば sendAiChat を呼ぶ）
 *
 * 中身は chat.js の handleChatInputKeydown と同じ考え方（日本語入力の変換確定Enterを
 * 送信と区別する）。呼ぶ送信関数が違うだけなので、それぞれの画面の関数として分けている。
 */
function handleAiChatInputKeydown(event) {
  if (event.key !== "Enter") return;
  if (event.isComposing) return;
  sendAiChat();
}

/**
 * 質問を送り、AIの回答をストリーミング表示する。
 *
 * 入力: なし（#ai-chat-input の値と currentStaffUserId を読む）
 * 出力: なし（モーダルの中身を書き換える）
 *
 * 処理:
 *   1. 送信中・空入力・対象社員が不明な場合は何もしない
 *   2. 質問を社長の吹き出しとして表示し、入力欄を空にする
 *   3. AIの吹き出しを「入力中...」で先に作る（ローディング表示）
 *   4. 記録先のセッションが無ければ作る
 *   5. POST /api/chat/staff-inquiry を叩き、届いたSSEイベントを種別ごとに処理する
 *      sources … 参照ソースIDを受け取る（表示は未実装。要素に持たせておく）
 *      token   … 吹き出しに文字を追記する
 *      done    … 完了。全文が揃ったのでMarkdownを整形して差し替える
 *      error   … エラー文言を吹き出しに表示する
 *   6. 成功・失敗にかかわらず送信中フラグと入力欄を元に戻す
 *
 * chat.js の関数をそのまま使っている部分:
 *   readSseStream / parseSseEvent … SSEの受信（イベントの区切りの扱いが同じため）
 *   renderMarkdownInto            … 回答のMarkdown整形（社員チャットと同じ見た目にする）
 *   extractHttpError              … ストリーム開始前のHTTPエラー文言の取り出し
 *   index.html が chat.js → admin.js の順で読み込んでいるので、そのまま呼べる。
 */
async function sendAiChat() {
  if (isStaffChatSending) return;

  const input = document.getElementById("ai-chat-input");
  const text = input.value.trim();
  if (!text) return;

  // どの社員についての質問か分からない状態では送らない（宛先のない質問を防ぐ）
  const targetUserId = currentStaffUserId;
  if (!targetUserId) {
    appendAiChatMessage("ai", "対象の社員が特定できません。スタッフ一覧から開き直してください。");
    return;
  }

  const container = document.getElementById("ai-chat-body");

  isStaffChatSending = true;
  setStaffChatSending(true);
  input.value = "";

  // 1. 社長の質問を吹き出しとして表示する
  appendAiChatMessage("user", text);

  // 2. AIの吹き出しを先に作る（最初の断片が届いた時点で中身を差し替える）
  const bubble = appendAiChatMessage("ai", "入力中...");

  // 回答が1文字でも届いたか / エラーや完了を表示済みかを覚えておく
  let hasToken = false;
  let isFinished = false;

  // 届いた断片をつないだ「整形前のテキスト」。
  // 吹き出しのDOMは整形後の形になり元の記法が取り出せなくなるので、
  // 完了時にMarkdownを組み立て直せるよう、素のテキストをこちらに持っておく
  let rawAnswer = "";

  const showError = (message) => {
    bubble.classList.add("is-error");
    bubble.textContent = message;
    isFinished = true;
    container.scrollTop = container.scrollHeight;
  };

  try {
    // 3. まだ会話が始まっていなければ、記録先のセッションを1つ作る
    if (staffChatSessionId === null) {
      await ensureStaffChatSession();
    }

    // 4. 社員別チャットのAPIを叩く。target_user_id で「誰について」の質問かを伝える
    const res = await fetch("/api/chat/staff-inquiry", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        question: text,
        target_user_id: targetUserId,
        session_id: staffChatSessionId,
      }),
    });

    // ストリームが始まる前のエラー（権限=403、社員が見つからない=404、空の質問=400 など）
    if (!res.ok) {
      showError(await extractHttpError(res));
      return;
    }

    if (!res.body) {
      showError("この環境ではストリーミング表示に対応していません");
      return;
    }

    // 5. イベントを種別ごとに処理する（社員チャットと同じ扱い）
    await readSseStream(res.body, ({ event, data }) => {
      if (event === "sources") {
        // 参照ソースID。画面表示は未実装なので、要素に持たせておくだけ
        const sources = data.referenced_sources || [];
        bubble.dataset.referencedSources = sources.join(",");
      } else if (event === "token") {
        // 最初の断片が来たら「入力中...」を消してから追記を始める
        if (!hasToken) {
          bubble.textContent = "";
          hasToken = true;
        }
        // ストリーミング中は素のテキストのまま出す（記法が途中だとちらつくため）
        rawAnswer += data.text || "";
        bubble.textContent = rawAnswer;
        container.scrollTop = container.scrollHeight;
      } else if (event === "done") {
        if (!hasToken) {
          showError("回答を生成できませんでした。もう一度お試しください。");
        } else {
          // 全文が揃ったのでMarkdownを解釈した表示に差し替える
          renderMarkdownInto(bubble, rawAnswer);
          container.scrollTop = container.scrollHeight;
          isFinished = true;
        }
      } else if (event === "error") {
        showError(data.message || "回答の生成中にエラーが発生しました");
      }
    });

    // done も error も来ないまま切れた場合（通信断など）
    if (!isFinished) {
      if (hasToken) {
        // 途中まで表示できているものは残し、注記だけ足す（整形はしない）
        bubble.textContent = `${rawAnswer}（回答が途中で切断されました）`;
      } else {
        showError("回答が途中で切断されました。もう一度お試しください。");
      }
      container.scrollTop = container.scrollHeight;
    }
  } catch (e) {
    console.error(e);
    if (!isFinished) showError("通信エラーが発生しました。接続を確認してください。");
  } finally {
    // 6. 成功・失敗にかかわらず元に戻す（戻さないと二度と送信できなくなる）
    isStaffChatSending = false;
    setStaffChatSending(false);
  }
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

    // レスポンスがJSONとは限らないので、パースの失敗は握りつぶして空の入れ物にする。
    // なぜ必要か:
    //   502や504のときはサーバーではなく手前のプロキシがHTMLのエラーページを返す。
    //   res.ok を見る前に res.json() を呼ぶと、そこで SyntaxError になって
    //   利用者の画面に「Unexpected token '<'」という中身のわからない文字が出てしまう。
    //   先にパースを切り離しておけば、下の判定で「アップロードに失敗しました」を出せる。
    let data = {};
    try {
      const parsed = await res.json();
      data = parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) {
      data = {};
    }

    if (!res.ok) throw new Error(data.detail || "アップロードに失敗しました");

    // 成功時は必ず file_name が返るが、万一欠けていても選んだファイル名で表示する
    setStaffSourceStatus(`「${data.file_name || file.name}」を登録しました`, "success");
    // 登録した1件が一覧に出るよう取り直す（アップロード中に別の社員を開いていたら何もしない）
    if (isStaffStillOpen(userId)) await loadStaffSources(userId);
  } catch (e) {
    setStaffSourceStatus(e.message, "error");
  } finally {
    // 同じファイルを続けて選べるようにinputをリセットする
    input.value = "";
  }
}
