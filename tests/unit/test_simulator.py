import pytest

from rl.env.simulator import LinkState, NetworkSimulator, flow_share_of_link, link_aggregate_metrics

# A simple 3-switch chain: s1-s2-s3, 10 Mbit / 2ms each, no cross traffic
# ambiguity -- makes the arithmetic easy to verify by hand.
CHAIN_TOPOLOGY = {
    "switches": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
    "hosts": [],
    "links": [
        {"endpoints": ["s1", "s2"], "bw": 10, "delay": "2ms"},
        {"endpoints": ["s2", "s3"], "bw": 10, "delay": "3ms"},
    ],
}


def _sim(seed=1) -> NetworkSimulator:
    return NetworkSimulator(CHAIN_TOPOLOGY, seed=seed)


# -- link_aggregate_metrics / flow_share_of_link (pure functions) ---------

def test_underutilized_link_has_low_latency_and_no_loss():
    link = LinkState(capacity_bps=10_000_000, propagation_delay_ms=2.0)
    latency_ms, loss_rate = link_aggregate_metrics(1_000_000, link)  # 10% utilization
    assert latency_ms > 2.0  # propagation + some queueing
    assert latency_ms < 4.0  # but not much, at 10% utilization
    assert loss_rate == 0.0


def test_overutilized_link_has_high_latency_and_real_loss():
    link = LinkState(capacity_bps=10_000_000, propagation_delay_ms=2.0)
    latency_ms, loss_rate = link_aggregate_metrics(20_000_000, link)  # 2x capacity offered
    assert latency_ms > 10.0  # queueing delay should dominate
    assert loss_rate == pytest.approx(0.5, abs=0.01)  # half the offered load can't fit


def test_flow_gets_full_share_when_link_not_congested():
    link = LinkState(capacity_bps=10_000_000, propagation_delay_ms=2.0)
    share = flow_share_of_link(flow_offered_bps=2_000_000, total_offered_bps=2_000_000, link=link)
    assert share == pytest.approx(2_000_000)


def test_flows_get_proportional_fair_share_when_congested():
    link = LinkState(capacity_bps=10_000_000, propagation_delay_ms=2.0)
    # Two flows, 6 Mbit and 2 Mbit offered, total 8 Mbit < 10 Mbit capacity -> both full.
    total = 8_000_000
    assert flow_share_of_link(6_000_000, total, link) == pytest.approx(6_000_000)
    assert flow_share_of_link(2_000_000, total, link) == pytest.approx(2_000_000)

    # Now push total offered to 20 Mbit (2x capacity) with the same 6:2 ratio.
    total = 20_000_000
    big_share = flow_share_of_link(15_000_000, total, link)
    small_share = flow_share_of_link(5_000_000, total, link)
    assert big_share + small_share == pytest.approx(10_000_000)  # sums to capacity
    assert big_share == pytest.approx(7_500_000)  # 15/20 * 10M
    assert small_share == pytest.approx(2_500_000)  # 5/20 * 10M


# -- NetworkSimulator ---------------------------------------------------

def test_single_flow_below_capacity_achieves_full_throughput():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=2_000_000)  # 2 Mbit on a 10 Mbit path
    result = sim.step()
    flow = result["flows"]["h1-h3"]
    assert flow["throughput_bps"] == pytest.approx(2_000_000)
    assert flow["loss_rate"] == 0.0


def test_multi_hop_path_composes_latency_as_sum():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=1_000_000)
    result = sim.step()
    flow = result["flows"]["h1-h3"]
    # >= sum of propagation delays (2ms + 3ms = 5ms), plus some queueing.
    assert flow["latency_ms"] >= 5.0


def test_multi_hop_path_throughput_is_bottleneck_not_sum():
    sim = _sim()
    # Make s2-s3 the bottleneck by overloading it with a second flow.
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=3_000_000)
    sim.set_flow("other", path=[2, 3], offered_load_bps=9_000_000)
    result = sim.step()
    flow = result["flows"]["h1-h3"]
    # s2-s3 is shared by both flows (3M + 9M = 12M offered on a 10M link);
    # h1-h3's fair share there is 10M * (3/12) = 2.5M, below its own 3M ask.
    assert flow["throughput_bps"] == pytest.approx(2_500_000, rel=0.01)


def test_congestion_fault_reduces_effective_capacity():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=8_000_000)
    baseline = sim.step()["flows"]["h1-h3"]["throughput_bps"]

    sim.inject_fault("s1-s2", capacity_multiplier=0.2)  # congestion: 10Mbit -> 2Mbit
    degraded = sim.step()["flows"]["h1-h3"]["throughput_bps"]

    assert degraded < baseline
    assert degraded == pytest.approx(2_000_000, rel=0.05)


def test_link_failure_fault_gives_zero_throughput_and_full_loss():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=2_000_000)
    sim.inject_fault("s1-s2", down=True)
    flow = sim.step()["flows"]["h1-h3"]
    assert flow["throughput_bps"] == 0.0
    assert flow["loss_rate"] == 1.0


def test_clear_fault_restores_healthy_behavior():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=2_000_000)
    sim.inject_fault("s1-s2", capacity_multiplier=0.1)
    degraded = sim.step()["flows"]["h1-h3"]["throughput_bps"]

    sim.clear_fault("s1-s2")
    restored = sim.step()["flows"]["h1-h3"]["throughput_bps"]

    assert restored > degraded
    assert restored == pytest.approx(2_000_000)


def test_remove_flow_stops_it_contributing_to_link_load():
    sim = _sim()
    sim.set_flow("h1-h3", path=[1, 2, 3], offered_load_bps=2_000_000)
    sim.set_flow("other", path=[1, 2], offered_load_bps=3_000_000)
    sim.remove_flow("other")
    result = sim.step()
    assert "other" not in result["flows"]
    assert result["links"]["s1-s2"]["utilization"] == pytest.approx(0.2)  # only h1-h3's 2M/10M now


def test_seeded_simulator_is_deterministic():
    sim1 = NetworkSimulator(CHAIN_TOPOLOGY, seed=42)
    sim2 = NetworkSimulator(CHAIN_TOPOLOGY, seed=42)
    sim1.set_flow("f", [1, 2, 3], 3_000_000)
    sim2.set_flow("f", [1, 2, 3], 3_000_000)
    assert sim1.step() == sim2.step()
