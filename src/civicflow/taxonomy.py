"""行业、控股性质、省域三套分类轴的版本化目录与公司归属。

每套轴存在多个版本，版本必须 confirmed 才能用于冻结月报；公司的某轴归属
可以是 tentative（待确认），截止时若仍待确认则阻断冻结，恢复后续跑。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .timeutil import Clock, canonical_date, canonical_instant

AXES = ("industry", "control_nature", "province")


@dataclass(frozen=True)
class TaxonomyBook:
    database: Database
    clock: Clock

    # -- 分类版本 -------------------------------------------------------------

    def create_version(self, *, axis: str, label: str, mapping: dict, actor: str) -> dict:
        if axis not in AXES:
            raise ValidationError(f"分类轴必须是 {', '.join(AXES)} 之一")
        if not isinstance(mapping, dict) or not mapping:
            raise ValidationError("分类映射不能为空")
        digest = digest_json({"axis": axis, "mapping": mapping})
        with self.database.transaction() as connection:
            row = connection.execute("SELECT version_id,status FROM taxonomy_versions WHERE axis=? AND digest=?",
                                     (axis, digest)).fetchone()
            if row:
                return {"version_id": row["version_id"], "status": row["status"], "dedup": True}
            version_id = new_id(f"tax:{axis}")
            now = self.clock.now()
            label = label.strip() if isinstance(label, str) else ""
            if not label:
                raise ValidationError("版本标签不能为空")
            connection.execute(
                "INSERT INTO taxonomy_versions(version_id,axis,version_label,mapping_json,digest,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,'tentative',?,?)",
                (version_id, axis, label, canonical_json(mapping), digest, actor, now))
            return {"version_id": version_id, "status": "tentative", "digest": digest}

    def confirm_version(self, version_id: str, *, actor: str) -> dict:
        if actor == "":
            raise ValidationError("确认人不能为空")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT status FROM taxonomy_versions WHERE version_id=?",
                                     (version_id,)).fetchone()
            if not row:
                raise NotFoundError("分类版本不存在")
            if row["status"] == "confirmed":
                return {"version_id": version_id, "status": "confirmed"}
            connection.execute("UPDATE taxonomy_versions SET status='confirmed',confirmed_at=? WHERE version_id=?",
                               (self.clock.now(), version_id))
            return {"version_id": version_id, "status": "confirmed"}

    def get_version(self, version_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM taxonomy_versions WHERE version_id=?",
                                     (version_id,)).fetchone()
            if not row:
                raise NotFoundError("分类版本不存在")
            return {"version_id": row["version_id"], "axis": row["axis"], "label": row["version_label"],
                    "status": row["status"], "digest": row["digest"], "mapping": json.loads(row["mapping_json"])}

    def latest_confirmed(self, connection: sqlite3.Connection, axis: str, *, as_of: str | None = None) -> dict | None:
        sql = ("SELECT * FROM taxonomy_versions WHERE axis=? AND status='confirmed'"
               " ORDER BY confirmed_at DESC, version_id DESC LIMIT 1")
        row = connection.execute(sql, (axis,)).fetchone()
        if not row:
            return None
        return {"version_id": row["version_id"], "axis": row["axis"], "label": row["version_label"],
                "digest": row["digest"], "mapping": json.loads(row["mapping_json"])}

    # -- 公司归属 -------------------------------------------------------------

    def assign(self, connection: sqlite3.Connection, *, company_id: str, axis: str, code: str,
               effective_from: str, status: str = "tentative", taxonomy_version_id: str | None = None,
               batch_id: str = "", record_no: str = "", actor: str) -> str:
        if axis not in AXES:
            raise ValidationError(f"分类轴必须是 {', '.join(AXES)} 之一")
        if status not in ("tentative", "confirmed"):
            raise ValidationError("分类状态必须是 tentative 或 confirmed")
        effective_from = canonical_date(effective_from)
        classification_id = new_id("cls")
        connection.execute(
            "INSERT INTO classifications(classification_id,company_id,axis,code,taxonomy_version_id,effective_from,"
            "status,batch_id,source_record_no,ingested_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (classification_id, company_id, axis, code, taxonomy_version_id, effective_from, status,
             batch_id, record_no, self.clock.now(), actor))
        return classification_id

    def classify(self, connection: sqlite3.Connection, company_id: str, axis: str, *, on_date: str) -> dict | None:
        """取某日（含）之前最近一条归属；优先 confirmed，其次 tentative 并标注。"""
        day = canonical_date(on_date)
        row = connection.execute(
            "SELECT * FROM classifications WHERE company_id=? AND axis=? AND effective_from<=?"
            " ORDER BY effective_from DESC, ingested_at DESC LIMIT 1", (company_id, axis, day)).fetchone()
        if not row:
            return None
        return {"classification_id": row["classification_id"], "code": row["code"], "status": row["status"],
                "effective_from": row["effective_from"], "taxonomy_version_id": row["taxonomy_version_id"]}

    def pending_companies(self, connection: sqlite3.Connection, *, axis: str | None = None) -> list[dict]:
        """当前最新归属仍为 tentative 的公司（用于恢复后判断是否可以续跑截止）。"""
        sql = ("SELECT c.company_id, c.axis, c.code, c.effective_from FROM classifications c"
               " WHERE c.status='tentative' AND NOT EXISTS ("
               "  SELECT 1 FROM classifications n WHERE n.company_id=c.company_id AND n.axis=c.axis"
               "  AND n.effective_from>=c.effective_from AND n.status='confirmed')")
        rows = connection.execute(sql).fetchall()
        result = [dict(row) for row in rows]
        if axis:
            result = [row for row in result if row["axis"] == axis]
        return result
