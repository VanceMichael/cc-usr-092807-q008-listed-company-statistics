"""上市公司月报：冻结水位、勘误版本、发布分离与指标钻取。

- freeze 冻结截止周期、已到达批次水位（含摘要）和三套分类的确认版本；
  缺批次或待确认分类时记录阻断项，服务恢复后以同一截止重试。
- 晚到批次只对受影响指标生成 errata 勘误版本，原月报保持 published 不被覆盖。
- publish 与录入/冻结职责分离；未公开公司对普通查询与不存在无法区分。
- explain 可从任一版本的比例/增量下钻到公司、证券事件、来源水位与更正理由。

水位内同一自然键的事实可能有多个更正版本，计算只取 ingested_at 最新一条。
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json
from .security import AccessContext
from .timeutil import Clock
from .companies import CompanyRegistry
from .taxonomy import TaxonomyBook

PERM_FREEZE = "write:monthly"
PERM_PUBLISH = "publish:monthly"
PERM_READ = "read:monthly"

_AXES = ("industry", "control_nature", "province")


@dataclass(frozen=True)
class MonthlyReportService:
    database: Database
    clock: Clock
    companies: CompanyRegistry
    taxonomy: TaxonomyBook

    # ------------------------------------------------------------------ 冻结

    def freeze(self, context: AccessContext, period: str, *, reason: str = "") -> dict:
        context.require(PERM_FREEZE)
        _split_period(period)
        with self.database.transaction() as connection:
            cycle = connection.execute("SELECT * FROM report_cycles WHERE period=?", (period,)).fetchone()
            if not cycle:
                raise NotFoundError(f"{period} 截止周期尚未登记")
            if connection.execute("SELECT 1 FROM monthly_reports WHERE period=? LIMIT 1", (period,)).fetchone():
                raise ConflictError("该月已有冻结月报，晚到材料请出具勘误")
            cutoff = cycle["cutoff_at"]
            blockers = self._blockers(connection, period, cutoff)
            if blockers:
                connection.execute(
                    "UPDATE report_cycles SET status='waiting',blockers_json=?,updated_at=? WHERE period=?",
                    (canonical_json(blockers), self.clock.now(), period))
                raise ConflictError({"message": "存在阻断项，截止任务保持等待，恢复后可续跑",
                                     "blockers": blockers})
            taxonomy = self._pinned_taxonomy(connection)
            computed = self._compute(connection, period, cutoff, taxonomy)
            report_id = self._persist_edition(
                connection, period=period, edition=1, kind="original", parent=None, cutoff=cutoff,
                taxonomy=taxonomy, computed=computed, trigger_batches=[], reason=reason,
                actor=context.actor_id)
            connection.execute(
                "UPDATE report_cycles SET status='frozen',blockers_json='[]',updated_at=? WHERE period=?",
                (self.clock.now(), period))
            return self._report_summary(connection, report_id)

    def issue_errata(self, context: AccessContext, period: str, *, note: str = "") -> dict:
        """晚到批次到达后，为受影响指标生成勘误版本；原月报保持不动。"""
        context.require(PERM_FREEZE)
        with self.database.transaction() as connection:
            latest = connection.execute(
                "SELECT * FROM monthly_reports WHERE period=? ORDER BY edition DESC LIMIT 1",
                (period,)).fetchone()
            if not latest or latest["state"] != "published":
                raise ConflictError("原月报发布后才能出具勘误")
            prior_cutoff = json.loads(latest["watermark_json"])["cutoff"]
            triggers = [dict(r) for r in connection.execute(
                "SELECT batch_id,source,batch_key,digest,note,received_at FROM source_batches"
                " WHERE period_label=? AND status IN ('applied','partial') AND received_at>?"
                " ORDER BY received_at,batch_id", (period, prior_cutoff)).fetchall()]
            if not triggers:
                raise ConflictError("没有晚于原水位的新批次，无需勘误")
            cutoff = max(t["received_at"] for t in triggers)
            blockers = self._blockers(connection, period, cutoff)
            if blockers:
                raise ConflictError({"message": "晚到材料仍有待确认事项", "blockers": blockers})
            taxonomy = self._pinned_taxonomy(connection)
            computed = self._compute(connection, period, cutoff, taxonomy)
            prior_lines = self._load_lines(connection, latest["report_id"])
            changed = _diff_lines(prior_lines, computed["lines"])
            if not changed:
                raise ConflictError("晚到批次未改变任何指标")
            edition = latest["edition"] + 1
            report_id = self._persist_edition(
                connection, period=period, edition=edition, kind="errata",
                parent=latest["report_id"], cutoff=cutoff, taxonomy=taxonomy, computed=computed,
                trigger_batches=triggers, only_changed=changed,
                reason=(note.strip() or "晚到批次引致的指标更正"), actor=context.actor_id)
            return self._report_summary(connection, report_id)

    # ------------------------------------------------------------------ 发布

    def publish(self, context: AccessContext, report_id: str) -> dict:
        context.require(PERM_PUBLISH)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM monthly_reports WHERE report_id=?", (report_id,)).fetchone()
            if not row:
                raise NotFoundError("月报不存在")
            if row["state"] == "published":
                raise ConflictError("月报已经发布")
            contributor = connection.execute(
                "SELECT 1 FROM report_contributors WHERE report_id=? AND actor_id=?",
                (report_id, context.actor_id)).fetchone()
            if contributor:
                raise PermissionDenied("录入或冻结人员不能发布同一份月报")
            connection.execute(
                "UPDATE monthly_reports SET state='published',published_at=?,published_by=? WHERE report_id=?",
                (self.clock.now(), context.actor_id, report_id))
            if row["kind"] == "original":
                connection.execute(
                    "UPDATE report_cycles SET status='published',updated_at=? WHERE period=?",
                    (self.clock.now(), row["period"]))
            return self._report_summary(connection, report_id)

    # ------------------------------------------------------------------ 查询

    def list_editions(self, context: AccessContext, period: str) -> list[dict]:
        context.require(PERM_READ)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT report_id FROM monthly_reports WHERE period=? ORDER BY edition", (period,)).fetchall()
            return [self._report_summary(connection, r["report_id"]) for r in rows]

    def explain(self, context: AccessContext, period: str, metric_key: str, dimension: str = "",
                *, edition: int | None = None) -> dict:
        """从指标（含比例/增量）下钻到公司、证券事件、来源水位和更正理由。"""
        context.require(PERM_READ)
        with self.database.connect() as connection:
            target = self._pick_edition(connection, period, edition)
            line, owner = self._line_with_owner(connection, target, metric_key, dimension)
            supports = [dict(r) for r in connection.execute(
                "SELECT company_id,company_name,security_id,market,event_kind,fact_kind,fact_id,batch_id,"
                "source_record_no,effective_at,value_text FROM report_support WHERE report_id=? AND metric_key=?"
                " AND dimension=? ORDER BY company_id,security_id,fact_kind",
                (owner["report_id"], metric_key, dimension)).fetchall()]
            watermark = json.loads(owner["watermark_json"])
            return {
                "period": period, "metric_key": metric_key, "dimension": dimension,
                "edition": owner["edition"], "edition_kind": owner["kind"], "edition_state": owner["state"],
                "value": json.loads(line["value_json"]),
                "previous_value": (json.loads(line["previous_value_json"])
                                   if line["previous_value_json"] is not None else None),
                "changed": bool(line["changed"]), "reason": line["reason"],
                "supporting_facts": supports,
                "watermark": {"cutoff": watermark["cutoff"], "batches": watermark["batches"]},
                "trigger_batches": json.loads(owner["trigger_batch_json"]),
                "correction_chain": self._correction_chain(connection, owner),
            }

    def company_timeline(self, context: AccessContext, unified_code: str) -> dict:
        """跨市场归并主体的时间串联；未公开主体与不存在返回同一错误。"""
        context.require(PERM_READ)
        with self.database.connect() as connection:
            company_id = self.companies.resolve(connection, unified_code)
            if not company_id:
                raise NotFoundError("公司不存在")
            row = connection.execute("SELECT public FROM companies WHERE company_id=?", (company_id,)).fetchone()
            if not row["public"] and not context.reveal_sensitive:
                raise NotFoundError("公司不存在")
            attrs = [dict(r) for r in connection.execute(
                "SELECT version,name,registered_address,effective_at,batch_id,source_record_no"
                " FROM company_attributes WHERE company_id=? ORDER BY version", (company_id,)).fetchall()]
            securities = [dict(r) for r in connection.execute(
                "SELECT security_id,market,ticker,currency FROM securities WHERE company_id=?"
                " ORDER BY market,ticker", (company_id,)).fetchall()]
            events = [dict(r) for r in connection.execute(
                "SELECT event_id,security_id,market,kind,event_at,detail_json,batch_id,source_record_no"
                " FROM market_events WHERE company_id=? ORDER BY event_at,market", (company_id,)).fetchall()]
            capitals = [dict(r) for r in connection.execute(
                "SELECT change_id,security_id,change_type,shares_after,effective_at,batch_id,source_record_no"
                " FROM capital_changes WHERE company_id=? ORDER BY effective_at,ingested_at",
                (company_id,)).fetchall()]
            valuations = [dict(r) for r in connection.execute(
                "SELECT security_id,trade_date,close_price,shares,market_value_cny,currency,batch_id"
                " FROM valuations WHERE company_id=? ORDER BY trade_date,ingested_at", (company_id,)).fetchall()]
            raisings = [dict(r) for r in connection.execute(
                "SELECT raising_id,security_id,kind,gross_cny_minor,raised_at,batch_id"
                " FROM raisings WHERE company_id=? ORDER BY raised_at", (company_id,)).fetchall()]
            classes = [dict(r) for r in connection.execute(
                "SELECT axis,code,status,effective_from,taxonomy_version_id,batch_id"
                " FROM classifications WHERE company_id=? ORDER BY axis,effective_from,ingested_at",
                (company_id,)).fetchall()]
            return {"company_id": company_id, "attributes": attrs, "securities": securities,
                    "events": events, "capital_changes": capitals, "valuations": valuations,
                    "raisings": raisings, "classifications": classes}

    def blockers(self, context: AccessContext, period: str) -> dict:
        context.require(PERM_READ)
        with self.database.connect() as connection:
            cycle = connection.execute("SELECT cutoff_at FROM report_cycles WHERE period=?", (period,)).fetchone()
            if not cycle:
                raise NotFoundError("截止周期尚未登记")
            return {"period": period, "blockers": self._blockers(connection, period, cycle["cutoff_at"])}

    # ------------------------------------------------------------ 阻断与续跑

    def _blockers(self, connection: sqlite3.Connection, period: str, cutoff: str) -> list[dict]:
        blockers: list[dict] = []
        expected = connection.execute("SELECT expected_json FROM report_cycles WHERE period=?",
                                      (period,)).fetchone()
        if expected:
            wanted = set(json.loads(expected["expected_json"]))
            arrived = {r["source"] for r in connection.execute(
                "SELECT DISTINCT source FROM source_batches WHERE period_label=?"
                " AND status IN ('applied','partial')", (period,)).fetchall()}
            blockers.extend({"kind": "missing_batch", "source": source}
                            for source in sorted(wanted - arrived))
        for axis in _AXES:
            if not connection.execute(
                    "SELECT 1 FROM taxonomy_versions WHERE axis=? AND status='confirmed' LIMIT 1",
                    (axis,)).fetchone():
                blockers.append({"kind": "taxonomy_unconfirmed", "axis": axis})
        self._materialize_visible(connection, cutoff)
        for item in self._tentative_listed(connection, period):
            blockers.append({"kind": "classification_pending", **item})
        return blockers

    def _tentative_listed(self, connection: sqlite3.Connection, period: str) -> list[dict]:
        end = _end_instant(period)
        end_date = end[:10]
        listed_ids = [r["company_id"] for r in connection.execute(
            """SELECT DISTINCT s.company_id FROM securities s
               WHERE (SELECT e.kind FROM tmp_latest_events e WHERE e.security_id=s.security_id
                        AND e.event_at<=? ORDER BY e.event_at DESC,e.ingested_at DESC LIMIT 1)='listing'""",
            (end,)).fetchall()]
        result: list[dict] = []
        for cid in listed_ids:
            for axis in _AXES:
                latest = connection.execute(
                    "SELECT code,status FROM tmp_latest_cls WHERE company_id=? AND axis=? AND effective_from<=?"
                    " ORDER BY effective_from DESC,ingested_at DESC LIMIT 1", (cid, axis, end_date)).fetchone()
                if latest is None or latest["status"] != "confirmed":
                    result.append({"company_id": cid,
                                   "company_name": self.companies.name_as_of(connection, cid, end),
                                   "axis": axis, "code": latest["code"] if latest else ""})
        return result

    # -------------------------------------------------------------- 指标计算

    def _pinned_taxonomy(self, connection: sqlite3.Connection) -> dict:
        pinned: dict[str, dict] = {}
        for axis in _AXES:
            row = connection.execute(
                "SELECT version_id,version_label,digest,mapping_json FROM taxonomy_versions"
                " WHERE axis=? AND status='confirmed' ORDER BY confirmed_at DESC,version_id DESC LIMIT 1",
                (axis,)).fetchone()
            if not row:
                raise ConflictError(f"{axis} 分类尚无确认版本")
            pinned[axis] = {"version_id": row["version_id"], "label": row["version_label"],
                            "digest": row["digest"], "mapping": json.loads(row["mapping_json"])}
        return pinned

    def _materialize_visible(self, connection: sqlite3.Connection, cutoff: str) -> None:
        """把水位内各事实自然键的最新更正版本物化为临时表。

        同水位内 ingested_at 可能并列（固定时钟/批量导入），按主键做确定性裁决。
        """
        connection.execute("DROP TABLE IF EXISTS tmp_visible_batches")
        connection.execute(
            "CREATE TEMP TABLE tmp_visible_batches AS SELECT batch_id FROM source_batches"
            " WHERE status IN ('applied','partial') AND received_at<=?", (cutoff,))
        connection.execute("DROP TABLE IF EXISTS tmp_latest_events")
        connection.execute(
            """CREATE TEMP TABLE tmp_latest_events AS
               SELECT me.* FROM market_events me
               WHERE me.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                 AND NOT EXISTS (
                   SELECT 1 FROM market_events me2
                   WHERE me2.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                     AND me2.security_id=me.security_id AND me2.kind=me.kind AND me2.event_at=me.event_at
                     AND (me2.ingested_at>me.ingested_at
                          OR (me2.ingested_at=me.ingested_at AND me2.event_id>me.event_id)))""")
        connection.execute("DROP TABLE IF EXISTS tmp_latest_capital")
        connection.execute(
            """CREATE TEMP TABLE tmp_latest_capital AS
               SELECT cc.* FROM capital_changes cc
               WHERE cc.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                 AND NOT EXISTS (
                   SELECT 1 FROM capital_changes cc2
                   WHERE cc2.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                     AND cc2.company_id=cc.company_id
                     AND COALESCE(cc2.security_id,'')=COALESCE(cc.security_id,'')
                     AND cc2.change_type=cc.change_type AND cc2.effective_at=cc.effective_at
                     AND (cc2.ingested_at>cc.ingested_at
                          OR (cc2.ingested_at=cc.ingested_at AND cc2.change_id>cc.change_id)))""")
        connection.execute("DROP TABLE IF EXISTS tmp_latest_valuations")
        connection.execute(
            """CREATE TEMP TABLE tmp_latest_valuations AS
               SELECT v.* FROM valuations v
               WHERE v.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                 AND NOT EXISTS (
                   SELECT 1 FROM valuations v2
                   WHERE v2.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                     AND v2.security_id=v.security_id AND v2.trade_date=v.trade_date
                     AND (v2.ingested_at>v.ingested_at
                          OR (v2.ingested_at=v.ingested_at AND v2.valuation_id>v.valuation_id)))""")
        connection.execute("DROP TABLE IF EXISTS tmp_latest_raisings")
        connection.execute(
            """CREATE TEMP TABLE tmp_latest_raisings AS
               SELECT r.* FROM raisings r
               WHERE r.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                 AND NOT EXISTS (
                   SELECT 1 FROM raisings r2
                   WHERE r2.batch_id IN (SELECT batch_id FROM tmp_visible_batches)
                     AND r2.company_id=r.company_id AND r2.security_id=r.security_id
                     AND r2.kind=r.kind AND r2.raised_at=r.raised_at
                     AND (r2.ingested_at>r.ingested_at
                          OR (r2.ingested_at=r.ingested_at AND r2.raising_id>r.raising_id)))""")
        connection.execute("DROP TABLE IF EXISTS tmp_latest_cls")
        connection.execute(
            """CREATE TEMP TABLE tmp_latest_cls AS
               SELECT cl.* FROM classifications cl
               LEFT JOIN source_batches sb ON sb.batch_id=cl.batch_id
               WHERE (cl.batch_id IS NULL OR sb.batch_id IS NOT NULL)
                 AND NOT EXISTS (
                   SELECT 1 FROM classifications cl2
                   LEFT JOIN source_batches sb2 ON sb2.batch_id=cl2.batch_id
                   WHERE (cl2.batch_id IS NULL OR sb2.batch_id IS NOT NULL)
                     AND cl2.company_id=cl.company_id AND cl2.axis=cl.axis
                     AND cl2.effective_from=cl.effective_from
                     AND (cl2.ingested_at>cl.ingested_at
                          OR (cl2.ingested_at=cl.ingested_at
                              AND (cl2.status='confirmed' AND cl.status='tentative'))
                          OR (cl2.ingested_at=cl.ingested_at AND cl2.status=cl.status
                              AND cl2.classification_id>cl.classification_id)))""")

    def _compute(self, connection: sqlite3.Connection, period: str, cutoff: str, taxonomy: dict) -> dict:
        start_date, end_date = _period_window(period)
        end_instant = _end_instant(period)
        self._materialize_visible(connection, cutoff)
        manufacturing_codes = {code for code, name in taxonomy["industry"]["mapping"].items()
                               if "制造业" in str(name)}
        companies = connection.execute(
            """SELECT DISTINCT c.company_id,c.canonical_name FROM companies c
               JOIN securities s ON s.company_id=c.company_id
               JOIN tmp_latest_events e ON e.security_id=s.security_id
               WHERE c.public=1 AND e.kind='listing' AND e.event_at<=?
               ORDER BY c.company_id""", (end_instant,)).fetchall()

        lines: dict[tuple[str, str], dict] = {}

        def put(key: str, dim: str, value, supports: list[dict] | None = None) -> None:
            lines[(key, dim)] = {"value": value, "supports": supports or []}

        new_listing_supports: list[dict] = []
        province_buckets: dict[str, list[str]] = {}
        control_buckets: dict[str, list[str]] = {}
        industry_value: dict[str, int] = {}
        industry_supports: dict[str, list[dict]] = {}
        manufacturing_value = 0
        manufacturing_supports: list[dict] = []
        listed = 0
        names: dict[str, str] = {}

        def name_of(cid: str) -> str:
            if cid not in names:
                names[cid] = self.companies.name_as_of(connection, cid, end_instant)
            return names[cid]

        for company in companies:
            cid = company["company_id"]
            cname = name_of(cid)
            sec_rows = connection.execute(
                """SELECT s.security_id,s.market,s.ticker FROM securities s WHERE s.company_id=?
                   AND (s.batch_id IS NULL OR s.batch_id IN (SELECT batch_id FROM tmp_visible_batches))
                   ORDER BY s.market,s.ticker""", (cid,)).fetchall()
            listed_securities = []
            first_listing = None
            for sec in sec_rows:
                latest_event = connection.execute(
                    "SELECT kind FROM tmp_latest_events WHERE security_id=? AND event_at<=?"
                    " ORDER BY event_at DESC LIMIT 1", (sec["security_id"], end_instant)).fetchone()
                if latest_event and latest_event["kind"] == "listing":
                    listed_securities.append(sec)
                first = connection.execute(
                    "SELECT * FROM tmp_latest_events WHERE security_id=? AND kind='listing'"
                    " ORDER BY event_at ASC LIMIT 1", (sec["security_id"],)).fetchone()
                if first and (first_listing is None or first["event_at"] < first_listing["event_at"]):
                    first_listing = first
            if not listed_securities:
                continue
            listed += 1
            if first_listing and start_date <= first_listing["event_at"][:10] <= end_date:
                new_listing_supports.append(_support(
                    cid, cname, first_listing["security_id"], first_listing["market"], "listing", "event",
                    first_listing["event_id"], first_listing["batch_id"], first_listing["source_record_no"],
                    first_listing["event_at"], first_listing["event_at"]))
            province = self._classification(connection, cid, "province", end_date)
            control = self._classification(connection, cid, "control_nature", end_date)
            industry = self._classification(connection, cid, "industry", end_date)
            if province:
                province_buckets.setdefault(province["code"], []).append(cid)
            if control:
                control_buckets.setdefault(control["code"], []).append(cid)
            for sec in listed_securities:
                val = connection.execute(
                    "SELECT * FROM tmp_latest_valuations WHERE security_id=? AND trade_date<=?"
                    " ORDER BY trade_date DESC LIMIT 1", (sec["security_id"], end_date)).fetchone()
                if not val:
                    continue
                if industry:
                    industry_value[industry["code"]] = industry_value.get(industry["code"], 0) \
                        + val["market_value_cny"]
                    support = _support(cid, cname, sec["security_id"], sec["market"], "", "valuation",
                                       val["valuation_id"], val["batch_id"], val["source_record_no"],
                                       val["trade_date"], str(val["market_value_cny"]))
                    industry_supports.setdefault(industry["code"], []).append(support)
                    if industry["code"] in manufacturing_codes:
                        manufacturing_value += val["market_value_cny"]
                        manufacturing_supports.append(support)

        raising_rows = connection.execute(
            "SELECT * FROM tmp_latest_raisings WHERE raised_at BETWEEN ? AND ?"
            " ORDER BY raised_at,raising_id", (start_date, end_date)).fetchall()
        raising_total = sum(r["gross_cny_minor"] for r in raising_rows)
        raising_supports = [_support(r["company_id"], name_of(r["company_id"]), r["security_id"], "", "",
                                     "raising", r["raising_id"], r["batch_id"], r["source_record_no"],
                                     r["raised_at"], str(r["gross_cny_minor"])) for r in raising_rows]

        put("listed_count", "", listed)
        put("new_listings", "", len(new_listing_supports), new_listing_supports)
        for code, ids in sorted(province_buckets.items()):
            put("province_distribution", code, len(ids),
                [_support(cid, name_of(cid), "", "", "", "classification", "", "", "", "", code) for cid in ids])
        for code, ids in sorted(control_buckets.items()):
            supports = [_support(cid, name_of(cid), "", "", "", "classification", "", "", "", "", code)
                        for cid in ids]
            put("control_distribution", code, len(ids), supports)
            put("control_share", code,
                {"numerator": len(ids), "denominator": listed, "ratio": _ratio(len(ids), listed)}, supports)
        for code, value in sorted(industry_value.items()):
            put("industry_market_cap", code, value, industry_supports.get(code, []))
        put("manufacturing_market_cap", "", manufacturing_value, manufacturing_supports)
        put("raising_total", "", raising_total, raising_supports)
        put("raising_count", "", len(raising_rows), raising_supports)
        return {"lines": [{"metric_key": k, "dimension": d, "value": v["value"], "supports": v["supports"]}
                          for (k, d), v in sorted(lines.items())]}

    @staticmethod
    def _classification(connection: sqlite3.Connection, company_id: str, axis: str, on_date: str):
        row = connection.execute(
            "SELECT * FROM tmp_latest_cls WHERE company_id=? AND axis=? AND effective_from<=?"
            " ORDER BY effective_from DESC LIMIT 1", (company_id, axis, on_date)).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- 持久化

    def _persist_edition(self, connection: sqlite3.Connection, *, period: str, edition: int, kind: str,
                         parent: str | None, cutoff: str, taxonomy: dict, computed: dict,
                         trigger_batches: list[dict], reason: str, actor: str,
                         only_changed: set[tuple[str, str]] | None = None) -> str:
        report_id = new_id("report")
        now = self.clock.now()
        watermark = {"cutoff": cutoff, "batches": [
            {k: b[k] for k in ("batch_id", "source", "batch_key", "digest", "received_at")}
            for b in connection.execute(
                "SELECT batch_id,source,batch_key,digest,received_at FROM source_batches"
                " WHERE status IN ('applied','partial') AND received_at<=? ORDER BY received_at,batch_id",
                (cutoff,)).fetchall()]}
        taxonomy_frozen = {axis: {k: v[k] for k in ("version_id", "label", "digest")}
                           for axis, v in taxonomy.items()}
        connection.execute(
            "INSERT INTO monthly_reports(report_id,period,edition,kind,state,cutoff_at,watermark_json,"
            "taxonomy_json,trigger_batch_json,reason,parent_report_id,frozen_at,frozen_by)"
            " VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?)",
            (report_id, period, edition, kind, cutoff, canonical_json(watermark),
             canonical_json(taxonomy_frozen), canonical_json(trigger_batches), reason, parent, now, actor))
        prior_values = self._load_lines(connection, parent) if parent else {}
        trigger_text = "、".join(f"{t['source']}:{t['batch_key']}" for t in trigger_batches)
        for item in computed["lines"]:
            key = (item["metric_key"], item["dimension"])
            if only_changed is not None and key not in only_changed:
                continue
            previous = prior_values.get(key)
            value_json = canonical_json(item["value"])
            changed = 1 if previous is not None and previous["value_json"] != value_json else 0
            line_reason = f"晚到批次 {trigger_text} 引致的更正" if kind == "errata" and changed else ""
            connection.execute(
                "INSERT INTO report_metric_lines(report_id,metric_key,dimension,value_json,previous_value_json,"
                "changed,reason) VALUES(?,?,?,?,?,?,?)",
                (report_id, item["metric_key"], item["dimension"], value_json,
                 previous["value_json"] if previous else None, changed, line_reason))
            for support in item["supports"]:
                connection.execute(
                    "INSERT INTO report_support(report_id,metric_key,dimension,company_id,company_name,"
                    "security_id,market,event_kind,fact_kind,fact_id,batch_id,source_record_no,effective_at,"
                    "value_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (report_id, item["metric_key"], item["dimension"], support["company_id"],
                     support["company_name"], support["security_id"], support["market"],
                     support["event_kind"], support["fact_kind"], support["fact_id"], support["batch_id"],
                     support["source_record_no"], support["effective_at"], support["value_text"]))
        contributors = {r["created_by"] for r in connection.execute(
            "SELECT DISTINCT created_by FROM source_batches WHERE received_at<=?", (cutoff,)).fetchall()}
        contributors.add(actor)
        for person in contributors:
            connection.execute(
                "INSERT OR IGNORE INTO report_contributors(report_id,actor_id,role) VALUES(?,?,'entry')",
                (report_id, person))
        return report_id

    # -------------------------------------------------------------- 辅助读取

    def _pick_edition(self, connection: sqlite3.Connection, period: str, edition: int | None):
        if edition is None:
            row = connection.execute(
                "SELECT * FROM monthly_reports WHERE period=? ORDER BY edition DESC LIMIT 1",
                (period,)).fetchone()
        else:
            row = connection.execute("SELECT * FROM monthly_reports WHERE period=? AND edition=?",
                                     (period, edition)).fetchone()
        if not row:
            raise NotFoundError(f"{period} 月报不存在")
        return row

    @staticmethod
    def _load_lines(connection: sqlite3.Connection, report_id: str | None) -> dict:
        if not report_id:
            return {}
        return {(r["metric_key"], r["dimension"]): r for r in connection.execute(
            "SELECT * FROM report_metric_lines WHERE report_id=?", (report_id,)).fetchall()}

    def _line_with_owner(self, connection: sqlite3.Connection, target, metric_key: str, dimension: str):
        current = target
        while True:
            row = connection.execute(
                "SELECT * FROM report_metric_lines WHERE report_id=? AND metric_key=? AND dimension=?",
                (current["report_id"], metric_key, dimension)).fetchone()
            if row:
                return row, current
            if current["parent_report_id"]:
                current = connection.execute("SELECT * FROM monthly_reports WHERE report_id=?",
                                             (current["parent_report_id"],)).fetchone()
            else:
                raise NotFoundError(f"指标 {metric_key}[{dimension}] 在该月任何版本中都不存在")

    def _correction_chain(self, connection: sqlite3.Connection, owner) -> list[dict]:
        chain: list[dict] = []
        current = owner
        while current:
            chain.append({"edition": current["edition"], "kind": current["kind"], "state": current["state"],
                          "reason": current["reason"], "frozen_by": current["frozen_by"],
                          "published_by": current["published_by"],
                          "frozen_at": current["frozen_at"], "published_at": current["published_at"],
                          "trigger_batches": json.loads(current["trigger_batch_json"])})
            if not current["parent_report_id"]:
                break
            current = connection.execute("SELECT * FROM monthly_reports WHERE report_id=?",
                                         (current["parent_report_id"],)).fetchone()
        return list(reversed(chain))

    def _report_summary(self, connection: sqlite3.Connection, report_id: str) -> dict:
        row = connection.execute("SELECT * FROM monthly_reports WHERE report_id=?", (report_id,)).fetchone()
        lines = connection.execute(
            "SELECT metric_key,dimension,value_json,changed FROM report_metric_lines WHERE report_id=?"
            " ORDER BY metric_key,dimension", (report_id,)).fetchall()
        return {"report_id": row["report_id"], "period": row["period"], "edition": row["edition"],
                "kind": row["kind"], "state": row["state"], "cutoff_at": row["cutoff_at"],
                "reason": row["reason"], "parent_report_id": row["parent_report_id"],
                "frozen_by": row["frozen_by"], "published_by": row["published_by"],
                "frozen_at": row["frozen_at"], "published_at": row["published_at"],
                "lines": [{"metric_key": r["metric_key"], "dimension": r["dimension"],
                           "value": json.loads(r["value_json"]), "changed": bool(r["changed"])} for r in lines]}


# ---------------------------------------------------------------- 函数工具


def _split_period(period: str) -> tuple[int, int]:
    try:
        year_s, month_s = period.split("-")
        year, month = int(year_s), int(month_s)
        if len(year_s) != 4 or not 1 <= month <= 12:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValidationError("月份格式必须是 YYYY-MM") from exc
    return year, month


def _period_window(period: str) -> tuple[str, str]:
    year, month = _split_period(period)
    last = calendar.monthrange(year, month)[1]
    return f"{period}-01", f"{period}-{last:02d}"


def _end_instant(period: str) -> str:
    return _period_window(period)[1] + "T23:59:59Z"


def _ratio(part: int, whole: int) -> str:
    if not whole:
        return "0"
    return format((Decimal(part) / Decimal(whole)).quantize(Decimal("0.0001")), "f")


def _support(company_id: str, company_name: str, security_id: str = "", market: str = "",
             event_kind: str = "", fact_kind: str = "", fact_id: str = "", batch_id: str = "",
             source_record_no: str = "", effective_at: str = "", value_text: str = "") -> dict:
    return {"company_id": company_id, "company_name": company_name, "security_id": security_id,
            "market": market, "event_kind": event_kind, "fact_kind": fact_kind, "fact_id": fact_id,
            "batch_id": batch_id, "source_record_no": source_record_no,
            "effective_at": effective_at, "value_text": value_text}


def _diff_lines(prior: dict[tuple[str, str], sqlite3.Row], current: list[dict]) -> set[tuple[str, str]]:
    changed: set[tuple[str, str]] = set()
    for item in current:
        key = (item["metric_key"], item["dimension"])
        old = prior.get(key)
        value_json = canonical_json(item["value"])
        if old is None or old["value_json"] != value_json:
            changed.add(key)
    return changed
