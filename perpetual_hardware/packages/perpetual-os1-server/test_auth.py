#!/usr/bin/env python3
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
		
		print("\n2. Testing login...")
		resp = await client.post(
			f"{BASE_URL}/api/auth/login",
			json={"email": "admin@example.com", "pin": "1234"}
		)
		print(f"   Status: {resp.status_code}")
		if resp.status_code == 200:
			data = resp.json()
			token = data["access_token"]
			print(f"   Got token: {token[:20]}...")
			
			print("\n3. Testing authenticated access...")
			headers = {"Authorization": f"Bearer {token}"}
			resp = await client.get(
				f"{BASE_URL}/api/robot/configuration",
				headers=headers
			)
			print(f"   Status: {resp.status_code} (Expected 200)")
			
			print("\n4. Testing current user endpoint...")
			resp = await client.get(
				f"{BASE_URL}/api/auth/me",
				headers=headers
			)
			print(f"   Status: {resp.status_code}")
			if resp.status_code == 200:
				user = resp.json()
				print(f"   User: {user['email']} ({user['role']})")
				print(f"   Permissions: {', '.join(user['permissions'][:3])}...")

if __name__ == "__main__":
	print("Starting auth test...")
	print(f"Environment: {os.getenv('ENV', 'production')}")
	asyncio.run(test_auth())