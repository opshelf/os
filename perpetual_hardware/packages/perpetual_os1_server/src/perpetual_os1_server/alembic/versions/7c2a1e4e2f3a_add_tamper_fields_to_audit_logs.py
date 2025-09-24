"""add tamper-evidence fields to audit_logs

Revision ID: 7c2a1e4e2f3a
Revises: e8f130fdc9e9
Create Date: 2025-08-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '7c2a1e4e2f3a'
down_revision = 'e8f130fdc9e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('audit_logs', sa.Column('seq_no', sa.Integer(), nullable=True))
    op.add_column('audit_logs', sa.Column('prev_hash', sa.String(length=64), nullable=True))
    op.add_column('audit_logs', sa.Column('entry_hash', sa.String(length=64), nullable=True))
    op.add_column('audit_logs', sa.Column('hmac', sa.String(length=64), nullable=True))
    op.add_column('audit_logs', sa.Column('hmac_key_id', sa.String(length=50), nullable=True))
    try:
        op.create_index('ix_audit_logs_seq_no', 'audit_logs', ['seq_no'], unique=False)
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index('ix_audit_logs_seq_no', table_name='audit_logs')
    except Exception:
        pass
    op.drop_column('audit_logs', 'hmac_key_id')
    op.drop_column('audit_logs', 'hmac')
    op.drop_column('audit_logs', 'entry_hash')
    op.drop_column('audit_logs', 'prev_hash')
    op.drop_column('audit_logs', 'seq_no')

