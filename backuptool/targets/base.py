from abc import ABC, abstractmethod
from pathlib import Path

class BaseTarget(ABC):
    def __init__(self, name: str, config: dict, tmp_dir: Path):
        self.name = name
        self.config = config
        self.tmp_dir = tmp_dir

    @abstractmethod
    def execute(self) -> Path | None:
        pass