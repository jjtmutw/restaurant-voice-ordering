from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import socket
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import paho.mqtt.client as mqtt
import requests


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MENU_PATH = BASE_DIR / "menu.json"
ORDERS_PATH = DATA_DIR / "orders.jsonl"
PRINT_JOBS_PATH = DATA_DIR / "print_jobs.jsonl"
PRINT_BIN_DIR = DATA_DIR / "print_jobs"


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def chinese_quantity_to_int(text: str) -> int:
    mapping = {
        "一": 1,
        "二": 2,
        "兩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    return mapping.get(text, 1)


def detect_quantity(text: str, alias_position: int) -> int:
    left = text[max(0, alias_position - 12):alias_position]
    digit_match = re.search(r"(\d+)\s*(杯|份|個)?$", left)
    if digit_match:
        return max(1, int(digit_match.group(1)))

    char_match = re.search(r"([一二兩三四五六七八九十])\s*(杯|份|個)?$", left)
    if char_match:
        return chinese_quantity_to_int(char_match.group(1))

    return 1


def build_suggestion_prompt(suggestions: list[str]) -> str:
    numbered = [f"第{index}項 {name}" for index, name in enumerate(suggestions, start=1)]
    numbered.append(f"第{len(suggestions) + 1}項 以上皆不是")
    return "我沒有完全聽清楚品項，請直接回答：" + "，".join(numbered) + "。"


def parse_suggestion_selection(text: str) -> int | None:
    normalized = normalize_text(text)
    if "以上皆不是" in normalized:
        return -1

    digit_match = re.search(r"第\s*(\d+)\s*項", text)
    if digit_match:
        return int(digit_match.group(1))

    chinese_match = re.search(r"第\s*([一二兩三四五六七八九十])\s*項", text)
    if chinese_match:
        return chinese_quantity_to_int(chinese_match.group(1))

    return None


def parse_order_item_selection(text: str) -> int | None:
    digit_match = re.search(r"第\s*(\d+)\s*項", text)
    if digit_match:
        return int(digit_match.group(1))

    chinese_match = re.search(r"第\s*([一二兩三四五六七八九十])\s*項", text)
    if chinese_match:
        return chinese_quantity_to_int(chinese_match.group(1))

    return None


def replace_drink_name_in_text(text: str, candidate: str) -> str:
    base_text = text.strip()
    if not base_text:
        return candidate

    marker_pattern = re.compile(
        r"(但最後一杯|最後一杯|最後那杯|再加一杯|再來一杯|再一杯|還要一杯|我要一杯|來一杯|點一杯|一杯)"
    )
    replace_start = 0
    for match in marker_pattern.finditer(base_text):
        replace_start = match.end()

    leading_text = base_text[:replace_start]
    trailing_text = base_text[replace_start:]
    stripped_trailing = trailing_text.lstrip()
    leading_whitespace = trailing_text[: len(trailing_text) - len(stripped_trailing)]
    option_pattern = re.compile(
        r"(大杯|中杯|熱的|溫的|常溫|去冰|少冰|微冰|正常冰|無糖|少糖|半糖|微糖|全糖|加珍珠|加布丁|加仙草|加粉角|加西谷米|加椰果|珍珠|布丁|仙草|粉角|西谷米|椰果)"
    )
    option_match = option_pattern.search(stripped_trailing)
    if not option_match:
        return f"{leading_text}{leading_whitespace}{candidate}".strip()

    return f"{leading_text}{leading_whitespace}{candidate}{stripped_trailing[option_match.start():]}".strip()


@dataclass
class DraftOrder:
    session_id: str
    status: str = "draft"
    items: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pending_suggestions: list[str] = field(default_factory=list)
    pending_suggestion_source_text: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    order_id: str | None = None
    confirmed_at: str | None = None
    subtotal: int = 0
    tax: int = 0
    service_charge: int = 0
    total: int = 0
    currency: str = "TWD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "items": deepcopy(self.items),
            "notes": list(self.notes),
            "pending_suggestions": list(self.pending_suggestions),
            "pending_suggestion_source_text": self.pending_suggestion_source_text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "order_id": self.order_id,
            "confirmed_at": self.confirmed_at,
            "subtotal": self.subtotal,
            "tax": self.tax,
            "service_charge": self.service_charge,
            "total": self.total,
            "currency": self.currency,
        }


class MenuCatalog:
    def __init__(self, path: pathlib.Path) -> None:
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.restaurant_name = self.raw.get("restaurant_name", "Restaurant")
        self.currency = self.raw.get("currency", "TWD")
        self.tax_rate = float(self.raw.get("tax_rate", 0))
        self.service_charge_rate = float(self.raw.get("service_charge_rate", 0))
        self.items = self.raw.get("items", [])
        self.option_rules = self.raw.get("option_rules", {})

    def extract_options(self, item: dict[str, Any], normalized_text: str, include_defaults: bool = True) -> list[str]:
        detected: list[str] = []
        for rule_name in item.get("option_rule_ids", []):
            rule = self.option_rules.get(rule_name, {})

            ranked_matches: list[tuple[int, str]] = []
            for choice in rule.get("choices", []):
                candidates = [str(choice["label"])] + [str(alias) for alias in choice.get("aliases", [])]
                normalized_candidates = [normalize_text(candidate) for candidate in candidates if candidate]
                longest_match = max(
                    (len(candidate) for candidate in normalized_candidates if candidate and candidate in normalized_text),
                    default=0,
                )
                if longest_match > 0:
                    ranked_matches.append((longest_match, choice["label"]))

            ranked_matches.sort(key=lambda item: item[0], reverse=True)
            matched_labels = [label for _, label in ranked_matches]

            if matched_labels and rule.get("multi"):
                for label in matched_labels:
                    if label not in detected:
                        detected.append(label)
            elif matched_labels:
                detected.append(matched_labels[0])
            elif include_defaults and rule.get("default"):
                detected.append(str(rule["default"]))
        return detected

    def option_labels_for_rule(self, rule_name: str) -> set[str]:
        rule = self.option_rules.get(rule_name, {})
        return {str(choice["label"]) for choice in rule.get("choices", [])}

    def merge_options(self, item: dict[str, Any], base_options: list[str], override_options: list[str]) -> list[str]:
        merged = list(base_options)

        for rule_name in item.get("option_rule_ids", []):
            rule = self.option_rules.get(rule_name, {})
            labels = self.option_labels_for_rule(rule_name)
            matching_overrides = [option for option in override_options if option in labels]
            if not matching_overrides:
                continue

            merged = [option for option in merged if option not in labels]
            if rule.get("multi"):
                for option in matching_overrides:
                    if option not in merged:
                        merged.append(option)
            else:
                merged.append(matching_overrides[0])

        return merged

    def extract_global_options(self, item: dict[str, Any], normalized_text: str) -> list[str]:
        markers = ["全部都要", "全都要", "都要", "全部", "都用", "都改成", "都改為"]
        marker_positions = [normalized_text.rfind(normalize_text(marker)) for marker in markers]
        marker_positions = [position for position in marker_positions if position >= 0]
        if not marker_positions:
            return []

        global_segment = normalized_text[max(marker_positions):]
        return self.extract_options(item, global_segment, include_defaults=False)

    def extract_last_item_options(self, item: dict[str, Any], normalized_text: str) -> list[str]:
        markers = [
            "但最後一杯",
            "最後一杯",
            "最後那杯",
            "最後一個",
            "最後那個",
            "最後一杯要",
            "最後那杯要",
            "除了最後一杯",
            "除了最後那杯",
            "最後一杯除外",
            "最後那杯除外",
        ]
        marker_positions = [normalized_text.rfind(normalize_text(marker)) for marker in markers]
        marker_positions = [position for position in marker_positions if position >= 0]
        if not marker_positions:
            return []

        last_segment = normalized_text[max(marker_positions):]
        return self.extract_options(item, last_segment, include_defaults=False)

    def excludes_last_item_from_global(self, normalized_text: str) -> bool:
        markers = ["除了最後一杯", "除了最後那杯", "最後一杯除外", "最後那杯除外"]
        return any(normalize_text(marker) in normalized_text for marker in markers)

    def detect_size(self, item: dict[str, Any], options: list[str]) -> str | None:
        if "sizes" not in item:
            return None
        for option in options:
            if option in item["sizes"]:
                return option
        return next(iter(item["sizes"].keys()))

    def resolve_unit_price(self, item: dict[str, Any], size_label: str | None) -> int:
        if "sizes" in item:
            if size_label and size_label in item["sizes"]:
                return int(item["sizes"][size_label])
            return int(next(iter(item["sizes"].values())))
        return int(item["price"])

    def resolve_option_price(self, item: dict[str, Any], selected_options: list[str]) -> int:
        total = 0
        for rule_name in item.get("option_rule_ids", []):
            rule = self.option_rules.get(rule_name, {})
            for choice in rule.get("choices", []):
                if choice["label"] in selected_options:
                    total += int(choice.get("price_delta", 0))
        return total

    def item_aliases(self, item: dict[str, Any]) -> list[str]:
        return [normalize_text(item["name"])] + [normalize_text(alias) for alias in item.get("aliases", [])]

    def find_item_mentions(self, normalized_text: str) -> list[dict[str, Any]]:
        mentions: list[dict[str, Any]] = []

        for item in self.items:
            aliases = [normalize_text(item["name"])] + [normalize_text(alias) for alias in item.get("aliases", [])]
            for alias in {alias for alias in aliases if alias}:
                for match in re.finditer(re.escape(alias), normalized_text):
                    mentions.append(
                        {
                            "item": item,
                            "alias": alias,
                            "start": match.start(),
                            "end": match.end(),
                            "length": len(alias),
                        }
                    )

        mentions.sort(key=lambda mention: (mention["start"], -mention["length"]))

        selected: list[dict[str, Any]] = []
        occupied_until = -1
        for mention in mentions:
            if mention["start"] < occupied_until:
                continue
            selected.append(mention)
            occupied_until = mention["end"]

        return selected

    def find_fuzzy_item_mentions(self, normalized_text: str, min_score: float = 0.78) -> list[dict[str, Any]]:
        fuzzy_matches: list[dict[str, Any]] = []
        text_length = len(normalized_text)
        if text_length < 2:
            return fuzzy_matches

        for item in self.items:
            best_match: dict[str, Any] | None = None
            for alias in {alias for alias in self.item_aliases(item) if len(alias) >= 2}:
                alias_length = len(alias)
                min_window = max(2, alias_length - 1)
                max_window = min(text_length, alias_length + 1)
                for window_size in range(min_window, max_window + 1):
                    for start in range(0, text_length - window_size + 1):
                        chunk = normalized_text[start:start + window_size]
                        score = SequenceMatcher(None, alias, chunk).ratio()
                        if score < min_score:
                            continue
                        if best_match is None or score > best_match["score"]:
                            best_match = {
                                "item": item,
                                "alias": alias,
                                "start": start,
                                "end": start + window_size,
                                "length": alias_length,
                                "score": score,
                            }

            if best_match:
                fuzzy_matches.append(best_match)

        fuzzy_matches.sort(key=lambda match: (-match["score"], match["start"]))
        return fuzzy_matches

    def suggest_items(self, text: str, limit: int = 3) -> list[str]:
        normalized = normalize_text(text)
        suggestions: list[tuple[float, str]] = []
        for match in self.find_fuzzy_item_mentions(normalized, min_score=0.7):
            suggestions.append((float(match["score"]), str(match["item"]["name"])))

        seen: set[str] = set()
        ordered: list[str] = []
        for _, name in sorted(suggestions, key=lambda item: item[0], reverse=True):
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)
            if len(ordered) >= limit:
                break
        return ordered

    def find_items(self, text: str) -> list[dict[str, Any]]:
        normalized = normalize_text(text)
        found: list[dict[str, Any]] = []

        mentions = self.find_item_mentions(normalized)
        if not mentions:
            fuzzy_mentions = self.find_fuzzy_item_mentions(normalized, min_score=0.86)
            if fuzzy_mentions:
                fuzzy_mentions.sort(key=lambda mention: mention["start"])
                mentions = fuzzy_mentions
        is_multi_item_utterance = len(mentions) > 1
        for index, mention in enumerate(mentions):
            item = mention["item"]
            position = int(mention["start"])
            next_start = len(normalized) if index + 1 >= len(mentions) else int(mentions[index + 1]["start"])
            segment_start = max(0, position - 12)
            segment_text = normalized[segment_start:next_start]

            quantity = detect_quantity(normalized, position)
            options = self.extract_options(item, segment_text)
            global_options = self.extract_global_options(item, normalized)
            skip_global_for_last_item = (
                is_multi_item_utterance
                and index == len(mentions) - 1
                and self.excludes_last_item_from_global(normalized)
            )
            if global_options and not skip_global_for_last_item:
                options = self.merge_options(item, options, global_options)
            if is_multi_item_utterance and index == len(mentions) - 1:
                last_item_options = self.extract_last_item_options(item, normalized)
                if last_item_options:
                    options = self.merge_options(item, options, last_item_options)
            size_label = self.detect_size(item, options)
            unit_price = self.resolve_unit_price(item, size_label)
            option_price = self.resolve_option_price(item, options)

            found.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "option_price": option_price,
                    "options": options,
                    "notes": "",
                    "subtotal": quantity * (unit_price + option_price),
                }
            )

        return found

    def calculate_totals(self, order: DraftOrder) -> DraftOrder:
        subtotal = sum(int(item["subtotal"]) for item in order.items)
        order.subtotal = subtotal
        order.tax = round(subtotal * self.tax_rate)
        order.service_charge = round(subtotal * self.service_charge_rate)
        order.total = order.subtotal + order.tax + order.service_charge
        order.currency = self.currency
        order.updated_at = utc_now()
        return order

    def merge_items(self, order: DraftOrder, items: list[dict[str, Any]]) -> DraftOrder:
        for incoming in items:
            existing = next(
                (
                    item
                    for item in order.items
                    if item["id"] == incoming["id"] and item.get("options", []) == incoming.get("options", [])
                ),
                None,
            )

            if existing:
                existing["quantity"] += incoming["quantity"]
                existing["subtotal"] = existing["quantity"] * (existing["unit_price"] + existing.get("option_price", 0))
            else:
                order.items.append(incoming)

        return self.calculate_totals(order)


class OpenAIInterpreter:
    def __init__(self, menu: MenuCatalog) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()
        self.menu = menu
        self.cooldown_seconds = int(os.environ.get("OPENAI_RATE_LIMIT_COOLDOWN_SECONDS", "300"))
        self.rate_limited_until = 0.0
        self.rate_limit_logged = False

    def available(self) -> bool:
        return bool(self.api_key)

    def should_skip(self) -> bool:
        if time.time() < self.rate_limited_until:
            if not self.rate_limit_logged:
                remaining = max(1, int(self.rate_limited_until - time.time()))
                logging.info(
                    "OpenAI cooldown active for %s more seconds; using rule-based parser",
                    remaining,
                )
                self.rate_limit_logged = True
            return True

        self.rate_limit_logged = False
        return False

    def mark_rate_limited(self) -> None:
        self.rate_limited_until = time.time() + self.cooldown_seconds
        self.rate_limit_logged = False

    def interpret(self, text: str, order: DraftOrder) -> dict[str, Any] | None:
        if not self.available() or self.should_skip():
            return None

        prompt = {
            "menu": {
                "currency": self.menu.currency,
                "items": self.menu.items,
                "option_rules": self.menu.option_rules,
            },
            "current_order": order.to_dict(),
            "customer_utterance": text,
            "task": (
                "Return JSON only. Fields: action(update_order|clarify|confirm), message, items. "
                "Each item must include id, quantity, options, notes. "
                "Apply size, sweetness, temperature, and add-ons only to the nearest referenced drink "
                "unless the customer explicitly says they apply to all drinks. "
                "If the customer says phrases like 都要, 全部都要, or 全都要, apply that modifier to every drink in this utterance. "
                "If the customer also says phrases like 但最後一杯 or 最後那杯, apply that later modifier only to the last drink and let it override the all-drinks modifier for that drink. "
                "If the customer says 除了最後一杯 or 最後一杯除外, do not apply the all-drinks modifier to the last drink unless they explicitly restate a last-drink modifier."
            ),
        }

        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": "You are a drink ordering parser. Output JSON only."}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}],
                    },
                ],
            },
            timeout=30,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError:
            if response.status_code == 429:
                self.mark_rate_limited()
            raise

        payload = response.json()
        output_text = payload.get("output_text", "").strip()
        return json.loads(output_text) if output_text else None


class EscPosBuilder:
    INIT = b"\x1b@"
    ALIGN_LEFT = b"\x1ba\x00"
    ALIGN_CENTER = b"\x1ba\x01"
    BOLD_ON = b"\x1bE\x01"
    BOLD_OFF = b"\x1bE\x00"
    DOUBLE_ON = b"\x1d!\x11"
    DOUBLE_OFF = b"\x1d!\x00"
    CUT = b"\x1dV\x00"

    @staticmethod
    def text(value: str) -> bytes:
        return value.encode("utf-8", errors="replace")

    def build_ticket(self, restaurant_name: str, order: DraftOrder) -> bytes:
        parts = [self.INIT, self.ALIGN_CENTER, self.BOLD_ON, self.DOUBLE_ON]
        parts.append(self.text(f"{restaurant_name}\n"))
        parts.extend([self.DOUBLE_OFF, self.BOLD_OFF, self.text("語音點單小票\n")])
        parts.append(self.text(f"訂單編號: {order.order_id}\n"))
        parts.append(self.text(f"確認時間: {order.confirmed_at}\n"))
        parts.append(self.text("-" * 32 + "\n"))
        parts.append(self.ALIGN_LEFT)

        for item in order.items:
            parts.append(self.text(f"{item['quantity']} x {item['name']} {item['subtotal']}元\n"))
            if item.get("options"):
                parts.append(self.text(f"  選項: {' / '.join(item['options'])}\n"))
            if item.get("notes"):
                parts.append(self.text(f"  備註: {item['notes']}\n"))

        parts.append(self.text("-" * 32 + "\n"))
        parts.append(self.text(f"小計: {order.subtotal} 元\n"))
        parts.append(self.text(f"稅金: {order.tax} 元\n"))
        parts.append(self.text(f"服務費: {order.service_charge} 元\n"))
        parts.extend([self.BOLD_ON, self.text(f"總計: {order.total} 元\n"), self.BOLD_OFF])
        parts.append(self.text("\n謝謝光臨\n\n\n"))
        parts.append(self.CUT)
        return b"".join(parts)


class PrinterService:
    def __init__(self, restaurant_name: str) -> None:
        self.restaurant_name = restaurant_name
        self.mode = os.environ.get("PRINTER_MODE", "file").strip().lower()
        self.host = os.environ.get("PRINTER_HOST", "").strip()
        self.port = int(os.environ.get("PRINTER_PORT", "9100"))
        self.builder = EscPosBuilder()

    def dispatch(self, order: DraftOrder) -> dict[str, Any]:
        payload_bytes = self.builder.build_ticket(self.restaurant_name, order)
        PRINT_BIN_DIR.mkdir(parents=True, exist_ok=True)
        bin_path = PRINT_BIN_DIR / f"{order.order_id}.bin"
        bin_path.write_bytes(payload_bytes)

        result = {
            "printer_mode": self.mode,
            "printer_bin_path": str(bin_path),
            "bytes": len(payload_bytes),
            "sent_to_device": False,
        }

        if self.mode == "tcp" and self.host:
            with socket.create_connection((self.host, self.port), timeout=5) as sock:
                sock.sendall(payload_bytes)
            result["sent_to_device"] = True

        return result


class OrderService:
    def __init__(self, menu: MenuCatalog) -> None:
        self.menu = menu
        self.interpreter = OpenAIInterpreter(menu)
        self.printer = PrinterService(menu.restaurant_name)
        self.sessions: dict[str, DraftOrder] = {}

    def get_order(self, session_id: str) -> DraftOrder:
        return self.sessions.setdefault(session_id, DraftOrder(session_id=session_id))

    def reset_order(self, session_id: str) -> DraftOrder:
        self.sessions[session_id] = DraftOrder(session_id=session_id)
        return self.sessions[session_id]

    def clear_pending_suggestions(self, order: DraftOrder) -> None:
        order.pending_suggestions = []
        order.pending_suggestion_source_text = ""

    def resolve_pending_suggestion(self, text: str, order: DraftOrder) -> dict[str, Any] | None:
        if not order.pending_suggestions:
            return None

        selection = parse_suggestion_selection(text)
        if selection is None:
            return None

        none_option = len(order.pending_suggestions) + 1
        if selection == -1 or selection == none_option:
            self.clear_pending_suggestions(order)
            return {
                "type": "clarification",
                "message": "收到，這些候選都不是。請再說一次飲料名稱，或直接在文字欄修改後送出。",
                "order": order.to_dict(),
            }

        if 1 <= selection <= len(order.pending_suggestions):
            candidate = order.pending_suggestions[selection - 1]
            source_text = order.pending_suggestion_source_text or text
            corrected_text = replace_drink_name_in_text(source_text, candidate)
            self.clear_pending_suggestions(order)
            return self.apply_rule_based_parse(corrected_text, order)

        return {
            "type": "clarification",
            "message": build_suggestion_prompt(order.pending_suggestions),
            "suggestions": list(order.pending_suggestions),
            "order": order.to_dict(),
        }

    def looks_like_confirmation(self, text: str) -> bool:
        normalized = normalize_text(text)
        keywords = ("確認", "可以", "沒問題", "就這樣", "送出", "結帳")
        return any(normalize_text(keyword) in normalized for keyword in keywords)

    def looks_like_cancel(self, text: str) -> bool:
        normalized = normalize_text(text)
        keywords = ("取消", "不要了", "重來", "清空")
        return any(normalize_text(keyword) in normalized for keyword in keywords)

    def looks_like_delete_item(self, text: str) -> bool:
        normalized = normalize_text(text)
        keywords = ("刪除", "刪掉", "移除", "取消第")
        return any(normalize_text(keyword) in normalized for keyword in keywords) and "項" in text

    def order_message(self, order: DraftOrder) -> str:
        item_summary = "，".join(
            f"第{index}項{item['quantity']}杯{item['name']}{'（' + '/'.join(item['options']) + '）' if item.get('options') else ''}"
            for index, item in enumerate(order.items, start=1)
        )
        return f"目前訂單為 {item_summary}，總計 {order.total} 元。若正確請按確認送單，或直接再說要加點與修改。"

    def delete_order_item(self, session_id: str, item_index: int) -> dict[str, Any]:
        order = self.get_order(session_id)
        if not order.items:
            return {
                "type": "clarification",
                "message": "目前訂單沒有可刪除的品項。",
                "order": order.to_dict(),
            }

        if item_index < 1 or item_index > len(order.items):
            return {
                "type": "clarification",
                "message": f"找不到第 {item_index} 項，請依照帳單上的項次重新指定。",
                "order": order.to_dict(),
            }

        removed_item = order.items.pop(item_index - 1)
        self.clear_pending_suggestions(order)
        updated = self.menu.calculate_totals(order)
        if not updated.items:
            return {
                "type": "assistant_reply",
                "message": f"已刪除第 {item_index} 項 {removed_item['name']}，目前訂單已清空。",
                "order": updated.to_dict(),
            }

        return {
            "type": "assistant_reply",
            "message": f"已刪除第 {item_index} 項 {removed_item['name']}。{self.order_message(updated)}",
            "order": updated.to_dict(),
        }

    def apply_rule_based_parse(self, text: str, order: DraftOrder) -> dict[str, Any]:
        if self.looks_like_delete_item(text):
            item_index = parse_order_item_selection(text)
            if item_index is not None:
                return self.delete_order_item(order.session_id, item_index)

        if self.looks_like_cancel(text):
            self.reset_order(order.session_id)
            return {
                "type": "order_cancelled",
                "message": "好的，這筆訂單已取消。",
                "order": self.get_order(order.session_id).to_dict(),
            }

        if self.looks_like_confirmation(text) and order.items:
            self.clear_pending_suggestions(order)
            return {
                "type": "confirm_requested",
                "message": "收到，如果內容正確，請按確認送單。",
                "order": order.to_dict(),
            }

        items = self.menu.find_items(text)
        if not items:
            suggestions = self.menu.suggest_items(text)
            if suggestions:
                order.pending_suggestions = list(suggestions)
                order.pending_suggestion_source_text = text
                return {
                    "type": "clarification",
                    "message": build_suggestion_prompt(suggestions),
                    "suggestions": list(suggestions),
                    "order": order.to_dict(),
                }
            self.clear_pending_suggestions(order)
            return {
                "type": "clarification",
                "message": "我還不太確定你要哪一杯，請再說一次品項、杯型、甜度冰量，必要時再加上加料。",
                "order": order.to_dict(),
            }

        self.clear_pending_suggestions(order)
        updated = self.menu.merge_items(order, items)
        return {
            "type": "assistant_reply",
            "message": self.order_message(updated),
            "order": updated.to_dict(),
        }

    def handle_utterance(self, text: str, session_id: str) -> dict[str, Any]:
        order = self.get_order(session_id)
        resolved = self.resolve_pending_suggestion(text, order)
        if resolved:
            return resolved
        parsed = None

        try:
            parsed = self.interpreter.interpret(text, order)
        except Exception as error:
            logging.warning("OpenAI interpret failed, fallback to rule parser: %s", error)

        if parsed:
            action = parsed.get("action")

            if action == "clarify":
                return {
                    "type": "clarification",
                    "message": parsed.get("message", "請再確認一次你想點的飲料內容。"),
                    "order": order.to_dict(),
                }

            if action == "confirm" and order.items:
                return {
                    "type": "confirm_requested",
                    "message": parsed.get("message", "如果內容正確，請按確認送單。"),
                    "order": order.to_dict(),
                }

            if action == "update_order":
                items = []
                for raw in parsed.get("items", []):
                    menu_item = next((item for item in self.menu.items if item["id"] == raw.get("id")), None)
                    if not menu_item:
                        continue
                    quantity = max(1, int(raw.get("quantity", 1)))
                    options = list(raw.get("options", []))
                    size_label = self.menu.detect_size(menu_item, options)
                    unit_price = self.menu.resolve_unit_price(menu_item, size_label)
                    option_price = self.menu.resolve_option_price(menu_item, options)
                    items.append(
                        {
                            "id": menu_item["id"],
                            "name": menu_item["name"],
                            "quantity": quantity,
                            "unit_price": unit_price,
                            "option_price": option_price,
                            "options": options,
                            "notes": str(raw.get("notes", "")),
                            "subtotal": quantity * (unit_price + option_price),
                        }
                    )
                if items:
                    updated = self.menu.merge_items(order, items)
                    return {
                        "type": "assistant_reply",
                        "message": parsed.get("message") or self.order_message(updated),
                        "order": updated.to_dict(),
                    }

        return self.apply_rule_based_parse(text, order)

    def confirm_order(self, session_id: str) -> dict[str, Any]:
        order = self.get_order(session_id)
        if not order.items:
            return {
                "type": "clarification",
                "message": "目前沒有可確認的飲料，請先點單。",
                "order": order.to_dict(),
            }

        order = self.menu.calculate_totals(order)
        order.status = "confirmed"
        order.order_id = order.order_id or f"ORD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        order.confirmed_at = utc_now()
        order.updated_at = utc_now()
        self.persist_order(order)
        printer_result = self.persist_print_job(order)

        return {
            "type": "order_confirmed",
            "message": f"訂單已確認，單號 {order.order_id}，請前往櫃台付款。",
            "order": order.to_dict(),
            "printer": printer_result,
        }

    def cancel_order(self, session_id: str) -> dict[str, Any]:
        order = self.reset_order(session_id)
        return {
            "type": "order_cancelled",
            "message": "本次點單已取消，歡迎重新點單。",
            "order": order.to_dict(),
        }

    def persist_order(self, order: DraftOrder) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with ORDERS_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(order.to_dict(), ensure_ascii=False) + "\n")

    def persist_print_job(self, order: DraftOrder) -> dict[str, Any]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        printer_text = self.build_ticket_text(order)
        printer_result = self.printer.dispatch(order)
        payload = {
            "order_id": order.order_id,
            "created_at": utc_now(),
            "printer_text": printer_text,
            "order": order.to_dict(),
            "printer_result": printer_result,
        }
        with PRINT_JOBS_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return printer_result

    def build_ticket_text(self, order: DraftOrder) -> str:
        item_lines: list[str] = []
        for item in order.items:
            item_lines.append(f"{item['quantity']} x {item['name']} {item['subtotal']}元")
            if item.get("options"):
                item_lines.append(f"  選項: {' / '.join(item['options'])}")
            if item.get("notes"):
                item_lines.append(f"  備註: {item['notes']}")

        lines = [
            f"=== {self.menu.restaurant_name} ===",
            "語音點單小票",
            f"訂單編號: {order.order_id}",
            f"確認時間: {order.confirmed_at}",
            *item_lines,
            f"總計: {order.total} {order.currency}",
        ]
        return "\n".join(lines)


def load_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("MQTT_HOST", "broker.emqx.io"),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "username": os.environ.get("MQTT_USERNAME", ""),
        "password": os.environ.get("MQTT_PASSWORD", ""),
        "request_topic": os.environ.get("MQTT_REQUEST_TOPIC", "restaurant/voice/orders/inbox"),
        "reply_prefix": os.environ.get("MQTT_REPLY_PREFIX", "restaurant/voice/orders/reply"),
        "counter_topic": os.environ.get("MQTT_COUNTER_TOPIC", "restaurant/voice/orders/counter"),
        "printer_topic": os.environ.get("MQTT_PRINTER_TOPIC", "restaurant/voice/orders/print"),
    }


def publish_json(client: mqtt.Client, topic: str, payload: dict[str, Any]) -> None:
    client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=False)


def main() -> None:
    load_env_file(BASE_DIR / ".env")
    config = load_config()
    menu = MenuCatalog(MENU_PATH)
    service = OrderService(menu)

    def on_connect(
        client: mqtt.Client,
        _userdata: Any,
        _flags: dict[str, Any],
        _reason_code: int,
        _properties: Any = None,
    ) -> None:
        logging.info("Connected. Subscribe topic: %s", config["request_topic"])
        client.subscribe(config["request_topic"])

    def on_message(client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except json.JSONDecodeError:
            logging.warning("Invalid JSON payload: %s", message.payload)
            return

        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            logging.warning("Missing session_id")
            return

        action = payload.get("action", "customer_utterance")
        if action == "customer_utterance":
            response = service.handle_utterance(str(payload.get("text", "")), session_id)
        elif action == "confirm_order":
            response = service.confirm_order(session_id)
        elif action == "cancel_order":
            response = service.cancel_order(session_id)
        elif action == "delete_order_item":
            response = service.delete_order_item(session_id, int(payload.get("item_index", 0)))
        else:
            response = {
                "type": "clarification",
                "message": f"未知 action: {action}",
                "order": service.get_order(session_id).to_dict(),
            }

        reply_topic = f"{config['reply_prefix']}/{session_id}"
        publish_json(client, reply_topic, response)
        logging.info("Published reply to %s", reply_topic)

        if response.get("type") == "order_confirmed":
            publish_json(client, config["counter_topic"], response)
            publish_json(client, config["printer_topic"], response)
            logging.info("Published counter and printer events for %s", response["order"]["order_id"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config["username"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message

    logging.info("Starting order service on MQTT %s:%s", config["host"], config["port"])
    client.connect(config["host"], config["port"], keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
