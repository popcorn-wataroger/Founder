// Founder - 共通ソース管理者（source_manager）専用画面
//
// なぜ admin.js の uploadSource() / registerUrl() を流用しないか:
//     あちらは管理者ホームのDOM（#source-scope / #source-owner / #source-status /
//     #source-file-input / #source-tbody）を直接参照している。共有するには
//     引数化のリファクタが必要で、1300行ある admin.js 全体に触ることになり、
//     管理者画面の回帰リスクを負う。
//     また同じIDを専用画面にも置くとDOMが重複し、どちらが取れるか不定になる。
//     入口ごとにファイルを分け、それぞれが1つの画面だけを見る形にしておく。
//
// なぜ scope をどこにも持たないか（重要）:
//     この画面から登録できるのは全社共通ソースだけ。scope を送らなければ
//     サーバー側（POST /api/sources/upload）の既定値 'common' が使われる。
//     画面に種別の選択肢を置かず、送信するコードにも scope を書かないことで、
//     'individual' を指定できる経路そのものが存在しない状態にする。
//     「検証で弾く」のではなく「送れる経路を作らない」という、
//     POST /api/sources/my-upload と同じ考え方。
//
//     なお最終的な権限判定はサーバー側が持つ。
//     入口は require_source_uploader（admin / source_manager のみ通す）、
//     個別ソースの登録は check_scope_permission が admin だけに限定する。
//     画面側でUIを出さないのは利便性のためで、権限の砦はあくまでサーバー側。

// 処理中・成功・失敗のメッセージを画面に表示する
//
// 入力: message … 表示する文字列（空なら消す） / type … "loading" | "success" | "error"
// 出力: なし（#sm-status の表示が変わる）
//
// textContent を使う理由:
//     ここに出す文字列にはユーザーが選んだファイル名やサーバーのエラー文が入る。
//     innerHTML だとファイル名に含まれるタグがHTMLとして解釈されてしまう。
function setCommonSourceStatus(message, type) {
  const el = document.getElementById("sm-status");
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

// ログイン時に保存したトークンを認証ヘッダとして組み立てる
//
// admin.js の authHeaders() と同じ処理だが、あちらは管理者画面用のファイルなので
// この画面から呼ぶと依存関係が逆流する（source_manager 画面が admin.js を必要とする）。
// 数行なのでこちらに持つ。
function smAuthHeaders(extra) {
  const token = localStorage.getItem("token");
  return Object.assign({ Authorization: `Bearer ${token}` }, extra || {});
}

// 「+ アップロード」ボタンとアップロードゾーンのクリックから、隠しinputを開く
function openCommonSourcePicker() {
  document.getElementById("sm-file-input").click();
}

// ゾーンにドラッグが乗ったとき：ブラウザの既定動作を止め、ハイライトを付ける
function onCommonSourceDragOver(event) {
  event.preventDefault();
  document.getElementById("sm-drop-zone").classList.add("dragover");
}

// ゾーンから外れたとき：ハイライトを解除する
function onCommonSourceDragLeave(event) {
  event.preventDefault();
  document.getElementById("sm-drop-zone").classList.remove("dragover");
}

// ファイルをドロップしたとき：既定動作（ブラウザがファイルを開く）を止め、先頭ファイルを登録する
function onCommonSourceDrop(event) {
  event.preventDefault();
  document.getElementById("sm-drop-zone").classList.remove("dragover");
  const file = event.dataTransfer.files[0];
  if (file) uploadCommonSource(file);
}

// ファイルを全社共通ソースとして登録する（multipart/form-data）
//
// 入力: file … ユーザーが選んだ、またはドロップしたファイル
// 処理: POST /api/sources/upload に送る。scope は送らない（サーバー既定の common になる）
// 出力: なし（#sm-status に結果を表示する）
//
// エラーになる場面:
//     403 … source_manager でも admin でもないロールで叩いた場合
//     400 … 対応していない拡張子
//     413 … 50MBを超えるファイル
//     500 … 保存・ベクトル化の失敗（この場合サーバー側でソース登録は取り消される）
async function uploadCommonSource(file) {
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);

  setCommonSourceStatus(`「${file.name}」をアップロード中...`, "loading");
  try {
    const res = await fetch("/api/sources/upload", {
      method: "POST",
      headers: smAuthHeaders(),
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "アップロードに失敗しました");

    setCommonSourceStatus(`「${data.file_name}」を全社共通ソースとして登録しました`, "success");
  } catch (e) {
    setCommonSourceStatus(e.message, "error");
  } finally {
    // 同じファイルを連続で選べるようにinputをリセットする
    // （リセットしないと2回目に onchange が発火しない）
    document.getElementById("sm-file-input").value = "";
  }
}

// URLを全社共通ソースとして登録する（JSON）
//
// 入力: なし（#sm-url-input の値を読む）
// 処理: POST /api/sources/url に送る。scope は送らない（サーバー既定の common になる）
// 出力: なし（#sm-status に結果を表示する）
//
// URLの安全性はサーバー側が判定する（app/safe_urls.py）。
// 画面側で形式を検査しても、APIを直接叩かれれば意味がないため、
// ここでは空欄チェックだけにして判定を二重に持たない。
async function registerCommonUrl() {
  const input = document.getElementById("sm-url-input");
  const url = input.value.trim();

  if (!url) {
    setCommonSourceStatus("URLを入力してください", "error");
    return;
  }

  setCommonSourceStatus("URLを登録中...", "loading");
  try {
    const res = await fetch("/api/sources/url", {
      method: "POST",
      headers: smAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url: url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "URL登録に失敗しました");

    setCommonSourceStatus("URLを全社共通ソースとして登録しました", "success");
    input.value = "";
  } catch (e) {
    setCommonSourceStatus(e.message, "error");
  }
}

// 画面を開いたときの初期化。前回の結果表示を消す
//
// なぜ必要か:
//     ステータス表示はDOMに残り続ける。別の人がログインしたときに
//     前の人が登録したファイル名が見えてしまうため、ログイン時にクリアする
//     （Issue #74 / PR #97 でチャット画面に入れたのと同じ対応）。
function initCommonSourceScreen() {
  setCommonSourceStatus("", null);
}
