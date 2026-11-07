"""match players and injuries

Revision ID: 4c2a9e1f7b10
Revises: dbfdb5d3bfa4
Create Date: 2026-11-08 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '4c2a9e1f7b10'
down_revision: str | None = 'dbfdb5d3bfa4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'match_players',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('match_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(length=1), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=True),
        sa.Column('player_id', sa.Integer(), nullable=False),
        sa.Column('starter', sa.Boolean(), nullable=True),
        sa.Column('position', sa.String(length=8), nullable=True),
        sa.Column('minutes', sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column('dnp_reason', sa.String(length=64), nullable=True),
        sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['player_id'], ['players.id']),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id']),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('match_id', 'source_id', 'player_id'),
    )
    op.create_index('match_players_player', 'match_players', ['player_id'], unique=False)
    op.create_table(
        'injuries',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('sport_id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=True),
        sa.Column('player_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('reported_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['player_id'], ['players.id']),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id']),
        sa.ForeignKeyConstraint(['sport_id'], ['sports.id']),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('injuries_player_time', 'injuries', ['player_id', 'fetched_at'], unique=False)
    op.create_index('injuries_team_time', 'injuries', ['team_id', 'fetched_at'], unique=False)


def downgrade() -> None:
    op.drop_index('injuries_team_time', table_name='injuries')
    op.drop_index('injuries_player_time', table_name='injuries')
    op.drop_table('injuries')
    op.drop_index('match_players_player', table_name='match_players')
    op.drop_table('match_players')
