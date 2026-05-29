"""add iobox sizer measurements table

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if inspector.has_table('iobox_sizer_measurements'):
		return

	op.create_table(
		'iobox_sizer_measurements',
		sa.Column('id', sa.Integer(), primary_key=True),
		sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
		sa.Column('trigger', sa.String(length=50), nullable=False),
		sa.Column('observer_mode', sa.Boolean(), nullable=False, server_default=sa.text('1')),
		sa.Column('upca', sa.String(length=255), nullable=True),
		sa.Column('item_name', sa.String(length=255), nullable=True),
		sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
		sa.Column('request_correlation_id', sa.String(length=100), nullable=True),
		sa.Column('command_payload', sa.JSON(), nullable=True),
		sa.Column('human_dimensions', sa.JSON(), nullable=True),
		sa.Column('human_dimensions_source', sa.String(length=50), nullable=True),
		sa.Column('measurement', sa.JSON(), nullable=True),
		sa.Column('detected', sa.Boolean(), nullable=False, server_default=sa.text('0')),
		sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.text('0')),
		sa.Column('error_type', sa.String(length=100), nullable=True),
		sa.Column('error_message', sa.Text(), nullable=True),
		sa.Column('duration_ms', sa.Integer(), nullable=True),
		sa.Column('config_snapshot', sa.JSON(), nullable=True),
		sa.Column('debug_image_path', sa.String(length=500), nullable=True),
	)
	op.create_index('ix_iobox_sizer_measurements_created_at', 'iobox_sizer_measurements', ['created_at'])
	op.create_index('ix_iobox_sizer_measurements_upca_created_at', 'iobox_sizer_measurements', ['upca', 'created_at'])
	op.create_index('ix_iobox_sizer_measurements_trigger_created_at', 'iobox_sizer_measurements', ['trigger', 'created_at'])


def downgrade() -> None:
	op.drop_index('ix_iobox_sizer_measurements_trigger_created_at', table_name='iobox_sizer_measurements')
	op.drop_index('ix_iobox_sizer_measurements_upca_created_at', table_name='iobox_sizer_measurements')
	op.drop_index('ix_iobox_sizer_measurements_created_at', table_name='iobox_sizer_measurements')
	op.drop_table('iobox_sizer_measurements')
