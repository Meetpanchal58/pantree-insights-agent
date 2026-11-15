"""Pulse statistics: robust z and Benjamini-Hochberg."""
from pantree_insights.pulse import benjamini_hochberg, robust_z


def test_robust_z_ignores_one_outlying_baseline_week():
    base = [100, 101, 99, 100, 102, 98, 100, 400]            # one festival week in the baseline
    assert robust_z(130, base) > 10                           # still clearly unusual
    import statistics
    plain = (130 - statistics.mean(base)) / statistics.stdev(base)
    assert plain < 0.5                                        # mean/SD would have missed it


def test_robust_z_flat_baseline():
    assert robust_z(100, [100] * 8) == 0.0
    assert robust_z(120, [100] * 8) == float("inf")


def test_benjamini_hochberg_controls_many_tests():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    assert benjamini_hochberg(p, 0.05) == [True, True] + [False] * 8   # classic textbook example
    assert sum(benjamini_hochberg([0.04] * 50, 0.10)) == 50            # all equal and below q
    assert sum(benjamini_hochberg([0.5] * 50, 0.10)) == 0
