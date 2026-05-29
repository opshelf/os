"""add audit log query indexes

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


INDEXES = {
	'ix_audit_logs_timestamp': ['timestamp'],
	'ix_audit_logs_user_id_timestamp': ['user_id', 'timestamp'],
	'ix_audit_logs_endpoint_timestamp': ['endpoint', 'timestamp'],
	'ix_audit_logs_method_endpoint_timestamp': ['method', 'endpoint', 'timestamp'],
	'ix_audit_logs_response_status_timestamp': ['response_status', 'timestamp'],
}


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if not inspector.has_table('audit_logs'):
		return

	existing_indexes = {idx['name'] for idx in inspector.get_indexes('audit_logs')}
	for index_name, columns in INDEXES.items():
		if index_name not in existing_indexes:
			op.create_index(index_name, 'audit_logs', columns, unique=False)


def downgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	if not inspector.has_table('audit_logs'):
		return

	existing_indexes = {idx['name'] for idx in inspector.get_indexes('audit_logs')}
	for index_name in reversed(INDEXES):
		if index_name in existing_indexes:
			op.drop_index(index_name, table_name='audit_logs')
