import enum


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"  # platform accepted the cancel; not yet confirmed
    CANCELLED = "CANCELLED"  # platform confirmed the run is cancelled
    RETRYING = "RETRYING"

    @classmethod
    def terminal(cls) -> set["JobStatus"]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED}


class Domain(str, enum.Enum):
    CLUSTER = "cluster"
    QUERY = "query"
    STORAGE = "storage"


class Platform(str, enum.Enum):
    DATABRICKS = "databricks"
    FABRIC = "fabric"


class LogLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
