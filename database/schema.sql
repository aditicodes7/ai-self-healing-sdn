-- ai-self-healing-sdn SQLite schema. Loaded via database/db.py:init_schema().
-- WAL mode is set by the connection layer (PRAGMA journal_mode=WAL), not here.
-- All CREATE statements are idempotent (IF NOT EXISTS) so this file can be
-- re-run safely against an existing database.

PRAGMA foreign_keys = ON;

-- Raw per-switch/per-port counters polled every telemetry.interval_s.
CREATE TABLE IF NOT EXISTS telemetry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL    NOT NULL,
    switch_id     TEXT    NOT NULL,
    port_no       INTEGER NOT NULL,
    link_id       TEXT,
    tx_bytes      INTEGER NOT NULL,
    rx_bytes      INTEGER NOT NULL,
    tx_packets    INTEGER NOT NULL,
    rx_packets    INTEGER NOT NULL,
    rx_dropped    INTEGER NOT NULL,
    tx_dropped    INTEGER NOT NULL,
    rx_errors     INTEGER NOT NULL,
    tx_errors     INTEGER NOT NULL,
    duration_sec  REAL,
    data_source   TEXT    NOT NULL
);

-- Derived per-(link, window) feature vectors. feature_json holds the full
-- vector (rolling means/stds, slopes, EWMA deviation, ...) produced by
-- common/features.py, identically for training and live inference.
CREATE TABLE IF NOT EXISTS features (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            REAL    NOT NULL,
    link_id              TEXT    NOT NULL,
    window_s             INTEGER NOT NULL,
    throughput           REAL,
    utilization          REAL,
    loss_rate            REAL,
    error_rate           REAL,
    latency_ms           REAL,
    latency_is_estimate  INTEGER NOT NULL DEFAULT 1,
    queue_occupancy      REAL,
    feature_json         TEXT    NOT NULL,
    data_source          TEXT    NOT NULL
);

-- ML degradation-probability output per (link, timestamp).
CREATE TABLE IF NOT EXISTS predictions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    link_id        TEXT    NOT NULL,
    probability    REAL    NOT NULL,
    threshold      REAL    NOT NULL,
    model_version  TEXT    NOT NULL,
    horizon_s      REAL    NOT NULL
);

-- Current known state of every link (upserted as telemetry/predictions arrive).
CREATE TABLE IF NOT EXISTS links (
    link_id        TEXT PRIMARY KEY,
    src_switch     TEXT NOT NULL,
    src_port       INTEGER NOT NULL,
    dst_switch     TEXT NOT NULL,
    dst_port       INTEGER NOT NULL,
    capacity_bps   INTEGER,
    state          TEXT NOT NULL DEFAULT 'HEALTHY',
    last_updated   REAL
);

-- Discovered/loaded topology graphs over time (LLDP discovery vs YAML).
CREATE TABLE IF NOT EXISTS topology_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL NOT NULL,
    topology_json  TEXT NOT NULL,
    source         TEXT NOT NULL
);

-- One row per detected degradation incident, from open to resolution.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id  TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL,
    opened_at    REAL NOT NULL,
    closed_at    REAL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    reason       TEXT
);

-- Every reroute decision considered, including negatives (Decision.change=False).
CREATE TABLE IF NOT EXISTS route_changes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id        TEXT,
    timestamp          REAL NOT NULL,
    flow_key           TEXT NOT NULL,
    old_path           TEXT,
    new_path           TEXT,
    decision_reason    TEXT NOT NULL,
    changed            INTEGER NOT NULL DEFAULT 0,
    selector           TEXT NOT NULL,
    -- Phase 18: set when path selection was attempted via the RL policy
    -- (service/ml_rl_service.py's POST /infer/route) but fell back to the
    -- weighted-cost heuristic (rank_candidate_paths) instead -- service
    -- down, timeout, or an invalid/unusable action. selector still records
    -- which system actually chose new_path; these two columns record
    -- *why* RL wasn't used, for every call, not just the failures anyone
    -- happens to notice.
    rl_fallback        INTEGER NOT NULL DEFAULT 0,
    rl_fallback_reason TEXT,
    -- Phase 22: the exact inputs the selector was shown -- candidate
    -- paths, per-link metrics, degradation probabilities, the degraded
    -- link and the current path. Added because E6 could not be
    -- interpreted without it: the RL and the heuristic disagreed on the
    -- real network, and an offline replay of the same policy against
    -- metrics reconstructed from the database chose a DIFFERENT path
    -- from the one actually taken. With no record of what the selector
    -- saw, there was no way to tell a bad decision apart from a
    -- different input, and both explanations imply opposite fixes.
    decision_inputs_json TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents (incident_id)
);

-- Every OpenFlow FlowMod issued, one row per switch in the installed path.
CREATE TABLE IF NOT EXISTS flow_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT,
    timestamp     REAL NOT NULL,
    switch_id     TEXT NOT NULL,
    match_json    TEXT NOT NULL,
    actions_json  TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    cookie        INTEGER NOT NULL,
    hard_timeout  INTEGER NOT NULL,
    result        TEXT NOT NULL,
    error_msg     TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents (incident_id)
);

-- Free-form structured events for the dashboard event timeline.
CREATE TABLE IF NOT EXISTS remediation_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT,
    timestamp    REAL NOT NULL,
    event_type   TEXT NOT NULL,
    detail_json  TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents (incident_id)
);

-- Post-remediation verification outcome, including retries.
CREATE TABLE IF NOT EXISTS verification_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT NOT NULL,
    timestamp     REAL NOT NULL,
    path          TEXT NOT NULL,
    classification TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    post_json     TEXT NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (incident_id) REFERENCES incidents (incident_id)
);

-- One row per (experiment, seed, system) run for the comparison study.
CREATE TABLE IF NOT EXISTS experiment_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    system         TEXT NOT NULL,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    results_json   TEXT,
    data_source    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry (timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_link_ts ON telemetry (link_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_features_link_ts ON features (link_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_predictions_link_ts ON predictions (link_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_topology_snapshots_ts ON topology_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_route_changes_incident ON route_changes (incident_id);
CREATE INDEX IF NOT EXISTS idx_flow_rules_incident ON flow_rules (incident_id);
CREATE INDEX IF NOT EXISTS idx_remediation_events_incident ON remediation_events (incident_id);
CREATE INDEX IF NOT EXISTS idx_verification_results_incident ON verification_results (incident_id);
CREATE INDEX IF NOT EXISTS idx_experiment_runs_experiment ON experiment_runs (experiment_id);
