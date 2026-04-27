from tentaclez.strategy import OpenLot, StrategyParams, TickerState, generate_signal


PARAMS = StrategyParams(
    dip_percent=0.02, profit_percent=0.04, min_trade_pct=0.10, max_trade_pct=0.20
)


def _state(**overrides):
    base = dict(
        symbol="QQQ",
        current_price=500.0,
        last_buy_price=None,
        open_lots=[],
        free_cash=100.0,
    )
    base.update(overrides)
    return TickerState(**base)


def test_no_anchor_returns_hold():
    sig = generate_signal(_state(), PARAMS)
    assert sig.action == "HOLD"
    assert "no reference price" in sig.reason


def test_buy_when_price_dips_at_least_threshold():
    state = _state(last_buy_price=500.0, current_price=490.0)  # -2.0% exactly
    sig = generate_signal(state, PARAMS)
    assert sig.action == "BUY"
    assert sig.suggested_amount_usd == 20.0  # 20% of $100 free cash
    assert sig.suggested_price == 490.0
    assert sig.target_price == round(490.0 * 1.04, 2)


def test_no_buy_above_threshold():
    state = _state(last_buy_price=500.0, current_price=495.0)  # only -1%
    sig = generate_signal(state, PARAMS)
    assert sig.action == "HOLD"
    assert "above dip threshold" in sig.reason


def test_buy_blocked_when_cash_zero():
    state = _state(last_buy_price=500.0, current_price=480.0, free_cash=0.0)
    sig = generate_signal(state, PARAMS)
    assert sig.action == "HOLD"
    assert "free cash" in sig.reason.lower()


def test_sell_when_price_hits_lot_target():
    lot = OpenLot(id=1, qty=2.0, entry_price=480.0, target_price=499.20)  # +4%
    state = _state(
        last_buy_price=480.0,
        current_price=500.0,
        open_lots=[lot],
    )
    sig = generate_signal(state, PARAMS)
    assert sig.action == "SELL"
    assert sig.lot_id == 1
    assert sig.suggested_qty == 2.0
    assert sig.expected_profit_usd == round((500.0 - 480.0) * 2.0, 2)


def test_sell_picks_oldest_when_multiple_lots_match():
    lots = [
        OpenLot(id=2, qty=1.0, entry_price=470.0, target_price=488.80),
        OpenLot(id=1, qty=1.0, entry_price=460.0, target_price=478.40),
    ]
    state = _state(
        last_buy_price=470.0,
        current_price=490.0,
        open_lots=lots,
    )
    sig = generate_signal(state, PARAMS)
    assert sig.action == "SELL"
    assert sig.lot_id == 1  # oldest by id wins


def test_sell_takes_priority_over_buy_on_same_tick():
    """If price simultaneously dips below last buy and hits an old lot's target, sell first."""
    # synthetic but illustrative: we hit target on lot 1 even though current price is below last_buy
    lot = OpenLot(id=1, qty=1.0, entry_price=400.0, target_price=416.0)
    state = _state(
        last_buy_price=500.0,
        current_price=420.0,  # below 500 * 0.98 = 490 → would be a buy; but >= 416 → sell wins
        open_lots=[lot],
    )
    sig = generate_signal(state, PARAMS)
    assert sig.action == "SELL"


def test_position_size_uses_max_pct_of_free_cash():
    state = _state(last_buy_price=500.0, current_price=490.0, free_cash=200.0)
    sig = generate_signal(state, PARAMS)
    assert sig.action == "BUY"
    assert sig.suggested_amount_usd == 40.0  # 20% of 200


def test_compounding_via_chained_targets():
    """Ladder math: after a buy at 490, the next sell target is 490 * 1.04 = 509.60."""
    lot = OpenLot(
        id=1,
        qty=20.0 / 490.0,  # bought $20 worth at 490
        entry_price=490.0,
        target_price=round(490.0 * 1.04, 2),
    )
    # price reaches the new target
    state = _state(
        last_buy_price=490.0,
        current_price=509.60,
        open_lots=[lot],
    )
    sig = generate_signal(state, PARAMS)
    assert sig.action == "SELL"
    # profit is positive
    assert sig.expected_profit_usd > 0
