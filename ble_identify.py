#!/usr/bin/env python3
"""BLE-based meeting participant identification tool."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from statistics import median


DEFAULT_CONFIG_DIR = Path.home() / ".macos-recorder"
REGISTRY_FILE = "device_registry.json"


class DeviceRegistry:
    """Manages BLE device-to-person mappings."""

    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / REGISTRY_FILE
        self.devices: dict = {}
        self.people: dict = {}
        self.ambient: list = []
        self._alias_index: dict = {}
        if self.path.exists():
            self._load()

    def _load(self):
        data = json.loads(self.path.read_text())
        self.devices = data.get("devices", {})
        self.people = data.get("people", {})
        self.ambient = data.get("ambient", [])
        self._build_alias_index()

    def _build_alias_index(self):
        self._alias_index = {}
        for name, info in self.devices.items():
            for alias in info.get("aliases", []):
                self._alias_index[alias] = name

    def save(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": self.devices,
            "people": self.people,
            "ambient": self.ambient,
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def lookup(self, device_name: str) -> dict | None:
        if device_name in self.devices:
            return self.devices[device_name]
        # Check pre-built alias index first
        canonical = self._alias_index.get(device_name)
        if canonical:
            return self.devices[canonical]
        # Fall back to scanning in case devices were mutated after construction
        for name, info in self.devices.items():
            if device_name in info.get("aliases", []):
                return info
        return None

    def is_ambient(self, device_name: str) -> bool:
        return device_name in self.ambient

    def migrate(self, owners_file: Path):
        data = json.loads(owners_file.read_text())
        owners = data.get("device_owners", {})
        today = date.today().isoformat()

        for category in ["certain", "high", "medium"]:
            for dev in owners.get(category, []):
                name = dev["device_name"]
                owner = dev.get("owner", "")
                affiliation = dev.get("affiliation", "")
                confidence = dev.get("confidence", category)

                self.devices[name] = {
                    "owner": owner,
                    "confidence": confidence,
                    "aliases": [dev["alias"]] if dev.get("alias") else [],
                    "first_seen": today,
                    "session_count": 0,
                }

                if owner and owner not in self.people:
                    self.people[owner] = {
                        "affiliation": affiliation,
                        "devices": [],
                    }
                if owner:
                    if name not in self.people[owner]["devices"]:
                        self.people[owner]["devices"].append(name)

        for dev in owners.get("unresolved", []):
            name = dev["device_name"]
            self.devices[name] = {
                "owner": dev.get("candidate", ""),
                "confidence": "low",
                "aliases": [],
                "first_seen": today,
                "session_count": 0,
            }

        for dev in owners.get("office_ambient", []):
            name = dev["device_name"]
            if name not in self.ambient:
                self.ambient.append(name)

        self._build_alias_index()
        self.save()
