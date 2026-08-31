from network.failure_injector import FAULT_TYPES


def test_six_fault_types_defined():
    assert len(FAULT_TYPES) == 6
    assert set(FAULT_TYPES) == {
        "congestion", "gradual_degradation", "packet_loss",
        "high_latency", "rising_errors", "link_failure",
    }
