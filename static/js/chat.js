// Founder - 社員用チャット（SSEストリーミング表示）
//
// この画面の流れ:
//   1. 入力欄の文字を POST /api/chat/stream に送る
//   2. サーバーは回答を「生成された端から」少しずつ流してくる（SSE）
//   3. 届いた断片をAIの吹き出しに追記していく＝文字が少しずつ出てくる表示になる
//
// 非ストリーミング版の POST /api/chat も残っているが、この画面では使わない。

// 送信中かどうか。連打で複数のストリームが同時に走り、
// 吹き出しが混ざるのを防ぐためのフラグ
let isSending = false;

// 資料をアップロード中かどうか。同じファイルを二重に登録しないためのフラグ
let isUploadingMySource = false;

// 今つないでいる会話（セッション）のID。まだ会話が始まっていなければ null。
//
// ブラウザ（localStorage）には保存しない。保存すると
// 「ブラウザが覚えている会話の持ち主」と「今ログインしている人」が
// 食い違う場面（ユーザー切り替え）が生まれるため。
// ログインのたびにサーバーから取り直すことで、この食い違いを起こさない。
let currentSessionId = null;

/**
 * 1行分のテキストを、**太字** だけ解釈しながら親要素に流し込む。
 *
 * 入力:
 *   parent … 追加先の要素（p / li / 見出しなど）
 *   text   … 1行分の文字列（Markdownのインライン記法を含みうる）
 * 出力:
 *   なし（parent に文字ノードと strong 要素を足す）
 *
 * 処理:
 *   text を "**" で分割し、「開き」と「閉じ」が揃っている区間だけを strong にする。
 *   分割後の配列で、奇数番目が **〜** の中身にあたる。
 *   ただし最後の要素が奇数番目のときは閉じる "**" が無かったということなので、
 *   太字にせず "**" を付け直して文字のまま出す。
 *   （ストリーミングが途中で切れたときに、記号だけ消えて見えるのを防ぐため）
 *
 * 文字は必ず createTextNode 経由で入れる。
 * 正規表現でHTML文字列を組み立てて innerHTML に代入する方法は使わない
 * （入力文がHTMLとして解釈される穴を作らないため。Issue #32 の対応と同じ方針）。
 */
function appendInlineMarkdown(parent, text) {
  const parts = text.split("**");

  for (let i = 0; i < parts.length; i++) {
    const isBoldSection = i % 2 === 1;
    const hasClosingMarker = i < parts.length - 1;

    if (isBoldSection && hasClosingMarker) {
      const strong = document.createElement("strong");
      strong.appendChild(document.createTextNode(parts[i]));
      parent.appendChild(strong);
    } else if (isBoldSection) {
      // 閉じていない "**"。記号ごと文字として残す
      parent.appendChild(document.createTextNode(`**${parts[i]}`));
    } else if (parts[i] !== "") {
      parent.appendChild(document.createTextNode(parts[i]));
    }
  }
}

/**
 * Markdown記法の文字列をDOMに組み立てて、要素の中身として描画する。
 *
 * 入力:
 *   element … 描画先の要素（AIの吹き出し）
 *   text    … AIの回答テキスト（Markdown記法を含みうる）
 * 出力:
 *   なし（element の中身を作り直す）
 *
 * 対応する記法（これ以外は素のテキストとして扱う）:
 *   ブロック（行単位で判定）
 *     "# " / "## " / "### " … 見出し
 *     "- " / "* " が続く塊    … 箇条書き（ul + li）
 *     "1. " のように数字+ドット+スペースが続く塊 … 番号付きリスト（ol + li）
 *     空行                    … 段落の区切り
 *     それ以外の連続行        … 1つの p にまとめ、行の間に br を挟む
 *   インライン
 *     **文字** … 太字（appendInlineMarkdown が担当）
 *
 * 見出しに h1〜h3 ではなく div を使う理由:
 *   吹き出しの中の見出しは「ページの文書構造としての見出し」ではなく見た目だけの用途。
 *   h タグを使うとページの見出し階層に紛れ込み、スクリーンリーダーの
 *   見出しジャンプなどが会話の断片だらけになってしまうため、class で見た目だけ付ける。
 *
 * innerHTML を使わない理由:
 *   AIの回答であっても、元になった社内文書の中身がそのまま含まれうる。
 *   HTMLとして解釈させない限り、何が書かれていても文字として表示されるだけで済む。
 */
function renderMarkdownInto(element, text) {
  // 中身を作り直すので、まず空にする（textContent = "" は子要素をすべて外す）
  element.textContent = "";

  const lines = String(text).split("\n");

  // その行が新しいブロックの始まりかどうかを判定する小さな道具
  const headingOf = (line) => /^(#{1,3}) (.*)$/.exec(line);
  const bulletOf = (line) => /^[-*] (.*)$/.exec(line);
  const numberedOf = (line) => /^\d+\. (.*)$/.exec(line);

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 空行は段落の区切り。ここでは何も作らずに読み飛ばす
    if (line.trim() === "") {
      i++;
      continue;
    }

    const heading = headingOf(line);
    if (heading) {
      const level = heading[1].length; // "#" の個数がそのまま見出しレベル
      const div = document.createElement("div");
      div.className = `md-heading md-h${level}`;
      appendInlineMarkdown(div, heading[2]);
      element.appendChild(div);
      i++;
      continue;
    }

    // 箇条書き・番号付きリストは「同じ種類の行が続く間」をひとかたまりにする
    const listMatcher = bulletOf(line) ? bulletOf : numberedOf(line) ? numberedOf : null;
    if (listMatcher) {
      const list = document.createElement(listMatcher === bulletOf ? "ul" : "ol");
      while (i < lines.length) {
        const item = listMatcher(lines[i]);
        if (!item) break;
        const li = document.createElement("li");
        appendInlineMarkdown(li, item[1]);
        list.appendChild(li);
        i++;
      }
      element.appendChild(list);
      continue;
    }

    // それ以外は段落。空行か別ブロックの始まりが来るまでを1つの p にまとめ、
    // 行の間には br を挟む（元の改行位置を保つため）
    const p = document.createElement("p");
    let isFirstLine = true;
    while (i < lines.length) {
      const current = lines[i];
      if (current.trim() === "") break;
      if (headingOf(current) || bulletOf(current) || numberedOf(current)) break;
      if (!isFirstLine) p.appendChild(document.createElement("br"));
      appendInlineMarkdown(p, current);
      isFirstLine = false;
      i++;
    }
    element.appendChild(p);
  }
}

/**
 * メッセージ用のDOM要素を作って、チャット欄の最後に追加する。
 *
 * 入力:
 *   container … チャット欄の要素（#chat-messages）
 *   role      … 'user'（自分）か 'ai'（Founder）
 *   text      … 最初に表示しておく文字列
 * 出力:
 *   吹き出し（.msg-bubble）の要素。あとから文字を追記するために返している
 *
 * innerHTML ではなく createElement / textContent を使う理由:
 *   - innerHTML += はチャット欄をまるごと作り直すため、
 *     直前に取得した吹き出しの参照が切れて、追記できなくなる
 *   - 入力文をそのままHTMLとして解釈させないため（<script> などを書かれても文字として表示される）
 *
 * Markdownの整形をAIの回答だけに限定している理由:
 *   ユーザーが打ち込んだ "**" は記法ではなく、打った通りに表示されるべき文字。
 *   自分の発言まで整形すると、書いた記号が勝手に消えて別物になってしまう。
 */
function appendMessage(container, role, text) {
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "user" ? "自" : "F";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  if (role === "ai") {
    // AIの回答はMarkdown記法を含むので、DOMに組み立てて表示する。
    // 履歴の復元（restoreChatHistory）もこの関数を通るため、過去の回答も同じように整形される
    renderMarkdownInto(bubble, text);
  } else {
    bubble.textContent = text;
  }

  msg.appendChild(avatar);
  msg.appendChild(bubble);
  container.appendChild(msg);
  container.scrollTop = container.scrollHeight;

  return bubble;
}

/**
 * チャット欄を「index.html に最初から書かれている状態」に戻す。
 *
 * 入力:  container … チャット欄の要素（#chat-messages）
 * 出力:  なし（JSが追加した吹き出しだけを取り除く）
 *
 * なぜ必要か:
 *   ログアウトは画面を切り替えるだけで #chat-messages の中身を消さない。
 *   そのため、消さずに履歴を足すと再ログインのたびに二重・三重になる。
 *   さらに別の社員でログインし直すと、前の人の会話が画面に残ったまま
 *   下に新しい人の履歴が足され、他人の会話が混ざって見えてしまう。
 *
 * 判定方法に data-initial（HTML側の印）を使う理由:
 *   - appendMessage が作る吹き出しにはこの属性が付かない。
 *     つまり「印が無いもの＝JSが後から足したもの」と確実に区別できる
 *   - first-child（先頭だけ残す）で判定すると、
 *     将来 index.html の挨拶を2つに増やしたり、順番を変えたりしたときに
 *     静かに壊れる。印を付けておけば位置や個数が変わっても判定は変わらない
 */
function resetChatMessages(container) {
  // container.children は生きたコレクションで、消しながら回すと添字がずれる。
  // Array.from でその時点の一覧をコピーしてから消す
  for (const child of Array.from(container.children)) {
    if (!child.hasAttribute("data-initial")) child.remove();
  }
}

/**
 * 前回までの会話を復元して、チャット欄に並べ直す。
 *
 * 入力:  なし（認証トークンは localStorage から読む）
 * 出力:  なし（画面を書き換え、currentSessionId を設定する）
 *
 * 処理:
 *   1. GET /api/chat/sessions で自分のセッション一覧を取る（新しい順）
 *   2. context_type が 'general'（社員チャット）のものだけに絞り、先頭＝最新を選ぶ
 *      1件も無ければ初回利用なので、何もせず終了（currentSessionId は null のまま）
 *   3. GET /api/chat/sessions/{id}/messages で、そのセッションの全メッセージを取る
 *   4. 返ってきた順（古い順）に吹き出しを作る
 *
 * DBの role とCSSのクラス名がずれている点に注意:
 *   DBは 'user' / 'assistant'、画面は 'user' / 'ai' を使う。
 *   ここで 'assistant' → 'ai' に変換しないと、AIの吹き出しの見た目が崩れる。
 *
 * エラーの扱い:
 *   復元に失敗しても、新しく質問すること自体はできる。
 *   そのため画面にはエラーを出さず console.error に留め、
 *   「履歴が出ないだけ」の状態でチャットを使えるようにしている。
 *
 * index.html の #chat-messages には最初から挨拶の吹き出しが入っている。
 * それは消さず、履歴はその下に追加する。
 */
async function restoreChatHistory() {
  const container = document.getElementById("chat-messages");
  if (!container) return;

  // 前のログインで表示した吹き出しと、覚えていたセッションを先に捨てる。
  // 通信に失敗してもここまでは必ず通るので、
  // 別の社員でログインし直したときに前の人の会話が画面に残ることはない。
  // currentSessionId も戻さないと、前の人のセッションに書き込もうとして弾かれる
  resetChatMessages(container);
  currentSessionId = null;

  // アップロードの結果表示も消す。
  // この表示は前のセッションの結果が残ったままになるため、ログアウトして
  // 別の社員でログインすると、前の人が登録したファイル名が画面に見えてしまう。
  // 画面は #screen-chat を作り直さず表示を切り替えているだけなので、
  // 要素は前の状態を保ったまま残る。ログインのたびにここで明示的に消す
  setMySourceStatus("");

  const headers = { Authorization: `Bearer ${localStorage.getItem("token")}` };

  try {
    // 1. セッション一覧（session_id の降順＝新しい順で返ってくる）
    const sessionsRes = await fetch("/api/chat/sessions", { headers });
    if (!sessionsRes.ok) {
      console.error("チャット履歴の取得に失敗しました（セッション一覧）:", sessionsRes.status);
      return;
    }
    const sessions = await sessionsRes.json();

    // 2. 社員チャット（general）の最新セッションを選ぶ。
    //    staff_inquiry（管理者が社員について聞いた会話）はこの画面のものではないので除く
    const latest = sessions.find((s) => s.context_type === "general");
    if (!latest) return; // 初めての利用。復元するものが無い

    currentSessionId = latest.session_id;

    // 3. そのセッションのメッセージ（message_id の昇順＝古い順で返ってくる）
    const messagesRes = await fetch(`/api/chat/sessions/${currentSessionId}/messages`, { headers });
    if (!messagesRes.ok) {
      console.error("チャット履歴の取得に失敗しました（メッセージ）:", messagesRes.status);
      return;
    }
    const messages = await messagesRes.json();

    // 4. 届いた順に吹き出しを作る
    for (const message of messages) {
      appendMessage(container, message.role === "user" ? "user" : "ai", message.content);
    }
  } catch (e) {
    // 通信エラーなど。履歴は出ないが、新規の質問は送れる状態のままにする
    console.error("チャット履歴の復元に失敗しました:", e);
  }
}

/**
 * SSEの1イベント（空行で区切られたひとかたまり）を、扱いやすい形に変換する。
 *
 * 入力:
 *   raw … 例 "event: token\ndata: {\"text\": \"こん\"}"
 * 出力:
 *   { event: 'token', data: { text: 'こん' } }
 *   data がJSONとして読めなかった場合は data を空オブジェクトにする
 *
 * 処理:
 *   行ごとに見て "event:" と "data:" を拾う。
 *   SSEの仕様上 data: は複数行に分かれることがあるので、
 *   いったん配列に集めて改行でつないでからJSONとして読む。
 */
function parseSseEvent(raw) {
  let event = "message";
  const dataLines = [];

  for (const line of raw.split("\n")) {
    // 環境によって行末に \r が付くことがあるので落としておく
    const trimmed = line.replace(/\r$/, "");

    if (trimmed.startsWith("event:")) {
      event = trimmed.slice("event:".length).trim();
    } else if (trimmed.startsWith("data:")) {
      dataLines.push(trimmed.slice("data:".length).trim());
    }
    // ":" で始まるコメント行や空行は無視する
  }

  let data = {};
  if (dataLines.length > 0) {
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch (e) {
      console.error("SSEのdataを解釈できませんでした:", dataLines.join("\n"));
    }
  }

  return { event, data };
}

/**
 * SSEストリームを最後まで読み、イベントが1つ完成するたびに onEvent を呼ぶ。
 *
 * 入力:
 *   body    … fetch のレスポンスボディ（ReadableStream）
 *   onEvent … ({ event, data }) を受け取る関数
 * 出力:
 *   なし（読み終わったら解決するPromise）
 *
 * バッファリングについて（ここが肝）:
 *   ネットワークから届くチャンクは、SSEのイベント境界とは無関係な位置で切れる。
 *   "event: token\\ndata: {\"te" ... のように行の途中で切れることもある。
 *   そのため受信テキストは buffer に貯め続け、
 *   区切りの空行（\n\n）が見つかった分だけを取り出して処理し、
 *   残り（＝まだ不完全なイベント）は buffer に残して次のチャンクを待つ。
 *
 *   TextDecoder に { stream: true } を渡しているのも同じ理由で、
 *   日本語のようなマルチバイト文字がチャンクの途中で分断されても、
 *   次のチャンクと合わせて正しい文字に復元してくれる。
 */
async function readSseStream(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // 完成しているイベント（\n\n までのかたまり）を取り出せるだけ取り出す
    let separator = buffer.indexOf("\n\n");
    while (separator !== -1) {
      const raw = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      if (raw.trim()) onEvent(parseSseEvent(raw));
      separator = buffer.indexOf("\n\n");
    }
  }

  // 末尾に残ったマルチバイト文字を確定させ、
  // 空行で終わっていない最後のイベントがあれば処理する
  buffer += decoder.decode();
  if (buffer.trim()) onEvent(parseSseEvent(buffer));
}

/**
 * ストリームを開始する前に起きたエラー（HTTPエラー）のメッセージを取り出す。
 *
 * 入力:  res … fetch のレスポンス（res.ok が false のもの）
 * 出力:  画面に出す文字列
 *
 * FastAPI は HTTPException を {"detail": "..."} 形式のJSONで返すので、
 * 読めればその文言を、読めなければステータスコードを添えた定型文を返す。
 */
async function extractHttpError(res) {
  try {
    const data = await res.json();
    if (data && data.detail) return String(data.detail);
  } catch (e) {
    // JSONでない（502のHTMLなど）場合はそのまま定型文へ
  }
  return `エラーが発生しました（${res.status}）`;
}

/**
 * 会話の記録先セッションをまだ持っていなければ作る。
 *
 * 入力:  なし
 * 出力:  なし（成功すれば currentSessionId が埋まる）
 *
 * 失敗しても例外は投げない。session_id が無いまま送信を続ければ、
 * サーバー側が質問ごとにセッションを作って回答自体は返してくれる。
 * 「履歴が1つにまとまらないこと」より「回答が返ること」を優先する。
 */
async function ensureSession() {
  try {
    const res = await fetch("/api/chat/sessions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("token")}`,
      },
      body: JSON.stringify({ context_type: "general" }),
    });
    if (!res.ok) {
      console.error("チャットセッションの作成に失敗しました:", res.status);
      return;
    }
    const data = await res.json();
    currentSessionId = data.session_id;
  } catch (e) {
    console.error("チャットセッションの作成に失敗しました:", e);
  }
}

/**
 * 入力欄のキー操作を受け取り、確定したEnterのときだけ送信する。
 *
 * 入力: event … input の keydown イベント
 * 出力: なし（条件を満たせば sendMessage を呼ぶ）
 *
 * event.isComposing を除外する理由（重要）:
 *   日本語入力では「へんかん」→ 変換候補から選ぶ → Enter で確定、という操作をする。
 *   このEnterはあくまで変換を確定するためのもので、送信の意思ではない。
 *   除外しないと、変換を確定した瞬間に書きかけの文が送信されてしまう。
 *   isComposing は「いま文字を変換中か」を表すフラグで、変換確定のEnterでは true になる。
 *
 * index.html のインラインに書かず、この関数に寄せている理由:
 *   条件が増えるとインラインでは読みづらく、社員チャットと社員別チャットで
 *   片方だけ直して挙動がずれる。判定はJS側の1か所にまとめる。
 */
function handleChatInputKeydown(event) {
  if (event.key !== "Enter") return;
  if (event.isComposing) return;
  sendMessage();
}

/**
 * メッセージを送信し、AIの回答をストリーミング表示する。
 *
 * 入力:  なし（#chat-input の値を読む）
 * 出力:  なし（画面を書き換える）
 *
 * 処理:
 *   1. 入力文を自分の吹き出しとして表示し、入力欄を空にする
 *   2. AIの吹き出しを「入力中...」で先に作っておく（あとで中身を差し替える）
 *   3. 記録先のセッションが無ければ作る
 *   4. POST /api/chat/stream を叩く
 *   5. 届いたイベントを種別ごとに処理する
 *      sources … 参照ソースIDを受け取る（表示は今後の課題。要素に持たせておく）
 *      token   … 吹き出しに文字を追記する
 *      done    … 完了。ローディング状態を解除する
 *      error   … エラー文言を吹き出しに表示する
 *   6. 最後に必ず送信中フラグを戻す
 */
async function sendMessage() {
  // 送信中なら何もしない（連打対策）
  if (isSending) return;

  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  const container = document.getElementById("chat-messages");

  isSending = true;
  input.value = "";

  // 1. 自分のメッセージを右側に表示
  appendMessage(container, "user", text);

  // 2. AIの吹き出しを先に作る。最初の token が届いた時点で中身を差し替える
  const bubble = appendMessage(container, "ai", "入力中...");

  // 回答が1文字でも届いたか / エラーや完了を表示済みかを覚えておく
  let hasToken = false;
  let isFinished = false;

  // 届いた断片をつないだ「整形前のテキスト」。
  // 吹き出しのDOMは整形後の形になり元の記法が取り出せなくなるので、
  // 完了時にMarkdownを組み立て直せるよう、素のテキストをこちらに持っておく
  let rawAnswer = "";

  // 吹き出しをエラー表示に切り替える（見た目は is-error クラスで色だけ変える）
  const showError = (message) => {
    bubble.classList.add("is-error");
    bubble.textContent = message;
    isFinished = true;
    container.scrollTop = container.scrollHeight;
  };

  try {
    // 3. まだ会話が始まっていなければ、記録先のセッションを1つ作る。
    //    ここで確保した session_id を以降の質問にも付けることで、
    //    1回の会話が1つのセッションにまとまり、次回リロード時に復元できる。
    if (currentSessionId === null) {
      await ensureSession();
    }

    // 4. ストリーミング用のAPIを叩く。
    //    認証はログイン時に localStorage へ保存したトークンを Authorization で送る（他APIと同じ）
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("token")}`,
      },
      body: JSON.stringify({ question: text, session_id: currentSessionId }),
    });

    // ストリームが始まる前のエラー（未ログイン=401、権限=403、空の質問=400 など）
    if (!res.ok) {
      showError(await extractHttpError(res));
      return;
    }

    if (!res.body) {
      showError("この環境ではストリーミング表示に対応していません");
      return;
    }

    // 5. イベントを種別ごとに処理する
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
        // ストリーミング中は素のテキストのまま出す。
        // まだ閉じていない "**" が太字になったり戻ったりしてちらつくので、
        // 整形は文章が出そろう done まで待つ
        rawAnswer += data.text || "";
        bubble.textContent = rawAnswer;
        container.scrollTop = container.scrollHeight;
      } else if (event === "done") {
        // 完走。1文字も届いていなかった場合はその旨を出す（空の吹き出しを残さない）
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
        // 途中まで表示できているので、それは残したまま注記だけ足す。
        // 文章が途中で切れている＝記法も途中なので、整形はせず素のテキストのままにする
        // （閉じていないリストや ** を無理に組み立てると、元の文と違う形になってしまう）
        bubble.textContent = `${rawAnswer}（回答が途中で切断されました）`;
      } else {
        showError("回答が途中で切断されました。もう一度お試しください。");
      }
      container.scrollTop = container.scrollHeight;
    }
  } catch (e) {
    // 通信エラーや読み取り中の例外
    console.error(e);
    if (!isFinished) showError("通信エラーが発生しました。接続を確認してください。");
  } finally {
    // 6. 成功・失敗にかかわらず送信中フラグを戻す（戻さないと二度と送信できなくなる）
    isSending = false;
  }
}

// ===== 自分の資料のアップロード =====

/**
 * 「＋ 資料を追加」ボタン：隠しファイル選択欄を開く。
 *
 * 入力: なし
 * 出力: なし（ファイル選択ダイアログが開く）
 *
 * ファイル入力を画面に出さずボタンから発火させるのは、
 * 既定の見た目のままだとチャット画面のデザインから浮くため。
 * 管理者画面の openStaffSourcePicker（admin.js）と同じ形にしている。
 */
function openMySourcePicker() {
  document.getElementById("my-source-file-input").click();
}

/**
 * アップロードの状況を表示する。
 *
 * 入力:
 *   message … 表示する文字列（空や null なら表示を消す）
 *   type    … "success" | "error" | "loading" | null（色分けに使うCSSクラス）
 * 出力: なし（#my-source-status の class と textContent を書き換える）
 *
 * ファイル名などをそのまま出すので、必ず textContent で入れる
 * （innerHTML に入れると、ファイル名に書いたHTMLが解釈されてしまう）。
 */
function setMySourceStatus(message, type) {
  const el = document.getElementById("my-source-status");
  el.className = "source-status" + (type ? " " + type : "");
  el.textContent = message || "";
}

/**
 * 選んだファイルを「自分の個別ソース」として登録する。
 *
 * 入力: file … ファイル選択欄で選ばれたファイル
 * 出力: なし（結果をメッセージで表示する）
 *
 * 処理:
 *   1. 連打を弾き、処理中フラグを立てる
 *   2. FormData にファイルだけを入れて POST /api/sources/my-upload に送る
 *   3. 結果を #my-source-status に表示する
 *   4. 終了時にファイル入力をリセットする
 *
 * なぜ FormData に file しか入れないか（重要）:
 *   誰の資料として登録するかは、サーバーがトークン（JWT）の user_id から決める。
 *   このAPIは scope も owner_user_id も受け取らない作りなので、
 *   送っても無視されるが、送る側も「他人を指す値を作らない」形に揃えておく。
 *   画面側に他人のIDを組み立てるコードがあると、後から別の用途に流用されたときに
 *   そこが穴になりうる。
 *
 * なぜ最後に input.value = "" するか:
 *   ファイル入力は「同じファイルを選び直す」と値が変わらないため onchange が発火しない。
 *   毎回空に戻しておけば、失敗したファイルをもう一度選んでやり直せる。
 */
async function uploadMySource(file) {
  const input = document.getElementById("my-source-file-input");
  if (!file) return;

  // アップロード中なら何もしない（連打対策。同じ資料が二重に登録されるのを防ぐ）
  if (isUploadingMySource) return;
  isUploadingMySource = true;

  // 送るのはファイルだけ。scope や owner_user_id は付けない
  const formData = new FormData();
  formData.append("file", file);

  setMySourceStatus(`「${file.name}」をアップロード中...`, "loading");

  try {
    // 認証はログイン時に localStorage へ保存したトークンを Authorization で送る（他APIと同じ）
    const res = await fetch("/api/sources/my-upload", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${localStorage.getItem("token")}`,
      },
      body: formData,
    });

    // 未ログイン(401)・対応外の形式(400)・サイズ超過(413)などは、
    // サーバーの detail をそのまま出す。JSONで返らない場合も extractHttpError が定型文にする
    if (!res.ok) {
      setMySourceStatus(await extractHttpError(res), "error");
      return;
    }

    const data = await res.json();
    // 成功時は必ず file_name が返るが、万一欠けていても選んだファイル名で表示する
    setMySourceStatus(`「${data.file_name || file.name}」を登録しました`, "success");
  } catch (e) {
    console.error(e);
    setMySourceStatus("通信エラーが発生しました。接続を確認してください。", "error");
  } finally {
    isUploadingMySource = false;
    // 同じファイルを続けて選べるようにinputをリセットする
    input.value = "";
  }
}
