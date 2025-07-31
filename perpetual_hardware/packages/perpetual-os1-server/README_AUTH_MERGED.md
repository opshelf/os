# Merged Authentication & Audit System

## Overview

This is the converged implementation that combines the best features from both authentication implementations:

- **Async SQLAlchemy** for better performance with FastAPI
- **OAuth2PasswordBearer** for standard authentication flow
- **Granular permission system** with wildcards
- **Comprehensive audit logging** with all metadata
- **Pydantic schemas** for validation

## Key Improvements

### 1. Async Database Operations

- Uses `aiosqlite` for async SQLite support
- All database operations are now async/await
- Better concurrency and performance

### 2. OAuth2 Standard Compliance

- `/api/auth/login` - OAuth2 form-based login (username/password)
- `/api/auth/login/json` - Alternative JSON login (email/pin)
- Bearer token authentication
- Compatible with standard OAuth2 tools

### 3. Granular Permission System

- Format: `resource:action` (e.g., `robot:control`, `users:write`)
- Wildcard support:
  - `users:*` - All user operations
  - `*:read` - Read all resources
  - `*` - Full access
- Permission inheritance through roles

### 4. Enhanced Audit Logging

- Tracks all API requests and responses
- Captures NATS hardware commands
- Records request duration
- Includes user agent and IP address
- Handles errors gracefully

## Setup

### Install Dependencies

```bash
uv pip install -e .
```

### Environment Variables

```bash
# Development mode
ENV=dev

# JWT secret (auto-generated if not set)
JWT_SECRET=your-secret-key

# Run seed script on startup
RUN_SEED_SCRIPT=true
```

### Run Server

```bash
ENV=dev RUN_SEED_SCRIPT=true uv run start
```

## API Usage

### Authentication

#### OAuth2 Form Login (standard)

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@opshelf.com&password=1234"
```

#### JSON Login (alternative)

```bash
curl -X POST http://localhost:8000/api/auth/login/json \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@opshelf.com", "pin": "1234"}'
```

#### Use Token

```bash
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/robot/configuration
```

### Permissions

Default roles and permissions:

**Admin**

- Full system access
- All CRUD operations
- Audit log access

**Operator**

- Hardware control (`robot:control`, `gantry:control`, etc.)
- Read access to all resources
- No user management

**Viewer**

- Read-only access to all resources
- No control operations

### Development Mode

When `ENV=dev`:

- Authentication is optional
- Auto-login as admin user
- All permissions granted
- Audit logging remains active

## Testing

Run the test script:

```bash
ENV=dev uv run python test_auth.py
```

## Architecture

```
┌─────────────────┐
│  FastAPI App    │
├─────────────────┤
│ OAuth2 Auth     │ ← OAuth2PasswordBearer
├─────────────────┤
│ Audit Middleware│ ← Async logging
├─────────────────┤
│ Route Handlers  │ ← Async endpoints
├─────────────────┤
│ Async SQLAlchemy│ ← aiosqlite
└─────────────────┘
```

## Migration Notes

This implementation merges:

- Async patterns from `feature/auth-and-audit`
- Granular permissions from `250730-users`
- Enhanced audit logging
- Proper Argon2 password hashing

The result is a production-ready authentication system that follows FastAPI best practices while maintaining flexibility and comprehensive logging.
