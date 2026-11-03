from __future__ import annotations

from stram_quant_mcp.denial_throttle import DenialThrottle


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _throttle(clock: _Clock, *, limit: int = 3, window: float = 60.0, max_principals: int = 100):
    return DenialThrottle(limit, window, max_principals, clock=clock)


def test_a_caller_is_blocked_at_the_limit_not_before() -> None:
    throttle = _throttle(_Clock())

    results = [throttle.record_denial("a") for _ in range(3)]

    assert results == [False, False, True]
    assert throttle.is_blocked("a")


def test_another_caller_is_not_affected() -> None:
    throttle = _throttle(_Clock())
    for _ in range(3):
        throttle.record_denial("a")

    assert not throttle.is_blocked("b")


def test_the_block_lifts_once_the_denials_age_out() -> None:
    clock = _Clock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_denial("a")

    clock.now += 61.0

    assert not throttle.is_blocked("a")


def test_a_caller_who_keeps_trying_stays_blocked() -> None:
    clock = _Clock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.record_denial("a")

    for _ in range(12):
        clock.now += 10.0
        throttle.record_denial("a")

    assert throttle.is_blocked("a")


def test_denials_spread_beyond_the_window_never_block() -> None:
    clock = _Clock()
    throttle = _throttle(clock)

    for _ in range(10):
        throttle.record_denial("a")
        clock.now += 30.0

    assert not throttle.is_blocked("a")


def test_memory_is_bounded_and_forgets_the_least_recent_caller() -> None:
    throttle = _throttle(_Clock(), limit=1, max_principals=2)

    for name in ("a", "b", "c"):
        throttle.record_denial(name)

    assert not throttle.is_blocked("a")
    assert throttle.is_blocked("b")
    assert throttle.is_blocked("c")
