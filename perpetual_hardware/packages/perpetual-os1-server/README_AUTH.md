# Authentication & Audit System

## Overview

This authentication system provides user management, role-based access control (RBAC), and comprehensive audit logging for the Perpetual OS1 Server API. The system is designed for safety compliance in laboratory automation environments.

## Architecture

### Components

1. **Database Models** (`database/models.py`)
   - `User`: User accounts with email, name, and PIN authentication
   - `Role`: Permission-based roles (admin, operator, viewer)
   - `AuditLog`: Complete audit trail of all API operations

2. **Authentication Core** (`core/auth.py`)
   - JWT token generation and validation
   - Argon2 password hashing for secure PIN storage
   - Token refresh mechanism

3. **Middleware** (`middleware/audit.py`)
   - Automatic logging of all API requests and responses
   - NATS command capture for hardware operations
   - Request duration tracking

4. **API Endpoints**
   - `/api/auth/*`: Authentication endpoints (login, logout, refresh)
   - `/api/users/*`: User management (CRUD operations)
   - `/api/roles/*`: Role management
   - `/api/audit/*`: Audit log queries

## Setup

### 1. Environment Variables

```bash
# Development mode (disables auth requirements)
ENV=dev

# JWT Secret (auto-generated if not set)
JWT_SECRET=your-secret-key

# Run seed script on startup
RUN_SEED_SCRIPT=true
```

### 2. Database Migration

```bash
# Run migrations
uv run alembic upgrade head

# Seed initial data (dev mode only)
ENV=dev uv run python scripts/seed_auth.py
```

### 3. Default Users (Dev Mode)

| Email                | PIN  | Role     |
| -------------------- | ---- | -------- |
| admin@opshelf.com    | 1234 | admin    |
| operator@opshelf.com | 5678 | operator |
| viewer@opshelf.com   | 9012 | viewer   |

## Permission Model

### Roles and Permissions

**Admin** - Full system access:

- `users:*`, `roles:*`, `audit:read`
- All robot and hardware control permissions

**Operator** - Hardware control:

- `robot:control`, `gantry:control`, `tool:control`
- `scanner:control`, `workstation:control`
- Read access to all resources

**Viewer** - Read-only access:

- `*:read` permissions only

### Permission Format

Permissions follow the format: `resource:action`

- Examples: `robot:control`, `users:write`, `audit:read`
- Wildcards: `users:*` (all user actions), `*:read` (read all resources)

## API Usage

### Authentication Flow

1. **Login**

```bash
POST /api/auth/login
{
  "email": "admin@opshelf.com",
  "pin": "1234"
}

Response:
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

2. **Use Token**

```bash
GET /api/robot/configuration
Authorization: Bearer eyJ...
```

3. **Refresh Token**

```bash
POST /api/auth/refresh
{
  "refresh_token": "eyJ..."
}
```

### User Management

```bash
# List users
GET /api/users

# Create user
POST /api/users
{
  "email": "new@opshelf.com",
  "first_name": "New",
  "last_name": "User",
  "pin": "9999",
  "role_id": 2
}

# Update user
PUT /api/users/{id}
{
  "is_active": false
}
```

### Audit Queries

```bash
# Query audit logs
GET /api/audit/logs?user_id=1&date_from=2024-01-01&status_code=200

# Get specific log
GET /api/audit/logs/{id}
```

## Development Mode

In development mode (`ENV=dev`):

- Authentication is optional
- Auto-login as admin user if configured
- All permissions granted to anonymous users
- Audit logging remains active

## Security Considerations

1. **PIN Storage**: All PINs are hashed using Argon2
2. **JWT Security**: Tokens expire after 30 minutes by default
3. **HTTPS Required**: Always use HTTPS in production
4. **Audit Trail**: All operations are logged, including failed attempts

## Emergency Access

Critical safety endpoints work without authentication:

- Emergency stop endpoints
- Safety status endpoints

## Troubleshooting

### Common Issues

1. **"Not authenticated" error**
   - Check if token is included in Authorization header
   - Verify token hasn't expired
   - Ensure ENV is set to "dev" for development

2. **"Missing permission" error**
   - Check user's role has required permission
   - Use `/api/auth/me` to see current permissions

3. **Database errors**
   - Run migrations: `uv run alembic upgrade head`
   - Check database file permissions

### Testing

```bash
# Quick test script
ENV=dev uv run python test_auth.py

# Run full test suite
uv run pytest tests/
```

## Migration from Unauthenticated System

1. Deploy with `ENV=dev` initially
2. Create user accounts for all operators
3. Test authentication with select endpoints
4. Switch to `ENV=production` when ready
5. Monitor audit logs for any issues
