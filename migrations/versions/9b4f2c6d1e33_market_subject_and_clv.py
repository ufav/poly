"""market subject (player props, team totals) and the CLV of paper bets

Revision ID: 9b4f2c6d1e33
Revises: 7d1e5b3a9c22
Create Date: 2026-11-10 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '9b4f2c6d1e33'
down_revision: str | None = '7d1e5b3a9c22'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('markets', sa.Column('subject', sa.String(length=80), nullable=True))
    op.drop_constraint('markets_match_id_kind_period_line_key', 'markets', type_='unique')
    op.execute("ALTER TABLE markets ADD CONSTRAINT markets_match_kind_period_line_subject_key "
               "UNIQUE NULLS NOT DISTINCT (match_id, kind, period, line, subject)")
    op.add_column('paper_bets', sa.Column('clv', sa.Numeric(precision=7, scale=4), nullable=True))


def downgrade() -> None:
    op.drop_column('paper_bets', 'clv')
    op.drop_constraint('markets_match_kind_period_line_subject_key', 'markets', type_='unique')
    op.execute("ALTER TABLE markets ADD CONSTRAINT markets_match_id_kind_period_line_key "
               "UNIQUE NULLS NOT DISTINCT (match_id, kind, period, line)")
    op.drop_column('markets', 'subject')
