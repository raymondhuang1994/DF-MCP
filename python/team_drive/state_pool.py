"""Fixed, persistent state slots leased by the existing OS worker lock.

Each active stdio worker owns one independent SQLite index. Slot names are
stable and are tried in order, so reconnects reuse warm indexes rather than
leaving a new index for every chat. OS handle closure reclaims crashed leases;
timestamps or PID guesses never authorize removal of a live worker's state.
"""
from pathlib import Path

from .errors import DriveError
from .fs_access import StateAccess, validate_state_boundaries

DEFAULT_MAX_SLOTS = 4
MAX_SLOTS = 32
POOL_VERSION = 1
JOB_HISTORY_LIMIT = 100


def pool_settings(config):
    options = config.get("state_pool", {})
    if not isinstance(options, dict) or set(options) - {"max_slots"}:
        raise DriveError("INVALID_CONFIG", "state_pool must be an object containing only max_slots.")
    maximum = options.get("max_slots", DEFAULT_MAX_SLOTS)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_SLOTS:
        raise DriveError("INVALID_CONFIG", "state_pool.max_slots must be an integer from 1 to 32.")
    return maximum


def slot_path(base, slot):
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= MAX_SLOTS:
        raise DriveError("INVALID_CONFIG", "State slot must be an integer from 1 to 32.")
    return Path(base) / "pool-v1" / ("slot-%02d" % slot)


class StateLease:
    def __init__(self, base, source, installation, max_slots=DEFAULT_MAX_SLOTS):
        self.max_slots = pool_settings({"state_pool": {"max_slots": max_slots}})
        self.base = validate_state_boundaries(base, source, installation)
        self.storage = None
        self.slot = None
        for number in range(1, self.max_slots + 1):
            candidate = None
            try:
                candidate = StateAccess(slot_path(self.base, number), source, installation)
                candidate.lock()
                self.storage, self.slot = candidate, number
                return
            except DriveError as exc:
                if candidate is not None:
                    candidate.close()
                if exc.code != "STATE_IN_USE":
                    raise
            except Exception:
                if candidate is not None:
                    candidate.close()
                raise
        raise DriveError("STATE_POOL_EXHAUSTED", "All configured index-state slots are in use.", True,
                         "Close an unused df-mcp connection and reconnect, or deliberately increase state_pool.max_slots (maximum 32).")

    @property
    def metadata(self):
        return {"mode": "pooled", "pool_version": POOL_VERSION, "slot": self.slot,
                "max_slots": self.max_slots, "persistent": True,
                "cleanup": {"job_history_limit": JOB_HISTORY_LIMIT,
                            "temporary_files": "reclaimed_on_slot_acquire", "exports": "manual"}}

    def close(self):
        if self.storage is not None:
            self.storage.close()
