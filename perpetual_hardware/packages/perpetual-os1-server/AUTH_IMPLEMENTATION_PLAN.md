# Authentication & Audit System Implementation Plan

## Overview
Implement user authentication, role-based authorization, and comprehensive audit logging for a laboratory automation API. The system must track all operations for safety compliance in a chemical laboratory environment.

## Technical Stack
- **ORM**: SQLAlchemy 2.0+ with Alembic for migrations
- **Database**: SQLite with WAL mode enabled
- **Authentication**: JWT (PyJWT library)
- **Password Hashing**: Argon2 (argon2-cffi)
- **API Documentation**: Built-in FastAPI OpenAPI generation
- **Testing**: Pytest with pytest-asyncio and pytest-cov

## Implementation Order

### Stage 1: Database Setup

1. **Install dependencies**:
   ```toml
   # Add to pyproject.toml dependencies
   "sqlalchemy>=2.0.0",
   "alembic>=1.13.0",
   "argon2-cffi>=23.1.0",
   "pyjwt>=2.8.0",
   ```

2. **Create database module** (`src/perpetual_os1_server/database/`):
   ```
   database/
   ├── __init__.py
   ├── base.py          # SQLAlchemy base and engine setup
   ├── models.py        # User, Role, AuditLog models
   └── session.py       # Database session management
   ```

3. **Initialize Alembic**:
   ```bash
   cd src/perpetual_os1_server
   alembic init alembic
   ```

4. **Configure database** in `database/base.py`:
   ```python
   from sqlalchemy import create_engine
   from sqlalchemy.orm import sessionmaker
   
   SQLALCHEMY_DATABASE_URL = "sqlite:///./data/auth.db?check_same_thread=False"
   engine = create_engine(
       SQLALCHEMY_DATABASE_URL,
       connect_args={"check_same_thread": False},
       pool_pre_ping=True,
       echo=False  # Set True for debugging
   )
   
   # Enable WAL mode for concurrent access
   with engine.connect() as conn:
       conn.execute(text("PRAGMA journal_mode=WAL"))
   ```

### Stage 2: Models Implementation

1. **Create models** in `database/models.py`:
   - Role model with id, name, permissions (JSON)
   - User model with id, email, first_name, last_name, pin_hash, role_id, is_active
   - AuditLog model with all tracking fields including nats_commands

2. **Create first migration**:
   ```bash
   alembic revision --autogenerate -m "Create auth tables"
   alembic upgrade head
   ```

3. **Create seed data script** (`scripts/seed_auth.py`):
   - Default roles: admin, operator, viewer
   - Development users if in dev mode
   - Must be idempotent (can run multiple times safely)

### Stage 3: Authentication Core

1. **Create auth module** (`src/perpetual_os1_server/core/auth.py`):
   - Password hashing functions using Argon2
   - JWT token creation and validation
   - Token refresh logic
   - User authentication against database

2. **Create auth settings** in `settings.py`:
   ```python
   class AuthSettings(BaseModel):
       enabled: bool = True
       require_auth: bool = True  # False for dev mode
       jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
       jwt_algorithm: str = "HS256"
       jwt_expire_minutes: int = 30
       jwt_refresh_expire_days: int = 7
       dev_mode: bool = False
       dev_auto_user: Optional[str] = None
   ```

3. **Create auth dependencies** (`routers/auth_deps.py`):
   - `get_current_user()` - Extract and validate JWT
   - `get_current_user_optional()` - For dev mode flexibility
   - `require_permissions()` - Permission checking decorator

### Stage 4: Auth API Endpoints

1. **Create auth router** (`routers/auth.py`):
   ```python
   POST /api/auth/login          # Email + PIN -> tokens
   POST /api/auth/refresh        # Refresh token -> new access token
   POST /api/auth/logout         # Invalidate tokens
   GET  /api/auth/me            # Current user info
   ```

2. **Create user management router** (`routers/users.py`):
   ```python
   GET    /api/users            # List users (admin only)
   POST   /api/users            # Create user
   GET    /api/users/{id}       # Get user
   PUT    /api/users/{id}       # Update user
   DELETE /api/users/{id}       # Soft delete (set inactive)
   ```

3. **Create role management router** (`routers/roles.py`):
   ```python
   GET    /api/roles            # List roles
   POST   /api/roles            # Create role (admin only)
   GET    /api/roles/{id}       # Get role
   PUT    /api/roles/{id}       # Update role
   DELETE /api/roles/{id}       # Delete role (check no users first)
   ```

### Stage 5: Audit Logging System

1. **Create audit middleware** (`middleware/audit.py`):
   - Capture all HTTP requests and responses
   - Track NATS commands published during request
   - Log to database with error handling
   - Must not break request on logging failure

2. **Implement NATS command tracking**:
   - Intercept all NATS publish calls during request
   - Store commands in request context
   - Include in audit log

3. **Create audit query endpoint** (`routers/audit.py`):
   ```python
   GET /api/audit/logs
   Query parameters:
   - user_id: Filter by user
   - date_from/date_to: Date range (ISO format)
   - endpoint: Filter by API endpoint
   - method: Filter by HTTP method
   - status_code: Filter by response status
   - limit: Max results (default 100, max 1000)
   - offset: Pagination offset
   ```

### Stage 6: Integration with Existing Code

1. **Update all routers** to include auth dependency:
   ```python
   from .auth_deps import get_current_user
   
   router = APIRouter(dependencies=[Depends(get_current_user)])
   ```

2. **Add permission checks** to sensitive endpoints:
   ```python
   @router.post("/commands", dependencies=[Depends(require_permissions("robot:control"))])
   ```

3. **Update lifespan** in `main.py`:
   - Initialize database
   - Run migrations
   - Seed initial data
   - Setup auth middleware

4. **Modify worker classes** to include user context:
   - Pass user info to NATS messages
   - Include user in hardware command logs

### Stage 7: Testing Implementation

1. **Unit tests** (`tests/unit/`):
   ```python
   test_auth.py:
   - test_password_hashing
   - test_password_verification
   - test_jwt_creation
   - test_jwt_validation
   - test_jwt_expiration
   - test_permission_checking
   
   test_models.py:
   - test_user_creation
   - test_role_assignment
   - test_audit_log_creation
   
   test_middleware.py:
   - test_audit_logging
   - test_nats_command_capture
   - test_error_handling
   ```

2. **Integration tests** (`tests/integration/`):
   ```python
   test_auth_flow.py:
   - test_login_success
   - test_login_invalid_credentials
   - test_token_refresh
   - test_logout
   - test_unauthorized_access
   
   test_user_management.py:
   - test_create_user
   - test_update_user
   - test_delete_user
   - test_list_users_pagination
   
   test_audit_system.py:
   - test_all_endpoints_logged
   - test_audit_query_filters
   - test_nats_commands_logged
   - test_concurrent_requests_logged
   ```

3. **Safety tests** (`tests/safety/`):
   ```python
   test_emergency_access.py:
   - test_emergency_stop_without_auth
   - test_safety_endpoints_always_accessible
   
   test_permission_enforcement.py:
   - test_robot_control_requires_permission
   - test_admin_endpoints_protected
   - test_cross_user_access_denied
   ```

4. **Test fixtures** (`tests/conftest.py`):
   ```python
   @pytest.fixture
   async def test_db():
       # Create test database
       
   @pytest.fixture
   async def test_client():
       # FastAPI test client with auth
       
   @pytest.fixture
   def test_users():
       # Standard test users with different roles
       
   @pytest.fixture
   def auth_headers():
       # JWT headers for different users
   ```

### Stage 8: Development Mode Support

1. **Update settings loading**:
   ```python
   def get_settings() -> Settings:
       # Load from data/settings.json
       # Override with environment variables
       # Set dev_mode based on environment
   ```

2. **Create dev mode middleware**:
   - Auto-login if configured
   - Show auth status in logs
   - Warning banner in responses

3. **Update documentation** with dev mode usage

### Stage 9: Documentation

1. **API documentation**:
   - Ensure all endpoints have OpenAPI descriptions
   - Add authentication requirements to docs
   - Include example requests/responses

2. **Create README_AUTH.md**:
   - Architecture overview
   - Setup instructions
   - Permission model explanation
   - Troubleshooting guide

## File Structure
```
src/perpetual_os1_server/
├── alembic/
│   └── versions/
├── database/
│   ├── __init__.py
│   ├── base.py
│   ├── models.py
│   └── session.py
├── core/
│   ├── auth.py
│   └── permissions.py
├── middleware/
│   ├── __init__.py
│   └── audit.py
├── routers/
│   ├── auth.py
│   ├── auth_deps.py
│   ├── users.py
│   ├── roles.py
│   └── audit.py
├── scripts/
│   └── seed_auth.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    └── safety/
```

## Critical Implementation Notes

1. **Never store PINs in plain text** - Always use Argon2 hashing
2. **Database migrations must be reversible** - Always include downgrade()
3. **Audit logging must never break requests** - Wrap in try/except
4. **Emergency endpoints must work without auth** - Explicitly exclude from middleware
5. **All datetime fields must be UTC** - Use `datetime.utcnow()`
6. **Test coverage must be >90%** - Use pytest-cov to verify
7. **NATS context must be preserved** - Pass user info through workers

## Validation Checklist

After implementation, verify:
- [ ] All endpoints return 401 when auth required and no token provided
- [ ] Dev mode allows anonymous access when configured
- [ ] Every API call creates an audit log entry
- [ ] NATS commands include user context
- [ ] Database migrations run without errors
- [ ] All tests pass with >90% coverage
- [ ] Emergency stop works without authentication
- [ ] Concurrent requests don't cause race conditions
- [ ] JWT tokens expire correctly
- [ ] Role permissions are enforced

## Dependencies to Add

```toml
[project]
dependencies = [
    # ... existing ...
    "sqlalchemy>=2.0.0",
    "alembic>=1.13.0", 
    "argon2-cffi>=23.1.0",
    "pyjwt>=2.8.0",
    "python-multipart>=0.0.6",  # For form data
]

[project.optional-dependencies]
test = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.1.0",
    "httpx>=0.25.0",  # For FastAPI testing
    "factory-boy>=3.3.0",  # For test data
]
```