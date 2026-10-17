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

class CloudflareConfig(_Model):
    enable: bool = False
    worker_url: Optional[str] = None
    api_token: Optional[str] = None

    @model_validator(mode='after')
    def check_required(self):
        if self.enable and not self.worker_url:
            raise ValueError("Cloudflare is enabled but 'worker_url' is missing.")
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

    @model_validator(mode='after')
    def check_required(self):
        if self.enable and not all([self.bucket_name, self.access_key, self.secret_key]):
            raise ValueError("S3 is enabled but 'bucket_name', 'access_key' and/or 'secret_key' is missing.")
        return self

class ServicesConfig(_Model):
    cloudflare: Optional[CloudflareConfig] = Field(default_factory=lambda: CloudflareConfig(enable=False))
    telegram: Optional[TelegramConfig] = Field(default_factory=lambda: TelegramConfig(enable=False))
    s3: Optional[S3Config] = Field(default_factory=lambda: S3Config(enable=False))

class MonitoringConfig(_Model):
    healthcheck_url: Optional[str] = None

class AppConfig(_Model):
    targets: Dict[str, TargetConfig]
    services: ServicesConfig
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

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

def load_config() -> dict:
    logger.info(f"Loading configuration from '{CONFIG_PATH}'...")
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw_config = yaml.safe_load(f)
        
        if not raw_config:
            raise ValueError("Configuration file is empty or invalid.")

        resolved_config_data = _resolve_env_vars(raw_config)

        try:
            validated_config = AppConfig(**resolved_config_data)
            logger.info("Configuration loaded and validated successfully.")
            
            return validated_config.model_dump()
            
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