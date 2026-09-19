"""widen storage_bins.barcode from VARCHAR(12) to the barcode bound

Tray codes are arbitrary customer strings, so the bin column is widened to
match items.barcode, the API/config surfaces, and the ORM model. SQLite
stores text without enforcing the declared width, so this is a schema
contract fix: no values change.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from openshelf_os.config.settings import BARCODE_MAX_LENGTH


revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


# The width c0d1e2f3a4b5_add_storage_bins declared.
ORIGINAL_BARCODE_LENGTH = 12


def upgrade() -> None:
	_widen(forward=True)


def downgrade() -> None:
	_widen(forward=False)


def _widen(*, forward: bool) -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if not inspector.has_table("storage_bins"):
		return
	target = BARCODE_MAX_LENGTH if forward else ORIGINAL_BARCODE_LENGTH
	length = _barcode_length(inspector)
	if length is None:
		# Column absent, or declared without a length (unbounded text): an
		# upgrade has nothing to do; a downgrade still restores the width.
		should_rebuild = not forward
	else:
		# Rebuild only when the declared width is on the wrong side of the
		# target: narrower than the bound on upgrade, wider on downgrade.
		# Moving the other way would only risk the rows for no gain.
		should_rebuild = length < target if forward else length > target
	if not should_rebuild:
		return

	if _foreign_keys_enabled(bind):
		# The batch rebuild drops and re-inserts storage_bins; with foreign
		# keys armed that fires ON DELETE CASCADE on storage_bin_items (and
		# nulls ON DELETE SET NULL children) while the migration reports
		# success. Setting PRAGMA foreign_keys=OFF inside the migration is
		# a no-op because the transaction is already open, so the connection
		# must be configured with it disabled before the migration runs.
		raise RuntimeError(
			"refusing to rebuild storage_bins while the migration connection "
			"has PRAGMA foreign_keys enabled: the batch rebuild drops and "
			"re-inserts the rows, which deletes the storage_bin_items "
			"children via ON DELETE CASCADE. Run the migration with foreign "
			"keys disabled on its connection (PRAGMA foreign_keys=OFF, set "
			"before the migration transaction opens)."
		)

	_rebuild_barcode_column(target)


def _barcode_length(inspector: sa.Inspector) -> int | None:
	"""Declared length of storage_bins.barcode, or None if absent/unbounded."""
	for column in inspector.get_columns("storage_bins"):
		if column["name"] == "barcode":
			length = getattr(column["type"], "length", None)
			return length if isinstance(length, int) else None
	return None


def _foreign_keys_enabled(bind: sa.Connection) -> bool:
	return bind.execute(sa.text("PRAGMA foreign_keys")).scalar_one() == 1


def _rebuild_barcode_column(target: int) -> None:
	bind = op.get_bind()
	with op.batch_alter_table(
		"storage_bins",
		recreate="always",
		copy_from=_rebuild_source_table(bind),
	) as batch_op:
		batch_op.alter_column("barcode", type_=sa.String(target))


def _rebuild_source_table(bind: sa.Connection) -> sa.Table:
	"""Reflect the columns and re-declare the named constraint and indexes.

	The batch recreation copies these onto the rebuilt table, so the table
	comes back with its uniqueness and indexes intact. Anything anonymous
	(the inline UNIQUE autoindex on barcode) is dropped on purpose: the
	unique ix_storage_bins_barcode index enforces the same rule.
	"""
	reflected = sa.Table("storage_bins", sa.MetaData(), autoload_with=bind)
	table = sa.Table(
		"storage_bins",
		sa.MetaData(),
		*(
			sa.Column(
				column.name,
				column.type,
				primary_key=column.primary_key,
				nullable=column.nullable,
				server_default=column.server_default,
			)
			for column in reflected.columns
		),
	)
	table.append_constraint(
		sa.UniqueConstraint("side", "shelf_idx", "cell_idx", name="uq_storage_bins_home_cell")
	)
	sa.Index("ix_storage_bins_barcode", table.c.barcode, unique=True)
	sa.Index("ix_storage_bins_status", table.c.status)
	return table
