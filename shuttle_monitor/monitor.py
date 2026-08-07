from __future__ import annotations

import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from shuttle_monitor.audit import (
    audit_envelope,
    build_file_manifest,
    product_quality_gate,
    verify_file_manifest,
)
from shuttle_monitor.contracts import (
    ContractError,
    build_envelope,
    validate_envelope,
    validate_live_envelope,
)
from shuttle_monitor.state import merge_history_events, write_state_directory

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "products.yaml"
SITE_DIR = ROOT / "site"
STATE_DIR = ROOT / "state"
WEB_DIR = ROOT / "web"
CLASH_API = os.getenv("CLASH_API", "http://127.0.0.1:9090")
LOCAL_HTTP_PROXY = os.getenv("SHUTTLE_HTTP_PROXY", "http://127.0.0.1:7890")
# 平台级代理探测的浏览器渲染节流：同一平台最多渲染 RENDER_LIMIT 次。
# 全部节点出口被平台风控时，继续换节点渲染无意义且烧预算（45s/次）。
RENDER_LIMIT = 5
SOLD_OUT_WORDS = ("售罄", "已抢光", "无货", "缺货", "补货中", "下架")
IN_STOCK_WORDS = ("立即购买", "加入购物车", "有货", "现货", "领券", "券后", "满减")
BLOCK_WORDS = (
    "验证码",
    "滑块验证",
    "安全验证",
    "访问异常",
    "请登录",
    "登录淘宝",
    "risk_handler",
    "captcha",
)
ACCESSORY_WORDS = ("球拍", "球鞋", "球包", "空桶", "桶盖", "赠品", "配件", "贴纸")
CONDITIONAL_PRICE_WORDS = ("分期", "每月", "月付", "每期", "定金", "订金")


@dataclass(frozen=True)
class ProductTask:
    platform: str
    platform_name: str
    brand: str
    model: str
    query_url: str
    product_url: str | None = None
    priority: int = 100
    speed: str = ""

    @property
    def model_key(self) -> str:
        return f"{self.brand} {self.model}"

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.model_key}:speed-{self.speed}"


@dataclass(frozen=True)
class ProductCandidate:
    platform: str
    model_key: str
    title: str
    price: float
    product_url: str
    stock_status: str
    native_product_id: str
    detail_verified: bool = False


@dataclass(frozen=True)
class MarkupClassification:
    outcome: str
    block_reason: str | None = None


@dataclass(frozen=True)
class AttemptResult:
    task: ProductTask
    outcome: str
    price: float | None
    product_url: str | None
    title: str | None
    http_status: int | None
    final_url: str | None
    method: str
    block_reason: str | None
    attempts: int
    latency_ms: int
    checked_at: str
    native_product_id: str | None = None
    detail_verified: bool = False


@dataclass(frozen=True)
class FetchResult:
    markup: str
    outcome: str
    http_status: int | None
    final_url: str | None
    block_reason: str | None
    attempts: int
    latency_ms: int


class RequestLimiter:
    def __init__(self, global_limit: int = 6, per_host_limit: int = 2):
        self._global = threading.BoundedSemaphore(global_limit)
        self._per_host_limit = per_host_limit
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def _host_semaphore(self, url: str) -> threading.BoundedSemaphore:
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            return self._hosts.setdefault(host, threading.BoundedSemaphore(self._per_host_limit))

    @staticmethod
    def _acquire(semaphore: threading.BoundedSemaphore, cancelled: threading.Event | None) -> None:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            if semaphore.acquire(timeout=0.05):
                return

    @contextmanager
    def slot(self, url: str, cancelled: threading.Event | None = None):
        host_semaphore = self._host_semaphore(url)
        global_acquired = False
        host_acquired = False
        try:
            self._acquire(self._global, cancelled)
            global_acquired = True
            self._acquire(host_semaphore, cancelled)
            host_acquired = True
            yield
        finally:
            if host_acquired:
                host_semaphore.release()
            if global_acquired:
                self._global.release()


class BrowserLimiter:
    def __init__(self, global_limit: int = 2):
        self._global = threading.BoundedSemaphore(global_limit)
        self._platforms: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, platform: str):
        with self._lock:
            platform_semaphore = self._platforms.setdefault(platform, threading.BoundedSemaphore(1))
        self._global.acquire()
        platform_acquired = False
        try:
            platform_semaphore.acquire()
            platform_acquired = True
            yield
        finally:
            if platform_acquired:
                platform_semaphore.release()
            self._global.release()


class BrowserCircuitBreaker:
    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self._state: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def allow(self, platform: str) -> bool:
        with self._lock:
            return self._state.get(platform, ("", 0))[1] < self.threshold

    def record(self, platform: str, block_reason: str | None) -> None:
        with self._lock:
            if not block_reason:
                self._state.pop(platform, None)
                return
            old_reason, old_count = self._state.get(platform, ("", 0))
            self._state[platform] = (
                block_reason,
                old_count + 1 if old_reason == block_reason else 1,
            )


def request_markup(
    url: str,
    *,
    getter: Any = requests.get,
    sleep: Any = time.sleep,
    proxies: dict[str, str] | None = None,
    max_attempts: int = 3,
) -> FetchResult:
    started = time.monotonic()
    attempts = 0
    last_error = "request_failed"
    for attempt in range(max_attempts):
        attempts = attempt + 1
        try:
            response = getter(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                },
                timeout=(10, 25),
                proxies=proxies,
                allow_redirects=True,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = f"connection:{type(exc).__name__}"
            if attempt + 1 < max_attempts:
                sleep(0.25 * (attempt + 1))
                continue
            return FetchResult(
                "", "error", None, None, last_error, attempts,
                int((time.monotonic() - started) * 1000),
            )
        status = int(response.status_code)
        final_url = str(response.url)
        if status in {408, 429} or 500 <= status <= 599:
            last_error = f"http_{status}"
            if attempt + 1 < max_attempts:
                sleep(0.25 * (attempt + 1))
                continue
            return FetchResult(
                str(response.text), "error", status, final_url, last_error, attempts,
                int((time.monotonic() - started) * 1000),
            )
        if status in {401, 403}:
            return FetchResult(
                str(response.text), "blocked", status, final_url, f"http_{status}", attempts,
                int((time.monotonic() - started) * 1000),
            )
        classification = classify_markup(str(response.text), final_url)
        outcome = classification.outcome
        if status >= 400 and outcome == "success":
            outcome = "error"
        return FetchResult(
            str(response.text),
            outcome,
            status,
            final_url,
            classification.block_reason if outcome != "error" else f"http_{status}",
            attempts,
            int((time.monotonic() - started) * 1000),
        )
    raise AssertionError(last_error)


def discover_proxy_nodes(payload: dict[str, Any]) -> list[str]:
    nodes: list[str] = []
    proxies = payload.get("proxies", {})
    group_types = {"selector", "urltest", "fallback", "loadbalance", "direct", "reject"}
    for value in payload.get("proxies", {}).values():
        if not isinstance(value, dict) or value.get("type") != "Selector":
            continue
        for name in value.get("all", []):
            candidate = proxies.get(name)
            candidate_type = str(candidate.get("type", "")).replace("-", "").casefold() if isinstance(candidate, dict) else ""
            if (
                name not in {"DIRECT", "REJECT"}
                and candidate_type
                and candidate_type not in group_types
                and name not in nodes
            ):
                nodes.append(str(name))
    return nodes


def select_proxy_node(
    platform: str,
    nodes: list[str],
    *,
    probe: Any,
    total_budget: float = 600,
    node_timeout: float = 15,
    monotonic: Any = time.monotonic,
) -> tuple[str | None, dict[str, Any]]:
    started = monotonic()
    tested = 0
    exhausted = False
    for node in nodes:
        if monotonic() - started >= total_budget:
            exhausted = True
            break
        tested += 1
        if probe(platform, node, node_timeout):
            return node, {"tested": tested, "selected": True, "budget_exhausted": False}
    return None, {"tested": tested, "selected": False, "budget_exhausted": exhausted}


def _attempt_task(
    task: ProductTask,
    request_limiter: RequestLimiter,
    browser_limiter: BrowserLimiter,
    breaker: BrowserCircuitBreaker,
    request_fn: Any,
    browser_fn: Any,
) -> AttemptResult:
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with request_limiter.slot(task.query_url):
            fetched = request_fn(task.query_url)
    except Exception as exc:
        return AttemptResult(
            task, "error", None, None, None, None, task.query_url, "requests",
            f"request_exception:{type(exc).__name__}", 1, 0, checked_at,
        )
    candidates = parse_product_cards(fetched.markup, task) if fetched.outcome == "success" else []
    if candidates:
        for discovered in sorted(candidates, key=lambda item: item.price)[:3]:
            try:
                with request_limiter.slot(discovered.product_url):
                    detail = request_fn(discovered.product_url)
            except Exception:
                continue
            verified = (
                verify_detail_candidate(
                    detail.markup,
                    task,
                    discovered,
                    detail.final_url or discovered.product_url,
                )
                if detail.outcome == "success"
                else None
            )
            if verified:
                return AttemptResult(
                    task, "success", verified.price, verified.product_url, verified.title,
                    detail.http_status, detail.final_url, "requests-detail", None,
                    fetched.attempts + detail.attempts,
                    fetched.latency_ms + detail.latency_ms, checked_at,
                    verified.native_product_id, True,
                )
        return AttemptResult(
            task, "rejected", None, None, None, fetched.http_status, fetched.final_url,
            "requests-detail", "detail_unverified", fetched.attempts,
            fetched.latency_ms, checked_at,
        )
    if fetched.outcome == "blocked" and fetched.http_status in {401, 403}:
        return AttemptResult(
            task, "blocked", None, None, None, fetched.http_status, fetched.final_url,
            "requests", fetched.block_reason, fetched.attempts, fetched.latency_ms, checked_at,
        )
    if fetched.outcome == "error":
        return AttemptResult(
            task, "error", None, None, None, fetched.http_status, fetched.final_url,
            "requests", fetched.block_reason, fetched.attempts, fetched.latency_ms, checked_at,
        )
    with browser_limiter.slot(task.platform):
        if not breaker.allow(task.platform):
            return AttemptResult(
                task, "blocked", None, None, None, fetched.http_status, fetched.final_url,
                "browser-skipped", "browser_circuit_open", fetched.attempts,
                fetched.latency_ms, checked_at,
            )
        try:
            browser_result = browser_fn(task.query_url, task.platform)
        except Exception as exc:
            return AttemptResult(
                task, "error", None, None, None, fetched.http_status, fetched.final_url,
                "browser", f"browser_exception:{type(exc).__name__}", fetched.attempts,
                fetched.latency_ms, checked_at,
            )
        browser_candidates = (
            parse_product_cards(browser_result.markup, task)
            if browser_result.outcome == "success"
            else []
        )
        if browser_candidates:
            for discovered in sorted(browser_candidates, key=lambda item: item.price)[:3]:
                detail = browser_fn(discovered.product_url, task.platform)
                verified = (
                    verify_detail_candidate(
                        detail.markup,
                        task,
                        discovered,
                        detail.final_url or discovered.product_url,
                    )
                    if detail.outcome == "success"
                    else None
                )
                if verified:
                    breaker.record(task.platform, None)
                    return AttemptResult(
                        task, "success", verified.price, verified.product_url, verified.title,
                        detail.http_status, detail.final_url, "browser-detail", None,
                        fetched.attempts + browser_result.attempts + detail.attempts,
                        fetched.latency_ms + browser_result.latency_ms + detail.latency_ms,
                        checked_at, verified.native_product_id, True,
                    )
            return AttemptResult(
                task, "rejected", None, None, None, browser_result.http_status,
                browser_result.final_url, "browser-detail", "detail_unverified",
                fetched.attempts + browser_result.attempts,
                fetched.latency_ms + browser_result.latency_ms, checked_at,
            )
        reason = browser_result.block_reason or "no_same_card_candidate"
        breaker.record(task.platform, reason)
        outcome = "blocked" if browser_result.outcome == "blocked" else "error"
        if browser_result.outcome == "success":
            outcome = "blocked"
        return AttemptResult(
            task, outcome, None, None, None, browser_result.http_status,
            browser_result.final_url, "browser", reason,
            fetched.attempts + browser_result.attempts,
            fetched.latency_ms + browser_result.latency_ms, checked_at,
        )


def crawl_tasks(
    tasks: list[ProductTask],
    *,
    request_fn: Any = request_markup,
    browser_fn: Any,
) -> list[AttemptResult]:
    request_limiter = RequestLimiter(global_limit=6, per_host_limit=2)
    browser_limiter = BrowserLimiter(global_limit=2)
    breaker = BrowserCircuitBreaker(threshold=10)

    def run(task: ProductTask) -> AttemptResult:
        return _attempt_task(
            task,
            request_limiter,
            browser_limiter,
            breaker,
            request_fn,
            browser_fn,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(run, tasks))


def build_live_evidence(
    results: list[AttemptResult],
    *,
    canaries: list[dict[str, Any]],
    proxy_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = (
        "platform",
        "model_key",
        "http_status",
        "final_url",
        "method",
        "outcome",
        "block_reason",
        "attempts",
        "latency_ms",
    )
    task_rows = []
    for result in results:
        task_rows.append(
            {
                "platform": result.task.platform,
                "model_key": result.task.model_key,
                "http_status": result.http_status,
                "final_url": result.final_url,
                "method": result.method,
                "outcome": result.outcome,
                "block_reason": result.block_reason,
                "attempts": result.attempts,
                "latency_ms": result.latency_ms,
            }
        )
    safe_canaries = [{field: row.get(field) for field in fields} for row in canaries]
    safe_stats = {
        platform: {
            "tested": int(stats.get("tested", 0)),
            "selected": bool(stats.get("selected", False)),
            "budget_exhausted": bool(stats.get("budget_exhausted", False)),
        }
        for platform, stats in proxy_stats.items()
    }
    return {"canaries": safe_canaries, "tasks": task_rows, "proxy_stats": safe_stats}


def validate_round(results: list[AttemptResult], tasks: list[ProductTask]) -> dict[str, Any]:
    expected = {task.key for task in tasks}
    actual = [result.task.key for result in results]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("round must contain each configured task exactly once")
    platform_counts: dict[str, dict[str, int]] = {}
    counts = {"success": 0, "blocked": 0, "rejected": 0, "error": 0, "out_of_stock": 0}
    for result in results:
        if result.outcome not in counts:
            raise ValueError(f"invalid outcome: {result.outcome}")
        if result.outcome != "success" and (result.price is not None or result.product_url is not None):
            raise ValueError("blocked/error results must have null price and product_url")
        if result.outcome == "success":
            if (
                result.price is None
                or not 0 < result.price <= 5000
                or not result.title
                or not result.product_url
                or not is_official_product_url(result.task.platform, result.product_url)
                or not result.detail_verified
                or not result.native_product_id
                or _native_product_id(result.task.platform, result.product_url)
                != result.native_product_id
            ):
                raise ValueError(
                    "success must contain native ID and detail-verified same-card evidence"
                )
        counts[result.outcome] += 1
        bucket = platform_counts.setdefault(
            result.task.platform,
            {
                "attempted": 0,
                "success": 0,
                "blocked": 0,
                "rejected": 0,
                "error": 0,
                "out_of_stock": 0,
            },
        )
        bucket["attempted"] += 1
        bucket[result.outcome] += 1
    summary: dict[str, Any] = {
        "attempted": len(results),
        **counts,
        "models": len({task.model_key for task in tasks}),
        "platforms": platform_counts,
    }
    if summary["attempted"] != sum(summary[name] for name in ("success", "blocked", "rejected", "error", "out_of_stock")):
        raise ValueError("outcome conservation failed")
    return summary


def sanitize_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for row in rows:
        platform = str(row.get("platform", ""))
        url = str(row.get("product_url", ""))
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if (
            not 0 < price <= 5000
            or row.get("confidence") == 0
            or not is_official_product_url(platform, url)
            or not row.get("model_key")
            or not row.get("title")
            or row.get("detail_verified") is not True
            or not row.get("native_product_id")
        ):
            continue
        clean.append({**row, "price": price})
    return clean


def build_public_data(
    results: list[AttemptResult],
    tasks: list[ProductTask],
    existing_history: list[dict[str, Any]],
    *,
    mode: str = "live",
) -> dict[str, Any]:
    summary = validate_round(results, tasks)
    history = sanitize_history(existing_history)
    historical_keys = {
        f"{row.get('platform')}:{row.get('model_key')}"
        for row in history
    }
    status_rows: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    for result in results:
        success = result.outcome == "success"
        evidence_hash = hashlib.sha256(
            json.dumps(
                {
                    "task_id": result.task.key,
                    "outcome": result.outcome,
                    "http_status": result.http_status,
                    "final_url": result.final_url,
                    "method": result.method,
                    "reason": result.block_reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        status_rows.append(
            {
                "task_id": result.task.key,
                "key": result.task.key,
                "platform": result.task.platform,
                "platform_name": result.task.platform_name,
                "brand": result.task.brand,
                "model_key": result.task.model_key,
                "speed": result.task.speed,
                "mode": mode,
                "query_url": result.task.query_url,
                "source_url": result.task.query_url,
                "outcome": result.outcome,
                "price_status": "fresh" if success else ("stale" if result.task.key in historical_keys else "none"),
                "price": result.price if success else None,
                "product_url": result.product_url if success else None,
                "native_product_id": result.native_product_id if success else None,
                "detail_verified": result.detail_verified if success else False,
                "http_status": result.http_status,
                "final_url": result.final_url,
                "method": result.method,
                "block_reason": result.block_reason,
                "rejection_reason": result.block_reason,
                "attempts": result.attempts,
                "latency_ms": result.latency_ms,
                "checked_at": result.checked_at,
                "started_at": result.checked_at,
                "finished_at": result.checked_at,
                "evidence_hash": evidence_hash,
                "parser_version": "shuttle-v4",
            }
        )
        if success:
            price_row = {
                "task_id": result.task.key,
                "platform": result.task.platform,
                "platform_name": result.task.platform_name,
                "model_key": result.task.model_key,
                "title": result.title,
                "price": result.price,
                "product_url": result.product_url,
                "native_product_id": result.native_product_id,
                "detail_verified": result.detail_verified,
                "outcome": "success",
                "mode": mode,
                "checked_at": result.checked_at,
                "observed_at": result.checked_at,
            }
            price_row["event_id"] = hashlib.sha256(
                json.dumps(
                    [
                        price_row["task_id"],
                        price_row["product_url"],
                        price_row["price"],
                        price_row["observed_at"],
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            prices.append(price_row)
    normalized_history: list[dict[str, Any]] = []
    for row in history:
        observed_at = row.get("observed_at") or row.get("checked_at")
        if not observed_at:
            continue
        item = {**row, "observed_at": observed_at}
        item.setdefault(
            "event_id",
            hashlib.sha256(
                json.dumps(
                    [
                        item.get("platform"),
                        item.get("model_key"),
                        item.get("product_url"),
                        item.get("price"),
                        observed_at,
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        normalized_history.append(item)
    now = max((result.checked_at for result in results), default=dt.datetime.now(dt.timezone.utc).isoformat())
    history = merge_history_events(normalized_history, prices, now=now)
    return {
        "status": status_rows,
        "prices": prices,
        "price_history": history,
        "summary": summary,
        "quality_gate": product_quality_gate(status_rows),
    }


def _render_status_page(public: dict[str, Any]) -> str:
    summary = public["summary"]
    alert = "" if public["prices"] else "<div class='alert'>本轮无有效价</div>"
    rows = []
    for row in public["status"]:
        price = f"¥{row['price']:.2f}" if row["price_status"] == "fresh" else "—"
        product = (
            f"<a href='{html.escape(str(row['product_url']))}'>商品</a>"
            if row["product_url"]
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(row['platform_name'])}</td>"
            f"<td>{html.escape(row['model_key'])}</td>"
            f"<td>{row['outcome']}</td><td>{row['price_status']}</td>"
            f"<td>{price}</td><td>{product}</td>"
            f"<td>{html.escape(str(row['block_reason'] or ''))}</td>"
            "</tr>"
        )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>羽毛球真实价格状态</title>"
        "<style>body{font-family:sans-serif;margin:24px}.alert{padding:14px;background:#fee2e2;"
        "font-weight:700}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px}</style>"
        "</head><body><h1>羽毛球真实价格状态</h1>"
        f"{alert}<p>attempted={summary['attempted']} success={summary['success']} "
        f"blocked={summary['blocked']} error={summary['error']}</p>"
        "<table><thead><tr><th>平台</th><th>型号</th><th>结果</th><th>价格状态</th>"
        "<th>本轮价格</th><th>商品</th><th>原因</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def publish_site(
    public: dict[str, Any],
    site_dir: Path = SITE_DIR,
    evidence: dict[str, Any] | None = None,
    *,
    envelope: dict[str, Any] | None = None,
    audit_report: dict[str, Any] | None = None,
) -> None:
    staging = site_dir.parent / f".{site_dir.name}-staging"
    if staging.exists():
        shutil.rmtree(staging)
    data_dir = staging / "data"
    data_dir.mkdir(parents=True)
    data_dir.joinpath("status.json").write_text(
        json.dumps(public["status"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("prices.json").write_text(
        json.dumps(public["prices"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("price_history.json").write_text(
        json.dumps(public["price_history"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("summary.json").write_text(
        json.dumps(public["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("live-evidence.json").write_text(
        json.dumps(evidence or {"canaries": [], "tasks": [], "proxy_stats": {}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if envelope is not None:
        data_dir.joinpath("batch.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report = audit_report or {
        "schema_version": 4,
        "structure_status": "blocked",
        "product_status": "blocked",
        "status": "blocked",
        "fingerprint": hashlib.sha256(b"local-unverified").hexdigest(),
        "violations": [{"code": "missing_batch_envelope"}],
    }
    staging.joinpath("audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for asset in ("index.html", "app.js", "styles.css"):
        shutil.copy2(WEB_DIR / asset, staging / asset)
    staging.joinpath("CNAME").write_text("shuttlecocks.jiucai.eu.org\n", encoding="utf-8")
    relative_paths = [
        str(path.relative_to(staging)).replace("\\", "/")
        for path in staging.rglob("*")
        if path.is_file()
    ]
    metadata = envelope or {
        "batch_id": "shuttlecock_price_monitor:local:0",
        "source_sha": "0" * 40,
        "mode": "fixture",
        "run_id": "local",
        "run_attempt": "0",
        "config_sha256": "0" * 64,
    }
    manifest = build_file_manifest(
        staging,
        relative_paths,
        batch_id=str(metadata["batch_id"]),
        source_sha=str(metadata["source_sha"]),
    )
    manifest.update(
        {
            "mode": metadata.get("mode"),
            "run_id": metadata.get("run_id"),
            "run_attempt": metadata.get("run_attempt"),
            "config_sha256": metadata.get("config_sha256"),
            "audit_status": report.get("status"),
        }
    )
    staging.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if site_dir.exists():
        shutil.rmtree(site_dir)
    staging.replace(site_dir)


def build_batch_envelope(
    public: dict[str, Any],
    tasks: list[ProductTask],
    evidence: dict[str, Any],
    *,
    mode: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    config_sha256: str,
    started_at: str,
    finished_at: str,
    baseline_batch_id: str | None = None,
) -> dict[str, Any]:
    evidence_sha = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    envelope = build_envelope(
        repo="shuttlecock_price_monitor",
        run_id=run_id,
        run_attempt=run_attempt,
        source_sha=source_sha,
        config_sha256=config_sha256,
        started_at=started_at,
        finished_at=finished_at,
        mode=mode,
        baseline_batch_id=baseline_batch_id,
        expected_tasks=69,
        statuses=public["status"],
        prices=public["prices"],
        evidence_sha256=evidence_sha,
        audit_status="pass" if public["quality_gate"] else "blocked",
    )
    validate_envelope(envelope, [task.key for task in tasks])
    return envelope


def build_tasks(config: dict[str, Any]) -> list[ProductTask]:
    tasks: list[ProductTask] = []
    for channel in config["channels"]:
        for priority, model in enumerate(model_entries(config)):
            query = quote_plus(
                f"{model['brand']} {model['model']} {model['speed']}速 羽毛球"
            )
            tasks.append(
                ProductTask(
                    platform=str(channel["id"]),
                    platform_name=str(channel["name"]),
                    brand=model["brand"],
                    model=model["model"],
                    query_url=str(channel["search_url"]).format(query=query),
                    priority=priority,
                    speed=model["speed"],
                )
            )
    if len({task.key for task in tasks}) != len(tasks):
        raise ValueError("duplicate platform/model tasks")
    return tasks


def prioritize_tasks(
    tasks: list[ProductTask],
    *,
    completed_task_ids: set[str] | None = None,
) -> list[ProductTask]:
    completed = completed_task_ids or set()
    return sorted(
        (task for task in tasks if task.key not in completed),
        key=lambda task: (task.priority, task.key),
    )


def select_rotating_canaries(
    tasks: list[ProductTask],
    *,
    batch_id: str,
    per_platform: int = 3,
) -> dict[str, list[ProductTask]]:
    selected: dict[str, list[ProductTask]] = {}
    for platform in sorted({task.platform for task in tasks}):
        platform_tasks = sorted(
            (task for task in tasks if task.platform == platform),
            key=lambda task: task.key,
        )
        if not platform_tasks:
            selected[platform] = []
            continue
        digest = hashlib.sha256(f"{batch_id}:{platform}".encode()).digest()
        offset = int.from_bytes(digest[:4], "big") % len(platform_tasks)
        selected[platform] = [
            platform_tasks[(offset + index) % len(platform_tasks)]
            for index in range(min(per_platform, len(platform_tasks)))
        ]
    return selected


def is_official_product_url(platform: str, url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path
    query = parse_qs(parsed.query)
    if parsed.scheme not in ("https", "http"):
        return False
    if platform == "taobao":
        return (
            (host in ("item.taobao.com", "h5.m.taobao.com", "m.taobao.com")
             and path in ("/item.htm", "/awp/core/detail.htm")
             and bool(query.get("id")))
            or (host == "detail.tmall.com" and path == "/item.htm" and bool(query.get("id")))
            or (host == "detail.m.tmall.com" and path == "/item.htm" and bool(query.get("id")))
        )
    if platform == "jd":
        return (
            (host == "item.jd.com" and re.fullmatch(r"/[1-9]\d*\.html", path) is not None)
            or (host == "item.m.jd.com" and re.fullmatch(r"/product/[1-9]\d*\.html", path) is not None)
        )
    if platform == "pdd":
        return (
            host in ("mobile.yangkeduo.com", "www.pinduoduo.com", "yangkeduo.com")
            and path in ("/goods.html", "/goods", "/goods2.html")
            and bool(query.get("goods_id"))
        )
    return False


def classify_markup(markup: str, final_url: str) -> MarkupClassification:
    sample = f"{final_url}\n{clean_text(markup)}".lower()
    for word in BLOCK_WORDS:
        if word.lower() in sample:
            return MarkupClassification("blocked", word)
    if not markup.strip():
        return MarkupClassification("error", "empty_response")
    return MarkupClassification("success")


def _absolute_product_url(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    return href


def _title_matches(task: ProductTask, title: str) -> bool:
    normalized = re.sub(r"\s+", "", title).lower()
    model = re.sub(r"\s+", "", task.model).lower()
    brand_tokens = [token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", task.brand)]
    speed_matches = not task.speed or any(
        marker in normalized
        for marker in (
            f"{task.speed}速",
            f"速度{task.speed}",
            f"speed{task.speed}",
            f"{task.speed}speed",
        )
    )
    return (
        "羽毛球" in normalized
        and model in normalized
        and any(token in normalized for token in brand_tokens)
        and speed_matches
        and not any(word in normalized for word in ACCESSORY_WORDS)
    )


def _hidden_node(node: Any) -> bool:
    current = node
    while current is not None and getattr(current, "attrs", None) is not None:
        style = str(current.get("style", "")).replace(" ", "").casefold()
        classes = {str(value).casefold() for value in current.get("class", [])}
        if (
            current.has_attr("hidden")
            or str(current.get("aria-hidden", "")).casefold() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
            or classes.intersection({"hidden", "sr-only", "visually-hidden"})
        ):
            return True
        current = current.parent
    return False


def _price_from_card(card: Any) -> float | None:
    price_nodes = card.select(
        ".price, .p-price, .goods-price, [class*='Price'], [class*='price']"
    )
    for node in price_nodes:
        if _hidden_node(node):
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if any(word in text for word in CONDITIONAL_PRICE_WORDS):
            continue
        match = re.search(r"(?:¥|￥)?\s*(?<![-\d])([0-9]+(?:\.[0-9]{1,2})?)(?!\d)", text)
        if not match:
            continue
        price = float(match.group(1))
        if 0 < price <= 5000:
            return price
    return None


def _native_product_id(platform: str, url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if platform == "taobao":
        values = query.get("id")
        return values[0] if values else None
    if platform == "jd":
        match = re.fullmatch(r"/([1-9]\d*)\.html", parsed.path)
        return match.group(1) if match else None
    if platform == "pdd":
        values = query.get("goods_id")
        return values[0] if values else None
    return None


def parse_product_cards(markup: str, task: ProductTask) -> list[ProductCandidate]:
    if classify_markup(markup, task.query_url).outcome != "success":
        return []
    soup = BeautifulSoup(markup, "html.parser")
    selectors = {
        "taobao": ".item[data-nid], [data-nid]",
        "jd": ".gl-item[data-sku], [data-sku]",
        "pdd": ".goods-item[data-goods-id], [data-goods-id]",
    }
    candidates: list[ProductCandidate] = []
    seen: set[str] = set()
    for card in soup.select(selectors.get(task.platform, "")):
        anchor = next(
            (
                item
                for item in card.select("a[href]")
                if is_official_product_url(task.platform, _absolute_product_url(str(item.get("href", ""))))
            ),
            None,
        )
        if anchor is None:
            continue
        product_url = _absolute_product_url(str(anchor.get("href", "")))
        native_product_id = _native_product_id(task.platform, product_url)
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not native_product_id or not _title_matches(task, title):
            continue
        price = _price_from_card(card)
        if price is None or product_url in seen:
            continue
        seen.add(product_url)
        candidates.append(
            ProductCandidate(
                platform=task.platform,
                model_key=task.model_key,
                title=title,
                price=price,
                product_url=product_url,
                stock_status=stock_status(card.get_text(" ", strip=True)),
                native_product_id=native_product_id,
            )
        )
    return candidates


def verify_detail_candidate(
    markup: str,
    task: ProductTask,
    discovered: ProductCandidate,
    final_url: str,
) -> ProductCandidate | None:
    """Turn a discovery card into a success only with independent detail evidence."""
    if (
        classify_markup(markup, final_url).outcome != "success"
        or not is_official_product_url(task.platform, final_url)
        or _native_product_id(task.platform, final_url) != discovered.native_product_id
    ):
        return None
    soup = BeautifulSoup(markup, "html.parser")
    title_node = soup.select_one("h1, [class*='sku-name'], [class*='goods-name'], [class*='main-title']")
    title = " ".join(title_node.get_text(" ", strip=True).split()) if title_node else ""
    if not _title_matches(task, title):
        return None
    prices: set[float] = set()
    for node in soup.select(".detail-price, [class*='detail-price'], [class*='price-current']"):
        if _hidden_node(node):
            continue
        text = node.get_text(" ", strip=True)
        if any(word in text for word in CONDITIONAL_PRICE_WORDS):
            continue
        match = re.search(r"(?:¥|￥)?\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
        if match and 0 < float(match.group(1)) <= 5000:
            prices.add(float(match.group(1)))
    if len(prices) != 1:
        return None
    return ProductCandidate(
        platform=task.platform,
        model_key=task.model_key,
        title=title,
        price=prices.pop(),
        product_url=final_url,
        stock_status=stock_status(soup.get_text(" ", strip=True)),
        native_product_id=discovered.native_product_id,
        detail_verified=True,
    )


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("config_revision"), int)
        or not isinstance(config.get("retired_task_ids"), list)
        or not isinstance(config.get("channels"), list)
        or not isinstance(config.get("models"), list)
    ):
        raise ValueError("configuration revision, retirement list, channels, and models are required")
    for group in config["models"]:
        speeds = [str(value) for value in group.get("speeds", [])]
        target_speed = str(group.get("target_speed", ""))
        if not target_speed or target_speed not in speeds:
            raise ValueError("each model group target_speed must be one of its configured speeds")
    return config


def clean_text(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def model_entries(config: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for group in config["models"]:
        for name in group["names"]:
            entries.append(
                {
                    "brand": group["brand"],
                    "model": str(name),
                    "speed": str(group["target_speed"]),
                    "notes": group.get("notes", ""),
                }
            )
    return entries


def stock_status(text: str) -> str:
    if any(word in text for word in SOLD_OUT_WORDS):
        return "缺货/售罄关键词命中"
    if any(word in text for word in IN_STOCK_WORDS):
        return "有货/可购买关键词命中"
    return "未识别库存关键词"


class ClashProxyController:
    def __init__(self, api_url: str = CLASH_API):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        response = self.session.get(f"{self.api_url}/proxies", timeout=5)
        response.raise_for_status()
        payload = response.json()
        selectors = [
            (str(name), value)
            for name, value in payload.get("proxies", {}).items()
            if isinstance(value, dict) and value.get("type") == "Selector"
        ]
        if not selectors:
            raise RuntimeError("no dynamic proxy selector")
        self.selector_name, _ = max(
            selectors,
            key=lambda item: len(item[1].get("all", [])),
        )
        self.nodes = discover_proxy_nodes(payload)

    def switch(self, node: str) -> None:
        endpoint = f"{self.api_url}/proxies/{quote(self.selector_name, safe='')}"
        response = self.session.put(endpoint, json={"name": node}, timeout=5)
        response.raise_for_status()


def browser_markup(
    url: str,
    platform: str,
    proxy_server: str | None = None,
) -> FetchResult:
    del platform
    started = time.monotonic()
    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            if proxy_server:
                launch_options["proxy"] = {"server": proxy_server}
            browser = playwright.chromium.launch(**launch_options)
            try:
                page = browser.new_page(locale="zh-CN")
                response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                markup = page.content()
                final_url = page.url
                classification = classify_markup(markup, final_url)
                return FetchResult(
                    markup,
                    classification.outcome,
                    response.status if response else None,
                    final_url,
                    classification.block_reason,
                    1,
                    int((time.monotonic() - started) * 1000),
                )
            finally:
                browser.close()
    except Exception as exc:
        return FetchResult(
            "",
            "error",
            None,
            url,
            f"browser:{type(exc).__name__}",
            1,
            int((time.monotonic() - started) * 1000),
        )


def _canary_evidence(task: ProductTask, fetched: FetchResult, method: str) -> dict[str, Any]:
    candidates = parse_product_cards(fetched.markup, task) if fetched.outcome == "success" else []
    outcome = "success" if candidates else fetched.outcome
    reason = fetched.block_reason
    if not candidates and outcome == "success":
        outcome = "blocked"
        reason = "no_same_card_candidate"
    return {
        "platform": task.platform,
        "model_key": task.model_key,
        "http_status": fetched.http_status,
        "final_url": fetched.final_url,
        "method": method,
        "outcome": outcome,
        "block_reason": None if candidates else reason,
        "attempts": fetched.attempts,
        "latency_ms": fetched.latency_ms,
    }


def _offline_results(tasks: list[ProductTask]) -> list[AttemptResult]:
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        AttemptResult(
            task,
            "blocked",
            None,
            None,
            None,
            None,
            task.query_url,
            "offline",
            "offline_smoke_no_network",
            0,
            0,
            checked_at,
        )
        for task in tasks
    ]


def run_live_round(
    tasks: list[ProductTask],
) -> tuple[list[AttemptResult], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    direct_session = requests.Session()
    direct_session.trust_env = False

    def direct_request(url: str) -> FetchResult:
        return request_markup(url, getter=direct_session.get)

    controller: ClashProxyController | None = None
    proxy_unavailable = False
    if os.getenv("PROXY_ENABLED", "").lower() == "true":
        try:
            controller = ClashProxyController()
        except (requests.RequestException, RuntimeError, ValueError):
            controller = None
            proxy_unavailable = True
    local_proxies = {"http": LOCAL_HTTP_PROXY, "https": LOCAL_HTTP_PROXY}
    selection_started = time.monotonic()
    routes: dict[str, str | None] = {}
    canaries: list[dict[str, Any]] = []
    proxy_stats: dict[str, dict[str, Any]] = {}
    batch_id = (
        f"shuttlecock_price_monitor:{os.getenv('GITHUB_RUN_ID', 'local')}:"
        f"{os.getenv('GITHUB_RUN_ATTEMPT', '0')}"
    )
    rotating_canaries = select_rotating_canaries(
        tasks,
        batch_id=batch_id,
        per_platform=3,
    )

    for platform in ("taobao", "jd", "pdd"):
        platform_canaries = rotating_canaries[platform]
        if proxy_unavailable:
            # 代理声称启用但控制面不可达：诚实 blocked，绝不静默降级直连。
            direct_evidence = [
                _canary_evidence(
                    task,
                    FetchResult("", "blocked", None, task.query_url, "proxy_unavailable", 0, 0),
                    "proxy-unavailable",
                )
                for task in platform_canaries
            ]
            canaries.extend(direct_evidence)
            routes[platform] = None
            proxy_stats[platform] = {
                "tested": 0,
                "selected": False,
                "budget_exhausted": False,
            }
            continue
        direct_results = [
            (task, direct_request(task.query_url))
            for task in platform_canaries
        ]
        direct_evidence = [
            _canary_evidence(task, fetched, "requests-direct")
            for task, fetched in direct_results
        ]
        canaries.extend(direct_evidence)
        if any(row["outcome"] == "success" for row in direct_evidence):
            routes[platform] = None
            proxy_stats[platform] = {
                "tested": 0,
                "selected": False,
                "budget_exhausted": False,
            }
            continue

        selected: str | None = None
        selected_probe: tuple[ProductTask, FetchResult] | None = None
        stats = {"tested": 0, "selected": False, "budget_exhausted": False}
        # 预算按平台分配：三平台各 200s 独立分片（共 600s），
        # 避免首个平台耗尽全局预算后其它平台零机会。
        remaining = 200.0
        if controller is not None and remaining > 0:
            last_fetch: list[tuple[ProductTask, FetchResult] | None] = [None]
            # 浏览器渲染节流：同一平台最多尝试 RENDER_LIMIT 次渲染。
            # 全部节点出口被风控时，继续换节点渲染无意义且烧预算。
            render_remaining: list[int] = [RENDER_LIMIT]

            def probe(_: str, node: str, timeout: float) -> bool:
                del timeout
                try:
                    controller.switch(node)
                    for task in platform_canaries:
                        current = request_markup(
                            task.query_url,
                            getter=direct_session.get,
                            proxies=local_proxies,
                            max_attempts=1,
                        )
                        last_fetch[0] = (task, current)
                        if (
                            current.outcome == "success"
                            and parse_product_cards(current.markup, task)
                        ):
                            probe_method[0] = "requests-proxy"
                            return True
                        # JS 壳：requests 可达但无商品卡（200/403/None 状态）时，
                        # 用浏览器渲染搜索页（走代理出口）再解析，受渲染节流限制。
                        # 连接失败（outcome=error）不触发浏览器，避免无谓开销。
                        if (
                            render_remaining[0] > 0
                            and current.outcome in ("blocked", "success")
                            and current.http_status in (200, 403, None)
                        ):
                            render_remaining[0] -= 1
                            try:
                                rendered = browser_markup(
                                    task.query_url,
                                    task.platform,
                                    LOCAL_HTTP_PROXY,
                                )
                                last_fetch[0] = (task, rendered)
                                if (
                                    rendered.outcome == "success"
                                    and parse_product_cards(rendered.markup, task)
                                ):
                                    probe_method[0] = "browser-proxy"
                                    return True
                            except Exception:
                                continue
                except requests.RequestException:
                    return False
                return False

            probe_method: list[str] = ["requests-proxy"]

            selected, stats = select_proxy_node(
                platform,
                controller.nodes,
                probe=probe,
                total_budget=remaining,
                node_timeout=15,
            )
            selected_probe = last_fetch[0]
        routes[platform] = selected
        proxy_stats[platform] = stats
        if selected_probe is not None:
            canaries.append(
                _canary_evidence(
                    selected_probe[0],
                    selected_probe[1],
                    probe_method[0],
                )
            )

    all_results: list[AttemptResult] = []
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    for platform in ("taobao", "jd", "pdd"):
        platform_tasks = [task for task in tasks if task.platform == platform]
        if proxy_unavailable:
            # 代理不可用：诚实 blocked，不发起任何直连或浏览器请求。
            all_results.extend(
                AttemptResult(
                    task,
                    "blocked",
                    None,
                    None,
                    None,
                    None,
                    task.query_url,
                    "proxy-unavailable",
                    "proxy_unavailable",
                    0,
                    0,
                    checked_at,
                )
                for task in platform_tasks
            )
            continue
        if routes[platform] is None and not any(
            row["platform"] == platform and row["outcome"] == "success"
            for row in canaries
        ):
            # 该平台 canary 全 blocked 且无代理可用：平台不可达，任务直接跳过，
            # 不逐任务发起请求或开浏览器（避免 45s×23 次的无谓超时）。
            all_results.extend(
                AttemptResult(
                    task,
                    "blocked",
                    None,
                    None,
                    None,
                    None,
                    task.query_url,
                    "canary-skip",
                    "platform_unreachable",
                    0,
                    0,
                    checked_at,
                )
                for task in platform_tasks
            )
            continue
        selected = routes[platform]
        if selected is not None and controller is not None:
            try:
                controller.switch(selected)
            except requests.RequestException:
                selected = None
        if selected is None:
            request_fn = direct_request

            def browser_fn(url: str, current_platform: str) -> FetchResult:
                return browser_markup(url, current_platform)
        else:
            def request_fn(url: str) -> FetchResult:
                return request_markup(
                    url,
                    getter=direct_session.get,
                    proxies=local_proxies,
                )

            def browser_fn(url: str, current_platform: str) -> FetchResult:
                return browser_markup(url, current_platform, LOCAL_HTTP_PROXY)

        all_results.extend(
            crawl_tasks(
                platform_tasks,
                request_fn=request_fn,
                browser_fn=browser_fn,
            )
        )
    order = {task.key: index for index, task in enumerate(tasks)}
    all_results.sort(key=lambda result: order[result.task.key])
    return all_results, canaries, proxy_stats


def _read_existing_history(site_dir: Path) -> list[dict[str, Any]]:
    for path in (STATE_DIR / "history.json", site_dir / "data" / "price_history.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            return data
    return []


def quality_gate(site_dir: Path = SITE_DIR) -> bool:
    try:
        statuses = json.loads((site_dir / "data" / "status.json").read_text(encoding="utf-8"))
        envelope = json.loads((site_dir / "data" / "batch.json").read_text(encoding="utf-8"))
        manifest = json.loads((site_dir / "manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((site_dir / "audit.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(statuses, list)
        and envelope.get("mode") == "live"
        and audit.get("structure_status") == "pass"
        and manifest.get("batch_id") == envelope.get("batch_id")
        and not verify_file_manifest(site_dir, manifest)
        and product_quality_gate(statuses)
    )


def structure_gate(site_dir: Path, expected_task_ids: list[str]) -> bool:
    try:
        envelope = json.loads((site_dir / "data" / "batch.json").read_text(encoding="utf-8"))
        manifest = json.loads((site_dir / "manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((site_dir / "audit.json").read_text(encoding="utf-8"))
        statuses = json.loads((site_dir / "data" / "status.json").read_text(encoding="utf-8"))
        prices = json.loads((site_dir / "data" / "prices.json").read_text(encoding="utf-8"))
        validate_live_envelope(envelope, expected_task_ids)
    except (OSError, json.JSONDecodeError, ContractError, TypeError):
        return False
    return (
        statuses == envelope.get("statuses")
        and prices == envelope.get("prices")
        and audit.get("structure_status") == "pass"
        and manifest.get("schema_version") == 4
        and manifest.get("batch_id") == envelope.get("batch_id")
        and manifest.get("source_sha") == envelope.get("source_sha")
        and manifest.get("config_sha256") == envelope.get("config_sha256")
        and manifest.get("mode") == "live"
        and not verify_file_manifest(site_dir, manifest)
    )


def _source_sha() -> str:
    value = os.getenv("SOURCE_SHA") or os.getenv("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40


def main() -> int:
    parser = argparse.ArgumentParser(description="Honest same-card shuttlecock price monitor.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--live", action="store_true", help="perform the complete 69-task live round")
    parser.add_argument("--output", action="store_true")
    parser.add_argument("--quality-gate", action="store_true")
    parser.add_argument("--structure-gate", action="store_true")
    parser.add_argument("--site-dir", type=Path, default=SITE_DIR)
    args = parser.parse_args()
    if args.quality_gate:
        passed = quality_gate(args.site_dir)
        print(json.dumps({"live_price_quality_gate": "pass" if passed else "fail"}))
        return 0 if passed else 1

    config = load_config(args.config)
    tasks = build_tasks(config)
    if args.structure_gate:
        passed = structure_gate(args.site_dir, [task.key for task in tasks])
        print(json.dumps({"structure_gate": "pass" if passed else "fail"}))
        return 0 if passed else 1
    existing_history = _read_existing_history(args.site_dir)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if args.live:
        results, canaries, proxy_stats = run_live_round(tasks)
    else:
        results = _offline_results(tasks)
        canaries = []
        proxy_stats = {}
    mode = "live" if args.live else "fixture"
    public = build_public_data(results, tasks, existing_history, mode=mode)
    evidence = build_live_evidence(results, canaries=canaries, proxy_stats=proxy_stats)
    config_sha = hashlib.sha256(args.config.read_bytes()).hexdigest()
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    envelope = build_batch_envelope(
        public,
        tasks,
        evidence,
        mode=mode,
        run_id=os.getenv("GITHUB_RUN_ID", "local"),
        run_attempt=os.getenv("GITHUB_RUN_ATTEMPT", "0"),
        source_sha=_source_sha(),
        config_sha256=config_sha,
        started_at=started_at,
        finished_at=finished_at,
    )
    report = audit_envelope(envelope, [task.key for task in tasks])
    if args.output:
        publish_site(
            public,
            site_dir=args.site_dir,
            evidence=evidence,
            envelope=envelope,
            audit_report=report,
        )
        if mode == "live":
            write_state_directory(
                STATE_DIR,
                repo="shuttlecock_price_monitor",
                branch=os.getenv("GITHUB_REF_NAME", "main"),
                run_id=envelope["run_id"],
                config_sha256=config_sha,
                batch_id=envelope["batch_id"],
                completed_task_ids=[row["task_id"] for row in public["status"]],
                history=public["price_history"],
            )
    print(json.dumps(public["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
