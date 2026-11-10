from backuptool.destinations.base import BaseDestination
from backuptool.destinations.telegram import TelegramDestination
from backuptool.destinations.s3 import S3Destination

# Keys match the field names under `services:` in config.yml (see ServicesConfig). A service
# whose settings are a map of named entries (s3) gets one destination instance per entry.
# To add a destination: write a BaseDestination subclass, add its settings model to
# ServicesConfig, and register it here - the manager needs no changes.
DESTINATION_TYPES: dict[str, type[BaseDestination]] = {
    'telegram': TelegramDestination,
    's3': S3Destination,
}
