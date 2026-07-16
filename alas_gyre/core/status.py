VALID_STATUSES = {"idle", "running", "error", "update", "disconnected", "queued"}


def normalize_status(status):
    return status if status in VALID_STATUSES else "idle"

