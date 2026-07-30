from pathlib import Path
from dataclasses import replace
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest
import requests

from shuttle_monitor.monitor import (
    AttemptResult,
    BrowserCircuitBreaker,
    BrowserLimiter,
    FetchResult,
    ProductTask,
    RequestLimiter,
    build_live_evidence,
    build_public_data,
    build_tasks,
    classify_markup,
    crawl_tasks,
    discover_proxy_nodes,
    is_official_product_url,
    load_config,
    parse_product_cards,
    publish_site,
    prioritize_tasks,
    request_markup,
    sanitize_history,
    select_proxy_node,
    select_rotating_canaries,
    validate_round,
    verify_detail_candidate,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return FIXTURES.joinpath(name).read_text(encoding="utf-8")


def test_builds_exactly_69_unique_tasks():
    tasks = build_tasks(load_config())
    assert len(tasks) == 69
    assert len({task.key for task in tasks}) == 69
    assert len({task.model_key for task in tasks}) == 23
    assert {task.platform for task in tasks} == {"taobao", "jd", "pdd"}
    assert all(task.query_url and task.product_url is None for task in tasks)
    assert all(task.speed and f":speed-{task.speed}" in task.key for task in tasks)
    assert all(task.speed in task.query_url for task in tasks)


@pytest.mark.parametrize(
    ("platform", "fixture_name", "expected_url", "expected_price"),
    [
        ("taobao", "taobao_product_cards.html", "https://detail.tmall.com/item.htm?id=10001", 118.0),
        ("jd", "jd_product_cards.html", "https://item.jd.com/10002.html", 121.5),
        ("pdd", "pdd_product_cards.html", "https://mobile.yangkeduo.com/goods.html?goods_id=10003", 109.9),
    ],
)
def test_parser_binds_title_price_and_official_url_from_same_card(
    platform: str, fixture_name: str, expected_url: str, expected_price: float
):
    task = ProductTask(platform, "平台", "尤尼克斯", "AS20", "https://query.invalid", None)
    candidates = parse_product_cards(fixture(fixture_name), task)
    assert len(candidates) == 1
    assert candidates[0].product_url == expected_url
    assert candidates[0].price == expected_price
    assert "尤尼克斯 AS20" in candidates[0].title
    assert candidates[0].native_product_id in {"10001", "10002", "10003"}


def test_configured_speed_must_match_the_same_discovery_card():
    task = next(
        task
        for task in build_tasks(load_config())
        if task.platform == "jd" and task.model == "AS20"
    )
    assert task.speed == "77"
    assert parse_product_cards(fixture("jd_product_cards.html"), replace(task, speed="78")) == []


@pytest.mark.parametrize(
    "markup",
    [
        """
        <li class="gl-item" data-sku="7">
          <a href="https://item.jd.com/7.html">尤尼克斯 AS20 羽毛球 77速 空桶配件</a>
          <span class="p-price">¥12.00</span>
        </li>
        """,
        """
        <li class="gl-item" data-sku="8">
          <a href="https://item.jd.com/8.html">尤尼克斯 AS20 羽毛球 77速</a>
          <span class="p-price">分期每月 ¥9.90 x 12</span>
        </li>
        """,
        """
        <li class="gl-item" data-sku="9">
          <a href="https://item.jd.com/9.html">尤尼克斯 AS20 羽毛球 77速</a>
          <span class="p-price" hidden>¥9.90</span>
        </li>
        """,
    ],
)
def test_accessory_installment_and_hidden_prices_are_rejected(markup):
    task = next(
        task
        for task in build_tasks(load_config())
        if task.platform == "jd" and task.model == "AS20"
    )
    assert parse_product_cards(markup, task) == []


@pytest.mark.parametrize(
    ("platform", "search_fixture", "detail_fixture", "expected_price"),
    [
        ("taobao", "taobao_product_cards.html", "taobao_product_detail.html", 116.0),
        ("jd", "jd_product_cards.html", "jd_product_detail.html", 120.0),
        ("pdd", "pdd_product_cards.html", "pdd_product_detail.html", 108.0),
    ],
)
def test_search_card_is_only_discovery_until_official_detail_is_verified(
    platform: str,
    search_fixture: str,
    detail_fixture: str,
    expected_price: float,
):
    task = ProductTask(platform, "平台", "尤尼克斯", "AS20", "https://query.invalid", None)
    discovered = parse_product_cards(fixture(search_fixture), task)[0]
    verified = verify_detail_candidate(
        fixture(detail_fixture),
        task,
        discovered,
        discovered.product_url,
    )
    assert verified is not None
    assert verified.price == expected_price
    assert verified.native_product_id == discovered.native_product_id
    assert verified.detail_verified is True


def test_detail_id_mismatch_or_ambiguous_price_is_rejected():
    task = ProductTask("jd", "京东", "尤尼克斯", "AS20", "https://query.invalid", None)
    discovered = parse_product_cards(fixture("jd_product_cards.html"), task)[0]
    assert verify_detail_candidate(
        fixture("jd_product_detail.html"),
        task,
        discovered,
        "https://item.jd.com/99999.html",
    ) is None
    assert verify_detail_candidate(
        fixture("jd_product_detail_ambiguous.html"),
        task,
        discovered,
        discovered.product_url,
    ) is None


@pytest.mark.parametrize(
    ("platform", "url", "valid"),
    [
        ("taobao", "https://item.taobao.com/item.htm?id=1", True),
        ("taobao", "https://detail.tmall.com/item.htm?id=1", True),
        ("taobao", "https://s.taobao.com/search?q=AS20", False),
        ("taobao", "https://evil.test/?next=item.taobao.com/item.htm?id=1", False),
        ("jd", "https://item.jd.com/123456.html", True),
        ("jd", "http://item.jd.com/123456.html", False),
        ("jd", "https://search.jd.com/Search?keyword=AS20", False),
        ("pdd", "https://mobile.yangkeduo.com/goods.html?goods_id=8", True),
        ("pdd", "https://mobile.yangkeduo.com/search_result.html?goods_id=8", False),
    ],
)
def test_official_product_url_validates_host_and_path(platform: str, url: str, valid: bool):
    assert is_official_product_url(platform, url) is valid


@pytest.mark.parametrize("price", [0, -1, 100000])
def test_non_positive_or_implausible_prices_are_rejected(price: float):
    markup = fixture("jd_product_cards.html").replace("121.50", str(price))
    task = ProductTask("jd", "京东", "尤尼克斯", "AS20", "https://query.invalid", None)
    assert parse_product_cards(markup, task) == []


@pytest.mark.parametrize("platform", ["taobao", "jd", "pdd"])
def test_challenge_fixture_is_blocked_and_never_parsed(platform: str):
    markup = fixture(f"{platform}_challenge.html")
    classification = classify_markup(markup, "https://example.test/")
    assert classification.outcome == "blocked"
    assert classification.block_reason
    task = ProductTask(platform, "平台", "尤尼克斯", "AS20", "https://query.invalid", None)
    assert parse_product_cards(markup, task) == []


def result_for(task: ProductTask, outcome: str = "blocked", **overrides):
    values = {
        "task": task,
        "outcome": outcome,
        "price": None,
        "product_url": None,
        "title": None,
        "http_status": 403,
        "final_url": task.query_url,
        "method": "requests",
        "block_reason": "captcha",
        "attempts": 1,
        "latency_ms": 12,
        "checked_at": "2026-07-30T00:00:00Z",
    }
    values.update(overrides)
    return AttemptResult(**values)


def test_round_schema_has_fixed_69_statuses_and_conservation():
    tasks = build_tasks(load_config())
    results = [result_for(task) for task in tasks]
    summary = validate_round(results, tasks)
    assert summary["attempted"] == summary["blocked"] == 69
    assert summary["success"] == summary["error"] == 0
    assert summary["models"] == 23
    assert summary["platforms"] == {
        "taobao": {
            "attempted": 23,
            "success": 0,
            "blocked": 23,
            "rejected": 0,
            "error": 0,
            "out_of_stock": 0,
        },
        "jd": {
            "attempted": 23,
            "success": 0,
            "blocked": 23,
            "rejected": 0,
            "error": 0,
            "out_of_stock": 0,
        },
        "pdd": {
            "attempted": 23,
            "success": 0,
            "blocked": 23,
            "rejected": 0,
            "error": 0,
            "out_of_stock": 0,
        },
    }


def test_blocked_and_error_cannot_carry_price_or_product_url():
    task = build_tasks(load_config())[0]
    poisoned = result_for(
        task,
        outcome="blocked",
        price=99.0,
        product_url="https://item.taobao.com/item.htm?id=1",
    )
    with pytest.raises(ValueError, match="blocked/error"):
        validate_round([poisoned], [task])


def test_success_without_native_id_and_detail_verification_fails_structure():
    task = build_tasks(load_config())[0]
    unverified = result_for(
        task,
        outcome="success",
        price=99.0,
        product_url="https://item.taobao.com/item.htm?id=1",
        title=f"{task.model_key} {task.speed}速 羽毛球",
        block_reason=None,
    )
    with pytest.raises(ValueError, match="detail"):
        validate_round([unverified], [task])


def test_polluted_legacy_history_is_not_migrated():
    old = [
        {"platform": "taobao", "model_key": "尤尼克斯 AS20", "price": 9999, "product_url": None},
        {
            "platform": "jd",
            "model_key": "尤尼克斯 AS20",
            "price": 99,
            "confidence": 0,
            "product_url": "https://item.jd.com/1.html",
        },
        {
            "platform": "pdd",
            "model_key": "尤尼克斯 AS20",
            "price": 88,
            "product_url": "https://mobile.yangkeduo.com/search_result.html?goods_id=1",
        },
    ]
    assert sanitize_history(old) == []


def test_legacy_price_without_detail_evidence_is_quarantined_from_public_history():
    unverifiable = [
        {
            "platform": "jd",
            "model_key": "尤尼克斯 AS20",
            "title": "尤尼克斯 AS20 77速",
            "price": 99,
            "product_url": "https://item.jd.com/1.html",
        }
    ]
    assert sanitize_history(unverifiable) == []


def test_all_blocked_still_publishes_status_page_but_quality_gate_is_false(tmp_path):
    tasks = build_tasks(load_config())
    results = [result_for(task) for task in tasks]
    public = build_public_data(results, tasks, [])
    assert len(public["status"]) == 69
    assert public["prices"] == []
    assert public["price_history"] == []
    assert public["quality_gate"] is False
    publish_site(public, tmp_path)
    assert (tmp_path / "data/status.json").exists()
    assert (tmp_path / "data/prices.json").read_text(encoding="utf-8").strip() == "[]"
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "app.js" in page
    assert "live-blocked" in (tmp_path / "app.js").read_text(encoding="utf-8")
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "audit.json").exists()
    assert "9999" not in page
    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == "shuttlecocks.jiucai.eu.org\n"


def test_partial_success_only_adds_verified_current_candidate():
    tasks = build_tasks(load_config())
    success_task = next(task for task in tasks if task.platform == "jd" and task.model == "AS20")
    results = [result_for(task) for task in tasks]
    index = tasks.index(success_task)
    results[index] = result_for(
        success_task,
        outcome="success",
        price=121.5,
        product_url="https://item.jd.com/10002.html",
        title="尤尼克斯 AS20 羽毛球 12只装 77速",
        http_status=200,
        block_reason=None,
        native_product_id="10002",
        detail_verified=True,
    )
    public = build_public_data(results, tasks, [])
    assert len(public["prices"]) == 1
    assert len(public["price_history"]) == 1
    assert public["prices"][0]["product_url"] == "https://item.jd.com/10002.html"
    current = next(row for row in public["status"] if row["key"] == success_task.key)
    blocked = next(row for row in public["status"] if row["key"] != success_task.key)
    assert current["price_status"] == "fresh"
    assert blocked["price_status"] == "none"
    assert blocked["price"] is None and blocked["product_url"] is None


class FakeResponse:
    def __init__(self, status_code=200, text="<html>ok</html>", url="https://example.test/final"):
        self.status_code = status_code
        self.text = text
        self.url = url


class SequenceGet:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_requests_retries_only_transient_statuses(status):
    getter = SequenceGet([FakeResponse(status), FakeResponse(status), FakeResponse(200)])
    fetched = request_markup("https://search.jd.com/Search?q=x", getter=getter, sleep=lambda _: None)
    assert fetched.attempts == 3
    assert fetched.http_status == 200
    assert getter.calls[0][1]["timeout"] == (5, 10)


def test_requests_retries_connection_errors_but_not_403_or_challenge():
    connection = SequenceGet(
        [requests.ConnectionError("down"), FakeResponse(200, "<html>product shell</html>")]
    )
    assert request_markup("https://example.test", getter=connection, sleep=lambda _: None).attempts == 2
    forbidden = SequenceGet([FakeResponse(403), FakeResponse(200)])
    fetched = request_markup("https://example.test", getter=forbidden, sleep=lambda _: None)
    assert fetched.attempts == 1 and fetched.outcome == "blocked"
    challenge = SequenceGet([FakeResponse(200, fixture("jd_challenge.html")), FakeResponse(200)])
    fetched = request_markup("https://example.test", getter=challenge, sleep=lambda _: None)
    assert fetched.attempts == 1 and fetched.outcome == "blocked"


def test_request_limiter_enforces_global_and_per_host_limits_and_releases_on_error():
    limiter = RequestLimiter(global_limit=6, per_host_limit=2)
    lock = threading.Lock()
    active = 0
    peak = 0
    host_active = {}
    host_peak = {}

    def work(url):
        nonlocal active, peak
        host = url.split("/")[2]
        with limiter.slot(url):
            with lock:
                active += 1
                host_active[host] = host_active.get(host, 0) + 1
                peak = max(peak, active)
                host_peak[host] = max(host_peak.get(host, 0), host_active[host])
            time.sleep(0.015)
            with lock:
                active -= 1
                host_active[host] -= 1

    urls = [f"https://a.test/{i}" for i in range(8)] + [f"https://b.test/{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(work, urls))
    assert peak <= 4  # two hosts, two slots each; also below global six
    assert host_peak == {"a.test": 2, "b.test": 2}

    with pytest.raises(RuntimeError):
        with limiter.slot("https://a.test/error"):
            raise RuntimeError("boom")
    with limiter.slot("https://a.test/after-error"):
        pass


def test_cancelled_wait_does_not_leak_or_overrelease_request_semaphores():
    limiter = RequestLimiter(global_limit=1, per_host_limit=1)
    cancelled = threading.Event()
    entered = threading.Event()

    with limiter.slot("https://a.test/held"):
        def wait_for_slot():
            entered.set()
            with limiter.slot("https://a.test/waiting", cancelled=cancelled):
                raise AssertionError("cancelled waiter entered slot")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait_for_slot)
            assert entered.wait(1)
            cancelled.set()
            with pytest.raises(CancelledError):
                future.result(timeout=1)
    with limiter.slot("https://a.test/reusable"):
        pass


def test_browser_limits_and_three_identical_blocks_open_platform_circuit():
    limiter = BrowserLimiter(global_limit=2)
    breaker = BrowserCircuitBreaker(threshold=3)
    for _ in range(2):
        assert breaker.allow("jd")
        with limiter.slot("jd"):
            breaker.record("jd", "captcha")
    assert breaker.allow("jd")
    with limiter.slot("jd"):
        breaker.record("jd", "captcha")
    assert not breaker.allow("jd")
    assert breaker.allow("taobao")
    breaker.record("jd", None)
    assert breaker.allow("jd")
    with limiter.slot("jd"):
        pass


def test_proxy_nodes_are_dynamic_and_selection_obeys_budgets():
    payload = {
        "proxies": {
            "selector-generated-name": {
                "type": "Selector",
                "all": ["DIRECT", "BALANCE", "node-a", "node-b", "REJECT"],
            },
            "BALANCE": {"type": "LoadBalance", "all": ["node-a", "node-b"]},
            "node-a": {"type": "Shadowsocks"},
            "node-b": {"type": "Trojan"},
        }
    }
    assert discover_proxy_nodes(payload) == ["node-a", "node-b"]
    calls = []
    ticks = iter([0, 5, 10, 16, 20])

    def probe(platform, node, timeout):
        calls.append((platform, node, timeout))
        return node == "node-b"

    selected, stats = select_proxy_node(
        "jd",
        ["node-a", "node-b", "node-c"],
        probe=probe,
        total_budget=20,
        node_timeout=15,
        monotonic=lambda: next(ticks),
    )
    assert selected == "node-b"
    assert calls == [("jd", "node-a", 15), ("jd", "node-b", 15)]
    assert stats == {"tested": 2, "selected": True, "budget_exhausted": False}


def test_task_priority_and_canaries_are_deterministic_and_cover_multiple_models():
    tasks = build_tasks(load_config())
    prioritized = prioritize_tasks(tasks, completed_task_ids={tasks[0].key})
    assert tasks[0] not in prioritized
    assert prioritized == sorted(prioritized, key=lambda task: (task.priority, task.key))
    first = select_rotating_canaries(tasks, batch_id="batch-a", per_platform=3)
    second = select_rotating_canaries(tasks, batch_id="batch-b", per_platform=3)
    assert {platform: len(rows) for platform, rows in first.items()} == {
        "taobao": 3,
        "jd": 3,
        "pdd": 3,
    }
    assert all(len({task.model_key for task in rows}) == 3 for rows in first.values())
    assert first != second


def fetched(markup, outcome="success", status=200, reason=None):
    return FetchResult(markup, outcome, status, "https://example.test/final", reason, 1, 7)


def test_crawl_attempts_every_task_even_when_all_requests_are_blocked():
    tasks = build_tasks(load_config())
    calls = []

    def request_fn(url):
        calls.append(url)
        return fetched(fixture("jd_challenge.html"), "blocked", 403, "http_403")

    results = crawl_tasks(tasks, request_fn=request_fn, browser_fn=lambda *_: pytest.fail("no browser on 403"))
    assert len(calls) == 69
    assert len(results) == 69
    assert {result.outcome for result in results} == {"blocked"}
    assert all(result.price is None and result.product_url is None for result in results)


def test_browser_challenge_fuse_still_emits_all_platform_results():
    tasks = [task for task in build_tasks(load_config()) if task.platform == "jd"]
    browser_calls = []

    def browser_fn(url, platform):
        browser_calls.append((url, platform))
        return fetched(fixture("jd_challenge.html"), "blocked", 200, "验证码")

    results = crawl_tasks(
        tasks,
        request_fn=lambda _: fetched("<html><body>JavaScript shell</body></html>"),
        browser_fn=browser_fn,
    )
    assert len(results) == 23
    assert len(browser_calls) == 3
    assert all(result.outcome == "blocked" for result in results)
    assert sum(result.block_reason == "browser_circuit_open" for result in results) == 20


def test_partial_live_parser_success_is_same_card_and_never_uses_browser():
    task = next(
        task
        for task in build_tasks(load_config())
        if task.platform == "jd" and task.model == "AS20"
    )
    calls = []

    def request_fn(url):
        calls.append(url)
        if url == task.query_url:
            return fetched(fixture("jd_product_cards.html"))
        return FetchResult(
            fixture("jd_product_detail.html"),
            "success",
            200,
            url,
            None,
            1,
            7,
        )

    results = crawl_tasks(
        [task],
        request_fn=request_fn,
        browser_fn=lambda *_: pytest.fail("parser success must not use browser"),
    )
    assert results[0].outcome == "success"
    assert results[0].price == 120.0
    assert results[0].product_url == "https://item.jd.com/10002.html"
    assert results[0].method == "requests-detail"
    assert calls == [task.query_url, "https://item.jd.com/10002.html"]


def test_live_evidence_has_required_observability_without_proxy_secrets_or_node_names():
    tasks = build_tasks(load_config())
    results = [
        result_for(task, final_url="https://login.example.test", block_reason="captcha")
        for task in tasks
    ]
    evidence = build_live_evidence(
        results,
        canaries=[
            {
                "platform": "jd",
                "http_status": 403,
                "final_url": "https://login.example.test",
                "method": "requests",
                "outcome": "blocked",
                "block_reason": "captcha",
                "attempts": 1,
                "latency_ms": 8,
            }
        ],
        proxy_stats={"jd": {"tested": 4, "selected": True, "budget_exhausted": False}},
    )
    assert len(evidence["tasks"]) == 69
    required = {
        "platform",
        "model_key",
        "http_status",
        "final_url",
        "method",
        "outcome",
        "block_reason",
        "attempts",
        "latency_ms",
    }
    assert all(required <= row.keys() for row in evidence["tasks"] + evidence["canaries"])
    serialized = __import__("json").dumps(evidence)
    assert "subscription" not in serialized.lower()
    assert "node-a" not in serialized
    assert "user:password@" not in serialized
