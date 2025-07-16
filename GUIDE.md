## System Overview

Storage robot controlled via Web UI and Python API server.

```
Web UI ↔ API Server (FastAPI) ↔ NATS ↔ Hardware Workers
          ↓
         Automatic Storage Logic
```

## Components

### Development UI (`openshelf-os1/`)

- **Stack**: React 19, TypeScript, Vite, Tailwind CSS
- **Routes**:

  - `/` - Robot Controller
  - `/inventory` - Stored items view
  - `/storage` - Storage builder
  - `/sizes` - Size database
  - `/configuration` - Robot config

- **Real-time SSE Components**:
  - `ScaleEventSSE` - Weight readings
  - `ScannerEventSSE` - Barcode scans
  - `RobotStatusEventSSE` - Robot position/status

### API Server (`perpetual-os1-server/`)

- **Stack**: FastAPI, Python, NATS
- **Endpoints**:
  - `/api/gantry/*` - Gantry control
  - `/api/robot/*` - Robot commands
  - `/api/scale/readings` - Scale SSE stream
  - `/api/scanner/scans` - Scanner SSE stream
  - `/api/print/*` - Label printing
  - `/api/sizes/*` - Size database

### Workers (startup initialized)

- **Robot Worker**: Movement command execution
- **Scale Worker**: Weight reading publisher
- **Scanner Worker**: Barcode scan publisher
- **Automatic Storage**: Storage flow orchestration

## Communication Patterns

### Manual Control

1. UI → REST API → Server
2. Server → NATS command
3. Worker → Hardware execution
4. Status → SSE → UI

### Automatic Storage

1. Scanner detects → `scanner.events`
2. Scale detects → `scale.events`
3. Stable weight + barcode → Size command
4. Size complete → Find location → Store command

## Development

```bash
# Start NATS
docker-compose up -d

# Start server
cd perpetual_hardware/packages/perpetual-os1-server
uv run start

# Start UI
cd openshelf-os1
npm run dev
```

## Technical Details

- **API Host**: Dev: `http://localhost:8000/api`, Prod: `/api`
- **NATS**: `nats://localhost:4222`
- **SSE**: Auto-reconnect 1000x @ 3s intervals
- **State**: Zustand store
- **Python**: Uses `uv` package manager

## Common Tasks

- **New API endpoint**: Create in `routers/`, add to `main.py`
- **New UI feature**: Create in `components/`, add route in `App.tsx`
- **Debug NATS**: `nats sub ">"`
- **Test automatic flow**: Place item on scale + scan barcode

## Message Topics

- `scanner.events` - Barcode scans
- `scale.events` - Weight readings
- `robot.status` - Robot state updates
- `events` - Robot command responses
