"""rename storage module side values left/right to front/back

Revision ID: f9a0b1c2d3e4
Revises: d8e9f0a1b2c3
Create Date: 2026-02-14 00:00:00.000000
"""

from alembic import op

from openshelf_os_server.core.migrations import rename_module_side_values


revision = 'f9a0b1c2d3e4'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
	# The startup path in `main._run_migrations` re-runs the same helper, so
	# the rename also survives databases that baselined past this revision.
	rename_module_side_values(op.get_bind(), forward=True)


def downgrade() -> None:
	rename_module_side_values(op.get_bind(), forward=False)
