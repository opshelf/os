"""add side to item locations

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2025-11-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if not inspector.has_table('item_locations'):
		return

	column_names = {column["name"] for column in inspector.get_columns('item_locations')}
	if 'side' in column_names:
		return

	if bind.dialect.name == 'sqlite':
		op.execute(sa.text(
			"ALTER TABLE item_locations ADD COLUMN side VARCHAR(20) NOT NULL DEFAULT 'left_module'"
		))
	else:
		op.add_column('item_locations', sa.Column('side', sa.String(20), nullable=False, server_default='left_module'))

	existing_indexes = {idx["name"] for idx in inspector.get_indexes('item_locations')}
	if 'idx_item_locations_shelf_cell' in existing_indexes:
		op.drop_index('idx_item_locations_shelf_cell', table_name='item_locations')
	if 'idx_item_locations_side_shelf_cell' not in existing_indexes:
		op.create_index('idx_item_locations_side_shelf_cell', 'item_locations', ['side', 'shelf_idx', 'cell_idx'])


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if not inspector.has_table('item_locations'):
		return

	column_names = {column["name"] for column in inspector.get_columns('item_locations')}
	if 'side' not in column_names:
		return

	existing_indexes = {idx["name"] for idx in inspector.get_indexes('item_locations')}
	if 'idx_item_locations_side_shelf_cell' in existing_indexes:
		op.drop_index('idx_item_locations_side_shelf_cell', table_name='item_locations')
	if 'idx_item_locations_shelf_cell' not in existing_indexes:
		op.create_index('idx_item_locations_shelf_cell', 'item_locations', ['shelf_idx', 'cell_idx'])

	op.drop_column('item_locations', 'side')
