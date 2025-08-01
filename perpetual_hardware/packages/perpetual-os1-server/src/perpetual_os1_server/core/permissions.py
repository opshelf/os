"""Permission checking utilities."""
from typing import List

def has_permission(user_permissions: List[str], required_permission: str) -> bool:
	"""Check if user has the required permission.
	
	Supports wildcard permissions:
	- "users:*" matches any users permission
	- "*:read" matches any read permission
	- "*" matches any permission
	"""
	if "*" in user_permissions:
		return True
	
	if required_permission in user_permissions:
		return True
	
	resource, action = required_permission.split(":", 1)
	
	if f"{resource}:*" in user_permissions:
		return True
	
	if f"*:{action}" in user_permissions:
		return True
	
	return False

def has_any_permission(user_permissions: List[str], required_permissions: List[str]) -> bool:
	"""Check if user has any of the required permissions."""
	return any(
		has_permission(user_permissions, perm)
		for perm in required_permissions
	)

def has_all_permissions(user_permissions: List[str], required_permissions: List[str]) -> bool:
	"""Check if user has all of the required permissions."""
	return all(
		has_permission(user_permissions, perm)
		for perm in required_permissions
	)