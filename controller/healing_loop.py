"""The closed loop itself: turns a stream of per-link degradation
predictions into incidents, reroutes, and verified outcomes.

Phases 11-18 built each decision component in isolation
(persistence_tracker, stability_manager, path_computer, flow_manager,
remediation_manager, verifier, rl_client) and unit-tested them
separately. This module is what finally runs them in sequence against a
live network:

    prediction -> persistence (is the signal real?)
               -> stability   (are we allowed to act, and is it worth it?)
               -> incident opened
               -> remediation (RL or heuristic picks a path, flows installed)
               -> verification (did it actually help? retry, or UNRESOLVED)
               -> incident closed

Deliberately NOT a ryu.RyuApp: it takes everything it needs as injected
callables/objects, so the whole loop is exercisable under venv-ml with
fakes, with no ryu, no Mininet and no real time. controller/
telemetry_collector.py is what constructs the live wiring and feeds it
real predictions.

Two things are injected specifically because the live environment is a
Ryu greenthread world:
  * `sleep_fn` -- verification settles for verify.settle_s between
    measurements. time.sleep() would stall Ryu's entire event loop, so
    the live caller passes hub.sleep.
  * `spawn_fn` -- a remediation with retries can take tens of seconds.
    Running that inline inside an OpenFlow event handler would block
    every other switch's telemetry, so the live caller passes
    hub.spawn. Tests pass a direct-call default and get deterministic,
    synchronous behaviour.
"""
from __future__ import annotations

import json
import time
import uuid

from common.logging_setup import configure_logging
from common.schema import IncidentStatus, LinkHealthState, VerificationClassification
from controller.baselines.rule_based import evaluate_current_conditions
from controller.path_computer import (
    LinkMetrics,
    compute_path_cost,
    generate_candidate_paths,
    link_id_for_edge,
)
from controller.verifier import Baseline

logger = configure_logging("controller.healing_loop")


def _run_inline(fn, *args, **kwargs):
    """Default spawn_fn: run synchronously. Live callers pass hub.spawn."""
    return fn(*args, **kwargs)


class HealingLoop:
    def __init__(
        self,
        db,
        persistence,
        stability,
        remediation,
        verifier,
        flow_ctx,
        graph_provider,
        datapaths_provider,
        link_metrics_provider,
        k_shortest: int,
        weight_utilization: float,
        weight_latency: float,
        weight_loss: float,
        weight_hop_count: float,
        sleep_fn=time.sleep,
        spawn_fn=_run_inline,
        system: str = "P2_ML_RL",
        b2_utilization_threshold: float = 0.80,
        b2_loss_threshold: float = 0.10,
    ):
        # Which system-under-test this controller instance is running as
        # (Phase 22). The ladder B1 -> B2 -> P1 -> P2 adds exactly one
        # mechanism per rung so a measured difference is attributable:
        #   B1_SHORTEST_PATH  install once, never react
        #   B2_RULE_BASED     react to CURRENT thresholds, no prediction,
        #                     no persistence, no learned policy
        #   P1_ML_HEURISTIC   prediction + persistence + weighted-cost path
        #   P2_ML_RL          prediction + persistence + RL-chosen path
        # P1 vs P2 is selected by whether the caller passes an rl_client
        # into the RemediationManager, so this class only has to know
        # about B1 and B2.
        self.system = system
        self.b2_utilization_threshold = b2_utilization_threshold
        self.b2_loss_threshold = b2_loss_threshold
        self.db = db
        self.persistence = persistence
        self.stability = stability
        self.remediation = remediation
        self.verifier = verifier
        self.flow_ctx = flow_ctx
        self.graph_provider = graph_provider
        self.datapaths_provider = datapaths_provider
        self.link_metrics_provider = link_metrics_provider
        self.k_shortest = k_shortest
        self.weight_utilization = weight_utilization
        self.weight_latency = weight_latency
        self.weight_loss = weight_loss
        self.weight_hop_count = weight_hop_count
        self.sleep_fn = sleep_fn
        self.spawn_fn = spawn_fn

        # The path the managed flow is currently believed to be using.
        # None until install_initial_path() has run -- the loop refuses to
        # reason about "is this link on our path" before it knows.
        self.active_path: list[int] | None = None
        # Guards against opening a second incident for a flow that is
        # already mid-remediation (predictions keep arriving every 2s
        # while a verification is still settling).
        self._remediation_in_flight = False

    # -- initial path ------------------------------------------------------

    def install_initial_path(self) -> list[int] | None:
        """Proactively install the shortest path for the managed flow, so
        the controller KNOWS which path traffic is on rather than
        inferring it from MAC-learning flood behaviour. Returns the
        installed path, or None if the topology can't support one yet."""
        graph = self.graph_provider()
        candidates = generate_candidate_paths(
            graph, self.flow_ctx.src_dpid, self.flow_ctx.dst_dpid, self.k_shortest
        )
        if not candidates:
            logger.warning(
                "cannot install initial path: no route between endpoints yet",
                extra={"src": self.flow_ctx.src_dpid, "dst": self.flow_ctx.dst_dpid},
            )
            return None

        path = candidates[0]
        incident_id = f"init-{uuid.uuid4().hex[:8]}"
        self._open_incident(incident_id, link_id="", reason="initial path installation")
        chosen, results = self.remediation.remediate(
            incident_id=incident_id,
            link_id="",
            flow_ctx=self.flow_ctx,
            graph=graph,
            link_metrics=self.link_metrics_provider(),
            datapaths=self.datapaths_provider(),
            # Exclude nothing: this is the first install, not a reroute
            # away from something.
            excluded_paths=[],
        )
        self.active_path = chosen if chosen is not None else path
        self._close_incident(incident_id, IncidentStatus.SUCCESS, "initial path installed")
        self._emit_event(incident_id, "initial_path_installed", {"path": self.active_path})
        logger.info("initial path installed", extra={"path": self.active_path})
        return self.active_path

    # -- second entry point: a link that has actually gone --------------

    def on_link_down(self, link_id: str) -> None:
        """A link has DISAPPEARED from the discovered topology.

        This exists because the ML predictor structurally cannot see a
        hard failure. Measured: with a link cut, the model kept scoring
        it 0.034 -- lower than before the cut -- because a dead link
        carries no traffic and returns no latency samples, which is
        indistinguishable from an idle healthy link in this feature set.
        Its labels make it worse: the model is trained to predict
        degradation ~10s BEFORE it happens, and a link that is already
        gone is not about to do anything.

        So hard failures are handled here, from LLDP's own link-removal
        event, and gradual degradation is handled by the predictor. Two
        fault classes, two mechanisms.

        Persistence is deliberately BYPASSED. That gate exists to filter
        uncertain predictions across several windows; a link's
        disappearance is an observed fact, not a probability, and making
        a certainty wait for corroboration would only add latency.

        Available to every system that reroutes at all (B2/P1/P2), not
        just the ML ones -- reacting to a link-down event is a
        control-plane capability, not a learned one. Attributing it to
        the ML arm would make P2 beat B2 on link_failure for a reason
        that has nothing to do with prediction.
        """
        if self.system == "B1_SHORTEST_PATH":
            return  # the do-nothing control never reroutes
        if self.active_path is None or self._remediation_in_flight:
            return
        if link_id not in self._active_path_link_ids():
            return  # a link we are not using going away is not our problem

        logger.info("link removed from topology; remediating", extra={"link_id": link_id})
        self._set_link_state(link_id, LinkHealthState.FAILED, override_failed=True)
        self._remediation_in_flight = True
        self.spawn_fn(
            self._remediate_and_verify, link_id, 1.0,
            "link removed from the discovered topology", None,
        )

    # -- the entry point ---------------------------------------------------

    def on_prediction(self, link_id: str, probability: float, timestamp: float | None = None) -> None:
        """Called for every successful degradation prediction. Fast and
        non-blocking: the expensive remediate+verify work is handed to
        spawn_fn."""
        if self.system == "B1_SHORTEST_PATH":
            # The do-nothing control: a path is installed once and never
            # revisited, so the fault's full cost is what gets measured.
            return

        if self.system == "B2_RULE_BASED":
            self._on_prediction_rule_based(link_id, timestamp)
            return

        self.persistence.record(link_id, probability)
        result = self.persistence.evaluate(link_id)

        if result.uncertain:
            logger.info(
                "abstaining: prediction inside uncertainty band",
                extra={"link_id": link_id, "probability": probability, "reason": result.reason},
            )
            return
        if not result.persistent_degradation:
            return

        if self.active_path is None:
            logger.warning(
                "persistent degradation but no known active path; ignoring",
                extra={"link_id": link_id},
            )
            return
        if link_id not in self._active_path_link_ids():
            # A link we don't use degrading is not our problem to fix.
            logger.info(
                "degraded link is not on the active path; no action",
                extra={"link_id": link_id, "active_path": self.active_path},
            )
            return
        if self._remediation_in_flight:
            logger.info("remediation already in flight; skipping", extra={"link_id": link_id})
            return

        graph = self.graph_provider()
        link_metrics = self.link_metrics_provider()

        # The cost comparison MUST account for the prediction, or the
        # whole ML pipeline has no effect on routing: a link predicted to
        # fail still looks cheap by its current (still-healthy) counters,
        # so the min-improvement gate rejects every alternative as "worse"
        # and the system never moves. Found exactly this way -- the
        # components each behaved correctly in isolation and the assembled
        # loop still refused to reroute (see docs/decisions.md).
        predicted_metrics = self._metrics_with_prediction(link_metrics, link_id, probability)

        current_cost = compute_path_cost(
            self.active_path, predicted_metrics, self.weight_utilization,
            self.weight_latency, self.weight_loss, self.weight_hop_count,
        )
        # Candidates must AVOID the degrading link -- rerouting onto
        # another path that still crosses it fixes nothing.
        candidates = [
            p for p in generate_candidate_paths(
                graph, self.flow_ctx.src_dpid, self.flow_ctx.dst_dpid, self.k_shortest
            )
            if p != self.active_path and link_id not in self._path_link_ids(p)
        ]
        if not candidates:
            logger.warning(
                "no alternate path avoids the degrading link; cannot remediate",
                extra={"link_id": link_id, "active_path": self.active_path},
            )
            return
        best_cost = min(
            compute_path_cost(
                p, predicted_metrics, self.weight_utilization, self.weight_latency,
                self.weight_loss, self.weight_hop_count,
            )
            for p in candidates
        )

        decision = self.stability.evaluate(
            flow_key=self.flow_ctx.flow_key,
            persistent_degradation=result.persistent_degradation,
            uncertain=result.uncertain,
            persistence_reason=result.reason,
            current_path_cost=current_cost,
            best_candidate_cost=best_cost,
        )
        if not decision.change:
            logger.info(
                "stability gate blocked the reroute",
                extra={"link_id": link_id, "reason": decision.reason},
            )
            return

        self._remediation_in_flight = True
        self.spawn_fn(self._remediate_and_verify, link_id, probability, decision.reason, timestamp)

    def _on_prediction_rule_based(self, link_id: str, timestamp: float | None) -> None:
        """B2: react to CURRENT measured conditions only.

        Deliberately ignores the prediction it was handed -- that is the
        whole point of the baseline. It also skips persistence and
        stability, so E4 can measure what those gates are worth. It is
        driven off the prediction tick purely because that is the
        controller's existing periodic hook; no probability is read.
        """
        if self.active_path is None or self._remediation_in_flight:
            return

        decision = evaluate_current_conditions(
            self.active_path,
            self.link_metrics_provider(),
            self.b2_utilization_threshold,
            self.b2_loss_threshold,
        )
        if not decision.should_reroute:
            return

        self._remediation_in_flight = True
        self.spawn_fn(self._remediate_and_verify, link_id, 0.0, decision.reason, timestamp)

    # -- the slow half (runs in its own greenthread live) -------------------

    def _remediate_and_verify(
        self, link_id: str, probability: float, decision_reason: str, timestamp: float | None
    ) -> None:
        incident_id = f"inc-{uuid.uuid4().hex[:8]}"
        try:
            self._open_incident(
                incident_id, link_id,
                reason=f"P(degradation)={probability:.3f}; {decision_reason}",
            )
            self._emit_event(
                incident_id, "degradation_predicted",
                {"link": link_id, "probability": round(probability, 4)},
            )
            self._set_link_state(link_id, LinkHealthState.DEGRADED)

            baseline_latency, baseline_loss = self._measure_active_path()
            baseline = Baseline(
                latency_ms=baseline_latency if baseline_latency is not None else 0.0,
                loss_rate=baseline_loss if baseline_loss is not None else 0.0,
            )

            failing_path = list(self.active_path) if self.active_path else []

            def remediate_fn(excluded_paths: list[list[int]]) -> list[int] | None:
                # The verifier passes [] on its first attempt, so without
                # this the very first remediation is free to "fix" the
                # incident by re-selecting the path that is already
                # failing. Seed the exclusion list with the failing path
                # (and anything else still crossing the degrading link)
                # so every attempt is a genuine alternative.
                graph = self.graph_provider()
                excluded = [p for p in excluded_paths]
                if failing_path and failing_path not in excluded:
                    excluded.insert(0, failing_path)
                # Any other candidate that still crosses the degrading
                # link is equally useless as a remedy.
                for candidate in generate_candidate_paths(
                    graph, self.flow_ctx.src_dpid, self.flow_ctx.dst_dpid, self.k_shortest
                ):
                    if link_id in self._path_link_ids(candidate) and candidate not in excluded:
                        excluded.append(candidate)

                chosen, _results = self.remediation.remediate(
                    incident_id=incident_id,
                    link_id=link_id,
                    flow_ctx=self.flow_ctx,
                    graph=graph,
                    link_metrics=self.link_metrics_provider(),
                    datapaths=self.datapaths_provider(),
                    excluded_paths=excluded,
                    degradation_probs={link_id: probability},
                )
                if chosen is not None:
                    self.active_path = chosen
                    self._emit_event(incident_id, "reroute_installed", {"path": chosen})
                return chosen

            outcome = self.verifier.verify_and_retry(
                incident_id=incident_id,
                baseline=baseline,
                remediate_fn=remediate_fn,
                measure_fn=self._measure_active_path,
                sleep_fn=self.sleep_fn,
            )

            status = {
                VerificationClassification.SUCCESS: IncidentStatus.SUCCESS,
                VerificationClassification.PARTIAL: IncidentStatus.PARTIAL,
                VerificationClassification.FAILED: IncidentStatus.FAILED,
            }.get(outcome.classification, IncidentStatus.UNRESOLVED)

            # A verification that never recovered leaves traffic on a path
            # we already know is bad; say so rather than reporting a clean
            # close. The verifier itself has already exhausted its retries.
            if status is not IncidentStatus.SUCCESS:
                logger.warning(
                    "remediation did not fully recover the flow",
                    extra={
                        "incident_id": incident_id, "classification": outcome.classification.value,
                        "reason": outcome.reason, "failing_path": failing_path,
                    },
                )

            self._close_incident(incident_id, status, outcome.reason)
            # The fault itself may still be present, but traffic is no
            # longer on this link once remediation succeeded; mark it
            # AT_RISK rather than either DEGRADED (stale alarm) or
            # HEALTHY (claiming a repair the system did not perform).
            # _set_link_state keeps FAILED sticky, so a link that
            # disappeared is not downgraded here.
            self._set_link_state(
                link_id,
                LinkHealthState.AT_RISK if status is IncidentStatus.SUCCESS
                else LinkHealthState.DEGRADED,
            )
            self._emit_event(
                incident_id,
                "verification_" + outcome.classification.value.lower(),
                {
                    "result": outcome.classification.value,
                    "post_latency_ms": outcome.post_latency_ms,
                    "post_loss_rate": outcome.post_loss_rate,
                },
            )
        except Exception:
            logger.exception("remediation/verification failed", extra={"incident_id": incident_id})
            self._close_incident(incident_id, IncidentStatus.UNRESOLVED, "internal error during remediation")
        finally:
            self._remediation_in_flight = False

    # -- measurement --------------------------------------------------------

    @staticmethod
    def _path_link_ids(path: list[int]) -> list[str]:
        return [link_id_for_edge(u, v) for u, v in zip(path, path[1:])]

    def _active_path_link_ids(self) -> list[str]:
        if not self.active_path:
            return []
        return self._path_link_ids(self.active_path)

    def _metrics_with_prediction(
        self, link_metrics: dict[str, LinkMetrics], degrading_link_id: str, probability: float
    ) -> dict[str, LinkMetrics]:
        """Returns link_metrics with the degrading link re-costed to
        reflect what the model says is coming, not only what the counters
        currently show.

        The prediction is folded in as an expected loss_rate and a
        utilization floor, both scaled by the model's own probability --
        a link we are 95% sure is failing is costed as nearly unusable, a
        marginal one barely at all. This is a COST-MODEL adjustment used
        purely to rank paths against each other; nothing here is written
        to the database or reported as a measurement.
        """
        current = link_metrics.get(degrading_link_id, LinkMetrics())
        adjusted = LinkMetrics(
            utilization=max(current.utilization or 0.0, probability),
            latency_ms=current.latency_ms,
            loss_rate=max(current.loss_rate or 0.0, probability),
        )
        return {**link_metrics, degrading_link_id: adjusted}

    def _measure_active_path(self) -> tuple[float | None, float | None]:
        """End-to-end latency/loss for the flow's current path, from the
        live per-link telemetry the collector is already gathering.
        Latency is additive across hops; loss compounds. Returns
        (None, None) components where nothing has been measured yet --
        never a fabricated zero."""
        link_metrics = self.link_metrics_provider()
        link_ids = self._active_path_link_ids()
        if not link_ids:
            return None, None

        latencies = [
            link_metrics[lid].latency_ms
            for lid in link_ids
            if lid in link_metrics and link_metrics[lid].latency_ms is not None
        ]
        losses = [
            link_metrics[lid].loss_rate
            for lid in link_ids
            if lid in link_metrics and link_metrics[lid].loss_rate is not None
        ]

        total_latency = sum(latencies) if latencies else None
        if losses:
            survival = 1.0
            for loss in losses:
                survival *= 1.0 - loss
            total_loss = 1.0 - survival
        else:
            total_loss = None
        return total_latency, total_loss

    # -- persistence of the loop's own decisions ----------------------------

    def _open_incident(self, incident_id: str, link_id: str, reason: str) -> None:
        self.db.insert(
            "incidents",
            {
                "incident_id": incident_id,
                "link_id": link_id,
                "opened_at": time.time(),
                "status": IncidentStatus.OPEN.value,
                "reason": reason,
            },
        )

    def _close_incident(self, incident_id: str, status: IncidentStatus, reason: str) -> None:
        self.db.execute(
            "UPDATE incidents SET status = ?, closed_at = ?, reason = ? WHERE incident_id = ?",
            (status.value, time.time(), reason, incident_id),
        )

    def clear_stale_link_states(self, max_age_s: float) -> None:
        """Return links to HEALTHY once nothing has been observed on them
        for a while.

        A remediated link was being left AT_RISK forever, so after a long
        run the dashboard listed six links as "starting to look risky"
        when they were simply ones that had been rerouted around at some
        point. A health state with no expiry is a running total of
        everything that ever went wrong, not a description of the network
        now.

        Deliberately NOT called on a fixed timer inside this class: the
        controller owns the schedule, so the loop stays free of its own
        background threads and remains testable synchronously.
        """
        cutoff = time.time() - max_age_s
        try:
            self.db.execute(
                # Explicitly clears FAILED as well: if discovery has not
                # refreshed a link for this long, the recorded state is
                # stale regardless of how severe it was.
                "UPDATE links SET state = ?, last_updated = ? "
                "WHERE state != ? AND last_updated < ?",
                (LinkHealthState.HEALTHY.value, time.time(),
                 LinkHealthState.HEALTHY.value, cutoff),
            )
        except Exception:
            logger.exception("could not expire stale link states")

    def _set_link_state(
        self, link_id: str, state: LinkHealthState, override_failed: bool = False
    ) -> None:
        """Records a link's health so the map can colour by it.

        topology_manager.py owns the structural columns and deliberately
        does not touch `state`; this is the only writer of it, so the two
        cannot fight over the same row.

        FAILED is sticky. A link that has disappeared from the topology
        is an observed fact, and the remediation path would otherwise
        overwrite it -- first with DEGRADED when the incident opens, then
        with AT_RISK once the reroute verifies -- leaving a dead link
        displayed as merely at risk. Rerouting around a link is not a
        repair. Only an explicit override (discovery seeing it again, or
        the stale-state expiry) clears it.
        """
        if not link_id:
            return
        try:
            if override_failed:
                self.db.execute(
                    "UPDATE links SET state = ?, last_updated = ? WHERE link_id = ?",
                    (state.value, time.time(), link_id),
                )
            else:
                self.db.execute(
                    "UPDATE links SET state = ?, last_updated = ? "
                    "WHERE link_id = ? AND state != ?",
                    (state.value, time.time(), link_id, LinkHealthState.FAILED.value),
                )
        except Exception:
            logger.exception("could not update link state", extra={"link_id": link_id})

    def _emit_event(self, incident_id: str, event_type: str, detail: dict) -> None:
        """Every event the dashboard's SSE stream shows originates here."""
        self.db.insert(
            "remediation_events",
            {
                "incident_id": incident_id,
                "timestamp": time.time(),
                "event_type": event_type,
                "detail_json": json.dumps(detail),
            },
        )
