from opcua_collector.backoff import Backoff


def test_backoff_grows_then_caps_and_resets() -> None:
    backoff = Backoff(initial_s=1, max_s=10, multiplier=2)
    assert [backoff.failure_delay() for _ in range(6)] == [1, 2, 4, 8, 10, 10]
    backoff.reset()
    assert backoff.failure_delay() == 1
