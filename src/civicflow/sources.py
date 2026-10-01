"""来源批次账本、数据水位、截止周期与冲突隔离。

- 批次以 (source, batch_key) 标识；同批重送且摘要一致直接回放，绝不再次累计。
- 同批重送但记录内容不一致，逐条比对，差异记录进入 source_quarantine，不动事实。
- 单条记录失败不影响同批其他记录（SAVEPOINT），批次状态记 partial。
- 截止周期登记期望来源；缺批次或待确认分类会阻塞冻结，恢复后可续跑同一截止。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol

from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .security import AccessContext
from .timeutil import Clock, canonical_instant


class RecordDispatcher(Protocol):
    def apply_record(self, connection: sqlite3.Connection, payload: dict, *, batch_id: str,
                     record_no: str, actor: str) -> dict: ...


@dataclass(frozen=True)
class SourceLedger:
    database: Database
    clock: Clock

    # -- 截止周期 -------------------------------------------------------------

    def register_cycle(self, *, period: str, cutoff_at: str, expected_sources: list[str], actor: str) -> dict:
        _require_period(period)
        if not expected_sources:
            raise ValidationError("至少登记一个期望来源")
        cutoff_at = canonical_instant(cutoff_at)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM report_cycles WHERE period=?", (period,)).fetchone()
            if row:
                raise ConflictError(f"{period} 截止周期已登记")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO report_cycles(period,cutoff_at,expected_json,status,blockers_json,created_by,"
                "created_at,updated_at) VALUES(?,?,?,'waiting','[]',?,?,?)",
                (period, cutoff_at, canonical_json(sorted(set(expected_sources))), actor, now, now))
            return {"period": period, "cutoff_at": cutoff_at,
                    "expected_sources": sorted(set(expected_sources)), "status": "waiting"}

    def get_cycle(self, period: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM report_cycles WHERE period=?", (period,)).fetchone()
            if not row:
                raise NotFoundError(f"{period} 截止周期不存在")
            return _cycle_to_dict(row)

    def mark_cycle(self, period: str, status: str, blockers: list[dict]) -> None:
        if status not in ("waiting", "ready", "frozen", "published"):
            raise ValidationError("周期状态不合法")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE report_cycles SET status=?,blockers_json=?,updated_at=? WHERE period=?",
                (status, canonical_json(blockers), self.clock.now(), period))

    def list_cycles(self, *, status: str | None = None) -> list[dict]:
        with self.database.connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM report_cycles WHERE status=? ORDER BY period",
                                          (status,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM report_cycles ORDER BY period").fetchall()
            return [_cycle_to_dict(row) for row in rows]

    # -- 缺批次检测 ------------------------------------------------------------

    def missing_sources(self, connection: sqlite3.Connection, period: str) -> list[str]:
        row = connection.execute("SELECT expected_json FROM report_cycles WHERE period=?", (period,)).fetchone()
        if not row:
            return []
        expected = set(json.loads(row["expected_json"]))
        arrived = {r["source"] for r in connection.execute(
            "SELECT DISTINCT source FROM source_batches WHERE period_label=? AND status IN ('applied','partial')",
            (period,)).fetchall()}
        return sorted(expected - arrived)

    # -- 批次接入 --------------------------------------------------------------

    def ingest_batch(self, *, source: str, batch_key: str, period_label: str, records: list[dict],
                     note: str, actor: str, dispatcher: RecordDispatcher) -> dict:
        source = require_safe(source, "来源"); batch_key = require_safe(batch_key, "批次编号")
        if not isinstance(records, list) or not records:
            raise ValidationError("批次至少包含一条记录")
        normalized = [_normalize_record(item) for item in records]
        batch_digest = digest_json([[item["record_no"], item["record_type"], digest_json(item["payload"])]
                                   for item in normalized])
        with self.database.transaction() as connection:
            existing = connection.execute("SELECT * FROM source_batches WHERE source=? AND batch_key=?",
                                          (source, batch_key)).fetchone()
            if existing:
                return self._replay_or_quarantine(connection, existing, normalized, actor)
            return self._apply_new(connection, source=source, batch_key=batch_key, period_label=period_label,
                                   records=normalized, batch_digest=batch_digest, note=note, actor=actor,
                                   dispatcher=dispatcher)

    def _apply_new(self, connection: sqlite3.Connection, *, source: str, batch_key: str, period_label: str,
                   records: list[dict], batch_digest: str, note: str, actor: str,
                   dispatcher: RecordDispatcher) -> dict:
        batch_id = new_id("batch")
        now = self.clock.now()
        connection.execute(
            "INSERT INTO source_batches(batch_id,source,batch_key,period_label,digest,note,record_count,status,"
            "received_at,created_by) VALUES(?,?,?,?,?,?,?,'partial',?,?)",
            (batch_id, source, batch_key, period_label, batch_digest, note.strip(), len(records), now, actor))
        applied = duplicate = 0
        quarantined: list[dict] = []
        for item in records:
            connection.execute("SAVEPOINT record_sp")
            try:
                fact = dispatcher.apply_record(connection, item["payload"], batch_id=batch_id,
                                               record_no=item["record_no"], actor=actor)
            except (ValidationError, ConflictError) as exc:
                connection.execute("ROLLBACK TO SAVEPOINT record_sp")
                connection.execute(
                    "INSERT INTO source_quarantine(batch_id,record_no,record_type,payload_json,incoming_digest,"
                    "reason,received_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["record_no"], item["record_type"], canonical_json(item["payload"]),
                     item["digest"], str(exc), now))
                connection.execute(
                    "INSERT INTO source_records(batch_id,record_no,record_type,ref_company,ref_security,"
                    "payload_digest,payload_json,status) VALUES(?,?,?,?,?,?,?,'quarantined')",
                    (batch_id, item["record_no"], item["record_type"],
                     str(item["payload"].get("unified_code", "")),
                     str(item["payload"].get("ticker", "")), item["digest"],
                     canonical_json(item["payload"])))
                quarantined.append({"record_no": item["record_no"], "reason": str(exc)})
                continue
            is_duplicate = bool(fact.get("duplicate"))
            duplicate += 1 if is_duplicate else 0
            applied += 0 if is_duplicate else 1
            connection.execute(
                "INSERT INTO source_records(batch_id,record_no,record_type,ref_company,ref_security,"
                "payload_digest,payload_json,status,fact_kind,fact_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, item["record_no"], item["record_type"],
                 str(item["payload"].get("unified_code", "")), str(item["payload"].get("ticker", "")),
                 item["digest"], canonical_json(item["payload"]),
                 "duplicate" if is_duplicate else "applied",
                 fact.get("fact_kind", ""), fact.get("fact_id", ""), now))
        status = "applied" if not quarantined else "partial"
        connection.execute("UPDATE source_batches SET status=?,applied_at=? WHERE batch_id=?", (status, now, batch_id))
        return {"batch_id": batch_id, "status": status, "applied": applied, "duplicate": duplicate,
                "quarantined": quarantined}

    def _replay_or_quarantine(self, connection: sqlite3.Connection, existing: sqlite3.Row,
                              records: list[dict], actor: str) -> dict:
        stored = {row["record_no"]: row for row in
                  connection.execute("SELECT * FROM source_records WHERE batch_id=?",
                                     (existing["batch_id"],)).fetchall()}
        if existing["digest"] == digest_json(
                [[item["record_no"], item["record_type"], digest_json(item["payload"])] for item in records]):
            return {"batch_id": existing["batch_id"], "status": existing["status"], "replayed": True,
                    "applied": 0, "duplicate": len(records), "quarantined": []}
        diffs: list[dict] = []
        now = self.clock.now()
        for item in records:
            prior = stored.get(item["record_no"])
            if prior and prior["payload_digest"] == item["digest"]:
                continue
            reason = "重送记录内容与首次不同" if prior else "重送批次出现首次没有的记录编号"
            connection.execute(
                "INSERT INTO source_quarantine(batch_id,record_no,record_type,payload_json,existing_digest,"
                "incoming_digest,reason,received_at) VALUES(?,?,?,?,?,?,?,?)",
                (existing["batch_id"], item["record_no"], item["record_type"], canonical_json(item["payload"]),
                 prior["payload_digest"] if prior else "", item["digest"], reason, now))
            diffs.append({"record_no": item["record_no"], "reason": reason})
        return {"batch_id": existing["batch_id"], "status": "quarantined", "replayed": True,
                "applied": 0, "duplicate": len(records) - len(diffs), "quarantined": diffs}

    # -- 水位 ------------------------------------------------------------------

    def applied_batches(self, connection: sqlite3.Connection, *, received_before: str | None = None) -> list[dict]:
        if received_before:
            rows = connection.execute(
                "SELECT * FROM source_batches WHERE status IN ('applied','partial') AND received_at<=?"
                " ORDER BY received_at,batch_id", (canonical_instant(received_before),)).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM source_batches WHERE status IN ('applied','partial')"
                " ORDER BY received_at,batch_id").fetchall()
        return [{"batch_id": r["batch_id"], "source": r["source"], "batch_key": r["batch_key"],
                 "period_label": r["period_label"], "note": r["note"], "status": r["status"],
                 "received_at": r["received_at"], "applied_at": r["applied_at"]} for r in rows]

    def watermark(self, *, received_before: str | None = None) -> dict:
        with self.database.connect() as connection:
            batches = self.applied_batches(connection, received_before=received_before)
        per_source: dict[str, str] = {}
        for batch in batches:
            if batch["received_at"] > per_source.get(batch["source"], ""):
                per_source[batch["source"]] = batch["received_at"]
        return {"cutoff": received_before, "batches": batches, "per_source": per_source}

    # -- 隔离区 ----------------------------------------------------------------

    def list_quarantine(self, *, resolution: str = "pending") -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_quarantine WHERE resolution=? ORDER BY quarantine_id",
                (resolution,)).fetchall()
            return [dict(row) for row in rows]

    def resolve_quarantine(self, quarantine_id: int, *, actor: str, resolution: str = "resubmitted") -> dict:
        if resolution not in ("resubmitted", "dismissed"):
            raise ValidationError("处置方式必须是 resubmitted 或 dismissed")
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE source_quarantine SET resolution=?,resolved_at=?,resolved_by=? WHERE quarantine_id=?"
                " AND resolution='pending'", (resolution, self.clock.now(), actor, quarantine_id)).rowcount
            if changed != 1:
                raise NotFoundError("待隔离记录不存在或已处置")
            return {"quarantine_id": quarantine_id, "resolution": resolution}


def _normalize_record(item: object) -> dict:
    if not isinstance(item, dict):
        raise ValidationError("批次记录必须是对象")
    record_no = item.get("record_no")
    record_type = item.get("record_type")
    payload = item.get("payload")
    require_safe(str(record_no), "记录编号")
    require_safe(str(record_type), "记录类型")
    if not isinstance(payload, dict):
        raise ValidationError("记录载荷必须是对象")
    return {"record_no": str(record_no), "record_type": str(record_type), "payload": payload,
            "digest": digest_json(payload)}


def _require_period(period: str) -> str:
    if not isinstance(period, str):
        raise ValidationError("月份格式必须是 YYYY-MM")
    try:
        year, month = period.split("-")
        if len(year) != 4 or not 1 <= int(month) <= 12:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValidationError("月份格式必须是 YYYY-MM") from exc
    return period


def _cycle_to_dict(row: sqlite3.Row) -> dict:
    return {"period": row["period"], "cutoff_at": row["cutoff_at"],
            "expected_sources": json.loads(row["expected_json"]), "status": row["status"],
            "blockers": json.loads(row["blockers_json"]), "updated_at": row["updated_at"]}
