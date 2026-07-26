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
 * メッセージを送信し、AIの回答をストリーミング表示する。
 *
 * 入力:  なし（#chat-input の値を読む）
 * 出力:  なし（画面を書き換える）
 *
 * 処理:
 *   1. 入力文を自分の吹き出しとして表示し、入力欄を空にする
 *   2. AIの吹き出しを「入力中...」で先に作っておく（あとで中身を差し替える）
 *   3. POST /api/chat/stream を叩く
 *   4. 届いたイベントを種別ごとに処理する
 *      sources … 参照ソースIDを受け取る（表示は今後の課題。要素に持たせておく）
 *      token   … 吹き出しに文字を追記する
 *      done    … 完了。ローディング状態を解除する
 *      error   … エラー文言を吹き出しに表示する
 *   5. 最後に必ず送信中フラグを戻す
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
    // 3. ストリーミング用のAPIを叩く。
    //    認証はログイン時に localStorage へ保存したトークンを Authorization で送る（他APIと同じ）
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("token")}`,
      },
      body: JSON.stringify({ question: text }),
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

    // 4. イベントを種別ごとに処理する
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
    // 5. 成功・失敗にかかわらず送信中フラグを戻す（戻さないと二度と送信できなくなる）
    isSending = false;
  }
}
