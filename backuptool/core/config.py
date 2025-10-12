import os
import re
import yaml
import logging

CONFIG_PATH = "config.yml"
logger = logging.getLogger(__name__)

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


def load_config():
    logger.info(f"Loading configuration from '{CONFIG_PATH}'...")
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        if not config:
            raise ValueError("Configuration file is empty or invalid.")

        resolved_config = _resolve_env_vars(config)
        logger.info("Configuration loaded and environment variables resolved successfully.")
        return resolved_config

    except FileNotFoundError:
        logger.error(f"Configuration file '{CONFIG_PATH}' not found.")
        raise
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file: {e}")
        raise