from shuttle_monitor.monitor import DiscountRule, best_cart_allocations, extract_candidates_from_text, lowest_per_channel_model


def test_extract_candidate_price_and_speed():
    channel = {"id": "jd", "name": "京东", "cart_discount_note": "领券"}
    model = {"brand": "尤尼克斯", "model": "AS20"}
    text = "尤尼克斯 AS20 羽毛球 12只装 球速77 券后 118 元 立即购买"
    candidates = extract_candidates_from_text(text, channel, model, "https://example.test")
    assert candidates
    assert candidates[0].base_price == 118
    assert candidates[0].speed == "77"
    assert candidates[0].stock_status == "可能有货"


def test_cart_discount_allocation_and_lowest_selection():
    channel = {"id": "taobao", "name": "淘宝", "cart_discount_note": "满减"}
    model = {"brand": "亚狮龙", "model": "Supreme"}
    first = extract_candidates_from_text("亚狮龙 Supreme 速度76 ￥120 现货", channel, model, "https://a.example")[0]
    second = extract_candidates_from_text("亚狮龙 Supreme 速度77 ￥110 现货", channel, model, "https://b.example")[0]
    records = best_cart_allocations([first, second], [DiscountRule("taobao", "每满200减30", 200, 30)])
    selected = lowest_per_channel_model(records)
    assert len(selected) == 1
    assert selected[0]["url"] == "https://b.example"
    assert selected[0]["effective_price"] < 110
