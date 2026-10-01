from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied
from civicflow.security import AccessContext

PERIOD = "2026-08"
CODE_A = "91330000MADEMO001A"
CODE_B = "91310000MADEMO001B"


def rec(no: str, kind: str, payload: dict) -> dict:
    body = {"record_type": kind, **payload}
    return {"record_no": no, "record_type": kind, "payload": body}


def company_records() -> list[dict]:
    return [
        rec("co-a", "company", {"unified_code": CODE_A, "name": "示例智造股份有限公司", "public": True,
                                "registered_address": "北京市海淀区", "effective_at": "2026-08-01T00:00:00+08:00"}),
        rec("co-a2", "company", {"unified_code": CODE_A, "name": "示例智造股份有限公司", "public": True,
                                 "registered_address": "浙江省杭州市西湖区",
                                 "effective_at": "2026-08-30T00:00:00+08:00"}),
        rec("co-b", "company", {"unified_code": CODE_B, "name": "华光金融控股股份有限公司", "public": True,
                                "registered_address": "上海市浦东新区", "effective_at": "2026-01-01T00:00:00+08:00"}),
        rec("cl-a-ind", "classification", {"unified_code": CODE_A, "axis": "industry", "code": "C",
                                           "effective_from": "2026-08-01", "status": "confirmed"}),
        rec("cl-a-ctl", "classification", {"unified_code": CODE_A, "axis": "control_nature", "code": "private",
                                           "effective_from": "2026-08-01", "status": "confirmed"}),
        rec("cl-a-pr1", "classification", {"unified_code": CODE_A, "axis": "province", "code": "11",
                                           "effective_from": "2026-08-01", "status": "confirmed"}),
        rec("cl-a-pr2", "classification", {"unified_code": CODE_A, "axis": "province", "code": "33",
                                           "effective_from": "2026-08-30", "status": "confirmed"}),
        rec("cl-b-ind", "classification", {"unified_code": CODE_B, "axis": "industry", "code": "J",
                                           "effective_from": "2026-01-01", "status": "confirmed"}),
        rec("cl-b-ctl", "classification", {"unified_code": CODE_B, "axis": "control_nature", "code": "state",
                                           "effective_from": "2026-01-01", "status": "confirmed"}),
        rec("cl-b-pr", "classification", {"unified_code": CODE_B, "axis": "province", "code": "31",
                                          "effective_from": "2026-01-01", "status": "confirmed"}),
    ]


def cn_records() -> list[dict]:
    return [
        rec("sec-a", "security", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001"}),
        rec("sec-b", "security", {"unified_code": CODE_B, "market": "SSE", "ticker": "600002"}),
        rec("evt-a", "event", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                               "kind": "listing", "event_at": "2026-08-10T09:30:00+08:00"}),
        rec("evt-b", "event", {"unified_code": CODE_B, "market": "SSE", "ticker": "600002",
                               "kind": "listing", "event_at": "2026-01-15T09:30:00+08:00"}),
        rec("cap-a", "capital", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                                 "change_type": "ipo_total", "shares_after": 100_000_000,
                                 "effective_at": "2026-08-10T09:30:00+08:00"}),
        rec("val-a", "valuation", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                                   "trade_date": "2026-08-31", "close_price": "10.00", "shares": 100_000_000}),
        rec("val-b", "valuation", {"unified_code": CODE_B, "market": "SSE", "ticker": "600002",
                                   "trade_date": "2026-08-31", "close_price": "5.00", "shares": 500_000_000}),
        rec("raise-a", "raising", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                                   "kind": "IPO", "gross_cny_minor": 91_800_000_000,
                                   "raised_at": "2026-08-10"}),
    ]


def hk_records() -> list[dict]:
    return [
        rec("sec-a-h", "security", {"unified_code": CODE_A, "market": "HKEX", "ticker": "08001",
                                    "currency": "HKD"}),
        rec("evt-a-h", "event", {"unified_code": CODE_A, "market": "HKEX", "ticker": "08001",
                                 "kind": "listing", "event_at": "2026-08-25T09:30:00+08:00"}),
        rec("val-a-h", "valuation", {"unified_code": CODE_A, "market": "HKEX", "ticker": "08001",
                                     "trade_date": "2026-08-29", "close_price": "8.00", "shares": 20_000_000,
                                     "currency": "HKD", "fx_to_cny": "0.92"}),
    ]


def cap_fix_records() -> list[dict]:
    return [
        rec("cap-a-fix", "capital", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                                     "change_type": "share_correction", "shares_after": 120_000_000,
                                     "effective_at": "2026-08-31T16:00:00+08:00"}),
        rec("val-a-fix", "valuation", {"unified_code": CODE_A, "market": "SSE", "ticker": "688001",
                                       "trade_date": "2026-08-31", "close_price": "10.00",
                                       "shares": 120_000_000}),
    ]


def seed_taxonomy(app: CivicFlow) -> None:
    app.taxonomy.create_version(axis="industry", label="行业分类 2026-08",
                                mapping={"C": "制造业", "J": "金融业"}, actor="tax-admin")
    app.taxonomy.create_version(axis="control_nature", label="控股性质 2026-08",
                                mapping={"private": "民营控股", "state": "国有控股"}, actor="tax-admin")
    app.taxonomy.create_version(axis="province", label="省域 2026-08",
                                mapping={"11": "北京市", "31": "上海市", "33": "浙江省"}, actor="tax-admin")
    for row in app.database.connect().execute("SELECT version_id FROM taxonomy_versions"):
        app.taxonomy.confirm_version(row["version_id"], actor="tax-admin")


def line_map(report: dict) -> dict:
    return {(item["metric_key"], item["dimension"]): item["value"] for item in report["lines"]}


class MonthlyMarketTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.sqlite3")
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-09-05T10:00:00Z")
        seed_taxonomy(self.app)
        self.analyst = AccessContext("analyst-li", permissions=frozenset({"write:monthly", "read:monthly"}))
        self.publisher = AccessContext("publisher-wang",
                                       permissions=frozenset({"publish:monthly", "read:monthly"}))
        self.reader = AccessContext("reader-zhao", permissions=frozenset({"read:monthly"}))

    def tearDown(self):
        self.temp.cleanup()

    def _register_cycle(self, expected=("registry", "exchange-cn", "exchange-hk")):
        self.app.sources.register_cycle(period=PERIOD, cutoff_at="2026-09-06T00:00:00Z",
                                        expected_sources=list(expected), actor="assoc-admin")

    def _ingest_august(self, *, sources=("registry", "exchange-cn", "exchange-hk")):
        if "registry" in sources:
            self.app.sources.ingest_batch(source="registry", batch_key="reg-202608", period_label=PERIOD,
                                          records=company_records(), note="登记", actor="entry-clerk",
                                          dispatcher=self.app.dispatcher)
        if "exchange-cn" in sources:
            self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608", period_label=PERIOD,
                                          records=cn_records(), note="境内", actor="entry-clerk",
                                          dispatcher=self.app.dispatcher)
        if "exchange-hk" in sources:
            self.app.sources.ingest_batch(source="exchange-hk", batch_key="hk-202608", period_label=PERIOD,
                                          records=hk_records(), note="港股", actor="entry-clerk",
                                          dispatcher=self.app.dispatcher)

    def _publish_august(self):
        self._register_cycle()
        self._ingest_august()
        frozen = self.app.monthly.freeze(self.analyst, PERIOD, reason="首次冻结")
        self.app.monthly.publish(self.publisher, frozen["report_id"])
        return frozen

    # -------------------------------------------------------------- 指标计算

    def test_august_metrics_join_cross_market_facts(self):
        report = self._publish_august()
        values = line_map(report)
        self.assertEqual(values[("listed_count", "")], 2)          # 主体口径，A+H 不重复计
        self.assertEqual(values[("new_listings", "")], 1)          # 仅八月首发的智造
        self.assertEqual(values[("province_distribution", "33")], 1)  # 月末已迁至浙江
        self.assertNotIn(("province_distribution", "11"), values)
        self.assertEqual(values[("control_share", "private")]["ratio"], "0.5000")
        # 制造业市值 = 境内 10 元*1 亿股 + 港股 8 港元*2000 万股*0.92
        self.assertEqual(values[("manufacturing_market_cap", "")], 100_000_000_000 + 14_720_000_000)
        self.assertEqual(values[("raising_total", "")], 91_800_000_000)

    def test_company_timeline_keeps_every_market_fact(self):
        self._publish_august()
        timeline = self.app.monthly.company_timeline(self.reader, CODE_A)
        markets = sorted(s["market"] for s in timeline["securities"])
        self.assertEqual(markets, ["HKEX", "SSE"])
        self.assertEqual(len(timeline["attributes"]), 2)  # 名称/注册地址两个时间版本
        self.assertEqual(len(timeline["events"]), 2)     # 境内上市与港股上市事实各自保留
        self.assertEqual({e["kind"] for e in timeline["events"]}, {"listing"})

    # -------------------------------------------------------------- 冻结水位

    def test_freeze_pins_watermark_late_batches_do_not_rewrite_original(self):
        original = self._publish_august()
        later = CivicFlow.open(self.db_path, fixed_now="2026-09-12T10:00:00Z")
        later.sources.ingest_batch(source="exchange-cn", batch_key="cn-capfix", period_label=PERIOD,
                                   records=cap_fix_records(), note="股本更正", actor="entry-clerk",
                                   dispatcher=later.dispatcher)
        # 原月报版本重放仍是旧水位、旧数值
        explanation = later.monthly.explain(self.reader, PERIOD, "manufacturing_market_cap", edition=1)
        self.assertEqual(explanation["value"], 100_000_000_000 + 14_720_000_000)
        self.assertEqual([b["batch_key"] for b in explanation["watermark"]["batches"]].count("cn-capfix"), 0)
        self.assertEqual(explanation["edition_state"], "published")
        edition_one = later.monthly.list_editions(self.reader, PERIOD)[0]
        self.assertEqual(edition_one["state"], "published")

    # -------------------------------------------------------------- 勘误

    def test_late_material_generates_errata_for_affected_metrics_only(self):
        original = self._publish_august()
        later = CivicFlow.open(self.db_path, fixed_now="2026-09-12T10:00:00Z")
        fix = later.sources.ingest_batch(source="exchange-cn", batch_key="cn-capfix", period_label=PERIOD,
                                         records=cap_fix_records(), note="股本更正", actor="entry-clerk",
                                         dispatcher=later.dispatcher)
        self.assertEqual(fix["applied"], 2)
        errata = later.monthly.issue_errata(self.analyst, PERIOD, note="交易所补正总股本")
        keys = {(line["metric_key"], line["dimension"]) for line in errata["lines"]}
        # 只出现真正变化的两个指标；新增家数、地区、募资等不在勘误中
        self.assertEqual(keys, {("industry_market_cap", "C"), ("manufacturing_market_cap", "")})
        cap_line = next(line for line in errata["lines"]
                        if line["metric_key"] == "manufacturing_market_cap")
        self.assertEqual(cap_line["value"], 120_000_000_000 + 14_720_000_000)
        self.assertTrue(cap_line["changed"])
        # 原月报未被覆盖
        editions = later.monthly.list_editions(self.reader, PERIOD)
        self.assertEqual([(e["edition"], e["kind"], e["state"]) for e in editions],
                         [(1, "original", "published"), (2, "errata", "draft")])
        self.assertNotEqual(original["report_id"], errata["report_id"])

    def test_errata_requires_published_original_and_new_watermark(self):
        self._register_cycle()
        self._ingest_august()
        draft = self.app.monthly.freeze(self.analyst, PERIOD)
        with self.assertRaises(ConflictError):
            self.app.monthly.issue_errata(self.analyst, PERIOD)
        self.app.monthly.publish(self.publisher, draft["report_id"])
        with self.assertRaises(ConflictError):
            self.app.monthly.issue_errata(self.analyst, PERIOD)  # 无晚到批次
        with self.assertRaises(ConflictError):
            self.app.monthly.freeze(self.analyst, PERIOD)  # 不能二次冻结

    # -------------------------------------------------------------- 幂等/隔离

    def test_same_batch_resend_is_replayed_not_accumulated(self):
        self._register_cycle()
        self.app.sources.ingest_batch(source="registry", batch_key="reg-202608", period_label=PERIOD,
                                      records=company_records(), note="登记", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        first = self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608",
                                              period_label=PERIOD, records=cn_records(), note="境内",
                                              actor="entry-clerk", dispatcher=self.app.dispatcher)
        replay = self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608",
                                               period_label=PERIOD, records=cn_records(), note="境内",
                                               actor="entry-clerk", dispatcher=self.app.dispatcher)
        self.assertEqual(first["batch_id"], replay["batch_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["applied"], 0)
        self.app.sources.ingest_batch(source="exchange-hk", batch_key="hk-202608", period_label=PERIOD,
                                      records=hk_records(), note="港股", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        report = self.app.monthly.freeze(self.analyst, PERIOD)
        self.assertEqual(line_map(report)[("new_listings", "")], 1)
        self.assertEqual(line_map(report)[("raising_total", "")], 91_800_000_000)

    def test_same_record_no_with_different_payload_is_quarantined(self):
        self._register_cycle()
        self._ingest_august()
        altered = hk_records()
        altered[2]["payload"]["close_price"] = "9.99"
        result = self.app.sources.ingest_batch(source="exchange-hk", batch_key="hk-202608",
                                               period_label=PERIOD, records=altered, note="港股",
                                               actor="entry-clerk", dispatcher=self.app.dispatcher)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual([q["record_no"] for q in result["quarantined"]], ["val-a-h"])
        pending = self.app.sources.list_quarantine()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reason"], "重送记录内容与首次不同")
        # 事实未被污染：仍能按原数据冻结
        report = self.app.monthly.freeze(self.analyst, PERIOD)
        self.assertEqual(line_map(report)[("manufacturing_market_cap", "")], 100_000_000_000 + 14_720_000_000)

    def test_single_bad_record_does_not_kill_batch(self):
        self._register_cycle(expected=("registry", "exchange-cn"))
        self.app.sources.ingest_batch(source="registry", batch_key="reg", period_label=PERIOD,
                                      records=company_records(), note="登记", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        records = cn_records()
        records[5]["payload"]["close_price"] = "not-a-number"  # 仅这一条非法
        result = self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn-bad",
                                               period_label=PERIOD, records=records, note="含坏记录",
                                               actor="entry-clerk", dispatcher=self.app.dispatcher)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertEqual(result["applied"], 7)

    # -------------------------------------------------------------- 职责分离

    def test_entry_and_freeze_actor_cannot_publish(self):
        self._register_cycle()
        self._ingest_august()
        frozen = self.app.monthly.freeze(self.analyst, PERIOD)
        # 录入人不能发布
        clerk = AccessContext("entry-clerk", permissions=frozenset({"publish:monthly"}))
        with self.assertRaises(PermissionDenied):
            self.app.monthly.publish(clerk, frozen["report_id"])
        # 冻结人不能发布
        analyst_publisher = AccessContext(
            "analyst-li", permissions=frozenset({"write:monthly", "publish:monthly"}))
        with self.assertRaises(PermissionDenied):
            self.app.monthly.publish(analyst_publisher, frozen["report_id"])
        # 无发布权限不能发布；无冻结权限不能冻结
        with self.assertRaises(PermissionDenied):
            self.app.monthly.publish(self.analyst, frozen["report_id"])
        with self.assertRaises(PermissionDenied):
            self.app.monthly.freeze(self.publisher, "2026-09")
        # 无关联的发布人可以发布
        self.app.monthly.publish(self.publisher, frozen["report_id"])

    # -------------------------------------------------------------- 未公开隔离

    def test_nonpublic_company_cannot_be_inferred(self):
        self._publish_august()
        with self.app.database.transaction() as conn:
            cid = self.app.companies.ensure_company(
                conn, unified_code="91440000MADEMO001C", name="未公开公司", public=False,
                effective_at="2026-08-15T00:00:00+08:00", actor="entry-clerk")
        # 普通读：未公开主体与不存在主体返回完全一致的错误
        with self.assertRaises(NotFoundError):
            self.app.monthly.company_timeline(self.reader, "91440000MADEMO001C")
        with self.assertRaises(NotFoundError):
            self.app.monthly.company_timeline(self.reader, "91999999MADEMO999X")
        with self.assertRaises(NotFoundError):
            self.app.companies.get(cid)
        # 系统视角仍可见
        self.assertTrue(self.app.companies.get(cid, include_private=True)["public"] is False)

    # ----------------------------------------------------- 缺失批次/待确认续跑

    def test_missing_batch_blocks_freeze_and_resumes_with_same_cutoff(self):
        self._register_cycle()
        self._ingest_august(sources=("registry", "exchange-cn"))  # 缺 exchange-hk
        with self.assertRaises(ConflictError) as caught:
            self.app.monthly.freeze(self.analyst, PERIOD)
        blockers = caught.exception.args[0]["blockers"]
        self.assertEqual([b["source"] for b in blockers if b["kind"] == "missing_batch"], ["exchange-hk"])
        self.assertEqual(self.app.sources.get_cycle(PERIOD)["status"], "waiting")
        # 服务恢复：批次补齐后以同一截止续跑成功
        self._ingest_august(sources=("exchange-hk",))
        frozen = self.app.monthly.freeze(self.analyst, PERIOD)
        self.assertEqual(frozen["kind"], "original")
        self.assertEqual(self.app.sources.get_cycle(PERIOD)["status"], "frozen")

    def test_tentative_classification_blocks_and_resumes_after_confirmation(self):
        self._register_cycle(expected=("registry", "exchange-cn"))
        records = [r for r in company_records()
                   if r["record_no"] not in ("cl-a-ind", "cl-a-ctl", "cl-a-pr1", "cl-a-pr2")]
        records.append(rec("cl-a-ind-t", "classification", {"unified_code": CODE_A, "axis": "industry",
                                                             "code": "C", "effective_from": "2026-08-01",
                                                             "status": "tentative"}))
        records.append(rec("cl-a-ctl-t", "classification", {"unified_code": CODE_A, "axis": "control_nature",
                                                             "code": "private", "effective_from": "2026-08-01",
                                                             "status": "tentative"}))
        records.append(rec("cl-a-pr2-t", "classification", {"unified_code": CODE_A, "axis": "province",
                                                             "code": "33", "effective_from": "2026-08-01",
                                                             "status": "tentative"}))
        self.app.sources.ingest_batch(source="registry", batch_key="reg", period_label=PERIOD,
                                      records=records, note="待确认行业", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn", period_label=PERIOD,
                                      records=cn_records(), note="境内", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        with self.assertRaises(ConflictError) as caught:
            self.app.monthly.freeze(self.analyst, PERIOD)
        pending = [b for b in caught.exception.args[0]["blockers"] if b["kind"] == "classification_pending"]
        self.assertTrue(any(b["axis"] == "industry" and b["company_id"] for b in pending))
        # 确认材料晚到：同一生效日给出 confirmed 归属
        confirm_batch = [
            rec("cl-a-ind-ok", "classification", {"unified_code": CODE_A, "axis": "industry",
                                                   "code": "C", "effective_from": "2026-08-01",
                                                   "status": "confirmed"}),
            rec("cl-a-ctl-ok", "classification", {"unified_code": CODE_A, "axis": "control_nature",
                                                   "code": "private", "effective_from": "2026-08-01",
                                                   "status": "confirmed"}),
            rec("cl-a-pr-ok", "classification", {"unified_code": CODE_A, "axis": "province",
                                                  "code": "33", "effective_from": "2026-08-01",
                                                  "status": "confirmed"}),
        ]
        self.app.sources.ingest_batch(source="registry", batch_key="reg-confirm", period_label=PERIOD,
                                      records=confirm_batch, note="行业确认", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        frozen = self.app.monthly.freeze(self.analyst, PERIOD)
        self.assertEqual(line_map(frozen)[("manufacturing_market_cap", "")], 100_000_000_000)

    def test_unconfirmed_taxonomy_version_blocks_freeze(self):
        fresh = tempfile.TemporaryDirectory()
        try:
            app2 = CivicFlow.open(str(Path(fresh.name) / "x.sqlite3"), fixed_now="2026-09-05T10:00:00Z")
            app2.taxonomy.create_version(axis="industry", label="行业", mapping={"C": "制造业"}, actor="t")
            app2.taxonomy.create_version(axis="control_nature", label="控股",
                                         mapping={"private": "民营"}, actor="t")
            app2.taxonomy.create_version(axis="province", label="省域", mapping={"11": "北京"}, actor="t")
            app2.sources.register_cycle(period="2026-09", cutoff_at="2026-10-06T00:00:00Z",
                                        expected_sources=["registry"], actor="a")
            analyst = AccessContext("a", permissions=frozenset({"write:monthly"}))
            with self.assertRaises(ConflictError) as caught:
                app2.monthly.freeze(analyst, "2026-09")
            axes = {b["axis"] for b in caught.exception.args[0]["blockers"]
                    if b["kind"] == "taxonomy_unconfirmed"}
            self.assertEqual(axes, {"industry", "control_nature", "province"})
        finally:
            fresh.cleanup()

    # -------------------------------------------------------------- 指标钻取

    def test_explain_traces_ratio_to_companies_events_batches_and_reasons(self):
        self._publish_august()
        later = CivicFlow.open(self.db_path, fixed_now="2026-09-12T10:00:00Z")
        later.sources.ingest_batch(source="exchange-cn", batch_key="cn-capfix", period_label=PERIOD,
                                   records=cap_fix_records(), note="股本更正", actor="entry-clerk",
                                   dispatcher=later.dispatcher)
        errata = later.monthly.issue_errata(self.analyst, PERIOD, note="交易所补正总股本")
        later.monthly.publish(self.publisher, errata["report_id"])
        explanation = later.monthly.explain(self.reader, PERIOD, "manufacturing_market_cap")
        self.assertEqual(explanation["edition"], 2)
        self.assertEqual(explanation["previous_value"], 100_000_000_000 + 14_720_000_000)
        self.assertIn("cn-capfix", explanation["reason"])
        supporting = explanation["supporting_facts"]
        # 可追到具体公司、具体证券、具体估值事实与来源批次编号
        self.assertEqual({s["company_name"] for s in supporting}, {"示例智造股份有限公司"})
        self.assertEqual({s["market"] for s in supporting}, {"SSE", "HKEX"})
        self.assertTrue(all(s["fact_id"] and s["batch_id"] and s["source_record_no"] for s in supporting))
        self.assertEqual([c["kind"] for c in explanation["correction_chain"]], ["original", "errata"])
        self.assertEqual(explanation["trigger_batches"][0]["batch_key"], "cn-capfix")
        # 比例钻取
        ratio = later.monthly.explain(self.reader, PERIOD, "control_share", "private")
        self.assertEqual(ratio["value"]["numerator"], 1)
        self.assertEqual(ratio["value"]["denominator"], 2)
        self.assertEqual({s["company_id"] for s in ratio["supporting_facts"]},
                         {supporting[0]["company_id"]})
        # 新增家数钻取到上市事件
        new_listings = later.monthly.explain(self.reader, PERIOD, "new_listings", edition=1)
        self.assertEqual(new_listings["supporting_facts"][0]["event_kind"], "listing")
        self.assertEqual(new_listings["supporting_facts"][0]["fact_kind"], "event")

    # -------------------------------------------------------------- 主体归并

    def test_merge_retains_each_market_facts_under_one_identity(self):
        self._register_cycle(expected=("registry", "exchange-cn", "exchange-hk"))
        records = company_records()
        records += [
            rec("co-d", "company", {"unified_code": "91440000MADEMO001D", "name": "华光金融控股股份有限公司",
                                    "public": True, "registered_address": "上海市浦东新区",
                                    "effective_at": "2026-01-01T00:00:00+08:00"}),
            rec("sec-d", "security", {"unified_code": "91440000MADEMO001D", "market": "HKEX", "ticker": "06002",
                                      "currency": "HKD"}),
            rec("evt-d", "event", {"unified_code": "91440000MADEMO001D", "market": "HKEX", "ticker": "06002",
                                   "kind": "listing", "event_at": "2026-02-01T09:30:00+08:00"}),
        ]
        self.app.sources.ingest_batch(source="registry", batch_key="reg", period_label=PERIOD,
                                      records=records, note="登记含重复建档", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        self.app.sources.ingest_batch(source="exchange-cn", batch_key="cn", period_label=PERIOD,
                                      records=cn_records(), note="境内", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        self.app.sources.ingest_batch(source="exchange-hk", batch_key="hk", period_label=PERIOD,
                                      records=hk_records(), note="港股", actor="entry-clerk",
                                      dispatcher=self.app.dispatcher)
        with self.app.database.transaction() as conn:
            merged = self.app.companies.merge_companies(
                conn, survivor_code=CODE_B, absorbed_code="91440000MADEMO001D",
                actor="matcher", reason="同一主体重复建档")
        # 归并后两个市场的事实都在存续主体之下
        timeline = self.app.monthly.company_timeline(AccessContext.system(), CODE_B)
        self.assertEqual({(s["market"], s["ticker"]) for s in timeline["securities"]},
                         {("SSE", "600002"), ("HKEX", "06002")})
        self.assertEqual({e["security_id"] for e in timeline["events"]},
                         {s["security_id"] for s in timeline["securities"]})
        frozen = self.app.monthly.freeze(self.analyst, PERIOD)
        # 归并后主体口径不因为重复建档虚增：仍是 2 家
        self.assertEqual(line_map(frozen)[("listed_count", "")], 2)
        # 被归并身份对普通查询等同不存在
        with self.assertRaises(NotFoundError):
            self.app.companies.get(merged["absorbed_company_id"])


if __name__ == "__main__":
    unittest.main()
