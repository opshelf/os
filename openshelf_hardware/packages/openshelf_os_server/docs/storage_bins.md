# Multi-item storage bins

The feature is opt-in. With `storage.bins` omitted or empty, storage and retrieval use the existing direct-cell workflow.

Configure each physical bin in the deployment's root `config.json` before starting the robot:

```json
{
  "storage": {
    "filename": "data/storage.json",
    "bins": [
      {
        "barcode": "900000000001",
        "home_location": {
          "side": "left_module",
          "shelf_idx": 2,
          "cell_idx": 0
        },
        "inner_dimensions": {
          "width": 180,
          "height": 110,
          "length": 260
        },
        "max_items": 6
      }
    ]
  }
}
```

Barcodes must be unique 12-digit values and home cells must be unique. Dimensions use the same units as item and cell dimensions. Bin definitions are synchronized into the database at robot startup and whenever the bin API is read. A configured home is excluded from direct-cell allocation.

An item is assigned only to an at-home bin that has remaining count capacity, fits the item with a 90-degree footprint rotation allowed, and contains no item with a different access level. Partially filled compatible bins are preferred. If no bin fits, the API returns `storage_mode: "direct"` and the existing workflow remains authoritative.

Only one bin workflow can be active. The lock survives restarts. Scan-wait states resume after restart; a restart during robot motion enters `recovery_required` and must be retried or reconciled by an administrator. A recovery after a successful physical return is reconcile-only, so it cannot enqueue a second return motion. Quarantined bin homes remain reserved from allocation.
