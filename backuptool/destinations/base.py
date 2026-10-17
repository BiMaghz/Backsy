import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel


@dataclass(frozen=True)
class SendResult:
    ok: bool
    link: str | None = None   # caption line(s) that other destinations may append (download links)
    detail: str = ""          # why it failed (for logs/alerts)


class BaseDestination(ABC):
    config_model: ClassVar[type[BaseModel]]   # validated settings class for this destination
    produces_link: bool = False               # True: upload first, so the link can go into the other captions

    def __init__(self, name: str, config: BaseModel | dict, killer=None):
        self._name = name
        self.config = config if isinstance(config, self.config_model) else self.config_model.model_validate(config)
        self.killer = killer
        self.logger = logging.getLogger(f"destination.{self.name}")

    @property
    def name(self) -> str:
        return self._name

    def send(self, archive_path: Path, base_caption: str, link_caption: str = "") -> SendResult:
        """Delivery failures never raise: they are reported as SendResult(ok=False)."""
        try:
            return self._send(archive_path, base_caption, link_caption)
        except Exception as e:
            self.logger.error(f"Upload to {self.name} failed: {e}")
            return SendResult(False, detail=f"{type(e).__name__}: {e}")

    @abstractmethod
    def _send(self, archive_path: Path, base_caption: str, link_caption: str) -> SendResult:
        pass
