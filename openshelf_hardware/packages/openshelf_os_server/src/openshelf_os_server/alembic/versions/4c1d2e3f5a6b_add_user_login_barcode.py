"""add login barcode to users

Revision ID: 4c1d2e3f5a6b
Revises: 3b9f4c2a7d10
"""

from alembic import op
import sqlalchemy as sa

revision = "4c1d2e3f5a6b"
down_revision = "3b9f4c2a7d10"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_users_login_barcode"


def upgrade() -> None:
	inspector = sa.inspect(op.get_bind())
	if not inspector.has_table("users"):
		return
	if "login_barcode" not in {column["name"] for column in inspector.get_columns("users")}:
		op.add_column("users", sa.Column("login_barcode", sa.String(length=64), nullable=True))
	if INDEX_NAME not in {index["name"] for index in inspector.get_indexes("users")}:
		# SQLite cannot add a UNIQUE column in place; a unique index gives the same
		# guarantee and is what Base.metadata declares for the column. NULLs stay
		# distinct, so every user without a login barcode remains valid.
		op.create_index(INDEX_NAME, "users", ["login_barcode"], unique=True)


def downgrade() -> None:
	inspector = sa.inspect(op.get_bind())
	if not inspector.has_table("users"):
		return
	if INDEX_NAME in {index["name"] for index in inspector.get_indexes("users")}:
		op.drop_index(INDEX_NAME, table_name="users")
	if "login_barcode" in {column["name"] for column in inspector.get_columns("users")}:
		with op.batch_alter_table("users") as batch:
			batch.drop_column("login_barcode")
