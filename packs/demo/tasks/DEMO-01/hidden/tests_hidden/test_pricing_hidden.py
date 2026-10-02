"""隐藏用例：口径与页面文案一致。"""
from shop.pricing import final_price


def test_threshold_is_100():
    assert final_price(100) == 72


def test_below_threshold_no_discount():
    assert final_price(99) == 89
