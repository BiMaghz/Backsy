import logging
from abc import ABC, abstractmethod
from pathlib import Path

class BaseDestination(ABC):

    def __init__(self, name: str, config: dict, killer=None):
        self._name = name
        self.config = config
        self.killer = killer
        self.logger = logging.getLogger(f"destination.{self.name}")

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    def send(self, archive_path: Path, caption: str) -> bool:
        pass