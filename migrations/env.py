"""Alembic environment: url from core.config, metadata from core.models."""

from logging.config import fileConfig

from alembic import context

from core.config import settings
from core.db import engine
from core.models import Base

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)


def run_offline() -> None:
    context.configure(url=settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    with engine().connect() as conn:
        context.configure(connection=conn, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
