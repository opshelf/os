"""add item sizer measurements

Revision ID: a8b9c0d1e2f3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a8b9c0d1e2f3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


TABLE_NAME = 'item_sizer_measurements'
INDEXES = {
	'ix_item_sizer_measurements_item_id': ['item_id'],
	'ix_item_sizer_measurements_upca': ['upca'],
	'ix_item_sizer_measurements_request_correlation_id': ['request_correlation_id'],
	'ix_item_sizer_measurements_created_at': ['created_at'],
}


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if not inspector.has_table(TABLE_NAME):
		op.create_table(
			TABLE_NAME,
			sa.Column('id', sa.Integer(), primary_key=True),
			sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
			sa.Column('upca', sa.String(length=255), nullable=True),
			sa.Column('item_name', sa.String(length=255), nullable=True),
			sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
			sa.Column('request_correlation_id', sa.String(length=64), nullable=True),
			sa.Column('manual_dimensions', sa.JSON(), nullable=True),
			sa.Column('manual_dimensions_source', sa.String(length=32), nullable=True),
			sa.Column('auto_dimensions', sa.JSON(), nullable=True),
			sa.Column('raw_measurement', sa.JSON(), nullable=True),
			sa.Column('command_payload', sa.JSON(), nullable=True),
			sa.Column('detected', sa.Boolean(), nullable=False, server_default=sa.false()),
			sa.Column('error_type', sa.String(length=100), nullable=True),
			sa.Column('error_message', sa.Text(), nullable=True),
			sa.Column('measurement_timestamp', sa.DateTime(timezone=True), nullable=True),
			sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
		)

	inspector = sa.inspect(bind)
	existing_indexes = {idx['name'] for idx in inspector.get_indexes(TABLE_NAME)}
	for index_name, columns in INDEXES.items():
		if index_name not in existing_indexes:
			op.create_index(index_name, TABLE_NAME, columns, unique=False)


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	if not inspector.has_table(TABLE_NAME):
		return

	existing_indexes = {idx['name'] for idx in inspector.get_indexes(TABLE_NAME)}
	for index_name in reversed(INDEXES):
		if index_name in existing_indexes:
			op.drop_index(index_name, table_name=TABLE_NAME)

	op.drop_table(TABLE_NAME)
