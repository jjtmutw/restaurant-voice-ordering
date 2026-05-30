# Restaurant Voice Ordering

這是一個全新的語音點餐專案，和原本的 `voice-mqtt-desktop` 分開。現在的範例菜單已改成 `CoCo都可 圓通店`，可以直接拿來示範飲料語音點單流程。

1. 前端網頁播放歡迎語音
2. 瀏覽器進行語音轉文字
3. 文字透過 MQTT 傳給 Python 後端
4. 後端用 ChatGPT 或規則引擎理解客戶需求
5. 回傳訂單 JSON、金額與確認訊息
6. 客戶確認後，把訂單送到櫃台平板與熱感印表機佇列

## 專案結構

- `web/index.html`: 客戶語音點餐頁
- `web/counter.html`: 櫃台平板頁
- `server/app.py`: MQTT + OpenAI + 菜單語意解析後端
- `server/menu.json`: CoCo 範例菜單與選項規則
- `skills/restaurant-menu-ordering/SKILL.md`: 菜單語意 skill 規格

## MQTT Topic 設計

- 客戶送出: `restaurant/voice/orders/inbox`
- 客戶回覆: `restaurant/voice/orders/reply/{session_id}`
- 櫃台平板: `restaurant/voice/orders/counter`
- 印表機事件: `restaurant/voice/orders/print`

## 啟動方式

### 1. 啟動 Python 後端

```powershell
cd restaurant-voice-ordering\server
copy .env.example .env
pip install -r requirements.txt
python app.py
```

如果要啟用 ChatGPT 語意解析，請在 `.env` 填入 `OPENAI_API_KEY`。
沒填也能跑，系統會自動退回規則式解析。

目前規則式解析已支援：

- CoCo 菜單品項別名
- `M / L` 杯型價格
- 甜度與冰量
- 常見加料，像是珍珠、粉角、布丁、仙草、西谷米、茉香茶凍
- 加料 `+10` 的帳單計算

### 2. 開啟前端點餐頁

把 `restaurant-voice-ordering\web\` 用任一靜態伺服器開起來，然後在平板或手機打開 `index.html`。

若要測試櫃台頁，另開 `counter.html`。

## 訂單 JSON 範例

```json
{
  "session_id": "table-ab12cd34",
  "status": "confirmed",
  "items": [
    {
      "id": "burger_beef_combo",
      "name": "牛肉起司堡套餐",
      "quantity": 2,
      "unit_price": 185,
      "options": [],
      "notes": "",
      "subtotal": 370
    }
  ],
  "subtotal": 370,
  "tax": 19,
  "service_charge": 37,
  "total": 426,
  "currency": "TWD"
}
```

## 熱感印表機

目前 `server/app.py` 會同時產生兩種列印輸出：

- `server/data/print_jobs.jsonl`：可閱讀的列印紀錄
- `server/data/print_jobs/{order_id}.bin`：ESC/POS 二進位資料，可直接送熱感印表機

預設 `PRINTER_MODE=file`，只會把 ESC/POS 檔案寫到磁碟。

如果你要直接送網路熱感印表機，可在 `.env` 設定：

```env
PRINTER_MODE=tcp
PRINTER_HOST=192.168.1.50
PRINTER_PORT=9100
```

下一步你可以把這段換成：

- USB / COM 埠 ESC/POS 印表機輸出
- 網路型熱感印表機 TCP 列印
- 櫃台系統 API

## 客製化菜單

只要修改 `server/menu.json`：

- 換掉 `items`
- 調整 `price`
- 加入 `aliases`
- 設定 `option_rules`

這樣同一套前後端就能套到不同餐廳。
