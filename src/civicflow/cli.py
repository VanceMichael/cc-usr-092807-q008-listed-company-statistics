"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .errors import NotFoundError
from .security import AccessContext

PERIOD = "2026-08"


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _seed_taxonomy(app: CivicFlow, actor: str = "taxonomy-admin") -> None:
    app.taxonomy.create_version(axis="industry", label="证监会行业分类 2026-08",
                                mapping={"C": "制造业", "J": "金融业"}, actor=actor)
    app.taxonomy.create_version(axis="control_nature", label="控股性质分类 2026-08",
                                mapping={"private": "民营控股", "state": "国有控股"}, actor=actor)
    app.taxonomy.create_version(axis="province", label="省域行政区划 2026-08",
                                mapping={"11": "北京市", "31": "上海市", "33": "浙江省"}, actor=actor)
    rows = app.database.connect().execute(
        "SELECT version_id FROM taxonomy_versions ORDER BY axis").fetchall()
    for row in rows:
        app.taxonomy.confirm_version(row["version_id"], actor=actor)


def _registry_records() -> list[dict]:
    return [
        {"record_no": "co-a-1", "record_type": "company", "payload": {
            "record_type": "company", "unified_code": "91330000MADEMO001A", "name": "示例智造股份有限公司",
            "public": True, "registered_address": "北京市海淀区中关村大街1号", "effective_at": "2026-08-01T00:00:00+08:00"}},
        {"record_no": "co-a-2", "record_type": "company", "payload": {
            "record_type": "company", "unified_code": "91330000MADEMO001A", "name": "示例智造股份有限公司",
            "public": True, "registered_address": "浙江省杭州市西湖区文三路100号",
            "effective_at": "2026-08-30T00:00:00+08:00"}},
        {"record_no": "co-b-1", "record_type": "company", "payload": {
            "record_type": "company", "unified_code": "91310000MADEMO001B", "name": "华光金融控股股份有限公司",
            "public": True, "registered_address": "上海市浦东新区世纪大道10号",
            "effective_at": "2026-01-01T00:00:00+08:00"}},
        {"record_no": "co-c-1", "record_type": "company", "payload": {
            "record_type": "company", "unified_code": "91440000MADEMO001C", "name": "尚未公开股份有限公司",
            "public": False, "registered_address": "广东省深圳市南山区", "effective_at": "2026-08-15T00:00:00+08:00"}},
        {"record_no": "cl-a-ind", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91330000MADEMO001A", "axis": "industry",
            "code": "C", "effective_from": "2026-08-01", "status": "confirmed"}},
        {"record_no": "cl-a-ctl", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91330000MADEMO001A", "axis": "control_nature",
            "code": "private", "effective_from": "2026-08-01", "status": "confirmed"}},
        {"record_no": "cl-a-pr1", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91330000MADEMO001A", "axis": "province",
            "code": "11", "effective_from": "2026-08-01", "status": "confirmed"}},
        {"record_no": "cl-a-pr2", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91330000MADEMO001A", "axis": "province",
            "code": "33", "effective_from": "2026-08-30", "status": "confirmed"}},
        {"record_no": "cl-b-ind", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91310000MADEMO001B", "axis": "industry",
            "code": "J", "effective_from": "2026-01-01", "status": "confirmed"}},
        {"record_no": "cl-b-ctl", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91310000MADEMO001B", "axis": "control_nature",
            "code": "state", "effective_from": "2026-01-01", "status": "confirmed"}},
        {"record_no": "cl-b-pr", "record_type": "classification", "payload": {
            "record_type": "classification", "unified_code": "91310000MADEMO001B", "axis": "province",
            "code": "31", "effective_from": "2026-01-01", "status": "confirmed"}},
    ]


def _cn_records() -> list[dict]:
    return [
        {"record_no": "sec-a", "record_type": "security", "payload": {
            "record_type": "security", "unified_code": "91330000MADEMO001A",
            "market": "SSE", "ticker": "688001", "currency": "CNY"}},
        {"record_no": "sec-b", "record_type": "security", "payload": {
            "record_type": "security", "unified_code": "91310000MADEMO001B",
            "market": "SSE", "ticker": "600002", "currency": "CNY"}},
        {"record_no": "evt-a", "record_type": "event", "payload": {
            "record_type": "event", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "kind": "listing", "event_at": "2026-08-10T09:30:00+08:00", "detail": {"board": "科创板"}}},
        {"record_no": "evt-b", "record_type": "event", "payload": {
            "record_type": "event", "unified_code": "91310000MADEMO001B", "market": "SSE", "ticker": "600002",
            "kind": "listing", "event_at": "2026-01-15T09:30:00+08:00"}},
        {"record_no": "cap-a", "record_type": "capital", "payload": {
            "record_type": "capital", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "change_type": "ipo_total", "shares_after": 100_000_000,
            "effective_at": "2026-08-10T09:30:00+08:00"}},
        {"record_no": "val-a", "record_type": "valuation", "payload": {
            "record_type": "valuation", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "trade_date": "2026-08-31", "close_price": "10.00", "shares": 100_000_000, "currency": "CNY"}},
        {"record_no": "val-b", "record_type": "valuation", "payload": {
            "record_type": "valuation", "unified_code": "91310000MADEMO001B", "market": "SSE", "ticker": "600002",
            "trade_date": "2026-08-31", "close_price": "5.00", "shares": 500_000_000, "currency": "CNY"}},
        {"record_no": "raise-a", "record_type": "raising", "payload": {
            "record_type": "raising", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "kind": "IPO", "gross_cny_minor": 9_180_000_000_00, "raised_at": "2026-08-10"}},
    ]


def _hk_records() -> list[dict]:
    return [
        {"record_no": "sec-a-h", "record_type": "security", "payload": {
            "record_type": "security", "unified_code": "91330000MADEMO001A",
            "market": "HKEX", "ticker": "08001", "currency": "HKD"}},
        {"record_no": "evt-a-h", "record_type": "event", "payload": {
            "record_type": "event", "unified_code": "91330000MADEMO001A", "market": "HKEX", "ticker": "08001",
            "kind": "listing", "event_at": "2026-08-25T09:30:00+08:00"}},
        {"record_no": "val-a-h", "record_type": "valuation", "payload": {
            "record_type": "valuation", "unified_code": "91330000MADEMO001A", "market": "HKEX", "ticker": "08001",
            "trade_date": "2026-08-29", "close_price": "8.00", "shares": 20_000_000,
            "currency": "HKD", "fx_to_cny": "0.92"}},
    ]


def _capital_fix_records() -> list[dict]:
    return [
        {"record_no": "cap-a-fix", "record_type": "capital", "payload": {
            "record_type": "capital", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "change_type": "share_correction", "shares_after": 120_000_000,
            "effective_at": "2026-08-31T16:00:00+08:00"}},
        {"record_no": "val-a-fix", "record_type": "valuation", "payload": {
            "record_type": "valuation", "unified_code": "91330000MADEMO001A", "market": "SSE", "ticker": "688001",
            "trade_date": "2026-08-31", "close_price": "10.00", "shares": 120_000_000, "currency": "CNY"}},
    ]


def demo(path: str) -> dict:
    db = Path(path)
    # 阶段一：截止前完成登记、报送、冻结与发布（时钟固定在 9 月 5 日）
    app = CivicFlow.open(db, fixed_now="2026-09-05T10:00:00Z")
    _seed_taxonomy(app)
    app.sources.register_cycle(period=PERIOD, cutoff_at="2026-09-06T00:00:00Z",
                               expected_sources=["registry", "exchange-cn", "exchange-hk"],
                               actor="association-admin")
    reg = app.sources.ingest_batch(source="registry", batch_key="reg-202608", period_label=PERIOD,
                                   records=_registry_records(), note="市场主体与分类登记",
                                   actor="entry-clerk", dispatcher=app.dispatcher)
    cn = app.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608", period_label=PERIOD,
                                  records=_cn_records(), note="境内交易所八月数据",
                                  actor="entry-clerk", dispatcher=app.dispatcher)
    hk = app.sources.ingest_batch(source="exchange-hk", batch_key="hk-202608", period_label=PERIOD,
                                  records=_hk_records(), note="港交所八月数据",
                                  actor="entry-clerk", dispatcher=app.dispatcher)
    # 同批原样重送：只回放，不再次累计
    replay = app.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608", period_label=PERIOD,
                                      records=_cn_records(), note="境内交易所八月数据",
                                      actor="entry-clerk", dispatcher=app.dispatcher)
    analyst = AccessContext("analyst-li", permissions=frozenset({"write:monthly", "read:monthly"}))
    frozen = app.monthly.freeze(analyst, PERIOD, reason="八月月报首次冻结")
    publisher = AccessContext("publisher-wang", permissions=frozenset({"publish:monthly", "read:monthly"}))
    app.monthly.publish(publisher, frozen["report_id"])
    original = app.monthly.list_editions(publisher, PERIOD)[0]

    # 阶段二：一周后（9 月 12 日）交易所补来股本更正；另演示编号冲突隔离
    later = CivicFlow.open(db, fixed_now="2026-09-12T10:00:00Z")
    fix = later.sources.ingest_batch(source="exchange-cn", batch_key="cn-202608-capfix", period_label=PERIOD,
                                     records=_capital_fix_records(), note="八月股本更正（晚到一周）",
                                     actor="entry-clerk", dispatcher=later.dispatcher)
    altered = _hk_records()
    altered[2]["payload"]["close_price"] = "9.99"  # 同批次同编号但数据不同
    quarantined = later.sources.ingest_batch(source="exchange-hk", batch_key="hk-202608", period_label=PERIOD,
                                             records=altered, note="港交所八月数据",
                                             actor="entry-clerk", dispatcher=later.dispatcher)
    errata_frozen = later.monthly.issue_errata(analyst, PERIOD, note="交易所补正八月总股本，制造业市值相应更正")
    later.monthly.publish(publisher, errata_frozen["report_id"])
    errata = later.monthly.list_editions(publisher, PERIOD)[1]

    reader = AccessContext("reader-zhao", permissions=frozenset({"read:monthly"}))
    cap_explain = later.monthly.explain(reader, PERIOD, "manufacturing_market_cap")
    ratio_explain = later.monthly.explain(reader, PERIOD, "control_share", "private")
    try:
        later.monthly.company_timeline(reader, "91440000MADEMO001C")
        private_probe = "可见（异常）"
    except NotFoundError:
        private_probe = "未公开主体与不存在主体返回一致，普通查询无法推断"

    return {
        "ingestion": {"registry": reg, "exchange_cn": cn, "exchange_hk": hk, "replay": replay,
                      "capital_fix": fix, "altered_resend": quarantined},
        "original_report": original,
        "errata_report": errata,
        "drilldown_manufacturing": {
            "value": cap_explain["value"], "previous_value": cap_explain["previous_value"],
            "reason": cap_explain["reason"],
            "supporting_facts": cap_explain["supporting_facts"],
            "watermark_batches": [b["batch_key"] for b in cap_explain["watermark"]["batches"]],
            "trigger_batches": [b["batch_key"] for b in cap_explain["trigger_batches"]],
            "correction_chain": [c["kind"] for c in cap_explain["correction_chain"]]},
        "drilldown_private_share": {"value": ratio_explain["value"],
                                    "supporting_companies": [s["company_name"] for s in ratio_explain["supporting_facts"]]},
        "nonpublic_probe": private_probe,
        "verification": later.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    commands.add_parser("list-quarantine")
    p_block = commands.add_parser("blockers"); p_block.add_argument("--period", default=PERIOD)
    p_list = commands.add_parser("list-reports"); p_list.add_argument("--period", default=PERIOD)
    p_explain = commands.add_parser("explain")
    p_explain.add_argument("--period", default=PERIOD)
    p_explain.add_argument("--metric", required=True)
    p_explain.add_argument("--dimension", default="")
    p_explain.add_argument("--edition", type=int, default=None)
    p_company = commands.add_parser("company-timeline"); p_company.add_argument("unified_code")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo":
        emit(demo(args.db))
    elif args.command == "verify":
        emit(app.verify())
    elif args.command == "list-cases":
        emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "list-quarantine":
        emit(app.sources.list_quarantine())
    elif args.command == "blockers":
        ctx = AccessContext("cli", permissions=frozenset({"read:monthly"}))
        emit(app.monthly.blockers(ctx, args.period))
    elif args.command == "list-reports":
        ctx = AccessContext("cli", permissions=frozenset({"read:monthly"}))
        emit(app.monthly.list_editions(ctx, args.period))
    elif args.command == "explain":
        ctx = AccessContext("cli", permissions=frozenset({"read:monthly"}), reveal_sensitive=True)
        emit(app.monthly.explain(ctx, args.period, args.metric, args.dimension, edition=args.edition))
    elif args.command == "company-timeline":
        ctx = AccessContext("cli", permissions=frozenset({"read:monthly"}), reveal_sensitive=True)
        emit(app.monthly.company_timeline(ctx, args.unified_code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
