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

// 登録リクエストが進行中かどうか。ファイル登録とURL登録で共有する
//
// なぜ共有するか（重要）:
//     POST /api/sources/upload と POST /api/sources/url は非冪等。
//     同じ要求を2回処理すると、DBに2行・Qdrantに2組のベクトルが作られる。
//     しかも source_manager にはソース一覧が見えないため（Issue #101）、
//     重複に気づく手段が画面上に無い。入口で防ぐ必要がある。
//
//     ファイルとURLで別々に持たないのは、片方の処理中にもう片方を
//     押せてしまうと、同じ #sm-status を2つの処理が奪い合い、
//     どちらの結果が最後に残るか分からなくなるため。
//
// 真偽値ではなく「実行中のログイン世代」を持つ理由（Issue #99）:
//     真偽値だと、前の人の登録が終わるまで true のままになり、ログアウトして
//     次にログインした人が「連打」と判定されて弾かれる。しかも return するだけなので
//     その人には何のメッセージも出ない。どの世代が使用中かを持てば、
//     同じ世代の連打だけを弾ける。実行していないときは null。
let commonSourceRequestGeneration = null;

// 削除処理が進行中のソースID。連打で同じソースの削除が並行するのを防ぐ（Issue #118）
//
// なぜ登録用の commonSourceRequestGeneration と分けるか:
//     あちらは「登録処理が1つでも走っているか」を持つ変数で、
//     ファイル登録とURL登録のどちらか一方しか同時に動かせない。
//     削除は一覧の行ごとに別のボタンがあり、違うソースを続けて消すのは正当な操作なので、
//     まとめて1つの錠前にすると使い勝手が落ちる。
//     防ぎたいのは「同じ source_id への二重送信」だけなので、IDごとに持つ。
//
// なぜ真偽値の変数ではなく Set か:
//     同時に複数のソースが削除中になりうるため、実行中のIDを列挙して持つ必要がある。
//     Set なら has / add / delete がそのまま「実行中か / 取る / 返す」に対応する。
const deletingSourceIds = new Set();

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

  // 通信を始めた時点のログイン世代を控える（Issue #99）。
  // 応答が返るまでにログアウトして別の人がログインしていた場合、
  // その結果は前の人のものなので #sm-status には出さない。
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない。
  // 別のログインに変わっていれば、前の人の通信が続いていても受け付ける。
  if (commonSourceRequestGeneration === generation) return;
  commonSourceRequestGeneration = generation;

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

    if (!isSameLoginGeneration(generation)) return;

    setCommonSourceStatus(`「${data.file_name}」を全社共通ソースとして登録しました`, "success");
    // 登録した1件を一覧に反映する（二重登録に気づけるようにするのがこのIssueの目的）。
    // 世代が変わっている場合はここまで来ないので、別の人の画面で一覧を取り直すこともない
    loadCommonSources();
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setCommonSourceStatus(e.message, "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    // try の中で return や throw が起きても finally は実行されるため、
    // 「使用中のまま二度と登録できない」状態にならない。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     登録まで「実行していない」ことにしてしまう。
    if (commonSourceRequestGeneration === generation) {
      commonSourceRequestGeneration = null;
    }
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

  // 空欄チェックは世代を取る前に行う。
  // 通信しないので、ここで取ると解放する場所が増えるだけになる
  if (!url) {
    setCommonSourceStatus("URLを入力してください", "error");
    return;
  }

  // 通信を始めた時点のログイン世代を控える（Issue #99）
  const generation = currentLoginGeneration();

  // 同じログインの中で進行中なら何もしない（連打での二重登録を防ぐ）。
  // 別のログインに変わっていれば、前の人の通信が続いていても受け付ける。
  if (commonSourceRequestGeneration === generation) return;
  commonSourceRequestGeneration = generation;

  setCommonSourceStatus("URLを登録中...", "loading");
  try {
    const res = await fetch("/api/sources/url", {
      method: "POST",
      headers: smAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url: url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "URL登録に失敗しました");

    if (!isSameLoginGeneration(generation)) return;

    setCommonSourceStatus("URLを全社共通ソースとして登録しました", "success");
    // 入力欄のクリアも照合の内側に置く。
    // 外に出すと、次にログインした人が入力中のURLを消してしまう
    input.value = "";
    loadCommonSources();
  } catch (e) {
    if (!isSameLoginGeneration(generation)) return;
    setCommonSourceStatus(e.message, "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    //
    // 自分が取った分だけ解放する理由:
    //     無条件に null にすると、あとから始まった新しい世代の
    //     登録まで「実行していない」ことにしてしまう。
    if (commonSourceRequestGeneration === generation) {
      commonSourceRequestGeneration = null;
    }
  }
}

// 一覧取得リクエストの通し番号。最新のリクエストだけがDOMを更新するために使う
//
// なぜ必要か:
//     loadCommonSources() は画面初期化時と登録成功後の2箇所から呼ばれ、
//     これらは並行して走りうる。先に投げた古いリクエストが後から完了すると、
//     登録直後の一覧を古い結果で上書きし、登録したはずのソースが
//     一覧から一時的に消える。番号が最新でなければ描画しないことで防ぐ。
let commonSourceListRequestId = 0;

// 登録済みの共通ソースを取得して一覧に描画する
//
// 入力: なし
// 処理: GET /api/sources/common を叩き、返ってきた配列を #sm-source-list に並べる
// 出力: なし（一覧の表示が変わる）
//
// エラーになる場面:
//     403 … source_manager でも admin でもないロールで叩いた場合
//     通信失敗 … オフラインやサーバー停止
//     どちらも一覧の中に文言を出すだけにして、#sm-status（登録結果の表示）は触らない。
//     登録の成否と一覧の取得失敗が同じ場所に出ると、どちらの結果か分からなくなる。
//
// このAPIが返すのは scope='common' のものだけ。
// 個別ソース（他人の評価・給与など）はファイル名も含めて1件も返らない。
async function loadCommonSources() {
  // このリクエストの番号を採番する。以降、最新かどうかをこの番号で判定する
  const requestId = ++commonSourceListRequestId;
  const container = document.getElementById("sm-source-list");
  setCommonSourceListMessage("読み込み中...");

  try {
    const res = await fetch("/api/sources/common", { headers: smAuthHeaders() });
    if (!res.ok) throw new Error("取得失敗");
    const sources = await res.json();

    // 待っている間に新しいリクエストが始まっていたら、この結果は捨てる
    if (requestId !== commonSourceListRequestId) return;

    if (sources.length === 0) {
      setCommonSourceListMessage("登録された共通ソースはありません。");
      return;
    }

    container.textContent = "";
    sources.forEach((source) => {
      container.appendChild(buildCommonSourceItem(source));
    });
  } catch (e) {
    // 失敗の表示も同じ理由で、最新のリクエストのときだけ出す
    if (requestId !== commonSourceListRequestId) return;
    setCommonSourceListMessage("一覧の取得に失敗しました。");
  }
}

// 一覧の中に1行だけの案内文（読み込み中・0件・失敗）を表示する
//
// 入力: message … 表示する文字列
// 出力: なし
function setCommonSourceListMessage(message) {
  const container = document.getElementById("sm-source-list");
  container.textContent = "";

  const placeholder = document.createElement("div");
  placeholder.className = "source-item-date";
  placeholder.textContent = message;
  container.appendChild(placeholder);
}

// 共通ソース1件分の行を組み立てて返す
//
// 入力: source … { source_id, file_name, file_type, uploaded_at }
// 出力: 組み立てた要素（呼び出し側が一覧に追加する）
//
// innerHTML を使わない理由:
//     file_name は登録した人が付けたファイル名で、タグを含む名前もあり得る。
//     textContent で入れることで、文字として表示される（XSSにならない）。
//
// 削除ボタンを付ける理由（Issue #118）:
//     Issue #101 の時点では削除ボタンを付けず、削除権限を与えるかどうかを
//     別の判断として切り離していた（一覧が見えれば二重登録には気づけるため）。
//     Issue #118 でクライアントと合意し、共通ソース管理者にも削除を許すことにした。
//
//     ただし削除できるのは共通ソース（scope='common'）だけで、
//     他人の個別ソース（評価・給与など）には届かない。
//     判定を持つのはサーバー側の delete_source() で、DBから取った行の scope を見て
//     admin 以外が個別ソースを指定した場合は403を返す。
//     この画面が扱うのは共通ソースだけ（GET /api/sources/common は
//     scope='common' しか返さない）なので、一覧に出る行はすべて削除してよい対象になる。
function buildCommonSourceItem(source) {
  const item = document.createElement("div");
  item.className = "source-item";

  const info = document.createElement("div");
  info.className = "source-item-info";

  const icon = document.createElement("div");
  icon.className = "source-item-icon tag-common";
  icon.textContent = source.file_type;

  const texts = document.createElement("div");

  const name = document.createElement("div");
  name.className = "source-item-name";
  name.textContent = source.file_name;

  const date = document.createElement("div");
  date.className = "source-item-date";
  date.textContent = formatCommonSourceDate(source.uploaded_at);

  // 削除ボタン。admin.js のソース一覧と同じ見た目（class="delete-btn" の「×」）にする
  //
  // onclick 属性ではなく addEventListener を使う理由:
  //     admin.js は innerHTML でHTMLの文字列を組み立てているため onclick 属性しか選べない。
  //     こちらは createElement で組み立てているので、source_id を文字列に埋め込まずに
  //     そのまま渡せる。値がHTMLとして解釈される経路を作らない。
  const deleteButton = document.createElement("button");
  deleteButton.className = "delete-btn";
  deleteButton.textContent = "×";
  deleteButton.addEventListener("click", () => deleteCommonSource(source.source_id));

  texts.appendChild(name);
  texts.appendChild(date);
  info.appendChild(icon);
  info.appendChild(texts);
  item.appendChild(info);
  item.appendChild(deleteButton);

  return item;
}

// 共通ソースを1件削除して、一覧を取り直す
//
// 入力: sourceId … 削除するソースのID（一覧の行が持っている source_id）
// 処理:
//     1. 確認ダイアログを出す。キャンセルなら何もしない
//     2. DELETE /api/sources/{source_id} を叩く
//     3. 成功したら一覧を再取得して、消えたことを画面に反映する
// 出力: なし（#sm-status の表示と一覧の内容が変わる）
//
// エラーになる場面:
//     403 … 個別ソースを指定した場合（この画面の一覧からは起こらないが、
//            APIを直接叩かれた場合はサーバーが弾く）
//     404 … 既に削除されている場合（他の管理者が先に消したときなど）
//     500 … Qdrantや実体の削除に失敗した場合（このときソースは削除されない）
//
// admin.js の deleteSource() と別に持つ理由:
//     処理はほぼ同じだが、あちらは管理者ホームのDOM（#source-status / #source-tbody）を
//     前提にしており、setSourceStatus() と loadSources() を呼ぶ。
//     この画面が扱うのは #sm-status と #sm-source-list なので、そのままでは使えない。
//     また admin.js を呼ぶと依存が逆流する（source_manager 画面が管理者画面用の
//     ファイルを必要とする）。formatCommonSourceDate と同じ判断で、こちらに持つ。
//
// 同じソースへの連打を deletingSourceIds で防ぐ理由（重要）:
//     削除ボタンを連打すると、1回目の応答を待たずに2回目のリクエストが飛ぶ。
//     サーバー側の delete_source() は「行を取得 → Qdrant削除 → 実体削除 → DB削除」の順で進むが、
//     2回目が1回目の完了前に行の取得を済ませてしまうと、その時点ではまだ行が消えていないため
//     404にならず、両方が削除処理へ進む。
//     結果としてQdrantと実体の削除が二重に走り、2回目のストレージ削除が
//     「もう無い」というエラーを返すと、実際には削除できているのに
//     画面には「削除に失敗しました」と表示される。
//
//     「削除は冪等だから連打しても2回目は404が返るだけ」と考えるのは誤り。
//     404で弾かれるのは1回目が完全に終わったあとに送った場合だけで、
//     並行して走った場合は上記の通り両方が通ってしまう。
//
// 登録側の世代チェック（commonSourceRequestGeneration）を使わない理由:
//     あれは「登録処理が1つでも走っているか」を持つ仕組みで、
//     ファイル登録とURL登録が同じ #sm-status を奪い合うのを防ぐためのもの。
//     削除は行ごとにボタンがあり、違うソースを続けて消すのは正当な操作なので、
//     source_id ごとの実行中状態（deletingSourceIds）で防ぐ。
async function deleteCommonSource(sourceId) {
  // このソースの削除が既に進行中なら、2回目以降は捨てる
  if (deletingSourceIds.has(sourceId)) return;

  if (!confirm("このソースを削除しますか？")) return;

  // 実行中として登録するのは confirm より後。
  //
  // なぜ confirm より前に登録しないか:
  //     confirm はユーザーが応答するまで戻ってこない。
  //     先に登録してしまうと、キャンセルを押した場合に削除は始まっていないのに
  //     登録だけが残り、以後そのソースは冒頭の has() で弾かれて二度と削除できなくなる。
  //     （キャンセル時に消す処理を足す手もあるが、解放する場所が2箇所に増える。
  //      「通信を始める直前に取り、finally で必ず返す」形にすれば取りこぼしが起きない）
  deletingSourceIds.add(sourceId);

  setCommonSourceStatus("削除中...", "loading");
  try {
    const res = await fetch(`/api/sources/${sourceId}`, {
      method: "DELETE",
      headers: smAuthHeaders(),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "削除に失敗しました");

    setCommonSourceStatus("ソースを削除しました", "success");
    await loadCommonSources();
  } catch (e) {
    setCommonSourceStatus(e.message, "error");
  } finally {
    // 成功・失敗のどちらでも必ず解放する。
    // try の中で return や throw が起きても finally は実行されるため、
    // 「実行中のまま二度と削除できない」状態にならない
    // （uploadCommonSource() の finally と同じ考え方）。
    deletingSourceIds.delete(sourceId);
  }
}

// 登録日時を「2026/01/15 09:30」の形に整える
//
// 入力: value … サーバーが返す日時文字列
// 出力: 整形した文字列（解釈できない値はそのまま返す）
//
// admin.js の formatSourceDate() と同じ処理だが、あちらは管理者画面用のファイルなので
// この画面から呼ぶと依存が逆流する。数行なのでこちらに持つ。
function formatCommonSourceDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  const 月 = String(date.getMonth() + 1).padStart(2, "0");
  const 日 = String(date.getDate()).padStart(2, "0");
  const 時 = String(date.getHours()).padStart(2, "0");
  const 分 = String(date.getMinutes()).padStart(2, "0");
  return `${date.getFullYear()}/${月}/${日} ${時}:${分}`;
}

// 画面を開いたときの初期化。前回の結果表示を消し、登録済みの一覧を読み込む
//
// なぜステータスを消すか:
//     ステータス表示はDOMに残り続ける。別の人がログインしたときに
//     前の人が登録したファイル名が見えてしまうため、ログイン時にクリアする
//     （Issue #74 / PR #97 でチャット画面に入れたのと同じ対応）。
function initCommonSourceScreen() {
  setCommonSourceStatus("", null);
  loadCommonSources();
}

// 共通ソース登録画面からチャット画面へ移動する（Issue #112）
//
// 入力: なし
// 出力: なし（表示中の画面が #screen-chat に変わる）
//
// 処理:
//     1. チャット画面へ切り替える
//     2. これまでの会話を取り直す
//
// なぜこの画面にチャットUIを作らず移動させるか:
//     チャットUIをこちらにも置くと、chat.js が使う #chat-messages と #chat-input が
//     画面内で重複し、どちらの要素を取るかが不定になる。
//     画面を移す形にすれば、chat.js を一切変更せずにそのまま使える。
//
// なぜ入るたびに restoreChatHistory() を呼ぶか:
//     session_id をブラウザ（localStorage）に保存していないため、
//     チャット画面に入るたびにサーバーから取り直す必要がある
//     （理由は chat.js の currentSessionId のコメントを参照）。
function openChatFromSourceManager() {
  showScreen("screen-chat");
  restoreChatHistory();
}

// チャット画面から全社共通ソースの登録画面へ移動する（Issue #116）
//
// 入力: なし
// 出力: なし（表示中の画面が #screen-source-manager に変わる）
//
// 一覧を再取得しない理由:
//     この画面を離れている間に共通ソースが増えることはない。
//     登録は #screen-source-manager の中でしか行えず、
//     登録した直後に uploadCommonSource() / registerCommonUrl() が
//     loadCommonSources() を呼んで反映済みのため。
function openChatCommonSourceScreen() {
  showScreen("screen-source-manager");
}
