"""来源记录到领域事实的分发器。

支持记录类型：
- company    公司设立/名称或注册地址变更
- security   跨市场证券登记
- event      上市/退市事件
- capital    股本变化（更正可重发同一生效日，月末取最新版本）
- valuation  月末收盘估值
- raising    募资
- classification 行业/控股性质/省域归属（可 tentative）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .companies import CompanyRegistry
from .errors import ValidationError
from .market_facts import MarketFacts
from .taxonomy import TaxonomyBook


@dataclass(frozen=True)
class IngestionDispatcher:
    companies: CompanyRegistry
    facts: MarketFacts
    taxonomy: TaxonomyBook

    def apply_record(self, connection: sqlite3.Connection, payload: dict, *, batch_id: str,
                     record_no: str, actor: str) -> dict:
        kind = payload.get("record_type")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            raise ValidationError(f"未知记录类型: {kind}")
        return handler(connection, payload, batch_id=batch_id, record_no=str(record_no), actor=actor)

    def _on_company(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                    actor: str) -> dict:
        company_id = self.companies.ensure_company(
            connection, unified_code=p["unified_code"], name=p["name"],
            public=bool(p.get("public", False)),
            registered_address=str(p.get("registered_address", "")),
            effective_at=p.get("effective_at"), batch_id=batch_id, record_no=record_no, actor=actor)
        return {"fact_kind": "company", "fact_id": company_id, "duplicate": False}

    def _on_security(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                     actor: str) -> dict:
        company_id = self._require_company(connection, p["unified_code"])
        security_id = self.companies.register_security(
            connection, company_id=company_id, market=p["market"], ticker=p["ticker"],
            currency=str(p.get("currency", "CNY")), batch_id=batch_id, actor=actor)
        return {"fact_kind": "security", "fact_id": security_id, "duplicate": False}

    def _on_event(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                  actor: str) -> dict:
        company_id, security_id = self._resolve(connection, p)
        return self.facts.record_event(
            connection, security_id=security_id, company_id=company_id, market=p["market"],
            kind=p["kind"], event_at=p["event_at"], detail=p.get("detail"),
            batch_id=batch_id, record_no=record_no, actor=actor)

    def _on_capital(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                    actor: str) -> dict:
        company_id, security_id = self._resolve(connection, p, optional_security=True)
        return self.facts.record_capital(
            connection, company_id=company_id, security_id=security_id,
            change_type=p["change_type"], shares_after=int(p["shares_after"]),
            effective_at=p["effective_at"], batch_id=batch_id, record_no=record_no, actor=actor)

    def _on_valuation(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                      actor: str) -> dict:
        company_id, security_id = self._resolve(connection, p)
        return self.facts.record_valuation(
            connection, company_id=company_id, security_id=security_id, trade_date=p["trade_date"],
            close_price=str(p["close_price"]), shares=int(p["shares"]),
            currency=str(p.get("currency", "CNY")), fx_to_cny=str(p.get("fx_to_cny", "1")),
            batch_id=batch_id, record_no=record_no, actor=actor)

    def _on_raising(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                    actor: str) -> dict:
        company_id, security_id = self._resolve(connection, p)
        return self.facts.record_raising(
            connection, company_id=company_id, security_id=security_id, kind=p["kind"],
            gross_cny_minor=int(p["gross_cny_minor"]), raised_at=p["raised_at"],
            batch_id=batch_id, record_no=record_no, actor=actor)

    def _on_classification(self, connection: sqlite3.Connection, p: dict, *, batch_id: str, record_no: str,
                           actor: str) -> dict:
        company_id = self._require_company(connection, p["unified_code"])
        classification_id = self.taxonomy.assign(
            connection, company_id=company_id, axis=p["axis"], code=p["code"],
            effective_from=p["effective_from"], status=str(p.get("status", "tentative")),
            taxonomy_version_id=p.get("taxonomy_version_id"), batch_id=batch_id,
            record_no=record_no, actor=actor)
        return {"fact_kind": "classification", "fact_id": classification_id, "duplicate": False}

    # -- 辅助 -----------------------------------------------------------------

    def _require_company(self, connection: sqlite3.Connection, unified_code: str) -> str:
        company_id = self.companies.resolve(connection, unified_code)
        if not company_id:
            raise ValidationError(f"公司 {unified_code} 尚未登记")
        return company_id

    def _resolve(self, connection: sqlite3.Connection, p: dict, *, optional_security: bool = False):
        company_id = self._require_company(connection, p["unified_code"])
        if "market" in p and "ticker" in p:
            row = connection.execute("SELECT security_id FROM securities WHERE market=? AND ticker=?",
                                     (p["market"], p["ticker"])).fetchone()
            if not row:
                raise ValidationError(f"证券 {p['market']}:{p['ticker']} 尚未登记")
            return company_id, row["security_id"]
        if optional_security:
            return company_id, None
        raise ValidationError("记录缺少 market/ticker")
