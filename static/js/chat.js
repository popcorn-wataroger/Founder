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

// 今つないでいる会話（セッション）のID。まだ会話が始まっていなければ null。
//
// ブラウザ（localStorage）には保存しない。保存すると
// 「ブラウザが覚えている会話の持ち主」と「今ログインしている人」が
// 食い違う場面（ユーザー切り替え）が生まれるため。
// ログインのたびにサーバーから取り直すことで、この食い違いを起こさない。
let currentSessionId = null;

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
 */
function appendMessage(container, role, text) {
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "user" ? "自" : "F";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;

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
        bubble.textContent += data.text || "";
        container.scrollTop = container.scrollHeight;
      } else if (event === "done") {
        // 完走。1文字も届いていなかった場合はその旨を出す（空の吹き出しを残さない）
        if (!hasToken) {
          showError("回答を生成できませんでした。もう一度お試しください。");
        } else {
          isFinished = true;
        }
      } else if (event === "error") {
        showError(data.message || "回答の生成中にエラーが発生しました");
      }
    });

    // done も error も来ないまま切れた場合（通信断など）
    if (!isFinished) {
      if (hasToken) {
        // 途中まで表示できているので、それは残したまま注記だけ足す
        bubble.textContent += "（回答が途中で切断されました）";
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
