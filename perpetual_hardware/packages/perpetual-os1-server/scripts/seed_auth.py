#!/usr/bin/env python3
"""Seed initial authentication data."""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from perpetual_os1_server.database.base import SessionLocal, engine
from perpetual_os1_server.database.models import User, Role
from argon2 import PasswordHasher
import json

ph = PasswordHasher()

DEFAULT_ROLES = [
	{
		"name": "admin",
		"permissions": [
			"users:read", "users:write", "users:delete",
			"roles:read", "roles:write", "roles:delete",
			"robot:control", "robot:read",
			"gantry:control", "gantry:read",
			"tool:control", "tool:read",
			"storage:read", "storage:write",
			"scanner:read", "scanner:control",
			"workstation:control", "workstation:read",
			"audit:read"
		]
	},
	{
		"name": "operator",
		"permissions": [
			"robot:control", "robot:read",
			"gantry:control", "gantry:read",
			"tool:control", "tool:read",
			"storage:read",
			"scanner:read", "scanner:control",
			"workstation:control", "workstation:read"
		]
	},
	{
		"name": "viewer",
		"permissions": [
			"robot:read",
			"gantry:read",
			"tool:read",
			"storage:read",
			"scanner:read",
			"workstation:read"
		]
	}
]

DEV_USERS = [
	{
		"email": "admin@example.com",
		"first_name": "Admin",
		"last_name": "User",
		"pin": "1234",
		"role": "admin"
	},
	{
		"email": "operator@example.com",
		"first_name": "Operator",
		"last_name": "User",
		"pin": "5678",
		"role": "operator"
	},
	{
		"email": "viewer@example.com",
		"first_name": "Viewer",
		"last_name": "User",
		"pin": "9012",
		"role": "viewer"
	}
]

def seed_roles(db):
	"""Create default roles if they don't exist."""
	for role_data in DEFAULT_ROLES:
		role = db.query(Role).filter_by(name=role_data["name"]).first()
		if role is None:
			role = Role(
				name=role_data["name"],
				permissions=role_data["permissions"]
			)
			db.add(role)
			print(f"Created role: {role_data['name']}")
		else:
			print(f"Role already exists: {role_data['name']}")
	
	db.commit()

def seed_dev_users(db):
	"""Create development users if they don't exist."""
	import os
	
	if os.getenv("ENV", "").lower() not in ["dev", "development"]:
		print("Skipping dev users - not in development mode")
		return
	
	for user_data in DEV_USERS:
		user = db.query(User).filter_by(email=user_data["email"]).first()
		if user is None:
			role = db.query(Role).filter_by(name=user_data["role"]).first()
			if role is None:
				print(f"Role not found: {user_data['role']}")
				continue
			
			user = User(
				email=user_data["email"],
				first_name=user_data["first_name"],
				last_name=user_data["last_name"],
				pin_hash=ph.hash(user_data["pin"]),
				role_id=role.id,
				is_active=True
			)
			db.add(user)
			print(f"Created user: {user_data['email']}")
		else:
			print(f"User already exists: {user_data['email']}")
	
	db.commit()

def main():
	"""Run seed script."""
	db = SessionLocal()
	try:
		print("Seeding roles...")
		seed_roles(db)
		
		print("\nSeeding development users...")
		seed_dev_users(db)
		
		print("\nSeeding complete!")
	finally:
		db.close()

if __name__ == "__main__":
	main()