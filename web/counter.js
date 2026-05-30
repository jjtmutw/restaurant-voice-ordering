const state = {
  client: null,
  orders: [],
};

const elements = {
  brokerUrl: document.querySelector("#counter-broker-url"),
  topic: document.querySelector("#counter-topic"),
  connectButton: document.querySelector("#counter-connect-button"),
  clearButton: document.querySelector("#counter-clear-button"),
  status: document.querySelector("#counter-status"),
  orderFeed: document.querySelector("#order-feed"),
};

function currency(value) {
  return new Intl.NumberFormat("zh-TW", {
    style: "currency",
    currency: "TWD",
    maximumFractionDigits: 0,
  }).format(value || 0);
}

function renderOrders() {
  if (state.orders.length === 0) {
    elements.orderFeed.innerHTML = '<p class="placeholder-copy">等待新的已確認訂單送進櫃台。</p>';
    return;
  }

  elements.orderFeed.innerHTML = state.orders
    .map((order) => {
      const items = order.items
        .map((item) => `${item.quantity} x ${item.name}${item.options?.length ? ` (${item.options.join("/")})` : ""}`)
        .join("<br>");

      return `
        <article class="ticket-card">
          <strong>${order.order_id}</strong>
          <div class="ticket-items">${items}</div>
          <div class="ticket-meta">
            <span>${currency(order.total)}</span>
            <span>${new Date(order.confirmed_at || order.created_at).toLocaleString("zh-TW")}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function connectCounter() {
  if (state.client) state.client.end(true);

  state.client = mqtt.connect(elements.brokerUrl.value.trim(), {
    clean: true,
    connectTimeout: 5000,
    clientId: `counter_${Math.random().toString(16).slice(2, 10)}`,
  });

  elements.status.textContent = "連線中";

  state.client.on("connect", () => {
    state.client.subscribe(elements.topic.value.trim());
    elements.status.textContent = "已連線";
  });

  state.client.on("message", (_topic, raw) => {
    try {
      const payload = JSON.parse(raw.toString());
      state.orders.unshift(payload.order || payload);
      renderOrders();
    } catch (error) {
      console.error(error);
    }
  });

  state.client.on("error", (error) => {
    elements.status.textContent = `錯誤: ${error.message}`;
  });
}

elements.connectButton.addEventListener("click", connectCounter);
elements.clearButton.addEventListener("click", () => {
  state.orders = [];
  renderOrders();
});

renderOrders();
