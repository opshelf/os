"""add access levels

Revision ID: a1b2c3d4e5f6
Revises: 7c2a1e4e2f3a
Create Date: 2025-10-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime


revision = 'a1b2c3d4e5f6'
down_revision = '7c2a1e4e2f3a'
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		'access_levels',
		sa.Column('id', sa.Integer(), nullable=False),
		sa.Column('name', sa.String(length=50), nullable=False),
		sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
		sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
		sa.PrimaryKeyConstraint('id'),
		sa.UniqueConstraint('name')
	)

	with op.batch_alter_table('users') as batch_op:
		batch_op.add_column(sa.Column('access_level_id', sa.Integer(), nullable=True))
		batch_op.create_foreign_key('fk_users_access_level_id', 'access_levels', ['access_level_id'], ['id'])

	op.execute("""
		INSERT INTO access_levels (name, created_at, updated_at)
		VALUES
			('Level 1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
			('Level 2', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
			('Level 3', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
	""")


def downgrade() -> None:
	with op.batch_alter_table('users') as batch_op:
		batch_op.drop_constraint('fk_users_access_level_id', type_='foreignkey')
		batch_op.drop_column('access_level_id')
	op.drop_table('access_levels')
