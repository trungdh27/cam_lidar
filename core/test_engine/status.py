from enum import Enum


class TestStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    READY = "READY"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    STOPPED = "STOPPED"
