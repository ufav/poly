"""ORM models of the normalized schema (docs/md1.md, section 12). One internal id per real-world entity; every
source's own id / name for it lives in source_entities (team / league / match mapping across Olimp, football-data,
Polymarket, ...). Predictions, bets and news tables come with their stages."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, SmallInteger,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB(none_as_null=True), list[Any]: JSONB(none_as_null=True)}


# ---------- reference ----------

class Sport(Base):
    __tablename__ = "sports"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)   # football, tennis, basketball, volleyball, hockey ...
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # cs2 / dota2 frozen


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)   # football_data, olimp_archive, olimpbet, polymarket ...
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))                # results / odds / venue / news / stats
    trust: Mapped[int] = mapped_column(SmallInteger, default=2)  # 1 official ... 4 social (section 6.3)


class League(Base):
    """A competition (league, cup, tournament level); one row per sport + country + name + gender + age group."""
    __tablename__ = "leagues"
    __table_args__ = (UniqueConstraint("sport_id", "country", "name", "gender", "age_group",
                                       postgresql_nulls_not_distinct=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    country: Mapped[str | None] = mapped_column(String(64))      # English name; None = international
    name: Mapped[str] = mapped_column(String(160))               # canonical English name
    level: Mapped[int | None] = mapped_column(SmallInteger)      # 1 = top division
    kind: Mapped[str] = mapped_column(String(16), default="league")  # league / cup / international / friendly
    gender: Mapped[str] = mapped_column(String(1), default="m")
    age_group: Mapped[str | None] = mapped_column(String(8))     # U19, U21 ...; None = senior


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("league_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    name: Mapped[str] = mapped_column(String(16))                # 2025/26 or 2025
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)


class Team(Base):
    """A match side: club, national team, tennis player or pair."""
    __tablename__ = "teams"
    __table_args__ = (Index("teams_sport_name", "sport_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    name: Mapped[str] = mapped_column(String(160))               # canonical English name
    country: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="club")  # club / national / player / pair
    gender: Mapped[str] = mapped_column(String(1), default="m")
    age_group: Mapped[str | None] = mapped_column(String(8))


class Player(Base):
    """Squad member (lineups, injuries, player stats - later stages)."""
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    name: Mapped[str] = mapped_column(String(160))
    country: Mapped[str | None] = mapped_column(String(64))
    birth_date: Mapped[dt.date | None] = mapped_column(Date)


# ---------- matches ----------

class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (Index("matches_sport_start", "sport_id", "start_time"),
                      Index("matches_home_start", "home_team_id", "start_time"),
                      Index("matches_away_start", "away_team_id", "start_time"),
                      Index("matches_league", "league_id"), Index("matches_season", "season_id"),
                      CheckConstraint("status IN ('scheduled','live','finished','cancelled','postponed',"
                                      "'abandoned','walkover')", name="matches_status"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"))
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"))
    stage: Mapped[str | None] = mapped_column(String(64))        # round / group / 1/8 final ...
    start_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    time_known: Mapped[bool] = mapped_column(Boolean, default=True)  # False = date only (start_time at 00:00 UTC)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    neutral: Mapped[bool] = mapped_column(Boolean, default=False)
    best_of: Mapped[int | None] = mapped_column(SmallInteger)    # tennis sets / volleyball / esports maps
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    home_score: Mapped[int | None] = mapped_column(SmallInteger)  # regular time (football) / sets / maps
    away_score: Mapped[int | None] = mapped_column(SmallInteger)
    periods: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))      # [[home, away], ...] halves / sets / quarters
    result_extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))  # extra time, penalties, retirement ...
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                    onupdate=func.now())


class MatchStatistic(Base):
    """Team stat of a match by period (0 = whole match): shots, shots_on_target, corners, yellow, red, fouls,
    offsides, possession, xg ... One row per source (sources may disagree)."""
    __tablename__ = "match_statistics"
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    stat: Mapped[str] = mapped_column(String(32), primary_key=True)
    period: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=0)
    home_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    away_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))


class MatchEvent(Base):
    """Goal / card / substitution ... with minute and side."""
    __tablename__ = "match_events"
    __table_args__ = (Index("match_events_match", "match_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    type: Mapped[str] = mapped_column(String(16))                # goal / own_goal / penalty_goal / yellow / red / sub
    minute: Mapped[int | None] = mapped_column(SmallInteger)
    side: Mapped[str | None] = mapped_column(String(1))          # h / a
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class MatchPlayer(Base):
    """Who played a match: lineup (starter / bench / did not play), minutes, the player's box score."""
    __tablename__ = "match_players"
    __table_args__ = (UniqueConstraint("match_id", "source_id", "player_id"),
                      Index("match_players_player", "player_id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    side: Mapped[str] = mapped_column(String(1))                 # h / a
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    starter: Mapped[bool | None] = mapped_column(Boolean)
    position: Mapped[str | None] = mapped_column(String(8))
    minutes: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    dnp_reason: Mapped[str | None] = mapped_column(String(64))   # did not play: coach's decision / injury ...
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class Injury(Base):
    """Injury report snapshots (append-only): a row when a player's status / comment changes."""
    __tablename__ = "injuries"
    __table_args__ = (Index("injuries_player_time", "player_id", "fetched_at"),
                      Index("injuries_team_time", "team_id", "fetched_at"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    status: Mapped[str] = mapped_column(String(32))              # Out / Day-To-Day / Questionable ... / Active
    comment: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


# ---------- markets and odds ----------

class Market(Base):
    """A betting market of a match: kind + period (0 = match; map / set / half number) + line."""
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("match_id", "kind", "period", "line", postgresql_nulls_not_distinct=True),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))                # 1x2 / moneyline / total / spread / btts ...
    period: Mapped[int] = mapped_column(SmallInteger, default=0)
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # total 2.5, handicap -1.5
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))  # venue ids (Polymarket condition / tokens ...)


class OddsQuote(Base):
    """One quote of one outcome at one moment (append-only history, section 6.2). Bookmakers: decimal odds;
    Polymarket: price (0-1) of the outcome token with bid / ask, odds = 1 / ask. snapshot marks the opening / closing
    lines of historical sources that give only those."""
    __tablename__ = "odds_history"
    __table_args__ = (Index("odds_market_outcome_time", "market_id", "outcome", "source_id", "taken_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    outcome: Mapped[str] = mapped_column(String(16))             # home / draw / away / over / under / yes / no
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    bookmaker: Mapped[str | None] = mapped_column(String(32))    # pinnacle / market_avg / bet365 ... (aggregators)
    odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    price: Mapped[Decimal | None] = mapped_column(Numeric(7, 5))
    bid: Mapped[Decimal | None] = mapped_column(Numeric(7, 5))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(7, 5))
    ask_size: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    snapshot: Mapped[str | None] = mapped_column(String(8))      # opening / closing / None = timed quote
    taken_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    provider_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(16))       # open / suspended / closed


# ---------- predictions and paper bets ----------

class Prediction(Base):
    """A probability of one outcome of one market made before the match (append-only: a new version when the
    probability or the price moved; never updated). approach: A model / B form / C combined / D external analyst."""
    __tablename__ = "predictions"
    __table_args__ = (Index("predictions_match", "match_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"))
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    outcome: Mapped[str] = mapped_column(String(16))
    approach: Mapped[str] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(String(32))               # tennis_cb_v1 / tennis_form_v1 / analyst ...
    p: Mapped[Decimal] = mapped_column(Numeric(6, 5))
    price: Mapped[Decimal | None] = mapped_column(Numeric(7, 5))  # the buy price (ask) at the time
    odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    ev: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    news: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class PaperBet(Base):
    """A recommendation as a paper bet: a single or a 2-leg parlay, flat stake, settled from the results."""
    __tablename__ = "paper_bets"
    __table_args__ = (Index("paper_bets_status", "status", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(16))                # value_single / fav_parlay / fav_single
    approach: Mapped[str] = mapped_column(String(8))
    legs: Mapped[list[Any]] = mapped_column(JSONB(none_as_null=True))  # [{prediction_id, match_id, outcome, odds, p}]
    odds: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    p: Mapped[Decimal] = mapped_column(Numeric(6, 5))            # (joint) probability by the approach
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(8), default="open")  # open / won / lost / void
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------- mapping ----------

class SourceEntity(Base):
    """A source's id / name of an internal entity: the table of correspondences (section 19). external_id = provider
    id, or a stable key built from names when the source has none (e.g. 'E0|Arsenal')."""
    __tablename__ = "source_entities"
    __table_args__ = (UniqueConstraint("source_id", "entity_type", "external_id"),
                      Index("source_entities_entity", "entity_type", "entity_id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    entity_type: Mapped[str] = mapped_column(String(16))         # league / team / player / match / market
    external_id: Mapped[str] = mapped_column(String(255))
    external_name: Mapped[str | None] = mapped_column(String(255))
    entity_id: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(16), default="provider")  # provider / exact / alias / fuzzy / manual
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawPayload(Base):
    """Raw provider response kept for re-normalization (instead of MinIO)."""
    __tablename__ = "raw_payloads"
    __table_args__ = (Index("raw_payloads_lookup", "source_id", "kind", "external_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    kind: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(255))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


SPORTS = [  # (id, code, name, active)
    (1, "football", "Football", True), (2, "tennis", "Tennis", True), (3, "basketball", "Basketball", True),
    (4, "volleyball", "Volleyball", True), (5, "hockey", "Ice hockey", True),
    (6, "table_tennis", "Table tennis", False), (7, "cs2", "Counter-Strike 2", False), (8, "dota2", "Dota 2", False),
]
SOURCES = [  # (id, code, name, kind, trust)
    (1, "football_data", "football-data.co.uk", "results", 2), (2, "olimp_archive", "Olimp results archive xlsx",
                                                                "results", 2),
    (3, "olimpbet", "Olimpbet results / line API", "results", 2), (4, "polymarket", "Polymarket", "venue", 1),
    (5, "tennis_data", "tennis-data.co.uk", "results", 2), (6, "sackmann", "Jeff Sackmann tennis_atp / tennis_wta",
                                                            "stats", 2),
    (7, "nhl", "NHL API", "results", 1), (8, "the_odds_api", "The Odds API", "odds", 2),
    (9, "espn", "ESPN site API (scoreboard / summary)", "results", 2),
]
