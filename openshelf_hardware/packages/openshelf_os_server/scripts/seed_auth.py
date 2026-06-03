#!/usr/bin/env python3
"""Seed initial authentication data."""
import sys
import asyncio
import os
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from sqlalchemy import select
from openshelf_os_server.database.base import engine, Base
from openshelf_os_server.database.session import AsyncSessionLocal, AsyncSession
from openshelf_os_server.database.models import User, Role, AccessLevel
from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.settings import get_settings

DEFAULT_ACCESS_LEVELS = [
	{"name": "Level 1"},
	{"name": "Level 2"},
	{"name": "Level 3"},
]

DEFAULT_ROLES = [
	{
		"name": "admin",
		"permissions": [
			"users:read", "users:write", "users:delete",
			"roles:read", "roles:write", "roles:delete",
			"robot:control",
			"storage:read", "storage:write", "storage:delete", "storage:access",
			"printer:print",
			"audit:read"
		]
	},
	{
		"name": "operator",
		"permissions": [
			"robot:control",
			"storage:read"
		]
	},
	{
		"name": "viewer",
		"permissions": [
			"storage:read"
		]
	}
]

DEV_USERS = [
	{
		"email": "admin@opshelf.com",
		"first_name": "Admin",
		"last_name": "User",
		"pin": "1234",
		"role": "admin"
	},
	{
		"email": "operator@opshelf.com",
		"first_name": "Operator",
		"last_name": "User",
		"pin": "5678",
		"role": "operator"
	},
	{
		"email": "viewer@opshelf.com",
		"first_name": "Viewer",
		"last_name": "User",
		"pin": "9012",
		"role": "viewer"
	}
]

async def seed_access_levels(db: AsyncSession) -> None:
	"""Create default access levels if they don't exist."""
	for level_data in DEFAULT_ACCESS_LEVELS:
		result = await db.execute(
			select(AccessLevel).filter_by(name=level_data["name"])
		)
		level = result.scalar_one_or_none()

		if level is None:
			level = AccessLevel(name=str(level_data["name"]))
			db.add(level)
			print(f"Created access level: {level_data['name']}")
		else:
			print(f"Access level already exists: {level_data['name']}")

	await db.commit()

async def seed_roles(db: AsyncSession) -> None:
	"""Create default roles if they don't exist."""
	for role_data in DEFAULT_ROLES:
		result = await db.execute(
			select(Role).filter_by(name=role_data["name"])
		)
		role = result.scalar_one_or_none()

		if role is None:
			role = Role(
				name=str(role_data["name"]),
				permissions=list(role_data["permissions"])
			)
			db.add(role)
			print(f"Created role: {role_data['name']}")
		else:
			print(f"Role already exists: {role_data['name']}")
	
	await db.commit()

async def seed_dev_users(db: AsyncSession) -> None:
	"""Create development users if they don't exist."""
	if os.getenv("ENV", "").lower() not in ["dev", "development"]:
		print("Skipping dev users - not in development mode")
		return
	
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)

	for user_data in DEV_USERS:
		result = await db.execute(
			select(User).filter_by(email=user_data["email"])
		)
		user = result.scalar_one_or_none()

		if user is None:
			result = await db.execute(
				select(Role).filter_by(name=user_data["role"])
			)
			role = result.scalar_one_or_none()

			if role is None:
				print(f"Role not found: {user_data['role']}")
				continue
			
			user = User(
				email=user_data["email"],
				first_name=user_data["first_name"],
				last_name=user_data["last_name"],
				pin_hash=auth_service.hash_pin(user_data["pin"]),
				role_id=role.id,
				is_active=True
			)
			db.add(user)
			print(f"Created user: {user_data['email']}")
		else:
			print(f"User already exists: {user_data['email']}")
	
	await db.commit()

async def main() -> None:
	"""Run seed script."""
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)

	async with AsyncSessionLocal() as db:
		print("Seeding access levels...")
		await seed_access_levels(db)

		print("\nSeeding roles...")
		await seed_roles(db)

		print("\nSeeding development users...")
		await seed_dev_users(db)

		print("\nSeeding complete!")

if __name__ == "__main__":
	asyncio.run(main())
