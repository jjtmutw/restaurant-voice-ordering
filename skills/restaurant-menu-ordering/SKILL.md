# Restaurant Menu Ordering Skill

這是一個給語音點餐系統使用的本地 skill 規格，目的是讓後端在接 OpenAI 或其他 LLM 時，有一致的菜單映射方式。
目前範例菜單以 `CoCo都可 圓通店` 為主。

## 目標

- 讀取 `server/menu.json`
- 將語音文字轉成結構化訂單 JSON
- 如果品項不明、數量不明、必要選項缺失，優先回傳追問
- 確認後產生可結帳的正式訂單

## 輸入

```json
{
  "session_id": "table-ab12cd34",
  "customer_utterance": "我要兩份牛肉起司堡套餐，再一杯冰紅茶微糖",
  "current_order": {}
}
```

## 輸出

```json
{
  "action": "update_order",
  "message": "目前有兩份牛肉起司堡套餐和一杯冰紅茶微糖。",
  "items": [
    {
      "id": "burger_beef_combo",
      "quantity": 2,
      "options": [],
      "notes": ""
    },
    {
      "id": "black_tea",
      "quantity": 1,
      "options": ["冰", "微糖", "中杯"],
      "notes": ""
    }
  ]
}
```

## 規則

- `action` 只能是 `update_order`、`clarify`、`confirm`
- `items[].id` 必須存在於 `server/menu.json`
- `quantity` 必須是正整數
- `options` 必須使用菜單中的正式標籤，不要輸出自由文字
- 若客戶只說「我要一杯紅茶」但未指定冰熱或糖度，可由後端使用預設值，或改成追問模式
- 若菜單有 `M / L` 價差或加料加價，請把杯型與加料一起放進 `options`

## 延伸

- 若要換另一家餐廳，只要重寫 `server/menu.json`
- 若要升級成 MCP server，可把這份 skill 的輸入輸出做成一個 `parse_order` 工具端點
