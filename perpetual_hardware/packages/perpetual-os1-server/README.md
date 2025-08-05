# Perpetual OS1 Server

FastAPI-based backend server for the Perpetual OS1 robotic automation system.

## Quick Start

### Development
```bash
# Install dependencies
uv pip install -e .

# Start server with auto-reload
ENV=dev uv run start

# Or manually with uvicorn
ENV=dev uv run uvicorn perpetual_os1_server.main:app --reload --host 0.0.0.0 --port 8000
```

### Production
```bash
# Start server
uv run start

# Or with specific host/port
uv run uvicorn perpetual_os1_server.main:app --host 0.0.0.0 --port 8000
```

## Architecture

- **FastAPI** application with async/await support
- **NATS JetStream** for command queuing and event streaming
- **SQLite** database with SQLAlchemy ORM
- **Worker processes** for hardware control (robot, scanner, scale)
- **SSE endpoints** for real-time status updates

## API Endpoints

### Auth & Users
- `/auth/*` - Authentication (login, logout, refresh)
- `/users/*` - User management
- `/roles/*` - Role and permission management
- `/audit/*` - Audit log access

### Robot Control
- `/api/robot/commands` - Queue robot commands
- `/api/robot/start` - Start robot operations
- `/api/robot/stop` - Stop robot
- `/api/robot/status` - Get current status
- `/api/robot/configuration` - Robot configuration

### Hardware Components
- `/api/gantry` - Gantry (X/Y/Z axis) control
- `/api/scale/readings` - Scale weight readings
- `/api/scanner/*` - Barcode scanner operations

### Storage & Inventory
- `/api/robot/storage` - Storage location management
- `/api/sizes/*` - Product size definitions
- `/api/print/*` - Label printing

## Environment Variables

- `ENV` - Environment mode (dev/prod)
- `DATABASE_URL` - SQLite database path
- `NATS_URL` - NATS server URL
- `RUN_SEED_SCRIPT` - Auto-run seed script on startup