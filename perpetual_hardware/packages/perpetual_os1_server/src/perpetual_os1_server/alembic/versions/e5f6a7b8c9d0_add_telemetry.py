"""add telemetry table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-02-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if inspector.has_table('telemetry'):
		return

	op.create_table(
		'telemetry',
		sa.Column('id', sa.Integer(), primary_key=True),
		sa.Column('cycle_count', sa.Integer(), nullable=False, server_default='0'),
		sa.Column('motor_movement_seconds', sa.Float(), nullable=False, server_default='0.0'),
		sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
	)

	op.execute(sa.text(
		"INSERT INTO telemetry (id, cycle_count, motor_movement_seconds) VALUES (1, 0, 0.0)"
	))


def downgrade() -> None:
	op.drop_table('telemetry')
