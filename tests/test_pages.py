from pathlib import Path


WEB = Path(__file__).parents[1] / "web"


def test_pages_assets_distinguish_live_states_without_sample_or_secret_fallbacks():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    styles = (WEB / "styles.css").read_text(encoding="utf-8")
    combined = f"{index}\n{script}\n{styles}"
    for state in ("loading", "structure-blocked", "live-blocked", "empty", "history-only"):
        assert state in combined
    assert "SAMPLE_ROWS" not in combined
    assert "fixture" not in script.lower()
    assert "localStorage" not in script
    assert "token" not in script.lower()
    assert ".innerHTML" not in script
    assert "textContent" in script
    assert "min-height: 44px" in styles
    for control in (
        "platform-filter",
        "brand-filter",
        "outcome-filter",
        "freshness-filter",
        "model-filter",
        "sort-order",
    ):
        assert control in index
    assert 'fetch("data/price_history.json"' in script
    assert "Promise.all" not in script
    assert "history-load" in index and "history-prev" in index and "history-next" in index
    assert "HISTORY_PAGE_SIZE = 50" in script
