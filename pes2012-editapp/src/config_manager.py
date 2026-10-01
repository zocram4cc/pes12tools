"""Minimal config store (RigcamLauncherNG pattern): YAML file with defaults."""
import os

import yaml
from pathlib import Path


class ConfigManager:
    def __init__(self, app_name="PES12EditApp"):
        if os.name == 'nt':
            self.config_path = Path(os.getenv('APPDATA')) / app_name
        else:
            self.config_path = Path.home() / '.config' / app_name
        self.config_file = self.config_path / 'config.yaml'
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.defaults = {'last_dir': os.path.expanduser('~'), 'game_dir': ''}
        self.config = self.load_config()

    def load_config(self):
        if not self.config_file.is_file():
            self.save_config(self.defaults)
            return dict(self.defaults)
        try:
            with open(self.config_file) as f:
                config = dict(self.defaults)
                config.update(yaml.safe_load(f) or {})
                return config
        except (yaml.YAMLError, IOError):
            return dict(self.defaults)

    def save_config(self, config):
        with open(self.config_file, 'w') as f:
            yaml.safe_dump(config, f)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save_config(self.config)
