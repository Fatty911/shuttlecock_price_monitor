from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "products.yaml"
SITE_DIR = ROOT / "site"
DATA_DIR = SITE_DIR / "data"

PRICE_RE = re.compile(r"(?:¥|￥|到手|券后|活动价|促销价|价格|价)\s*([1-9]\d{1,3}(?:\.\d{1,2})?)")
LOOSE_PRICE_RE = re.compile(r"(?:¥|￥)\s*([1-9]\d{1,3}(?:\.\d{1,2})?)")
SPEED_RE = re.compile(r"(?:速度|球速|标速|速别|speed)?\s*(76|77|78|1号|2号|3号|一速|二速|三速)", re.I)
SOLD_OUT_WORDS = ("售罄", "已抢光", "无货", "缺货", "补货中", "下架")
IN_STOCK_WORDS = ("立即购买", "加入购物车", "有货", "现货", "领券", "券后", "满减")


@dataclass(frozen=True)
class DiscountRule:
    channel: str
    label: str
    threshold: float
    amount: float
    stackable: bool = True


@dataclass(frozen=True)
class Candidate:
    channel: str
    channel_name: str
    model_key: str
    brand: str
    model: str
    speed: str
    title: str
    url: str
    seller: str
    base_price: float
    quantity: int
    source: str
    coupon_note: str
    stock_status: str
    confidence: int
    checked_at: str

    @property
    def subtotal(self) -> float:
        return round(self.base_price * self.quantity, 2)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_text(url: str, timeout: int = 25) -> tuple[str, str | None, bool]:
    headers = {"User-Agent": "Mozilla/5.0 shuttlecock-price-monitor/1.0"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        if len(text) > 500 and "enable javascript" not in text.lower():
            return text, None, False
    except Exception as exc:
        request_error = f"requests: {exc}"
    else:
        request_error = "requests: page text too short or js shell"
    if sync_playwright is None:
        return "", request_error + "; playwright not installed", False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(locale="zh-CN")
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            text = " ".join(page.locator("body").inner_text(timeout=timeout * 1000).split())
            browser.close()
            return text, None, True
    except Exception as exc:
        return "", f"{request_error}; browser: {exc}", True


def model_entries(config: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for group in config["models"]:
        for name in group["names"]:
            entries.append({"brand": group["brand"], "model": str(name), "notes": group.get("notes", "")})
    return entries


def infer_speed(text: str) -> str:
    match = SPEED_RE.search(text)
    if not match:
        return "未识别"
    speed = match.group(1)
    return {"一速": "1号", "二速": "2号", "三速": "3号"}.get(speed, speed)


def stock_status(text: str) -> str:
    if any(word in text for word in SOLD_OUT_WORDS):
        return "可能缺货"
    if any(word in text for word in IN_STOCK_WORDS):
        return "可能有货"
    return "需人工复核"


def extract_candidates_from_text(text: str, channel: dict[str, Any], model: dict[str, str], source_url: str) -> list[Candidate]:
    if not text:
        return []
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    keywords = [model["brand"], model["model"]]
    windows = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, re.I):
            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 360)
            windows.append(text[start:end])
    candidates: list[Candidate] = []
    for window in windows[:12]:
        price_match = PRICE_RE.search(window) or LOOSE_PRICE_RE.search(window)
        if not price_match:
            continue
        price = float(price_match.group(1))
        if price < 20 or price > 500:
            continue
        title = window[:120]
        confidence = 40 + 20 * (model["brand"] in window) + 25 * (model["model"].lower() in window.lower())
        candidates.append(
            Candidate(
                channel=channel["id"],
                channel_name=channel["name"],
                model_key=f"{model['brand']} {model['model']}",
                brand=model["brand"],
                model=model["model"],
                speed=infer_speed(window),
                title=title,
                url=source_url,
                seller="搜索页候选",
                base_price=price,
                quantity=1,
                source="search-page",
                coupon_note=channel.get("cart_discount_note", ""),
                stock_status=stock_status(window),
                confidence=min(confidence, 100),
                checked_at=now,
            )
        )
    return candidates


def fixture_candidates(config: dict[str, Any]) -> list[Candidate]:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    rows: list[Candidate] = []
    for channel in config["channels"]:
        for idx, model in enumerate(model_entries(config), 1):
            synthetic_price = 68 + (idx % 7) * 9 + {"taobao": 0, "jd": 3, "pdd": -4}.get(channel["id"], 0)
            rows.append(
                Candidate(
                    channel=channel["id"], channel_name=channel["name"], model_key=f"{model['brand']} {model['model']}",
                    brand=model["brand"], model=model["model"], speed="76/77/78", title=f"{model['brand']} {model['model']} 羽毛球 12只装 速度可选",
                    url=channel["search_url"].format(query=quote_plus(f"{model['brand']} {model['model']} 羽毛球")), seller="待抓取确认",
                    base_price=float(synthetic_price), quantity=1, source="baseline-watchlist", coupon_note=channel.get("cart_discount_note", ""),
                    stock_status="待抓取", confidence=30, checked_at=now,
                )
            )
    return rows


def discount_for_subtotal(subtotal: float, rules: list[DiscountRule]) -> tuple[float, list[str]]:
    total = 0.0
    labels: list[str] = []
    for rule in rules:
        times = math.floor(subtotal / rule.threshold) if rule.stackable else int(subtotal >= rule.threshold)
        if times > 0:
            value = times * rule.amount
            total += value
            labels.append(f"{rule.label} -¥{value:.2f}")
    return round(total, 2), labels


def best_cart_allocations(candidates: list[Candidate], rules: list[DiscountRule]) -> list[dict[str, Any]]:
    by_channel: dict[str, list[Candidate]] = {}
    for item in candidates:
        by_channel.setdefault(item.channel, []).append(item)
    output: list[dict[str, Any]] = []
    for channel, items in by_channel.items():
        channel_rules = [rule for rule in rules if rule.channel == channel]
        subtotal = sum(item.subtotal for item in items)
        discount, labels = discount_for_subtotal(subtotal, channel_rules)
        factor = (subtotal - discount) / subtotal if subtotal else 1
        for item in items:
            output.append({
                **item.__dict__,
                "cart_subtotal": round(subtotal, 2),
                "allocated_discount": round(item.subtotal * (1 - factor), 2),
                "effective_price": round(item.base_price * factor, 2),
                "discounts": labels,
            })
    return output


def lowest_per_channel_model(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["channel"], record["model_key"])
        old = best.get(key)
        if old is None or (record["effective_price"], -record["confidence"]) < (old["effective_price"], -old["confidence"]):
            best[key] = record
    return sorted(best.values(), key=lambda r: (r["model_key"], r["effective_price"], r["channel"]))


def build_records(config: dict[str, Any], live: bool) -> list[dict[str, Any]]:
    candidates: list[Candidate] = []
    if live:
        for channel in config["channels"]:
            for model in model_entries(config):
                query = quote_plus(f"{model['brand']} {model['model']} 羽毛球")
                url = channel["search_url"].format(query=query)
                text, error, used_browser = fetch_text(url)
                found = extract_candidates_from_text(text, channel, model, url)
                if not found:
                    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                    found = [Candidate(channel["id"], channel["name"], f"{model['brand']} {model['model']}", model["brand"], model["model"], "未识别", f"未自动识别：{error or '页面未出现可解析价格'}", url, "未识别", 9999.0, 1, "search-fallback-browser" if used_browser else "search-fallback", channel.get("cart_discount_note", ""), "需人工复核", 0, now)]
                candidates.extend(found)
    else:
        candidates = fixture_candidates(config)
    rules = [DiscountRule(**item) for item in config.get("discounts", [])]
    return lowest_per_channel_model(best_cart_allocations(candidates, rules))


def render_html(records: list[dict[str, Any]]) -> str:
    generated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = []
    for item in records:
        discounts = "；".join(item.get("discounts") or ["未识别/无"])
        price = "需人工复核" if item["effective_price"] >= 9999 else f"¥{item['effective_price']:.2f}"
        rows.append("<tr>" + "".join([
            f"<td>{html.escape(item['channel_name'])}</td>",
            f"<td><a href='{html.escape(item['url'])}'>{html.escape(item['model_key'])}</a></td>",
            f"<td>{html.escape(item['speed'])}</td>",
            f"<td>{price}</td>",
            f"<td>{html.escape(discounts)}</td>",
            f"<td>{html.escape(item['stock_status'])}</td>",
            f"<td>{item['confidence']}</td>",
            f"<td>{html.escape(item['seller'])}</td>",
            f"<td>{html.escape(item['coupon_note'])}</td>",
        ]) + "</tr>")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>羽毛球最低到手价监控</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#172033}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border:1px solid #d7dde8;padding:8px;vertical-align:top}}th{{background:#f3f6fb}}.hint{{background:#fff7ed;border:1px solid #fed7aa;padding:12px;margin:16px 0}}.ok{{color:#047857;font-weight:700}}</style></head><body><h1>羽毛球最低到手价监控</h1><p>生成时间：{generated_at}。域名：<span class="ok">shuttlecocks.jiucai.eu.org</span></p><div class="hint">自动监控会按淘宝/京东/拼多多 × 型号保留最低到手价链接，并把跨店满减/优惠券按购物车总额比例摊回单筒价格。电商页面强风控、优惠券定向和支付券可能导致自动价与账号实付不同，购买前请点进链接二次确认。</div><table><thead><tr><th>电商渠道</th><th>羽毛球型号</th><th>球速</th><th>到手价/筒</th><th>满减/券</th><th>库存</th><th>置信度</th><th>卖家</th><th>领券/备注</th></tr></thead><tbody>{''.join(rows)}</tbody></table><p>机器可读数据：<a href="data/results.json">data/results.json</a></p></body></html>"""


def write_outputs(records: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.joinpath("results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    SITE_DIR.joinpath("index.html").write_text(render_html(records), encoding="utf-8")
    SITE_DIR.joinpath("CNAME").write_text("shuttlecocks.jiucai.eu.org\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor best effective prices for badminton shuttlecocks.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--live", action="store_true", help="fetch ecommerce search pages; default builds baseline watchlist")
    parser.add_argument("--output", action="store_true")
    args = parser.parse_args()
    records = build_records(load_config(args.config), live=args.live)
    if args.output:
        write_outputs(records)
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
