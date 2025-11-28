"""add access levels

Revision ID: a1b2c3d4e5f6
Revises: 7c2a1e4e2f3a
Create Date: 2025-10-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '7c2a1e4e2f3a'
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)
	dialect = bind.dialect.name

	# Clean up any temp table left by interrupted sqlite batch ops.
	if inspector.has_table("_alembic_tmp_users"):
		op.execute(sa.text("DROP TABLE _alembic_tmp_users"))

	if not inspector.has_table('access_levels'):
		op.create_table(
			'access_levels',
			sa.Column('id', sa.Integer(), nullable=False),
			sa.Column('name', sa.String(length=50), nullable=False),
			sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
			sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
			sa.PrimaryKeyConstraint('id'),
			sa.UniqueConstraint('name')
		)

	user_columns: set[str] = set()
	if inspector.has_table('users'):
		user_columns = {col["name"] for col in inspector.get_columns('users')}

	if 'access_level_id' not in user_columns and inspector.has_table('users'):
		if dialect == "sqlite":
			# SQLite cannot add foreign keys via ALTER TABLE; add column only.
			op.execute(sa.text("ALTER TABLE users ADD COLUMN access_level_id INTEGER"))
		else:
			op.add_column('users', sa.Column('access_level_id', sa.Integer(), nullable=True))
			op.create_foreign_key('fk_users_access_level_id', 'users', 'access_levels', ['access_level_id'], ['id'])

	if inspector.has_table('access_levels'):
		existing_levels = {row[0] for row in bind.execute(sa.text("SELECT name FROM access_levels")).fetchall()}
		levels_to_insert = ["Level 1", "Level 2", "Level 3"]
		missing_levels = [lvl for lvl in levels_to_insert if lvl not in existing_levels]

		for level_name in missing_levels:
			stmt = sa.text(
				"INSERT INTO access_levels (name, created_at, updated_at) "
				"VALUES (:name, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
			).bindparams(sa.bindparam("name", level_name))
			op.execute(stmt)


def downgrade() -> None:
	with op.batch_alter_table('users') as batch_op:
		batch_op.drop_constraint('fk_users_access_level_id', type_='foreignkey')
		batch_op.drop_column('access_level_id')
	op.drop_table('access_levels')
