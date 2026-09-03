from network.failure_injector import DEFAULT_RAMP_IN_S, FAULT_TYPES


def test_six_fault_types_defined():
    assert len(FAULT_TYPES) == 6
    assert set(FAULT_TYPES) == {
        "congestion", "gradual_degradation", "packet_loss",
        "high_latency", "rising_errors", "link_failure",
    }


def test_default_ramp_exceeds_label_horizon():
    # The pre-fault ramp exists specifically so the anticipation window
    # (label.horizon_s, default 10s) falls entirely inside a period where
    # the link is genuinely changing -- see docs/limitations.md's Phase 9
    # entry. If this constant ever shrank below the horizon, that
    # guarantee would silently break.
    from common.config_loader import load_config

    config = load_config()
    assert DEFAULT_RAMP_IN_S > config.label.horizon_s
