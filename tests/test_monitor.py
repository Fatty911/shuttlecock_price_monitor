from pathlib import Path
from dataclasses import replace
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest
import requests

from shuttle_monitor.monitor import (
    AttemptResult,
    ProductCandidate,
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


def test_builds_exactly_93_unique_tasks():
    tasks = build_tasks(load_config())
    assert len(tasks) == 93
    assert len({task.key for task in tasks}) == 93
    assert len({task.model_key for task in tasks}) == 31
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
        ("jd", "http://item.jd.com/123456.html", True),
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


def test_round_schema_has_fixed_93_statuses_and_conservation():
    tasks = build_tasks(load_config())
    results = [result_for(task) for task in tasks]
    summary = validate_round(results, tasks)
    assert summary["attempted"] == summary["blocked"] == 93
    assert summary["success"] == summary["error"] == 0
    assert summary["models"] == 31
    assert summary["platforms"] == {
        "taobao": {
            "attempted": 31,
            "success": 0,
            "blocked": 31,
            "rejected": 0,
            "error": 0,
            "out_of_stock": 0,
        },
        "jd": {
            "attempted": 31,
            "success": 0,
            "blocked": 31,
            "rejected": 0,
            "error": 0,
            "out_of_stock": 0,
        },
        "pdd": {
            "attempted": 31,
            "success": 0,
            "blocked": 31,
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
    assert len(public["status"]) == 93
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
    assert getter.calls[0][1]["timeout"] == (10, 25)


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
    assert len(calls) == 93
    assert len(results) == 93
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
    assert len(results) == 31
    assert len(browser_calls) == 10
    assert all(result.outcome == "blocked" for result in results)
    assert sum(result.block_reason == "browser_circuit_open" for result in results) == 21


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
    assert len(evidence["tasks"]) == 93
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


def test_proxy_enabled_but_controller_unreachable_blocks_all_without_direct_requests(monkeypatch):
    """PROXY_ENABLED=true 但 Clash 控制面不可达时：全部 blocked(proxy_unavailable)，
    不得静默降级直连（避免把代理故障伪装成直连结果）。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.setenv("PROXY_ENABLED", "true")
    calls = []

    def boom(*args, **kwargs):
        raise requests.RequestException("control plane down")

    monkeypatch.setattr(mon.ClashProxyController, "__init__", boom)
    # 如果实现错误地走了直连/浏览器，这里会被调用
    monkeypatch.setattr(mon, "request_markup", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not issue direct requests")))
    monkeypatch.setattr(mon, "browser_markup", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch browser")))

    results, canaries, stats = mon.run_live_round(tasks)
    assert len(results) == 93
    assert all(r.outcome == "blocked" and r.block_reason == "proxy_unavailable" for r in results)
    assert len(canaries) == 9
    assert all(c["outcome"] == "blocked" and c["block_reason"] == "proxy_unavailable" for c in canaries)
    assert all(s["tested"] == 0 and s["selected"] is False for s in stats.values())


def test_proxy_disabled_still_uses_direct_requests(monkeypatch):
    """PROXY_ENABLED=false（默认无代理）时保持直连语义不变。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.delenv("PROXY_ENABLED", raising=False)
    direct_hits = []

    def fake_request_markup(url, *args, **kwargs):
        direct_hits.append(url)
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    def fake_browser_markup(url, platform, proxy_server=None):
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)
    results, canaries, stats = mon.run_live_round(tasks)
    assert len(results) == 93
    assert direct_hits, "直连 canary 应被调用"
    assert all(s["tested"] == 0 for s in stats.values())


def test_all_blocked_canaries_skip_platform_tasks_without_extra_requests(monkeypatch):
    """直连 canary 全 blocked 且无代理时：该平台任务直接跳过（快速 blocked），
    不逐任务发起请求或开浏览器。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.delenv("PROXY_ENABLED", raising=False)
    request_calls = []
    browser_calls = []

    def fake_request_markup(url, *args, **kwargs):
        request_calls.append(url)
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    def fake_browser_markup(url, platform, proxy_server=None):
        browser_calls.append(url)
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)

    results, canaries, stats = mon.run_live_round(tasks)
    assert len(results) == 93
    # 只允许 9 次 canary 直连（3 平台 × 3），任务全部跳过
    assert len(request_calls) == 9, f"expected 9 canary requests, got {len(request_calls)}"
    assert len(browser_calls) == 0, f"expected no browser launches, got {len(browser_calls)}"
    assert all(r.outcome == "blocked" and r.block_reason == "platform_unreachable" for r in results)
    summary = validate_round(results, tasks)
    assert summary["attempted"] == summary["blocked"] == 93


def test_success_canary_keeps_platform_tasks_running(monkeypatch):
    """canary 有 success 的平台必须照常逐任务爬取（不跳过）。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.delenv("PROXY_ENABLED", raising=False)
    request_calls = []

    def fake_request_markup(url, *args, **kwargs):
        request_calls.append(url)
        return FetchResult("<html>ok</html>", "success", 200, url, None, 1, 5)

    def fake_parse_cards(markup, task):
        # 让所有 canary 都能解析出候选 → canary success
        return [
            ProductCandidate(
                platform=task.platform,
                model_key=task.model_key,
                title="匹配",
                price=100.0,
                product_url=f"https://item.{task.platform}.test/1",
                stock_status="in_stock",
                native_product_id="1",
            )
        ]

    def fake_browser_markup(url, platform, proxy_server=None):
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "parse_product_cards", fake_parse_cards)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)

    results, canaries, stats = mon.run_live_round(tasks)
    # canary 全部 success → 平台任务照常发起请求（远多于 9 次 canary）
    assert len(request_calls) > 9, f"expected task requests beyond canaries, got {len(request_calls)}"
    assert len(results) == 93
    # 任务被实际爬取而非跳过
    assert all(r.method != "canary-skip" for r in results)


def test_canary_probe_uses_browser_render_for_js_shell(monkeypatch):
    """requests 返回 JS 壳（200 无卡片）时，probe 必须用浏览器渲染解析出卡片，
    平台任务照常爬取（不再 canary-skip）。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.setenv("PROXY_ENABLED", "true")
    browser_calls = []

    # controller 正常（模拟代理可用）
    class FakeController:
        def __init__(self):
            self.nodes = ["node-a"]
        def switch(self, node):
            pass

    monkeypatch.setattr(mon, "ClashProxyController", FakeController)

    # requests 返回 JS 壳：200 但无商品卡
    def fake_request_markup(url, *args, **kwargs):
        return FetchResult("<html><body>JS shell</body></html>", "success", 200, url, None, 1, 5)

    # 浏览器渲染后能解析出卡片
    def fake_browser_markup(url, platform, proxy_server=None):
        browser_calls.append((url, proxy_server))
        return FetchResult("<html>rendered</html>", "success", 200, url, None, 1, 5)

    def fake_parse_cards(markup, task):
        if "rendered" in markup:
            return [
                ProductCandidate(
                    platform=task.platform,
                    model_key=task.model_key,
                    title="匹配",
                    price=100.0,
                    product_url=f"https://item.{task.platform}.test/1",
                    stock_status="in_stock",
                    native_product_id="1",
                )
            ]
        return []

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)
    monkeypatch.setattr(mon, "parse_product_cards", fake_parse_cards)

    results, canaries, stats = mon.run_live_round(tasks)
    # 浏览器必须被调用（渲染 JS 壳），且走代理
    assert browser_calls, "browser render must be attempted for JS shell"
    assert all(proxy_server == mon.LOCAL_HTTP_PROXY for _, proxy_server in browser_calls)
    # 平台任务不应全 canary-skip（至少能尝试爬取）
    assert any(r.method != "canary-skip" for r in results)


def test_canary_probe_skips_browser_for_connection_failures(monkeypatch):
    """requests 连接失败（error）时，probe 不得触发浏览器渲染。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.setenv("PROXY_ENABLED", "true")
    browser_calls = []

    class FakeController:
        def __init__(self):
            self.nodes = ["node-a"]
        def switch(self, node):
            pass

    monkeypatch.setattr(mon, "ClashProxyController", FakeController)

    def fake_request_markup(url, *args, **kwargs):
        return FetchResult("", "error", 503, url, "http_503", 1, 5000)

    def fake_browser_markup(url, platform, proxy_server=None):
        browser_calls.append(url)
        return FetchResult("", "blocked", 403, url, "http_403", 1, 5)

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)

    results, canaries, stats = mon.run_live_round(tasks)
    assert browser_calls == [], f"browser must not run for connection failures, got {len(browser_calls)}"
    assert len(results) == 93


def test_canary_browser_render_is_throttled_per_platform(monkeypatch):
    """同一平台浏览器渲染受 RENDER_LIMIT 节流：超过后不再触发渲染。"""
    import os
    from shuttle_monitor import monitor as mon
    tasks = build_tasks(load_config())
    monkeypatch.setenv("PROXY_ENABLED", "true")
    browser_calls = []

    class FakeController:
        def __init__(self):
            self.nodes = [f"node-{i}" for i in range(20)]
        def switch(self, node):
            pass

    monkeypatch.setattr(mon, "ClashProxyController", FakeController)

    def fake_request_markup(url, *args, **kwargs):
        return FetchResult("<html>js shell</html>", "success", 200, url, None, 1, 5)

    def fake_browser_markup(url, platform, proxy_server=None):
        browser_calls.append((platform, url))
        return FetchResult("<html>still shell</html>", "success", 200, url, None, 1, 5)

    def fake_parse_cards(markup, task):
        return []

    monkeypatch.setattr(mon, "request_markup", fake_request_markup)
    monkeypatch.setattr(mon, "browser_markup", fake_browser_markup)
    monkeypatch.setattr(mon, "parse_product_cards", fake_parse_cards)

    results, canaries, stats = mon.run_live_round(tasks)
    # 每平台渲染 ≤ RENDER_LIMIT，总渲染 ≤ 3 × RENDER_LIMIT
    assert len(browser_calls) <= 3 * mon.RENDER_LIMIT, f"got {len(browser_calls)} renders"
    per_platform = {}
    for platform, _url in browser_calls:
        per_platform[platform] = per_platform.get(platform, 0) + 1
    assert all(count <= mon.RENDER_LIMIT for count in per_platform.values()), per_platform
    # 三个平台都应有探测机会（预算独立分片）
    assert all(s["tested"] > 0 for s in stats.values()), stats
