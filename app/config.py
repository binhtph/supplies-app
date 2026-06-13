import json
import os
from pathlib import Path
from typing import Dict, Any

CONFIG_FILE = Path("data/config.json")

DEFAULT_CONFIG = {
    "auth": {
        "username": "admin",
        "password_hash": ""  # Will be generated on first run
    }
}

class Config:
    def __init__(self):
        self.config = self.load()
    
    def load(self) -> Dict[str, Any]:
        """Load config from file, with env var priority for auth"""
        config = DEFAULT_CONFIG.copy()
        
        # Load auth from environment variables
        if os.getenv("AUTH_USERNAME"):
            config["auth"]["username"] = os.getenv("AUTH_USERNAME")
        
        # Load from config file (if exists)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    file_config = json.load(f)
                for section, values in file_config.items():
                    if section in config:
                        config[section].update(values)
                return config
            except Exception as e:
                print(f"Error loading config file: {e}")
                return config
        else:
            self.config = config
            self.save()
            return config
    
    def save(self, config: Dict[str, Any] = None):
        """Save config to file"""
        if config:
            self.config = config
        
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get(self, section: str = None):
        """Get config section or entire config"""
        if section:
            return self.config.get(section, {})
        return self.config
    
    def update(self, section: str, data: Dict[str, Any]):
        """Update a config section"""
        if section in self.config:
            self.config[section].update(data)
            self.save()
        else:
            print(f"Config section '{section}' not found")

config = Config()
