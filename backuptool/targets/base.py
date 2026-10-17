from abc import ABC, abstractmethod
from pathlib import Path

from backuptool.core.config import CompressionConfig, TargetConfig

class BaseTarget(ABC):
    def __init__(self, name: str, config: TargetConfig | dict, tmp_dir: Path, compression: CompressionConfig | None = None):
        self.name = name
        self.config = config if isinstance(config, TargetConfig) else TargetConfig.model_validate(config)
        self.tmp_dir = tmp_dir
        self.compression = compression or CompressionConfig()

    @abstractmethod
    def execute(self) -> Path | None:
        pass
