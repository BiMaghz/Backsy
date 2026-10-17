from backuptool.destinations.base import BaseDestination
from backuptool.destinations.cloudflare import CloudflareDestination
from backuptool.destinations.telegram import TelegramDestination
from backuptool.destinations.s3 import S3Destination

# Keys match the field names under `services:` in config.yml (see ServicesConfig).
# To add a destination: write a BaseDestination subclass, add its settings model to
# ServicesConfig, and register it here - the manager needs no changes.
DESTINATION_TYPES: dict[str, type[BaseDestination]] = {
    'cloudflare': CloudflareDestination,
    'telegram': TelegramDestination,
    's3': S3Destination,
}
