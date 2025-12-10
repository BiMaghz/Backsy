import os
import re
import yaml
import logging
from typing import List, Dict, Optional, Literal
from pydantic import BaseModel, Field, model_validator, ValidationError

CONFIG_PATH = "config.yml"
logger = logging.getLogger(__name__)

class DatabaseConfig(BaseModel):
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
        return self

class AuthConfig(BaseModel):
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

class TargetConfig(BaseModel):
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
        return self

class CloudflareConfig(BaseModel):
    enable: bool = False
    worker_url: Optional[str] = None
    api_token: Optional[str] = None

class TelegramConfig(BaseModel):
    enable: bool = False
    token: Optional[str] = None
    chat_id: Optional[str] = None
    topic_id: Optional[str] = None
    send_file: bool = True

class S3Config(BaseModel):
    enable: bool = False
    endpoint_url: Optional[str] = None
    bucket_name: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    region_name: Optional[str] = None
    generate_link: bool  = False

class ServicesConfig(BaseModel):
    cloudflare: Optional[CloudflareConfig] = Field(default_factory=lambda: CloudflareConfig(enable=False))
    telegram: Optional[TelegramConfig] = Field(default_factory=lambda: TelegramConfig(enable=False))
    s3: Optional[S3Config] = Field(default_factory=lambda: S3Config(enable=False))

class MonitoringConfig(BaseModel):
    healthcheck_url: Optional[str] = None

class AppConfig(BaseModel):
    targets: Dict[str, TargetConfig]
    services: ServicesConfig
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

def _resolve_env_vars(config_data):
    if isinstance(config_data, dict):
        for key, value in config_data.items():
            config_data[key] = _resolve_env_vars(value)
    elif isinstance(config_data, list):
        for i, item in enumerate(config_data):
            config_data[i] = _resolve_env_vars(item)
    elif isinstance(config_data, str):
        placeholder_pattern = re.compile(r'\$\{([^}]+)\}')
        match = placeholder_pattern.search(config_data)
        
        if match:
            var_name = match.group(1)
            var_value = os.getenv(var_name)
            if var_value is None:
                error_msg = f"Environment variable '{var_name}' not found, but is required by the configuration."
                logger.critical(error_msg)
                raise ValueError(error_msg)
            return config_data.replace(match.group(0), var_value)
            
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