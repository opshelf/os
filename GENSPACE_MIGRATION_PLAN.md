# Device e9184e3a Migration Plan

## Summary
Migrate device e9184e3a from commit `4d3a8fb` to HEAD. The main changes are parameter renames in config.json and one new required calibration value.

## Current State

**Device e9184e3a config parameters:**
```json
"SLIDE_EXTENSION_FRONT": 51,
"SLIDE_EXTENSION_BACK": 45,
"IO_STAGE_OFFSET": 8
```

**HEAD expects:**
```json
"STAGE_EXTENSION_IO_BOX": <calibrated value - REQUIRED>,
"STAGE_NUDGE": 8,
"STAGE_EXTENSION_SHELF_FRONT": 51,
"STAGE_EXTENSION_SHELF_BACK": 45
```

## Parameter Changes

| Old Parameter | New Parameter | Action |
|--------------|---------------|--------|
| `SLIDE_EXTENSION_FRONT` | `STAGE_EXTENSION_SHELF_FRONT` | Rename, keep value (51) |
| `SLIDE_EXTENSION_BACK` | `STAGE_EXTENSION_SHELF_BACK` | Rename, keep value (45) |
| `IO_STAGE_OFFSET` | (removed) | Delete |
| (none) | `STAGE_EXTENSION_IO_BOX` | Add - **REQUIRES CALIBRATION** |
| (none) | `STAGE_NUDGE` | Add with default 8 |

## Migration Steps

### Step 1: Update code on device
```bash
cd /opt/opshelf/repos/perpetual_os && git pull
```

### Step 2: Migrate config.json
Run migration script to:
1. Rename `SLIDE_EXTENSION_FRONT` → `STAGE_EXTENSION_SHELF_FRONT`
2. Rename `SLIDE_EXTENSION_BACK` → `STAGE_EXTENSION_SHELF_BACK`
3. Remove `IO_STAGE_OFFSET`
4. Add `STAGE_NUDGE: 8`
5. Add `STAGE_EXTENSION_IO_BOX: 51` (temporary value = old SLIDE_EXTENSION_FRONT)

### Step 3: Rebuild and restart containers
```bash
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up -d --build api dev_ui
```

### Step 4: Calibrate IO Box Stage Extension
1. Open dev UI calibration page
2. Go to IO Box calibration section
3. Use "Move" to position at IO box
4. Adjust stage (S) until position is correct
5. Save calibration

This will update `STAGE_EXTENSION_IO_BOX` to the correct calibrated value.

## Motor Config (Optional)

New optional motor fields with defaults:
- `ppr`: 10000 (pulses per revolution)
- `max_following_error`: 4000

These are optional and use sensible defaults. No migration needed unless specific motors require different values.

## Risk Assessment

**Low Risk:**
- Parameter renames are straightforward
- Defaults exist for new optional fields

**Medium Risk:**
- `STAGE_EXTENSION_IO_BOX` temporary value (51) may not be correct
- Robot will need manual calibration before IO box operations work correctly

## Rollback

If migration fails:
1. `git checkout 4d3a8fb`
2. Restore config.json from backup
3. Rebuild containers
