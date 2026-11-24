"""SQLite 表结构。

所有业务写入都经过服务层，表结构保留口径快照、访问决定、交付内容和
影响记录，以便在数据迟到或授权撤销后复核历史结论。
"""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE id_sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE staff (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('store_manager', 'analyst', 'data_governor', 'business_owner')),
    department TEXT NOT NULL,
    region_scope TEXT NOT NULL,
    personal_clearance INTEGER NOT NULL DEFAULT 0 CHECK (personal_clearance IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE stores (
    store_id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL,
    region TEXT NOT NULL,
    store_format TEXT NOT NULL,
    opened_on TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE datasets (
    dataset_id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    description TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('internal', 'personal', 'restricted')),
    source_tables TEXT NOT NULL,
    personal_data INTEGER NOT NULL CHECK (personal_data IN (0, 1)),
    owner TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE metrics (
    metric_id TEXT PRIMARY KEY,
    metric_name TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    grain TEXT NOT NULL,
    owner TEXT NOT NULL,
    audience_departments TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'review', 'retired')),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL DEFAULT '',
    calculation_key TEXT NOT NULL,
    source_dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
    definition_text TEXT NOT NULL,
    personal_data INTEGER NOT NULL CHECK (personal_data IN (0, 1))
);

CREATE TABLE access_policies (
    policy_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
    region TEXT NOT NULL,
    role TEXT NOT NULL,
    department TEXT NOT NULL,
    purpose TEXT NOT NULL,
    min_clearance INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'limit', 'deny')),
    policy_version TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL
);

CREATE TABLE business_events (
    event_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE data_versions (
    data_version TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'published')),
    published_at TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL
);

CREATE TABLE store_daily (
    date TEXT NOT NULL,
    store_id TEXT NOT NULL REFERENCES stores (store_id),
    region TEXT NOT NULL,
    visits INTEGER NOT NULL,
    transactions INTEGER NOT NULL,
    group_visits INTEGER NOT NULL,
    group_transactions INTEGER NOT NULL,
    staff_transactions INTEGER NOT NULL,
    gross_sales REAL NOT NULL,
    group_gross_sales REAL NOT NULL,
    return_amount REAL NOT NULL,
    return_count INTEGER NOT NULL,
    stockout_skus INTEGER NOT NULL,
    total_skus INTEGER NOT NULL,
    available_seq INTEGER NOT NULL,
    PRIMARY KEY (date, store_id)
);

CREATE TABLE member_daily (
    member_ref TEXT NOT NULL,
    date TEXT NOT NULL,
    store_id TEXT NOT NULL REFERENCES stores (store_id),
    region TEXT NOT NULL,
    transactions INTEGER NOT NULL,
    spend REAL NOT NULL,
    available_seq INTEGER NOT NULL,
    PRIMARY KEY (member_ref, date, store_id)
);

CREATE INDEX idx_store_daily_region_date ON store_daily (region, date);
CREATE INDEX idx_member_daily_region_date ON member_daily (region, date);

CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    requester_id TEXT NOT NULL REFERENCES staff (user_id),
    decision_owner_id TEXT NOT NULL REFERENCES staff (user_id),
    assigned_analyst_id TEXT REFERENCES staff (user_id),
    department TEXT NOT NULL,
    region_scope TEXT NOT NULL,
    question_text TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    clarified_question TEXT NOT NULL DEFAULT '',
    metric_id TEXT REFERENCES metrics (metric_id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'new', 'clarifying', 'ready_for_access', 'access_pending', 'blocked',
        'ready_for_analysis', 'in_analysis', 'delivered', 'closed_unanswered',
        'accepted_no_action', 'follow_up_pending', 'action_pending',
        'action_in_progress', 'completed', 'cancelled'
    )),
    status_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_requests_owner_state ON requests (decision_owner_id, state);
CREATE INDEX idx_requests_requester ON requests (requester_id);

CREATE TABLE clarifications (
    clarification_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    author_id TEXT NOT NULL REFERENCES staff (user_id),
    kind TEXT NOT NULL CHECK (kind IN ('question', 'answer', 'note')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_clarifications_request ON clarifications (request_id, created_at);

CREATE TABLE access_requests (
    access_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
    purpose TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES staff (user_id),
    requested_at TEXT NOT NULL,
    justification TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'granted', 'denied', 'revoked')),
    revoked_at TEXT NOT NULL DEFAULT '',
    revoked_by TEXT REFERENCES staff (user_id),
    revoke_reason TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX uq_active_access
    ON access_requests (request_id, dataset_id)
    WHERE status IN ('pending', 'granted');

CREATE TABLE access_decisions (
    decision_id TEXT PRIMARY KEY,
    access_id TEXT NOT NULL REFERENCES access_requests (access_id),
    governor_id TEXT NOT NULL REFERENCES staff (user_id),
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'limit', 'deny')),
    justification TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    output_scope TEXT NOT NULL,
    policy_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (access_id, governor_id)
);

CREATE TABLE analyses (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    metric_id TEXT NOT NULL REFERENCES metrics (metric_id),
    dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
    data_version TEXT NOT NULL REFERENCES data_versions (data_version),
    purpose TEXT NOT NULL,
    output_scope TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    metric_snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'delivered', 'void')),
    created_by TEXT NOT NULL REFERENCES staff (user_id),
    created_at TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT '',
    delivery_note TEXT NOT NULL DEFAULT '',
    acknowledged_partial INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged_partial IN (0, 1))
);

CREATE INDEX idx_analyses_request ON analyses (request_id, delivered_at);

CREATE TABLE analysis_impacts (
    impact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analyses (run_id),
    kind TEXT NOT NULL CHECK (kind IN ('late-data-recovered', 'data-corrected', 'permission-revoked')),
    detected_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    data_version TEXT NOT NULL DEFAULT '',
    previous_data_version TEXT NOT NULL DEFAULT '',
    affected_start TEXT NOT NULL,
    affected_end TEXT NOT NULL,
    affected_stores TEXT NOT NULL,
    detail TEXT NOT NULL,
    UNIQUE (run_id, kind, source_ref, data_version)
);

CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    run_id TEXT NOT NULL REFERENCES analyses (run_id),
    owner_id TEXT NOT NULL REFERENCES staff (user_id),
    answered INTEGER NOT NULL CHECK (answered IN (0, 1)),
    follow_up INTEGER NOT NULL CHECK (follow_up IN (0, 1)),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id)
);

CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests (request_id),
    run_id TEXT NOT NULL REFERENCES analyses (run_id),
    proposed_by TEXT NOT NULL REFERENCES staff (user_id),
    business_owner TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'manual-external',
    state TEXT NOT NULL CHECK (state IN ('proposed', 'accepted', 'completed', 'cancelled')),
    proposed_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT '',
    due_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    result_ref TEXT NOT NULL DEFAULT '',
    result_note TEXT NOT NULL DEFAULT '',
    cancelled_at TEXT NOT NULL DEFAULT '',
    cancel_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_actions_request ON actions (request_id);
CREATE INDEX idx_actions_owner_state ON actions (business_owner, state);
CREATE UNIQUE INDEX uq_open_action ON actions (request_id) WHERE state IN ('proposed', 'accepted');

CREATE TABLE audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL
);

CREATE INDEX idx_audit_request ON audit_events (request_id, event_id);
"""
