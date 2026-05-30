const MQTT_CONFIG = {
  brokerUrl: "wss://broker.emqx.io:8084/mqtt",
  requestTopic: "restaurant/voice/orders/inbox",
  replyPrefix: "restaurant/voice/orders/reply",
  username: "",
  password: "",
};

function mqttBrokerUrls() {
  const urls = ["wss://broker.emqx.io:8084/mqtt"];
  if (window.location.protocol !== "https:") {
    urls.push("ws://broker.emqx.io:8083/mqtt");
  }
  return urls;
}

const MENU_SECTIONS = [
  {
    title: "經典純茶",
    items: [
      "茉莉綠茶 M30 / L35",
      "四季春青茶 M30 / L35",
      "21歲輕烏龍 M30 / L35",
      "高山紅茶 M30 / L35",
      "日安大麥 M30 / L35",
      "仙草蜜 M35 / L40",
      "四季珍椰青 M45 / L50",
    ],
  },
  {
    title: "激推水果茶",
    items: [
      "21歲輕檸烏龍 M45 / L55",
      "檸檬奇遇桔 60",
      "金桔檸檬 M60 / L70",
      "粉角檸檬冬瓜 M55 / L65",
      "百香芒果綠茶 M45 / L55",
      "綠茶養樂多 70",
      "蕎麥冬瓜露 M45 / L50",
    ],
  },
  {
    title: "就愛喝奶茶",
    items: [
      "奶茶三兄弟 70",
      "阿薩姆奶茶 M40 / L50",
      "珍珠／粉角奶茶 M50 / L60",
      "西谷米奶茶 M50 / L60",
      "茉香凍奶綠 M50 / L60",
      "手作仙草凍乳 70",
      "英式鮮奶茶 M55 / L70",
      "珍珠／粉角鮮奶茶 M65 / L75",
    ],
  },
  {
    title: "職人金獎咖啡",
    items: [
      "職人美式 50",
      "職人拿鐵 65",
      "珍珠／粉角職人拿鐵 75",
      "珍珠黑糖拿鐵 80",
      "紅柚香檸美式 80",
      "西西里手搖檸檬美式 80",
      "生椰職人拿鐵 75",
      "粉角生椰拿鐵 85",
    ],
  },
];

const ENDING_PHRASES = ["好了", "就這樣", "完成", "完畢", "以上", "沒了"];
const LISTENING_IDLE_TIMEOUT_MS = 30000;

const state = {
  mqttClient: null,
  recognition: null,
  isListening: false,
  keepListeningSession: false,
  manualStopRequested: false,
  shouldAutoSendOnEnd: false,
  finalTranscript: "",
  interimTranscript: "",
  lastSubmittedText: "",
  listeningIdleTimer: null,
  mqttUrlIndex: 0,
  sessionId: `table-${Math.random().toString(16).slice(2, 10)}`,
  draftOrder: null,
  isAutoConnecting: false,
};

const elements = {
  welcomePanel: document.querySelector("#welcome-panel"),
  orderingShell: document.querySelector("#ordering-shell"),
  startOrderButton: document.querySelector("#start-order-button"),
  sessionIdLabel: document.querySelector("#session-id-label"),
  mqttStatus: document.querySelector("#mqtt-status"),
  speechStatus: document.querySelector("#speech-status"),
  listenButton: document.querySelector("#listen-button"),
  stopButton: document.querySelector("#stop-button"),
  transcriptInput: document.querySelector("#transcript-input"),
  suggestionPanel: document.querySelector("#suggestion-panel"),
  suggestionList: document.querySelector("#suggestion-list"),
  sendButton: document.querySelector("#send-button"),
  clearButton: document.querySelector("#clear-button"),
  conversationLog: document.querySelector("#conversation-log"),
  confirmButton: document.querySelector("#confirm-button"),
  cancelButton: document.querySelector("#cancel-button"),
  summaryLines: document.querySelector("#summary-lines"),
  subtotalValue: document.querySelector("#subtotal-value"),
  taxValue: document.querySelector("#tax-value"),
  serviceValue: document.querySelector("#service-value"),
  totalValue: document.querySelector("#total-value"),
  menuSections: document.querySelector("#menu-sections"),
  orderStatusLabel: document.querySelector("#order-status-label"),
};

function currency(value) {
  return new Intl.NumberFormat("zh-TW", {
    style: "currency",
    currency: "TWD",
    maximumFractionDigits: 0,
  }).format(value || 0);
}

function normalizeSpeechText(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

function stripEndingPhrase(text) {
  let normalized = normalizeSpeechText(text);
  for (const phrase of ENDING_PHRASES) {
    const pattern = new RegExp(`${phrase}[。！!，,、\\s]*$`);
    if (pattern.test(normalized)) {
      normalized = normalized.replace(pattern, "").trim();
      break;
    }
  }
  return normalized;
}

function hasEndingPhrase(text) {
  const normalized = normalizeSpeechText(text);
  return ENDING_PHRASES.some((phrase) => normalized.includes(phrase));
}

function currentTranscript() {
  return normalizeSpeechText(`${state.finalTranscript} ${state.interimTranscript}`);
}

function syncTranscriptInput() {
  elements.transcriptInput.value = currentTranscript();
}

function clearListeningIdleTimer() {
  if (state.listeningIdleTimer) {
    window.clearTimeout(state.listeningIdleTimer);
    state.listeningIdleTimer = null;
  }
}

function resetListeningIdleTimer() {
  clearListeningIdleTimer();
  if (!state.keepListeningSession) return;

  state.listeningIdleTimer = window.setTimeout(() => {
    state.keepListeningSession = false;
    state.manualStopRequested = true;
    state.shouldAutoSendOnEnd = false;
    setSpeechStatus("30 秒內沒有辨識到語音，已退出收音模式");
    if (state.recognition && state.isListening) {
      state.recognition.stop();
    }
  }, LISTENING_IDLE_TIMEOUT_MS);
}

function extractSuggestionCandidates(message) {
  const match = /第1項\s*([^，。]+)(.*)/.exec(message || "");
  if (!match) return [];

  const text = `${match[1]}${match[2]}`;
  const candidates = [];
  const pattern = /第\d+項\s*([^，。]+)/g;
  let patternMatch;
  while ((patternMatch = pattern.exec(text)) !== null) {
    const value = patternMatch[1].trim();
    if (value && value !== "以上皆不是") {
      candidates.push(value);
    }
  }
  return candidates;
}

function renderSuggestionTray(candidates = []) {
  if (!elements.suggestionPanel || !elements.suggestionList) return;

  elements.suggestionList.innerHTML = "";
  if (!Array.isArray(candidates) || candidates.length === 0) {
    elements.suggestionPanel.classList.add("hidden");
    return;
  }

  candidates.forEach((candidate, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion-chip";
    const selectionLabel = `第${index + 1}項`;
    button.textContent = `${selectionLabel} ${candidate}`;
    button.addEventListener("click", () => applySuggestionSelection(selectionLabel));
    elements.suggestionList.appendChild(button);
  });

  const noneButton = document.createElement("button");
  noneButton.type = "button";
  noneButton.className = "suggestion-chip secondary-chip";
  noneButton.textContent = `第${candidates.length + 1}項 以上皆不是`;
  noneButton.addEventListener("click", () => applySuggestionSelection("以上皆不是"));
  elements.suggestionList.appendChild(noneButton);

  elements.suggestionPanel.classList.remove("hidden");
}

function applySuggestionSelection(label) {
  elements.transcriptInput.value = label;
  sendUtterance(label);
}

function addMessage(role, message) {
  const row = document.createElement("article");
  const label = role === "user" ? "客戶" : role === "assistant" ? "系統" : "狀態";
  row.className = `message ${role}`;
  row.innerHTML = `<strong>${label}</strong><span>${message}</span>`;
  elements.conversationLog.appendChild(row);
  elements.conversationLog.scrollTop = elements.conversationLog.scrollHeight;
}

function setMqttStatus(text) {
  elements.mqttStatus.textContent = text;
}

function setSpeechStatus(text) {
  elements.speechStatus.textContent = text;
}

function speak(text, onEnd) {
  if (!text) {
    if (typeof onEnd === "function") onEnd();
    return;
  }

  if (!("speechSynthesis" in window)) {
    if (typeof onEnd === "function") onEnd();
    return;
  }

  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "zh-TW";
  utterance.rate = 1;
  if (typeof onEnd === "function") {
    utterance.onend = () => onEnd();
    utterance.onerror = () => onEnd();
  }
  window.speechSynthesis.speak(utterance);
}

function replyTopic() {
  return `${MQTT_CONFIG.replyPrefix}/${state.sessionId}`;
}

function publish(payload) {
  if (!state.mqttClient || !state.mqttClient.connected) {
    addMessage("system", "尚未連上後端，請稍候幾秒後再送出。");
    return;
  }

  state.mqttClient.publish(MQTT_CONFIG.requestTopic, JSON.stringify(payload));
}

function requestDeleteItem(itemIndex) {
  publish({
    action: "delete_order_item",
    session_id: state.sessionId,
    item_index: itemIndex,
    timestamp: new Date().toISOString(),
  });
}

function renderMenu() {
  elements.menuSections.innerHTML = MENU_SECTIONS.map(
    (section) => `
      <section class="menu-section">
        <h4>${section.title}</h4>
        <div class="menu-items">
          ${section.items.map((item) => `<div class="menu-item">${item}</div>`).join("")}
        </div>
      </section>
    `,
  ).join("");
}

function renderOrderPanel(order, type = "draft") {
  state.draftOrder = order;

  if (!order || !order.items || order.items.length === 0) {
    elements.orderStatusLabel.textContent = "尚未建立";
    elements.summaryLines.innerHTML =
      '<p class="placeholder-copy">系統整理好的飲料、杯型、甜度、冰量和加料會顯示在這裡。</p>';
    elements.subtotalValue.textContent = currency(0);
    elements.taxValue.textContent = currency(0);
    elements.serviceValue.textContent = currency(0);
    elements.totalValue.textContent = currency(0);
    return;
  }

  const statusMap = {
    assistant_reply: "整理中",
    confirm_requested: "等待確認",
    order_confirmed: "已確認送出",
    order_cancelled: "已取消",
    clarification: "待補充",
    draft: "整理中",
  };

  elements.orderStatusLabel.textContent = statusMap[type] || "整理中";
  elements.summaryLines.innerHTML = order.items
    .map((item, index) => {
      const meta = [];
      if (item.options?.length) meta.push(item.options.join(" / "));
      if (item.notes) meta.push(item.notes);

      return `
        <div class="summary-line">
          <div class="summary-main">
            <div class="summary-head">
              <button class="summary-delete" type="button" data-item-index="${index + 1}">刪除</button>
              <span class="summary-index">${index + 1}.</span>
              <strong class="summary-name">${item.name}</strong>
              <span class="summary-qty">x ${item.quantity}</span>
              <span class="summary-price-mobile">${currency(item.subtotal)}</span>
            </div>
            <div class="summary-meta">${meta.join(" | ") || "標準配方"}</div>
          </div>
          <span class="summary-price">${currency(item.subtotal)}</span>
        </div>
      `;
    })
    .join("");

  elements.summaryLines.querySelectorAll(".summary-delete").forEach((button) => {
    button.addEventListener("click", () => {
      const itemIndex = Number(button.dataset.itemIndex || "0");
      if (itemIndex > 0) {
        requestDeleteItem(itemIndex);
      }
    });
  });

  elements.subtotalValue.textContent = currency(order.subtotal);
  elements.taxValue.textContent = currency(order.tax);
  elements.serviceValue.textContent = currency(order.service_charge);
  elements.totalValue.textContent = currency(order.total);
}

function shouldAutoListenAfterServerMessage(payload, candidates) {
  if (Array.isArray(candidates) && candidates.length > 0) {
    return true;
  }

  return payload?.type === "clarification" || payload?.type === "confirm_requested";
}

function handleServerMessage(payload) {
  if (payload.message) {
    const candidates =
      Array.isArray(payload.suggestions) && payload.suggestions.length
        ? payload.suggestions
        : extractSuggestionCandidates(payload.message);
    renderSuggestionTray(candidates);
    addMessage("assistant", payload.message);
    speak(payload.message, () => {
      if (shouldAutoListenAfterServerMessage(payload, candidates) && !state.isListening) {
        startListening();
      }
    });
  }

  if (payload.order) {
    renderOrderPanel(payload.order, payload.type);
  }

  if (payload.type === "order_confirmed") {
    addMessage("system", `訂單已成立，單號 ${payload.order?.order_id || "-"}`);
  }
}

function connectMqtt() {
  if (state.mqttClient?.connected || state.isAutoConnecting) return;

  if (state.mqttClient) {
    state.mqttClient.removeAllListeners();
    state.mqttClient.end(true);
    state.mqttClient = null;
  }

  state.isAutoConnecting = true;
  const urls = mqttBrokerUrls();
  const brokerUrl = urls[state.mqttUrlIndex] || urls[0];
  setMqttStatus(`連線中`);

  const options = {
    clean: true,
    connectTimeout: 5000,
    reconnectPeriod: 2000,
    resubscribe: true,
    clientId: `restaurant_web_${Math.random().toString(16).slice(2, 10)}`,
  };

  if (MQTT_CONFIG.username) options.username = MQTT_CONFIG.username;
  if (MQTT_CONFIG.password) options.password = MQTT_CONFIG.password;

  state.mqttClient = mqtt.connect(brokerUrl, options);

  state.mqttClient.on("connect", () => {
    state.isAutoConnecting = false;
    state.mqttUrlIndex = 0;
    state.mqttClient.subscribe(replyTopic());
    setMqttStatus("已連線");
    addMessage("system", "已連上點餐後端，可以開始語音點單。");
  });

  state.mqttClient.on("message", (_topic, raw) => {
    try {
      handleServerMessage(JSON.parse(raw.toString()));
    } catch (error) {
      console.error(error);
    }
  });

  state.mqttClient.on("error", (error) => {
    setMqttStatus("連線失敗，準備重試");
    addMessage("system", `MQTT 連線失敗：${error.message}`);
  });

  state.mqttClient.on("reconnect", () => {
    setMqttStatus("重新連線中");
  });

  state.mqttClient.on("offline", () => {
    setMqttStatus("離線，正在重連");
  });

  state.mqttClient.on("close", () => {
    state.isAutoConnecting = false;
    const shouldTryNextUrl = !state.mqttClient?.connected && state.mqttUrlIndex + 1 < mqttBrokerUrls().length;
    if (shouldTryNextUrl) {
      state.mqttUrlIndex += 1;
      setMqttStatus("切換備援 MQTT 中");
      window.setTimeout(connectMqtt, 250);
      return;
    }
    setMqttStatus("已斷線，持續重連中");
  });
}

function createRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    addMessage("system", "目前瀏覽器不支援語音辨識，請改用 Chrome 或 Edge。");
    return null;
  }

  const recognition = new Recognition();
  recognition.lang = "zh-TW";
  recognition.continuous = true;
  recognition.interimResults = true;

  recognition.onstart = () => {
    state.isListening = true;
    resetListeningIdleTimer();
    setSpeechStatus('持續收音中，說「好了」或「完成」即可送出');
  };

  recognition.onresult = (event) => {
    let receivedTranscript = false;
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const transcript = normalizeSpeechText(event.results[i][0]?.transcript || "");
      if (!transcript) continue;
      receivedTranscript = true;

      if (event.results[i].isFinal) {
        state.finalTranscript = normalizeSpeechText(`${state.finalTranscript} ${transcript}`);
        state.interimTranscript = "";
      } else {
        state.interimTranscript = transcript;
      }
    }

    if (receivedTranscript) {
      resetListeningIdleTimer();
    }

    syncTranscriptInput();

    if (!state.shouldAutoSendOnEnd && hasEndingPhrase(currentTranscript())) {
      clearListeningIdleTimer();
      state.shouldAutoSendOnEnd = true;
      setSpeechStatus("已聽到結尾，正在整理並送出");
      recognition.stop();
    }
  };

  recognition.onerror = (event) => {
    state.isListening = false;
    clearListeningIdleTimer();
    if (event.error === "aborted" && state.manualStopRequested) {
      setSpeechStatus("已停止收音");
      return;
    }

    if (state.keepListeningSession && !state.shouldAutoSendOnEnd) {
      setSpeechStatus("收音短暫中斷，正在重新啟動");
      window.setTimeout(() => {
        if (!state.isListening && state.keepListeningSession) {
          state.recognition?.start();
        }
      }, 250);
      return;
    }

    state.keepListeningSession = false;
    state.shouldAutoSendOnEnd = false;
    setSpeechStatus(`語音辨識異常：${event.error}`);
  };

  recognition.onend = () => {
    state.isListening = false;
    clearListeningIdleTimer();
    const mergedText = stripEndingPhrase(currentTranscript());

    if (state.shouldAutoSendOnEnd && mergedText) {
      state.keepListeningSession = false;
      state.manualStopRequested = false;
      state.shouldAutoSendOnEnd = false;
      elements.transcriptInput.value = mergedText;
      state.finalTranscript = "";
      state.interimTranscript = "";
      setSpeechStatus("已自動送出本句需求");
      sendUtterance(mergedText);
      return;
    }

    if (state.keepListeningSession && !state.manualStopRequested) {
      setSpeechStatus('持續收音中，說「好了」或「完成」即可送出');
      window.setTimeout(() => {
        if (!state.isListening && state.keepListeningSession) {
          state.recognition?.start();
        }
      }, 250);
      return;
    }

    state.finalTranscript = "";
    state.interimTranscript = "";
    state.keepListeningSession = false;
    state.manualStopRequested = false;
    state.shouldAutoSendOnEnd = false;
    setSpeechStatus("待命中");
  };

  return recognition;
}

function startListening() {
  if (!state.recognition) state.recognition = createRecognition();
  if (!state.recognition || state.isListening) return;

  elements.transcriptInput.value = "";
  state.keepListeningSession = true;
  state.manualStopRequested = false;
  state.finalTranscript = "";
  state.interimTranscript = "";
  state.shouldAutoSendOnEnd = false;
  state.recognition.start();
}

function stopListening() {
  clearListeningIdleTimer();
  state.keepListeningSession = false;
  state.manualStopRequested = true;
  state.shouldAutoSendOnEnd = false;
  if (state.recognition && state.isListening) {
    state.recognition.stop();
    return;
  }
  setSpeechStatus("待命中");
}

function sendUtterance(textOverride = "") {
  const text = stripEndingPhrase(textOverride || elements.transcriptInput.value);
  if (!text) {
    addMessage("system", "請先輸入或說出本句需求，再送出。");
    return;
  }

  clearListeningIdleTimer();
  state.lastSubmittedText = text;
  renderSuggestionTray([]);
  elements.transcriptInput.value = text;
  addMessage("user", text);
  publish({
    action: "customer_utterance",
    session_id: state.sessionId,
    text,
    language: "zh-TW",
    source: "customer-web",
    timestamp: new Date().toISOString(),
  });
}

function startOrderFlow() {
  elements.welcomePanel.classList.add("hidden");
  elements.orderingShell.classList.remove("hidden");
  elements.sessionIdLabel.textContent = state.sessionId;
  connectMqtt();
  const greeting = "歡迎來到 CoCo 點單工作台，請直接說出想喝的飲料與需求。";
  addMessage("assistant", greeting);
  speak(greeting, () => {
    if (!state.isListening) {
      startListening();
    }
  });
}

elements.startOrderButton.addEventListener("click", startOrderFlow);
elements.listenButton.addEventListener("click", startListening);
elements.stopButton.addEventListener("click", stopListening);
elements.sendButton.addEventListener("click", () => sendUtterance());
elements.clearButton.addEventListener("click", () => {
  clearListeningIdleTimer();
  elements.transcriptInput.value = "";
  renderSuggestionTray([]);
  state.keepListeningSession = false;
  state.manualStopRequested = false;
  state.finalTranscript = "";
  state.interimTranscript = "";
  state.shouldAutoSendOnEnd = false;
});
elements.confirmButton.addEventListener("click", () =>
  publish({
    action: "confirm_order",
    session_id: state.sessionId,
    timestamp: new Date().toISOString(),
  }),
);
elements.cancelButton.addEventListener("click", () =>
  publish({
    action: "cancel_order",
    session_id: state.sessionId,
    timestamp: new Date().toISOString(),
  }),
);

renderMenu();
renderOrderPanel(null);
