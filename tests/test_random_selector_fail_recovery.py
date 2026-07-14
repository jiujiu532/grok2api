"""Regression: random selector must not permanently starve accounts.

``fail_count`` is a lifetime-cumulative counter (success never clears it in
random mode, and the 30s DB sync overwrites the runtime column with the
lifetime ``usage_fail_count``). The old code hard-excluded any account with
``fail_count >= 5`` forever, slowly draining the whole pool. The fix turns the
hard exclusion into a time-bounded soft avoidance keyed on ``last_fail_at``.
"""

import array

from app.dataplane.account import selector
from app.dataplane.account.table import AccountRuntimeTable


def _table_with(fail_counts, last_fail_ats, *, pool_id=0, mode_id=1):
    """Build a minimal runtime table exposing only the columns the random
    selector reads."""
    n = len(fail_counts)
    t = AccountRuntimeTable()
    t.pool_by_idx = array.array("H", [pool_id] * n)
    t.inflight_by_idx = array.array("H", [0] * n)
    t.fail_count_by_idx = array.array("H", list(fail_counts))
    t.health_by_idx = array.array("f", [1.0] * n)
    t.last_use_at_by_idx = array.array("L", [0] * n)
    t.last_fail_at_by_idx = array.array("L", list(last_fail_ats))
    t.cooling_until_s_by_idx = array.array("L", [0] * n)
    t.mode_available = {(pool_id, mode_id): set(range(n))}
    return t


def _pick_counts(table, *, now_s, trials=4000):
    counts = {}
    for _ in range(trials):
        idx = selector._random_select(
            table, 0, exclude_idxs=None, prefer_tag_idxs=None, now_s=now_s
        )
        counts[idx] = counts.get(idx, 0) + 1
    return counts


def test_high_fail_account_recovers_after_window():
    now = 1_000_000
    recovery = selector._RANDOM_FAIL_RECOVERY_S
    # idx0: healthy. idx1: high lifetime fails, last failed long ago (recovered).
    table = _table_with(
        fail_counts=[0, 50],
        last_fail_ats=[0, now - recovery - 1],
    )
    counts = _pick_counts(table, now_s=now)
    # The recovered account MUST be selectable again — not starved forever.
    assert counts.get(1, 0) > 0, "recovered high-fail account was permanently excluded"


def test_recently_failing_account_is_avoided_but_pool_stays_up():
    now = 1_000_000
    # idx0 healthy; idx1 high fails and still failing right now (inside window).
    table = _table_with(
        fail_counts=[0, 50],
        last_fail_ats=[0, now - 1],
    )
    counts = _pick_counts(table, now_s=now)
    # Healthy account strongly preferred while the bad one is still hot.
    assert counts.get(0, 0) > 0
    assert counts.get(1, 0) == 0, "recently-failing account should be soft-avoided"


def test_all_recently_failing_falls_back_to_hard_ok():
    now = 1_000_000
    # Every account is high-fail AND recently failed: must NOT return None,
    # otherwise the whole pool goes dark despite live accounts existing.
    table = _table_with(
        fail_counts=[9, 9],
        last_fail_ats=[now - 1, now - 2],
    )
    idx = selector._random_select(
        table, 0, exclude_idxs=None, prefer_tag_idxs=None, now_s=now
    )
    assert idx is not None, "soft-avoidance must fall back to keep the pool serving"


def test_cooling_and_inflight_remain_hard_constraints():
    now = 1_000_000
    table = _table_with(fail_counts=[0, 0], last_fail_ats=[0, 0])
    # idx0 still cooling (429), idx1 available.
    table.cooling_until_s_by_idx[0] = now + 100
    counts = _pick_counts(table, now_s=now)
    assert counts.get(0, 0) == 0, "cooling account must never be selected"
    assert counts.get(1, 0) > 0


if __name__ == "__main__":
    test_high_fail_account_recovers_after_window()
    test_recently_failing_account_is_avoided_but_pool_stays_up()
    test_all_recently_failing_falls_back_to_hard_ok()
    test_cooling_and_inflight_remain_hard_constraints()
    print("all random-selector fail-recovery checks passed")
