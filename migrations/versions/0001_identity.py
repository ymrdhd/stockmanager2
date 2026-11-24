"""Initial single-row database identity; no business tables."""

from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'system_metadata',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('database_uuid', sa.String(36), nullable=False),
        sa.Column('instance_role', sa.String(16), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.CheckConstraint('id = 1'),
        sa.CheckConstraint("instance_role IN ('formal', 'sandbox', 'backup')"),
        sa.CheckConstraint('generation >= 1'),
        sa.CheckConstraint('schema_version >= 1'),
    )


def downgrade() -> None:
    raise RuntimeError('automatic downgrade is not supported')
