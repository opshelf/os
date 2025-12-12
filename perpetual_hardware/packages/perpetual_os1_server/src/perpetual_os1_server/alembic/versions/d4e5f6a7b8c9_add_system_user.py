"""add system user flag and developer user

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2025-12-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from argon2 import PasswordHasher


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None

ph = PasswordHasher()


def upgrade() -> None:
	bind = op.get_bind()
	inspector = sa.inspect(bind)

	user_columns: set[str] = set()
	if inspector.has_table('users'):
		user_columns = {col["name"] for col in inspector.get_columns('users')}

	if 'is_system' not in user_columns:
		op.execute(sa.text("ALTER TABLE users ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0"))

	admin_role = bind.execute(sa.text("SELECT id FROM roles WHERE name = 'admin'")).fetchone()
	if admin_role is None:
		return

	admin_role_id = admin_role[0]

	existing_dev_user = bind.execute(
		sa.text("SELECT id FROM users WHERE email = 'developer@opshelf.com'")
	).fetchone()

	if existing_dev_user is None:
		pin_hash = ph.hash("1111")
		stmt = sa.text(
			"INSERT INTO users (email, first_name, last_name, pin_hash, role_id, is_active, is_system, created_at, updated_at) "
			"VALUES (:email, :first_name, :last_name, :pin_hash, :role_id, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		).bindparams(
			sa.bindparam("email", "developer@opshelf.com"),
			sa.bindparam("first_name", "Developer"),
			sa.bindparam("last_name", "System"),
			sa.bindparam("pin_hash", pin_hash),
			sa.bindparam("role_id", admin_role_id),
		)
		op.execute(stmt)


def downgrade() -> None:
	bind = op.get_bind()

	op.execute(sa.text("DELETE FROM users WHERE email = 'developer@opshelf.com'"))

	inspector = sa.inspect(bind)
	user_columns: set[str] = set()
	if inspector.has_table('users'):
		user_columns = {col["name"] for col in inspector.get_columns('users')}

	if 'is_system' in user_columns:
		op.execute(sa.text("ALTER TABLE users DROP COLUMN is_system"))
