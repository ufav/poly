"""predictions and paper bets

Revision ID: 7d1e5b3a9c22
Revises: 4c2a9e1f7b10
Create Date: 2026-11-08 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '7d1e5b3a9c22'
down_revision: str | None = '4c2a9e1f7b10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'predictions',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('match_id', sa.Integer(), nullable=False),
        sa.Column('market_id', sa.BigInteger(), nullable=True),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('approach', sa.String(length=8), nullable=False),
        sa.Column('model', sa.String(length=32), nullable=False),
        sa.Column('p', sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column('price', sa.Numeric(precision=7, scale=5), nullable=True),
        sa.Column('odds', sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column('ev', sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column('news', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('predictions_match', 'predictions', ['match_id', 'created_at'], unique=False)
    op.create_table(
        'paper_bets',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('approach', sa.String(length=8), nullable=False),
        sa.Column('legs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('odds', sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column('p', sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column('stake', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pnl', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('notified', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('paper_bets_status', 'paper_bets', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('paper_bets_status', table_name='paper_bets')
    op.drop_table('paper_bets')
    op.drop_index('predictions_match', table_name='predictions')
    op.drop_table('predictions')
