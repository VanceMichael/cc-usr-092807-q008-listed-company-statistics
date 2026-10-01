"""公司统一身份、跨市场证券与主体归并。

公司以统一社会信用代码作为自然键，名称等属性按生效时间保留全部版本；
同一主体在不同市场发行的证券各自保留事实行，但都归并到同一 company_id。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id, require_safe
from .timeutil import Clock, canonical_instant

UNIFIED = "unified"


@dataclass(frozen=True)
class CompanyRegistry:
    database: Database
    clock: Clock

    # -- 写入（由录入事务在同一连接内调用） ---------------------------------

    def ensure_company(self, connection: sqlite3.Connection, *, unified_code: str, name: str,
                       public: bool = False, registered_address: str = "",
                       effective_at: str | None = None,
                       batch_id: str = "", record_no: str = "", actor: str) -> str:
        """按统一代码找到主体或创建主体；名称或注册地址变化只追加属性版本。"""
        code = require_safe(unified_code, "统一社会信用代码")
        name = _require_text(name, "公司名称")
        registered_address = registered_address.strip()
        effective_at = canonical_instant(effective_at) if effective_at else self.clock.now()
        row = connection.execute(
            "SELECT company_id FROM company_identifiers WHERE id_type=? AND code=?", (UNIFIED, code)).fetchone()
        if row:
            company_id = row["company_id"]
            latest = connection.execute(
                "SELECT name,registered_address FROM company_attributes WHERE company_id=? ORDER BY version DESC LIMIT 1",
                (company_id,)).fetchone()
            if latest is None or latest["name"] != name or latest["registered_address"] != registered_address:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM company_attributes WHERE company_id=?",
                    (company_id,)).fetchone()["v"]
                connection.execute(
                    "INSERT INTO company_attributes(company_id,version,name,registered_address,effective_at,"
                    "batch_id,source_record_no,ingested_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (company_id, version, name, registered_address, effective_at,
                     batch_id, record_no, self.clock.now(), actor))
            if public:
                connection.execute("UPDATE companies SET public=1 WHERE company_id=?", (company_id,))
            return company_id
        company_id = new_id("company")
        now = self.clock.now()
        connection.execute(
            "INSERT INTO companies(company_id,canonical_name,public,created_at,created_by) VALUES(?,?,?,?,?)",
            (company_id, name, 1 if public else 0, now, actor))
        connection.execute(
            "INSERT INTO company_identifiers(company_id,id_type,code) VALUES(?,?,?)", (company_id, UNIFIED, code))
        connection.execute(
            "INSERT INTO company_attributes(company_id,version,name,registered_address,effective_at,"
            "batch_id,source_record_no,ingested_at,actor_id) VALUES(?,1,?,?,?,?,?,?,?)",
            (company_id, name, registered_address, effective_at, batch_id, record_no, now, actor))
        return company_id

    def resolve(self, connection: sqlite3.Connection, unified_code: str) -> str | None:
        row = connection.execute(
            "SELECT company_id FROM company_identifiers WHERE id_type=? AND code=?",
            (UNIFIED, require_safe(unified_code, "统一社会信用代码"))).fetchone()
        return row["company_id"] if row else None

    def mark_public(self, connection: sqlite3.Connection, company_id: str) -> None:
        connection.execute("UPDATE companies SET public=1 WHERE company_id=?", (company_id,))

    def register_security(self, connection: sqlite3.Connection, *, company_id: str, market: str,
                          ticker: str, currency: str = "CNY", batch_id: str = "", actor: str) -> str:
        market = require_safe(market, "市场"); ticker = require_safe(ticker, "证券代码")
        row = connection.execute("SELECT security_id,company_id FROM securities WHERE market=? AND ticker=?",
                                 (market, ticker)).fetchone()
        if row:
            if row["company_id"] != company_id:
                raise ConflictError(f"证券 {market}:{ticker} 已归属于其他主体，需先做主体归并")
            return row["security_id"]
        security_id = new_id("security")
        connection.execute(
            "INSERT INTO securities(security_id,company_id,market,ticker,currency,batch_id,created_at,created_by)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (security_id, company_id, market, ticker, currency, batch_id, self.clock.now(), actor))
        return security_id

    def merge_companies(self, connection: sqlite3.Connection, *, survivor_code: str, absorbed_code: str,
                        actor: str, reason: str) -> dict:
        """把两个已存在主体归并为一个；被归并方各市场证券事实保留，仅改挂主体。

        若双方在同一市场登记了相同代码，事实改挂到存续方证券，重复证券行删除。
        """
        if survivor_code == absorbed_code:
            raise ValidationError("归并双方不能相同")
        survivor = self.resolve(connection, survivor_code)
        absorbed = self.resolve(connection, absorbed_code)
        if not survivor or not absorbed:
            raise NotFoundError("归并双方都必须已存在")
        duplicate_securities = connection.execute(
            "SELECT d.security_id AS dup_id, k.security_id AS keep_id FROM securities d"
            " JOIN securities k ON k.market=d.market AND k.ticker=d.ticker"
            " WHERE d.company_id=? AND k.company_id=?", (absorbed, survivor)).fetchall()
        for row in duplicate_securities:
            for table in ("market_events", "capital_changes", "valuations", "raisings"):
                connection.execute(f"UPDATE {table} SET security_id=?,company_id=? WHERE security_id=?",
                                   (row["keep_id"], survivor, row["dup_id"]))
            connection.execute("DELETE FROM securities WHERE security_id=?", (row["dup_id"],))
        connection.execute("UPDATE securities SET company_id=? WHERE company_id=?", (survivor, absorbed))
        for table in ("market_events", "capital_changes", "valuations", "raisings", "classifications"):
            connection.execute(f"UPDATE {table} SET company_id=? WHERE company_id=?", (survivor, absorbed))
        connection.execute("DELETE FROM company_identifiers WHERE company_id=? AND id_type=?", (absorbed, UNIFIED))
        connection.execute(
            "INSERT INTO company_identifiers(company_id,id_type,code) VALUES(?,?,?)",
            (survivor, "merged_from", absorbed_code))
        connection.execute("UPDATE companies SET public=0 WHERE company_id=?", (absorbed,))
        connection.execute("UPDATE companies SET canonical_name=(SELECT name FROM company_attributes WHERE company_id=?"
                           " ORDER BY version DESC LIMIT 1) WHERE company_id=?", (survivor, survivor))
        return {"survivor_company_id": survivor, "absorbed_company_id": absorbed, "reason": reason}

    # -- 读取 -----------------------------------------------------------------

    def name_as_of(self, connection: sqlite3.Connection, company_id: str, as_of: str) -> str:
        row = connection.execute(
            "SELECT name FROM company_attributes WHERE company_id=? AND effective_at<=?"
            " ORDER BY effective_at DESC,version DESC LIMIT 1", (company_id, canonical_instant(as_of))).fetchone()
        if row:
            return row["name"]
        row = connection.execute("SELECT canonical_name FROM companies WHERE company_id=?", (company_id,)).fetchone()
        if not row:
            raise NotFoundError("公司不存在")
        return row["canonical_name"]

    def get(self, company_id: str, *, include_private: bool = False) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM companies WHERE company_id=?", (company_id,)).fetchone()
            if not row:
                raise NotFoundError("公司不存在")
            if not row["public"] and not include_private:
                # 未公开事项与不存在事项返回同一错误，避免被推断存在性
                raise NotFoundError("公司不存在")
            attrs = connection.execute(
                "SELECT version,name,effective_at,batch_id,source_record_no FROM company_attributes"
                " WHERE company_id=? ORDER BY version", (company_id,)).fetchall()
            securities = connection.execute(
                "SELECT security_id,market,ticker,currency FROM securities WHERE company_id=? ORDER BY market,ticker",
                (company_id,)).fetchall()
            return {
                "company_id": row["company_id"], "canonical_name": row["canonical_name"],
                "public": bool(row["public"]),
                "attributes": [dict(item) for item in attrs],
                "securities": [dict(item) for item in securities],
            }

    def list_public(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT company_id,canonical_name FROM companies WHERE public=1 ORDER BY canonical_name").fetchall()
            return [dict(row) for row in rows]


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()
