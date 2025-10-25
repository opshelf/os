"""add access level to items

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2025-10-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table('items') as batch_op:
		batch_op.add_column(sa.Column('access_level_id', sa.Integer(), nullable=True))
		batch_op.create_foreign_key('fk_items_access_level_id', 'access_levels', ['access_level_id'], ['id'])


def downgrade() -> None:
	with op.batch_alter_table('items') as batch_op:
		batch_op.drop_constraint('fk_items_access_level_id', type_='foreignkey')
		batch_op.drop_column('access_level_id')
