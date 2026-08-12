from abc import ABC, abstractmethod

from core.test_result import TestResult


class BaseTest(ABC):

    test_id = "UNKNOWN"
    test_name = "Unknown Test"
    timeout = 10

    def __init__(self, device=None):
        self.device = device

    def setup(self):
        pass

    @abstractmethod
    def run(self) -> TestResult:
        raise NotImplementedError

    def cleanup(self):
        pass
