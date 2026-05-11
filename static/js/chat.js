// Founder - 社員用チャット

// メッセージを送信する関数
async function sendMessage() {
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  const container = document.getElementById("chat-messages");

  // ユーザーのメッセージを右側に表示
  container.innerHTML += `
    <div class="msg user">
      <div class="msg-avatar">自</div>
      <div class="msg-bubble">${text}</div>
    </div>`;
  input.value = "";
  container.scrollTop = container.scrollHeight;

  // ローディング表示
  container.innerHTML += `
    <div class="msg ai" id="loading">
      <div class="msg-avatar">F</div>
      <div class="msg-bubble">入力中...</div>
    </div>`;
  container.scrollTop = container.scrollHeight;

  // バックエンドのAPIを呼び出す
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text }),
  });
  const data = await res.json();

  // ローディングを消してAIの返答を左側に表示
  document.getElementById("loading").remove();
  container.innerHTML += `
    <div class="msg ai">
      <div class="msg-avatar">F</div>
      <div class="msg-bubble">${data.reply}</div>
    </div>`;
  container.scrollTop = container.scrollHeight;
}