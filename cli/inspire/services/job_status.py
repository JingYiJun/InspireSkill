"""Shared training-job status vocabulary (other workload types differ)."""

_STATUS = {
    "PENDING": "PENDING",
    "CREATING": "PENDING",
    "QUEUING": "QUEUING",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "STOPPED": "CANCELLED",
}
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
RAW_TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "STOPPED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
        "job_stopped",
    }
)


def normalize_status(value: str) -> str:
    return _STATUS.get(value.removeprefix("job_").upper(), "UNKNOWN")
