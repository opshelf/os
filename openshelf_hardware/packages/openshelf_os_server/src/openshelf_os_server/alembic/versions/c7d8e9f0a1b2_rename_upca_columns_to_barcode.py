"""rename item upca columns to barcode

Item codes are arbitrary customer strings, so the columns and indexes that carried
the UPC-A-specific name now read `barcode`. Only the names change: values are stored
as strings either way.

Revision ID: c7d8e9f0a1b2
Revises: b5c6d7e8f9a0
Create Date: 2026-09-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c7d8e9f0a1b2"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


# (table, old column, new column, old index, new index, unique)
RENAMES = (
	("items", "upca", "barcode", "ix_items_upca", "ix_items_barcode", True),
	(
		"item_sizer_measurements",
		"upca",
		"barcode",
		"ix_item_sizer_measurements_upca",
		"ix_item_sizer_measurements_barcode",
		False,
	),
)


def upgrade() -> None:
	_rename(forward=True)


def downgrade() -> None:
	_rename(forward=False)


def _rename(*, forward: bool) -> None:
	inspector = sa.inspect(op.get_bind())

	for table, old_column, new_column, old_index, new_index, unique in RENAMES:
		if not inspector.has_table(table):
			continue

		source_column, target_column = (old_column, new_column) if forward else (new_column, old_column)
		source_index, target_index = (old_index, new_index) if forward else (new_index, old_index)

		columns = {column["name"] for column in inspector.get_columns(table)}
		indexes = {index["name"] for index in inspector.get_indexes(table)}

		# Drop the index first so its definition never carries a stale column
		# name; it is recreated below pointed at the renamed column.
		if source_index in indexes:
			op.drop_index(source_index, table_name=table)

		if source_column in columns and target_column not in columns:
			# A native RENAME COLUMN leaves the rows untouched. A batch rebuild
			# would drop and re-insert the table, and with PRAGMA
			# foreign_keys=ON that fires ON DELETE CASCADE / SET NULL on every
			# child row while the migration still reports success.
			op.alter_column(table, source_column, new_column_name=target_column)

		if target_index not in indexes:
			op.create_index(target_index, table, [target_column], unique=unique)
