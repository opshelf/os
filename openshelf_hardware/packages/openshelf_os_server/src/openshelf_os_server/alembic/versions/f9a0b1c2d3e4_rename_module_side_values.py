"""rename storage module side values left/right to front/back

Revision ID: f9a0b1c2d3e4
Revises: d8e9f0a1b2c3
Create Date: 2026-02-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f9a0b1c2d3e4'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if inspector.has_table('item_locations'):
		op.execute(sa.text("UPDATE item_locations SET side = 'front_module' WHERE side = 'left_module'"))
		op.execute(sa.text("UPDATE item_locations SET side = 'back_module' WHERE side = 'right_module'"))

	if inspector.has_table('storage_bins'):
		op.execute(sa.text("UPDATE storage_bins SET side = 'front_module' WHERE side = 'left_module'"))
		op.execute(sa.text("UPDATE storage_bins SET side = 'back_module' WHERE side = 'right_module'"))


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if inspector.has_table('item_locations'):
		op.execute(sa.text("UPDATE item_locations SET side = 'left_module' WHERE side = 'front_module'"))
		op.execute(sa.text("UPDATE item_locations SET side = 'right_module' WHERE side = 'back_module'"))

	if inspector.has_table('storage_bins'):
		op.execute(sa.text("UPDATE storage_bins SET side = 'left_module' WHERE side = 'front_module'"))
		op.execute(sa.text("UPDATE storage_bins SET side = 'right_module' WHERE side = 'back_module'"))
