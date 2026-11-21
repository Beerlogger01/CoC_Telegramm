from app.account_strength import evaluate, rank


def profile(th=15, level=5):
    return {
        "tag": "#A",
        "townHallLevel": th,
        "heroes": [{"level": level, "maxLevel": 10}],
        "troops": [{"name": "Barbarian", "level": level, "maxLevel": 10}],
    }


def test_th_dominates_and_performance_is_irrelevant():
    low = profile(14, 10)
    high = profile(15, 1)
    assert evaluate(high)["accountStrength"] > evaluate(low)["accountStrength"]
    assert evaluate(high) == evaluate(
        {**high, "warStars": 9999, "donations": 0, "missedAttacks": 100}
    )


def test_unknown_is_not_zero_and_ratios_are_normalized():
    assert evaluate({"townHallLevel": 15})["accountStrength"] is None
    a = profile()
    b = profile()
    b["heroes"] = [{"level": 50, "maxLevel": 100}]
    assert evaluate(a)["accountStrength"] == evaluate(b)["accountStrength"]
    assert evaluate(a)["coverage"] == 60


def test_builder_base_excluded_and_peers():
    a = profile()
    b = profile(level=10)
    b["tag"] = "#B"
    a["heroes"].append({"level": 100, "maxLevel": 100, "village": "builderBase"})
    rows = rank([a, b])
    assert rows[0]["tag"] == "#B"
    assert rows[0]["peerPercentile"] == 75
