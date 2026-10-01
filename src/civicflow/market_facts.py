"""市场事实追加表：上市退市事件、股本变化、收盘估值、募资。

事实行按 (batch_id, source_record_no) 唯一，保证同一批次重送不会再次累计；
跨批次出现相同自然键时：内容一致视为重复报送（不新增），内容不一致则追加为
更正版本，月末计算按 ingested_at 取最新一条。编号相同而数据不同的冲突由来源层隔离。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .errors import NotFoundError, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json, digest_json
from .ledger import to_minor
from .timeutil import Clock, canonical_date, canonical_instant

EVENT_KINDS = ("listing", "delisting")


@dataclass(frozen=True)
class MarketFacts:
    clock: Clock

    # -- 上市 / 退市事件 ------------------------------------------------------

    def record_event(self, connection: sqlite3.Connection, *, security_id: str, company_id: str, market: str,
                     kind: str, event_at: str, detail: dict | None, batch_id: str, record_no: str,
                     actor: str) -> dict:
        if kind not in EVENT_KINDS:
            raise ValidationError("事件类型必须是 listing 或 delisting")
        event_at = canonical_instant(event_at)
        detail = detail or {}
        prior = connection.execute(
            "SELECT * FROM market_events WHERE security_id=? AND kind=? AND event_at=?"
            " ORDER BY ingested_at DESC", (security_id, kind, event_at)).fetchone()
        if prior and json.loads(prior["detail_json"]) == detail:
            return {"fact_kind": "event", "fact_id": prior["event_id"], "duplicate": True}
        event_id = new_id("event")
        connection.execute(
            "INSERT INTO market_events(event_id,security_id,company_id,market,kind,event_at,detail_json,"
            "batch_id,source_record_no,ingested_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, security_id, company_id, market, kind, event_at,
             canonical_json(detail), batch_id, record_no, self.clock.now(), actor))
        return {"fact_kind": "event", "fact_id": event_id, "duplicate": False}

    # -- 股本变化 -------------------------------------------------------------

    def record_capital(self, connection: sqlite3.Connection, *, company_id: str, security_id: str | None,
                       change_type: str, shares_after: int, effective_at: str,
                       batch_id: str, record_no: str, actor: str) -> dict:
        if not isinstance(shares_after, int) or isinstance(shares_after, bool) or shares_after < 0:
            raise ValidationError("变化后股本必须是非负整数")
        effective_at = canonical_instant(effective_at)
        change_type = require_type(change_type)
        prior = connection.execute(
            "SELECT * FROM capital_changes WHERE company_id=? AND change_type=? AND effective_at=?"
            " AND (security_id IS ? OR security_id=?) ORDER BY ingested_at DESC",
            (company_id, change_type, effective_at, security_id, security_id)).fetchone()
        if prior and prior["shares_after"] == shares_after:
            return {"fact_kind": "capital", "fact_id": prior["change_id"], "duplicate": True}
        change_id = new_id("capital")
        connection.execute(
            "INSERT INTO capital_changes(change_id,company_id,security_id,change_type,shares_after,effective_at,"
            "batch_id,source_record_no,ingested_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (change_id, company_id, security_id, change_type, shares_after, effective_at,
             batch_id, record_no, self.clock.now(), actor))
        return {"fact_kind": "capital", "fact_id": change_id, "duplicate": False}

    def shares_as_of(self, connection: sqlite3.Connection, company_id: str, as_of: str) -> int:
        row = connection.execute(
            "SELECT shares_after FROM capital_changes WHERE company_id=? AND effective_at<=?"
            " ORDER BY effective_at DESC,ingested_at DESC LIMIT 1",
            (company_id, canonical_instant(as_of))).fetchone()
        if not row:
            raise NotFoundError("公司缺少该时点的股本记录")
        return row["shares_after"]

    # -- 收盘估值 -------------------------------------------------------------

    def record_valuation(self, connection: sqlite3.Connection, *, company_id: str, security_id: str,
                         trade_date: str, close_price: str, shares: int, currency: str,
                         fx_to_cny: str = "1", batch_id: str = "", record_no: str = "", actor: str) -> dict:
        day = canonical_date(trade_date)
        price = _decimal(close_price, "收盘价")
        if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
            raise ValidationError("计价股本必须是正整数")
        fx = _decimal(fx_to_cny, "汇率")
        value_cny = to_minor((price * shares * fx).quantize(Decimal("0.01")))
        prior = connection.execute(
            "SELECT * FROM valuations WHERE security_id=? AND trade_date=? ORDER BY ingested_at DESC",
            (security_id, day)).fetchone()
        if prior and (prior["close_price"] == str(price) and prior["shares"] == shares
                      and prior["fx_to_cny"] == str(fx)):
            return {"fact_kind": "valuation", "fact_id": prior["valuation_id"], "duplicate": True,
                    "market_value_cny": prior["market_value_cny"]}
        valuation_id = new_id("val")
        connection.execute(
            "INSERT INTO valuations(valuation_id,company_id,security_id,trade_date,close_price,shares,"
            "market_value_native,currency,fx_to_cny,market_value_cny,batch_id,source_record_no,ingested_at,actor_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (valuation_id, company_id, security_id, day, str(price), shares,
             str(price * shares), currency, str(fx), value_cny, batch_id, record_no, self.clock.now(), actor))
        return {"fact_kind": "valuation", "fact_id": valuation_id, "duplicate": False,
                "market_value_cny": value_cny}

    # -- 募资 -----------------------------------------------------------------

    def record_raising(self, connection: sqlite3.Connection, *, company_id: str, security_id: str, kind: str,
                       gross_cny_minor: int, raised_at: str, batch_id: str, record_no: str, actor: str) -> dict:
        if not isinstance(gross_cny_minor, int) or gross_cny_minor < 0:
            raise ValidationError("募资金额（分）必须是非负整数")
        kind = require_type(kind)
        raised_at = canonical_date(raised_at)
        prior = connection.execute(
            "SELECT * FROM raisings WHERE company_id=? AND security_id=? AND kind=? AND raised_at=?"
            " ORDER BY ingested_at DESC", (company_id, security_id, kind, raised_at)).fetchone()
        if prior and prior["gross_cny_minor"] == gross_cny_minor:
            return {"fact_kind": "raising", "fact_id": prior["raising_id"], "duplicate": True}
        raising_id = new_id("raise")
        connection.execute(
            "INSERT INTO raisings(raising_id,company_id,security_id,kind,gross_cny_minor,raised_at,batch_id,"
            "source_record_no,ingested_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (raising_id, company_id, security_id, kind, gross_cny_minor, raised_at,
             batch_id, record_no, self.clock.now(), actor))
        return {"fact_kind": "raising", "fact_id": raising_id, "duplicate": False}


def require_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("类型不能为空")
    return value.strip()


def _decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{label}格式错误") from exc
    if not number.is_finite() or number <= 0:
        raise ValidationError(f"{label}必须是有限正数")
    return number
