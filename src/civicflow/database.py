"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    public INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_identifiers (
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    id_type TEXT NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY(company_id, id_type, code),
    UNIQUE(id_type, code)
);
CREATE TABLE IF NOT EXISTS company_attributes (
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    registered_address TEXT NOT NULL DEFAULT '',
    effective_at TEXT NOT NULL,
    batch_id TEXT,
    source_record_no TEXT,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    PRIMARY KEY(company_id, version)
);
CREATE INDEX IF NOT EXISTS company_attributes_asof ON company_attributes(company_id, effective_at, version);
CREATE TABLE IF NOT EXISTS securities (
    security_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    security_type TEXT NOT NULL DEFAULT 'equity',
    currency TEXT NOT NULL DEFAULT 'CNY',
    batch_id TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(market, ticker)
);
CREATE INDEX IF NOT EXISTS securities_company ON securities(company_id);
CREATE TABLE IF NOT EXISTS market_events (
    event_id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES securities(security_id),
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    market TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('listing','delisting')),
    event_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    batch_id TEXT NOT NULL,
    source_record_no TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE(batch_id, source_record_no)
);
CREATE INDEX IF NOT EXISTS market_events_company ON market_events(company_id, event_at);
CREATE INDEX IF NOT EXISTS market_events_latest ON market_events(security_id, kind, event_at, ingested_at);
CREATE TABLE IF NOT EXISTS capital_changes (
    change_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    security_id TEXT REFERENCES securities(security_id),
    change_type TEXT NOT NULL,
    shares_after INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source_record_no TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE(batch_id, source_record_no)
);
CREATE INDEX IF NOT EXISTS capital_changes_asof ON capital_changes(company_id, security_id, effective_at, ingested_at);
CREATE TABLE IF NOT EXISTS valuations (
    valuation_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    security_id TEXT NOT NULL REFERENCES securities(security_id),
    trade_date TEXT NOT NULL,
    close_price TEXT NOT NULL,
    shares INTEGER NOT NULL,
    market_value_native TEXT NOT NULL,
    currency TEXT NOT NULL,
    fx_to_cny TEXT NOT NULL DEFAULT '1',
    market_value_cny INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    source_record_no TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE(batch_id, source_record_no)
);
CREATE INDEX IF NOT EXISTS valuations_asof ON valuations(security_id, trade_date, ingested_at);
CREATE TABLE IF NOT EXISTS raisings (
    raising_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    security_id TEXT NOT NULL REFERENCES securities(security_id),
    kind TEXT NOT NULL,
    gross_cny_minor INTEGER NOT NULL,
    raised_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source_record_no TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    UNIQUE(batch_id, source_record_no)
);
CREATE INDEX IF NOT EXISTS raisings_window ON raisings(raised_at);
CREATE TABLE IF NOT EXISTS taxonomy_versions (
    version_id TEXT PRIMARY KEY,
    axis TEXT NOT NULL CHECK(axis IN ('industry','control_nature','province')),
    version_label TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('tentative','confirmed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(axis, digest)
);
CREATE TABLE IF NOT EXISTS classifications (
    classification_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    axis TEXT NOT NULL,
    code TEXT NOT NULL,
    taxonomy_version_id TEXT REFERENCES taxonomy_versions(version_id),
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('tentative','confirmed')),
    batch_id TEXT,
    source_record_no TEXT,
    ingested_at TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS classifications_asof ON classifications(company_id, axis, effective_from, ingested_at);
CREATE TABLE IF NOT EXISTS source_batches (
    batch_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    period_label TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    record_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied','partial','quarantined')),
    received_at TEXT NOT NULL,
    applied_at TEXT,
    created_by TEXT NOT NULL,
    UNIQUE(source, batch_key)
);
CREATE TABLE IF NOT EXISTS source_records (
    batch_id TEXT NOT NULL REFERENCES source_batches(batch_id),
    record_no TEXT NOT NULL,
    record_type TEXT NOT NULL,
    ref_company TEXT NOT NULL DEFAULT '',
    ref_security TEXT NOT NULL DEFAULT '',
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied','duplicate','quarantined')),
    fact_kind TEXT NOT NULL DEFAULT '',
    fact_id TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    PRIMARY KEY(batch_id, record_no)
);
CREATE TABLE IF NOT EXISTS source_quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    record_no TEXT NOT NULL,
    record_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    existing_digest TEXT NOT NULL DEFAULT '',
    incoming_digest TEXT NOT NULL,
    reason TEXT NOT NULL,
    received_at TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS source_quarantine_open ON source_quarantine(batch_id, resolution);
CREATE TABLE IF NOT EXISTS report_cycles (
    period TEXT PRIMARY KEY,
    cutoff_at TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','ready','frozen','published')),
    blockers_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_reports (
    report_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    edition INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('original','errata')),
    state TEXT NOT NULL CHECK(state IN ('draft','published')),
    cutoff_at TEXT NOT NULL,
    watermark_json TEXT NOT NULL,
    taxonomy_json TEXT NOT NULL,
    trigger_batch_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    parent_report_id TEXT REFERENCES monthly_reports(report_id),
    frozen_at TEXT NOT NULL,
    frozen_by TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    UNIQUE(period, edition)
);
CREATE TABLE IF NOT EXISTS report_metric_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES monthly_reports(report_id),
    metric_key TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    value_json TEXT NOT NULL,
    previous_value_json TEXT,
    changed INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(report_id, metric_key, dimension)
);
CREATE TABLE IF NOT EXISTS report_support (
    support_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES monthly_reports(report_id),
    metric_key TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    company_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    security_id TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    event_kind TEXT NOT NULL DEFAULT '',
    fact_kind TEXT NOT NULL,
    fact_id TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL,
    source_record_no TEXT NOT NULL DEFAULT '',
    effective_at TEXT NOT NULL DEFAULT '',
    value_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS report_support_lookup ON report_support(report_id, metric_key, dimension);
CREATE TABLE IF NOT EXISTS report_contributors (
    report_id TEXT NOT NULL REFERENCES monthly_reports(report_id),
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'entry',
    PRIMARY KEY(report_id, actor_id, role)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
