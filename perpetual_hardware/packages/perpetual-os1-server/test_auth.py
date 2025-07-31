#!/usr/bin/env python3
# /// script
# dependencies = ["httpx"]
# ///
"""Quick test script to verify auth system works."""
import httpx
import asyncio
import os

BASE_URL = "http://localhost:8000"

async def test_auth():
	"""Test authentication flow."""
	async with httpx.AsyncClient() as client:
		print("1. Testing unauthenticated access...")
		resp = await client.get(f"{BASE_URL}/api/robot/configuration")
		if os.getenv("ENV") == "dev":
			print(f"   Status: {resp.status_code} (Expected 200 in dev mode)")
		else:
			print(f"   Status: {resp.status_code} (Expected 401)")
		
		print("\n2. Testing OAuth2 form login...")
		resp = await client.post(
			f"{BASE_URL}/api/auth/login",
			data={"username": "admin@opshelf.com", "password": "1234"}
		)
		print(f"   Status: {resp.status_code}")
		if resp.status_code == 200:
			data = resp.json()
			token = data["access_token"]
			print(f"   Got token: {token[:20]}...")
			
			print("\n3. Testing JSON login...")
			resp = await client.post(
				f"{BASE_URL}/api/auth/login/json",
				json={"email": "admin@opshelf.com", "pin": "1234"}
			)
			print(f"   Status: {resp.status_code}")

			print("\n4. Testing authenticated access...")
			headers = {"Authorization": f"Bearer {token}"}
			resp = await client.get(
				f"{BASE_URL}/api/robot/configuration",
				headers=headers
			)
			print(f"   Status: {resp.status_code} (Expected 200)")
			
			print("\n5. Testing current user endpoint...")
			resp = await client.get(
				f"{BASE_URL}/api/auth/me",
				headers=headers
			)
			print(f"   Status: {resp.status_code}")
			if resp.status_code == 200:
				user = resp.json()
				print(f"   User: {user['email']} ({user['role']})")
				print(f"   Permissions: {', '.join(user['permissions'][:3])}...")

			print("\n6. Testing permission check...")
			resp = await client.post(
				f"{BASE_URL}/api/robot/commands",
				headers=headers,
				json={"command": "stop"}  # Valid robot command
			)
			print(f"   Robot control status: {resp.status_code} (Expected 200 for admin)")

			print("\n7. Testing audit logs...")
			resp = await client.get(
				f"{BASE_URL}/api/audit/logs?limit=5",
				headers=headers
			)
			print(f"   Audit query status: {resp.status_code}")
			if resp.status_code == 200:
				data = resp.json()
				print(f"   Found {data['total']} audit logs")

if __name__ == "__main__":
	print("Starting auth test...")
	print(f"Environment: {os.getenv('ENV', 'production')}")
	asyncio.run(test_auth())
