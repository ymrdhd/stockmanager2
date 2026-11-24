"""Alembic runs only through lock-owning initialization or explicit maintenance."""

from alembic import context

connection = context.config.attributes.get('connection')
if connection is None or not connection.in_transaction():
    raise RuntimeError('migration requires the application-managed SQLite transaction')
context.configure(connection=connection, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
