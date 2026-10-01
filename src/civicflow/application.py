"""应用装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .companies import CompanyRegistry
from .database import Database
from .idempotency import IdempotencyStore
from .inbox import Inbox
from .ingestion import IngestionDispatcher
from .jobs import JobQueue
from .ledger import Ledger
from .market_facts import MarketFacts
from .monthly import MonthlyReportService
from .outbox import Outbox
from .repository import EntityRepository
from .reservations import ReservationBook
from .sources import SourceLedger
from .taxonomy import TaxonomyBook
from .timeutil import Clock


@dataclass(frozen=True)
class CivicFlow:
    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    companies: CompanyRegistry
    taxonomy: TaxonomyBook
    facts: MarketFacts
    sources: SourceLedger
    dispatcher: IngestionDispatcher
    monthly: MonthlyReportService

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        companies = CompanyRegistry(database, clock)
        taxonomy = TaxonomyBook(database, clock)
        facts = MarketFacts(clock)
        sources = SourceLedger(database, clock)
        dispatcher = IngestionDispatcher(companies, facts, taxonomy)
        monthly = MonthlyReportService(database, clock, companies, taxonomy)
        return cls(database, clock, repository, Inbox(database, clock), Outbox(database, clock),
                   Ledger(database, clock), ReservationBook(database), JobQueue(database, clock),
                   companies, taxonomy, facts, sources, dispatcher, monthly)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
            quarantine_count = connection.execute(
                "SELECT COUNT(*) AS n FROM source_quarantine WHERE resolution='pending'").fetchone()["n"]
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count,
                "pending_quarantine": quarantine_count}
