"""Dimension-aware scoring rules (evals/scoring.py)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from scoring import align, score  # noqa: E402

GOLD = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.231, 0.253]})


def test_same_answer_with_other_names_order_and_extra_columns():
    agent = pd.DataFrame({"cat": ["Footwear", "Apparel"], "delivered": [9, 8], "rate": [0.253, 0.231]})
    assert score(GOLD, agent, ["category"], ["return_rate"]) == (True, "match")


def test_percent_scale_accepted():
    agent = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_pct": [23.1, 25.3]})
    assert score(GOLD, agent, ["category"], ["return_rate"], percent_metrics={"return_rate"})[0]


def test_values_on_the_wrong_category_fail():
    agent = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.253, 0.231]})
    ok, reason = score(GOLD, agent, ["category"], ["return_rate"])
    assert not ok and "metric" in reason


def test_missing_extra_and_duplicate_rows_fail():
    extra = pd.DataFrame({"category": ["Apparel", "Footwear", "Accessories"], "return_rate": [0.231, 0.253, 0.07]})
    missing = GOLD.iloc[:1]
    dup = pd.DataFrame({"category": ["Apparel", "Apparel", "Footwear"], "return_rate": [0.231, 0.231, 0.253]})
    assert not score(GOLD, extra, ["category"], ["return_rate"])[0]
    assert not score(GOLD, missing, ["category"], ["return_rate"])[0]
    assert not score(GOLD, dup, ["category"], ["return_rate"])[0]


def test_one_percent_tolerance_is_not_the_default():
    near = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.2321, 0.2541]})   # <1%
    far = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.240, 0.253]})      # 3.9%
    assert not score(GOLD, near, ["category"], ["return_rate"])[0]
    assert not score(GOLD, far, ["category"], ["return_rate"])[0]


def test_dates_align_across_formats_and_single_values():
    gold = pd.DataFrame({"month": pd.to_datetime(["2026-01-01", "2026-02-01"]), "orders": [10, 12]})
    agent = pd.DataFrame({"month": ["2026-01-01T00:00:00.000", "2026-02-01T00:00:00.000"], "orders": [10, 12]})
    assert score(gold, agent, ["month"], ["orders"])[0]
    assert not score(pd.DataFrame({"aov": [1796.9]}), pd.DataFrame({"x": [1797.0]}), [], ["aov"])[0]


def test_exact_counts_no_percentage_scaling_and_explicit_money_tolerance():
    gold = pd.DataFrame({"orders": [10000]})
    assert not score(gold, pd.DataFrame({"orders": [10001]}), [], ["orders"])[0]
    assert not score(gold, pd.DataFrame({"orders": [1000000]}), [], ["orders"])[0]
    money = pd.DataFrame({"revenue": [123.456]})
    rounded = pd.DataFrame({"revenue": [123.46]})
    assert not score(money, rounded, [], ["revenue"])[0]
    assert score(money, rounded, [], ["revenue"], tolerances={"revenue": {"atol": 0.01, "rtol": 0}})[0]


def test_all_numeric_columns_are_checked_when_annotations_are_missing():
    from scoring import infer_columns
    frame = pd.DataFrame({"brand": ["A"], "revenue": [100.0], "orders": [10], "units": [12]})
    keys, metrics = infer_columns(frame)
    assert keys == ["brand"] and set(metrics) == {"revenue", "orders", "units"}
    assert not score(frame, frame.assign(units=13), keys, metrics)[0]


def test_numeric_dimensions_and_one_metric_cannot_stand_in_for_other_metrics():
    gold = pd.DataFrame({"year": [2026], "orders": [2026], "units": [2026]})
    assert not score(gold, gold[["year"]], ["year"], ["orders", "units"])[0]
    assert not score(gold, gold[["year", "units"]], ["year"], ["orders", "units"])[0]


def test_infinite_values_cannot_match_finite_results():
    gold = pd.DataFrame({"revenue": [100.0]})
    assert not score(gold, pd.DataFrame({"revenue": [float("inf")]}), [], ["revenue"])[0]


def test_empty_gold_needs_empty_agent_and_zero_counts_compare():
    empty = pd.DataFrame({"category": [], "orders": []})
    assert score(empty, empty.copy(), ["category"], ["orders"])[0]
    assert not score(empty, pd.DataFrame({"category": ["A"], "orders": [1]}), ["category"], ["orders"])[0]
    assert score(pd.DataFrame({"orders": [0]}), pd.DataFrame({"n": [0]}), [], ["orders"])[0]


def test_descriptive_key_column_is_optional_when_the_other_keys_identify_rows():
    """category is determined by sku_id; leaving it out is the same answer."""
    gold = pd.DataFrame({"sku_id": ["S1", "S2"], "category": ["Footwear", "Apparel"], "qty": [3, 5]})
    agent = pd.DataFrame({"sku_id": ["S2", "S1"], "qty": [5, 3]})
    assert score(gold, agent, ["sku_id", "category"], ["qty"]) == (True, "match")


def test_a_key_column_that_splits_rows_is_still_required():
    gold = pd.DataFrame({"channel_group": ["Paid", "Paid"], "channel": ["Meta", "Google"], "spend": [1.0, 2.0]})
    agent = pd.DataFrame({"channel_group": ["Paid"], "spend": [3.0]})
    assert not score(gold, agent, ["channel_group", "channel"], ["spend"])[0]


def test_computed_labels_match_on_their_words():
    gold = pd.DataFrame({"day_type": ["Normal day", "Sale event"], "discount": [0.1, 0.3]})
    agent = pd.DataFrame({"day_type": ["normal day", "sale-event day"], "discount": [0.1, 0.3]})
    assert score(gold, agent, ["day_type"], ["discount"], label_columns={"day_type"}) == (True, "match")
    wrong = pd.DataFrame({"day_type": ["normal day", "sale-event day"], "discount": [0.3, 0.1]})
    assert not score(gold, wrong, ["day_type"], ["discount"], label_columns={"day_type"})[0]


def test_unrequested_top_n_size_compares_the_top_rows():
    from scoring import trim_top_n
    gold = pd.DataFrame({"city": ["A", "B"], "rate": [0.5, 0.4]})
    agent = pd.DataFrame({"city": ["A", "B", "C"], "rate": [0.5, 0.4, 0.3]})
    ranked = "SELECT city, rate FROM t ORDER BY rate DESC LIMIT 2"
    assert score(gold, trim_top_n(agent, gold, ranked, "Which cities have the highest rate?"), ["city"], ["rate"])[0]
    # the question names a size, or the gold is not a ranked LIMIT: no trimming
    assert len(trim_top_n(agent, gold, ranked, "What are the top 2 cities by rate?")) == 3
    assert len(trim_top_n(agent, gold, "SELECT city, rate FROM t", "Rate by city?")) == 3
    # a different top-2 still fails
    other = pd.DataFrame({"city": ["A", "C", "B"], "rate": [0.5, 0.45, 0.4]})
    trimmed = trim_top_n(other, gold, ranked, "Which cities have the highest rate?")
    assert not score(gold, trimmed, ["city"], ["rate"])[0]


def test_named_wrong_metric_cannot_borrow_a_supporting_column():
    gold = pd.DataFrame({"category": ["Apparel"], "net_sales": [100.]})
    actual = pd.DataFrame({"category": ["Apparel"], "net_sales": [999.], "unrelated": [100.]})
    assert not score(gold, actual, ["category"], ["net_sales"])[0]


def test_case_distinct_brands_stay_distinct_in_evaluation():
    gold = pd.DataFrame({"brand": ["ADIDAS", "Adidas"], "orders": [10, 20]})
    assert score(gold, gold.copy(), ["brand"], ["orders"])[0]
    assert not score(gold, gold.assign(orders=[20, 10]), ["brand"], ["orders"])[0]
    single = gold.iloc[[0]]
    assert not score(single, single.assign(brand="Adidas"), ["brand"], ["orders"])[0]


def test_true_false_keys_match_readable_labels():
    """Our prompt asks for readable labels (Prime / Non-Prime) instead of true / false."""
    gold = pd.DataFrame({"is_prime": [True, False], "orders": [10, 20]})
    agent = pd.DataFrame({"customer_type": ["Non-Prime", "Prime"], "orders": [20, 10]})
    assert score(gold, agent, ["is_prime"], ["orders"]) == (True, "match")
    swapped = pd.DataFrame({"customer_type": ["Non-Prime", "Prime"], "orders": [10, 20]})
    assert not score(gold, swapped, ["is_prime"], ["orders"])[0]


def test_a_complete_list_holds_the_golds_top_n():
    """"Each sale event": the gold keeps its top 2 by lift; the agent lists every event, ordered by date."""
    gold = pd.DataFrame({"name": ["B", "C"], "lift": [0.5, 0.4]})
    agent = pd.DataFrame({"name": ["A", "B", "C"], "lift": [0.1, 0.5, 0.4]})
    assert align(gold, agent, ["name"], ["lift"], superset=True)[0]
    assert not align(gold, agent, ["name"], ["lift"])[0]                 # without the each-question rule
    wrong = agent.assign(lift=[0.1, 0.5, 0.3])
    assert not align(gold, wrong, ["name"], ["lift"], superset=True)[0]


def test_a_singular_superlative_may_be_answered_by_its_winner():
    from scoring import winner_only

    gold = pd.DataFrame({"surface": ["plp", "home_recs", "search_results"], "ctr": [0.02, 0.05, 0.03]})
    winner = pd.DataFrame({"surface": ["home_recs"], "ctr": [0.05]})
    question = "Which page surface has the best click-through rate?"
    assert score(winner_only(winner, gold, "ctr", question), winner, ["surface"], ["ctr"])[0]
    wrong = pd.DataFrame({"surface": ["plp"], "ctr": [0.02]})
    assert not score(winner_only(wrong, gold, "ctr", question), wrong, ["surface"], ["ctr"])[0]
    # Plural or open questions still need every row; "worst" has no fixed direction.
    for other in ("Which page surfaces have the best click-through rate?", "What is CTR by page surface?",
                  "Which page surface has the worst click-through rate?"):
        assert winner_only(winner, gold, "ctr", other) is gold
