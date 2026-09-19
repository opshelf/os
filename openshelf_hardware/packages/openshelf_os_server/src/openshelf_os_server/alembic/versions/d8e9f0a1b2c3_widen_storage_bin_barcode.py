"""widen storage_bins.barcode from VARCHAR(12) to the barcode bound

Tray codes are arbitrary customer strings, so the bin column is widened
to match items.barcode, the API/config surfaces, and the ORM model. No
values change; the widening is a schema contract fix.

The widening is dialect-aware and rebuild-free. On engines that can
change a column type in place, the declared width is altered natively.
SQLite is left alone: it stores text without enforcing the declared
width, and it cannot change a column type without dropping and recreating
the table, so the widening is unnecessary there and the rebuild would
only risk the rows on every upgrading device.

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
	if bind.dialect.name == "sqlite":
		# See the module docstring: the declared width is unenforced
		# metadata on SQLite, so leave it alone without touching rows.
		return
	inspector = sa.inspect(bind)
	if not inspector.has_table("storage_bins"):
		return
	target = BARCODE_MAX_LENGTH if forward else ORIGINAL_BARCODE_LENGTH
	length = _barcode_length(inspector)
	if length is None:
		# Column absent, or declared without a length (unbounded text): an
		# upgrade has nothing to do; a downgrade still restores the width.
		should_widen = not forward
	else:
		# Alter only when the declared width is on the wrong side of the
		# target: narrower than the bound on upgrade, wider on downgrade.
		# Moving the other way would only risk the rows for no gain.
		should_widen = length < target if forward else length > target
	if not should_widen:
		return

	# A native type change runs in place on these engines, so no rows,
	# children, indexes, or constraints are touched.
	op.alter_column(
		"storage_bins",
		"barcode",
		type_=sa.String(target),
		existing_type=sa.String(length) if length is not None else sa.String(),
		existing_nullable=False,
	)


def _barcode_length(inspector: sa.Inspector) -> int | None:
	"""Declared length of storage_bins.barcode, or None if absent/unbounded."""
	for column in inspector.get_columns("storage_bins"):
		if column["name"] == "barcode":
			length = getattr(column["type"], "length", None)
			return length if isinstance(length, int) else None
	return None
