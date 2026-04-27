from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Lot(Base):
    """An open buy lot (FIFO matching when selling)."""

    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Trade(Base):
    """Closed trade (realized P&L)."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pnl_usd: Mapped[float] = mapped_column(Float)


class SignalLog(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(8))
    score: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    reasons: Mapped[str] = mapped_column(String(1024))


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

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def add_lot(
        self,
        symbol: str,
        qty: float,
        price: float,
        note: str | None = None,
        at: datetime | None = None,
    ) -> Lot:
        async with self._session() as s:
            lot = Lot(
                symbol=symbol.upper(),
                qty=qty,
                entry_price=price,
                entry_at=at or _utcnow(),
                note=note,
            )
            s.add(lot)
            await s.commit()
            await s.refresh(lot)
            return lot

    async def positions(self) -> list[Position]:
        async with self._session() as s:
            rows = (await s.execute(select(Lot))).scalars().all()
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
        """FIFO-match against open lots; record realized trades; return them."""
        at = at or _utcnow()
        symbol = symbol.upper()
        closed: list[Trade] = []
        remaining = qty

        async with self._session() as s:
            lots = (
                (
                    await s.execute(
                        select(Lot).where(Lot.symbol == symbol).order_by(Lot.entry_at)
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

    async def log_signal(
        self,
        symbol: str,
        action: str,
        score: float,
        price: float,
        reasons: list[str],
        at: datetime | None = None,
    ) -> None:
        async with self._session() as s:
            s.add(
                SignalLog(
                    at=at or _utcnow(),
                    symbol=symbol.upper(),
                    action=action,
                    score=score,
                    price=price,
                    reasons=" | ".join(reasons)[:1024],
                )
            )
            await s.commit()
