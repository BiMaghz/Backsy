import os
from pathlib import Path
import re
import yaml
import logging
from typing import List, Dict, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError

from backuptool.utils.paths import plan_paths

from backuptool.core.settings import CONFIG_PATH
# SSH host keys that remote targets are verified against (lives next to config.yml).
KNOWN_HOSTS_PATH = Path(CONFIG_PATH).resolve().parent / "known_hosts"
# Target names end up in file names, archive names and S3 keys.
_TARGET_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
logger = logging.getLogger(__name__)

class _Model(BaseModel):
    # Unquoted YAML numbers (chat_id: -100123..., topic_id: 5) are valid for str fields.
    model_config = ConfigDict(coerce_numbers_to_str=True)

class DatabaseConfig(_Model):
    enable: bool = False
    type: Optional[Literal['mysql', 'mariadb', 'postgresql']] = None
    container: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    name: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None

    @model_validator(mode='after')
    def check_db_requirements(self):
        if self.enable:
            if not all([self.type, self.name, self.user, self.password]):
                raise ValueError("If database is enabled, type, name, user, and password are required.")
            if self.container and self.host:
                raise ValueError("Database: set either 'container' or 'host', not both (they are mutually exclusive).")
        return self

class AuthConfig(_Model):
    method: Literal['key', 'password']
    key_path: Optional[str] = None
    password: Optional[str] = None

    @model_validator(mode='after')
    def check_auth_method(self):
        if self.method == 'key' and not self.key_path:
            raise ValueError("Auth method is 'key' but 'key_path' is missing.")
        if self.method == 'password' and not self.password:
            raise ValueError("Auth method is 'password' but 'password' is missing.")
        return self

class TargetConfig(_Model):
    type: Literal['local', 'remote']
    paths: List[str] = Field(default_factory=list)
    exclude: List[str] = Field(default_factory=list)
    database: Optional[DatabaseConfig] = Field(default_factory=lambda: DatabaseConfig(enable=False))
    
    host: Optional[str] = None
    user: Optional[str] = "root"
    port: int = 22
    auth: Optional[AuthConfig] = None

    @model_validator(mode='after')
    def check_remote_requirements(self):
        if self.type == 'remote':
            if not self.host:
                raise ValueError("Target type is 'remote' but 'host' is missing.")
            if not self.auth:
                raise ValueError("Target type is 'remote' but 'auth' configuration is missing.")
        plan_paths(self.paths)  # fail fast on unusable path entries / aliases
        return self

class TelegramConfig(_Model):
    enable: bool = False
    token: Optional[str] = None
    chat_id: Optional[str] = None
    topic_id: Optional[str] = None
    send_file: bool = True

    @model_validator(mode='after')
    def check_required(self):
        if self.enable and not (self.token and self.chat_id):
            raise ValueError("Telegram is enabled but 'token' and/or 'chat_id' is missing.")
        return self

class S3Config(_Model):
    enable: bool = False
    endpoint_url: Optional[str] = None
    bucket_name: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    region_name: Optional[str] = None
    generate_link: bool  = False
    prefix: Optional[str] = None                       # key prefix inside the bucket, e.g. "backsy/"
    addressing_style: Literal['auto', 'path', 'virtual'] = 'auto'   # path-style is needed by some providers (e.g. MinIO)

    @field_validator('prefix')
    @classmethod
    def normalize_prefix(cls, value):
        if value is None:
            return None
        value = value.strip().lstrip('/')
        if not value:
            return None
        return value if value.endswith('/') else value + '/'

    @model_validator(mode='after')
    def check_required(self):
        if self.enable and not all([self.bucket_name, self.access_key, self.secret_key]):
            raise ValueError("S3 is enabled but 'bucket_name', 'access_key' and/or 'secret_key' is missing.")
        return self

class ServicesConfig(_Model):
    telegram: Optional[TelegramConfig] = Field(default_factory=lambda: TelegramConfig(enable=False))
    # Any number of named S3 destinations: s3: {primary: {...}, offsite: {...}}
    s3: Dict[str, S3Config] = Field(default_factory=dict)

    @field_validator('s3', mode='before')
    @classmethod
    def normalize_s3(cls, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("'services.s3' must be a mapping of destination names to their settings.")
        if all(isinstance(entry, dict) for entry in value.values()):
            # Named destinations (also the empty map). An entry is enabled unless it says `enable: false`.
            return {name: {'enable': True, **entry} for name, entry in value.items()}
        if set(value) <= set(S3Config.model_fields):
            # Older format: one destination written directly under `s3:`. Keep it working as "default".
            return {'default': value}
        raise ValueError(
            "'services.s3' must be either a map of named destinations (s3: {primary: {...}, offsite: {...}}) "
            "or the older single-destination block; it looks like a mix of both."
        )

    @field_validator('s3')
    @classmethod
    def check_s3_destinations(cls, destinations):
        bad = [name for name in destinations if not _TARGET_NAME.match(name)]
        if bad:
            raise ValueError(
                f"Invalid S3 destination name(s): {bad}. Use 1-64 letters, digits, '.', '_' or '-', "
                f"starting with a letter or digit."
            )
        seen: dict[tuple, str] = {}
        for name, destination in destinations.items():
            if not destination.enable:
                continue
            key = (destination.endpoint_url or '', destination.bucket_name, destination.prefix or '')
            if key in seen:
                raise ValueError(f"S3 destinations '{seen[key]}' and '{name}' point to the same bucket and prefix.")
            seen[key] = name
        return destinations

class MonitoringConfig(_Model):
    healthcheck_url: Optional[str] = None

class CompressionConfig(_Model):
    algorithm: Literal['gzip', 'zstd'] = 'gzip'
    level: Optional[int] = None      # gzip: 1-9 (default 6) | zstd: 1-19 (default 3)
    threads: int = Field(default=0, ge=0)   # zstd only; 0 = use all cores

    @model_validator(mode='after')
    def check_level(self):
        if self.level is not None:
            low, high = (1, 9) if self.algorithm == 'gzip' else (1, 19)
            if not low <= self.level <= high:
                raise ValueError(f"compression.level for {self.algorithm} must be between {low} and {high}.")
        return self

class AppConfig(_Model):
    targets: Dict[str, TargetConfig]
    services: ServicesConfig
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)

    @field_validator('targets')
    @classmethod
    def check_target_names(cls, targets):
        bad = [name for name in targets if not _TARGET_NAME.match(name)]
        if bad:
            raise ValueError(
                f"Invalid target name(s): {bad}. Use 1-64 letters, digits, '.', '_' or '-', "
                f"starting with a letter or digit."
            )
        return targets

_PLACEHOLDER = re.compile(r'\$\{([^}]+)\}')

def _resolve_env_vars(config_data):
    if isinstance(config_data, dict):
        if config_data.get('enable') is False:
            return config_data  # disabled section: its placeholders are never used, don't require them
        for key, value in config_data.items():
            config_data[key] = _resolve_env_vars(value)
    elif isinstance(config_data, list):
        for i, item in enumerate(config_data):
            config_data[i] = _resolve_env_vars(item)
    elif isinstance(config_data, str):
        def _substitute(match):
            var_name = match.group(1)
            var_value = os.getenv(var_name)
            if var_value is None:
                error_msg = f"Environment variable '{var_name}' not found, but is required by the configuration."
                logger.critical(error_msg)
                raise ValueError(error_msg)
            return var_value
        return _PLACEHOLDER.sub(_substitute, config_data)

    return config_data

def _reject_removed_services(raw_config) -> None:
    """
    Cloudflare (Worker/KV) support was removed. Pydantic would silently ignore a leftover
    `services.cloudflare` section, and the user would believe backups still go there, so an
    enabled one is a hard error. A disabled leftover section is harmless and ignored.
    """
    services = raw_config.get('services') if isinstance(raw_config, dict) else None
    cloudflare = services.get('cloudflare') if isinstance(services, dict) else None
    if isinstance(cloudflare, dict) and cloudflare.get('enable'):
        message = (
            "Cloudflare (Worker/KV) support has been removed, but 'services.cloudflare' is still enabled in "
            "config.yml. Remove that section and use S3 and/or Telegram instead."
        )
        logger.critical(message)
        raise ValueError(message)

def load_config() -> AppConfig:
    logger.info(f"Loading configuration from '{CONFIG_PATH}'...")
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw_config = yaml.safe_load(f)
        
        if not raw_config:
            raise ValueError("Configuration file is empty or invalid.")

        _reject_removed_services(raw_config)

        resolved_config_data = _resolve_env_vars(raw_config)

        try:
            validated_config = AppConfig(**resolved_config_data)
            logger.info("Configuration loaded and validated successfully.")
            
            return validated_config
            
        except ValidationError as e:
            logger.critical("Configuration Validation Failed:")
            for error in e.errors():
                loc = " -> ".join(str(l) for l in error['loc'])
                logger.critical(f"  - Field: {loc} | Error: {error['msg']}")
            raise ValueError("Invalid configuration structure.")

    except FileNotFoundError:
        logger.error(f"Configuration file '{CONFIG_PATH}' not found.")
        raise
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file: {e}")
        raise