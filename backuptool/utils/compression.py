import logging
from dataclasses import dataclass
from typing import Callable

from backuptool.core.config import CompressionConfig

logger = logging.getLogger(__name__)

GZIP_DEFAULT_LEVEL = 6
ZSTD_DEFAULT_LEVEL = 3   # with -T0 (all cores): ~17x faster than gzip -6 on typical dumps/configs

@dataclass(frozen=True)
class Compressor:
    program: str     # command line handed to `tar --use-compress-program`
    extension: str   # ".tar.gz" or ".tar.zst"
    name: str        # what is actually used: "gzip" or "zstd"

def resolve_compressor(settings: CompressionConfig, has_tool: Callable[[str], bool], where: str = "this host") -> Compressor:
    """
    Picks the compressor for the configured algorithm. `has_tool(name)` tells whether a binary
    exists on the machine that will run tar (this host, or the remote server). If zstd was
    requested but is missing there, the backup still happens, as gzip, with a warning: the
    archive name carries its real extension, so it is never mistaken for a zstd file.
    """
    if settings.algorithm == 'zstd':
        if has_tool('zstd'):
            level = settings.level or ZSTD_DEFAULT_LEVEL
            return Compressor(f"zstd -{level} -T{settings.threads}", ".tar.zst", "zstd")
        logger.warning(f"Compression is set to zstd but 'zstd' is not installed on {where}; falling back to gzip.")
        level = GZIP_DEFAULT_LEVEL
    else:
        level = settings.level or GZIP_DEFAULT_LEVEL

    tool = "pigz" if has_tool("pigz") else "gzip"
    return Compressor(f"{tool} -{level}", ".tar.gz", "gzip")
