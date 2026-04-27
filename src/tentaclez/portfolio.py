from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .strategy import OpenLot


class Base(DeclarativeBase):
    pass


class Lot(Base):
    """An open buy lot with its individual sell target."""

    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Trade(Base):
    """Closed trade — realized P&L row."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pnl_usd: Mapped[float] = mapped_column(Float)


class Budget(Base):
    """Per-ticker cash budget. Each ticker has its own sandbox so a deep
    drawdown on one symbol can't drain the cash earmarked for another.
    """

    __tablename__ = "budgets"
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SignalLog(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(1024))


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float
    avg_price: float
    cost_basis: float


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class PortfolioStore:
    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, echo=False, future=True)
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def init(self, ensure_symbols: list[str] | None = None) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if ensure_symbols:
            await self._ensure_budgets(ensure_symbols)

    async def _ensure_budgets(self, symbols: list[str]) -> None:
        """Create a $0 budget row for any ticker that doesn't have one yet."""
        async with self._session() as s:
            for symbol in symbols:
                sym = symbol.upper()
                existing = (
                    await s.execute(select(Budget).where(Budget.symbol == sym))
                ).scalar_one_or_none()
                if existing is None:
                    s.add(Budget(symbol=sym, amount_usd=0.0, updated_at=_utcnow()))
            await s.commit()

    # ---- budgets ----

    async def get_budget(self, symbol: str) -> float:
        async with self._session() as s:
            row = (
                await s.execute(select(Budget).where(Budget.symbol == symbol.upper()))
            ).scalar_one_or_none()
            return float(row.amount_usd) if row else 0.0

    async def set_budget(self, symbol: str, value: float) -> float:
        async with self._session() as s:
            sym = symbol.upper()
            row = (
                await s.execute(select(Budget).where(Budget.symbol == sym))
            ).scalar_one_or_none()
            if row is None:
                row = Budget(symbol=sym, amount_usd=float(value), updated_at=_utcnow())
                s.add(row)
            else:
                row.amount_usd = float(value)
                row.updated_at = _utcnow()
            await s.commit()
            return float(row.amount_usd)

    async def adjust_budget(self, symbol: str, delta: float) -> float:
        async with self._session() as s:
            sym = symbol.upper()
            row = (
                await s.execute(select(Budget).where(Budget.symbol == sym))
            ).scalar_one_or_none()
            if row is None:
                row = Budget(symbol=sym, amount_usd=float(delta), updated_at=_utcnow())
                s.add(row)
            else:
                row.amount_usd = float(row.amount_usd) + float(delta)
                row.updated_at = _utcnow()
            await s.commit()
            return float(row.amount_usd)

    async def all_budgets(self) -> dict[str, float]:
        async with self._session() as s:
            rows = (await s.execute(select(Budget))).scalars().all()
            return {r.symbol: float(r.amount_usd) for r in rows}

    async def total_cash(self) -> float:
        budgets = await self.all_budgets()
        return sum(budgets.values())

    # ---- lots / trades ----

    async def add_lot(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        target_price: float,
        note: str | None = None,
        at: datetime | None = None,
    ) -> Lot:
        async with self._session() as s:
            lot = Lot(
                symbol=symbol.upper(),
                qty=qty,
                entry_price=entry_price,
                target_price=target_price,
                entry_at=at or _utcnow(),
                note=note,
            )
            s.add(lot)
            await s.commit()
            await s.refresh(lot)
            return lot

    async def open_lots(self, symbol: str | None = None) -> list[Lot]:
        async with self._session() as s:
            stmt = select(Lot).order_by(Lot.entry_at, Lot.id)
            if symbol:
                stmt = stmt.where(Lot.symbol == symbol.upper())
            return list((await s.execute(stmt)).scalars().all())

    async def open_lots_for_strategy(self, symbol: str) -> list[OpenLot]:
        rows = await self.open_lots(symbol)
        return [
            OpenLot(id=r.id, qty=r.qty, entry_price=r.entry_price, target_price=r.target_price)
            for r in rows
        ]

    async def last_buy_price(self, symbol: str) -> float | None:
        async with self._session() as s:
            stmt = (
                select(Lot.entry_price)
                .where(Lot.symbol == symbol.upper())
                .order_by(Lot.entry_at.desc(), Lot.id.desc())
                .limit(1)
            )
            row = (await s.execute(stmt)).scalar_one_or_none()
            return float(row) if row is not None else None

    async def positions(self) -> list[Position]:
        rows = await self.open_lots()
        agg: dict[str, list[Lot]] = {}
        for lot in rows:
            agg.setdefault(lot.symbol, []).append(lot)
        out: list[Position] = []
        for symbol, lots in agg.items():
            qty = sum(lot.qty for lot in lots)
            cost = sum(lot.qty * lot.entry_price for lot in lots)
            if qty == 0:
                continue
            out.append(
                Position(
                    symbol=symbol,
                    qty=qty,
                    avg_price=cost / qty if qty else 0,
                    cost_basis=cost,
                )
            )
        return out

    async def close_position(
        self, symbol: str, qty: float, exit_price: float, at: datetime | None = None
    ) -> list[Trade]:
        """FIFO match against open lots; record trades; bump free cash by proceeds.

        Returns the trade rows that were created.
        """
        at = at or _utcnow()
        symbol = symbol.upper()
        closed: list[Trade] = []
        remaining = qty

        async with self._session() as s:
            lots = (
                (
                    await s.execute(
                        select(Lot).where(Lot.symbol == symbol).order_by(Lot.entry_at, Lot.id)
                    )
                )
                .scalars()
                .all()
            )
            for lot in lots:
                if remaining <= 0:
                    break
                take = min(lot.qty, remaining)
                pnl = (exit_price - lot.entry_price) * take
                trade = Trade(
                    symbol=symbol,
                    qty=take,
                    entry_price=lot.entry_price,
                    exit_price=exit_price,
                    entry_at=lot.entry_at,
                    exit_at=at,
                    pnl_usd=pnl,
                )
                s.add(trade)
                closed.append(trade)
                lot.qty -= take
                remaining -= take
                if lot.qty <= 0:
                    await s.delete(lot)
            if remaining > 0:
                raise ValueError(
                    f"trying to sell {qty} of {symbol} but only {qty - remaining} held"
                )
            await s.commit()
            for t in closed:
                await s.refresh(t)
        return closed

    async def recent_trades(self, limit: int = 10) -> list[Trade]:
        async with self._session() as s:
            stmt = select(Trade).order_by(Trade.exit_at.desc()).limit(limit)
            return list((await s.execute(stmt)).scalars().all())

    # ---- signals log ----

    async def log_signal(
        self,
        symbol: str,
        action: str,
        price: float,
        reason: str,
        at: datetime | None = None,
    ) -> None:
        async with self._session() as s:
            s.add(
                SignalLog(
                    at=at or _utcnow(),
                    symbol=symbol.upper(),
                    action=action,
                    price=price,
                    reason=reason[:1024],
                )
            )
            await s.commit()
