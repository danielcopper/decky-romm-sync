"""User-facing message constants for the saves package.

All status and error message strings returned in the ``message`` /
``error`` fields of save-sync API responses live here so they stay
consistent across modules. Add to this file rather than inlining new
literals in service code.
"""

SAVE_SYNC_DISABLED = "Save sync is disabled"
DEVICE_NOT_REGISTERED = "Device not registered"
