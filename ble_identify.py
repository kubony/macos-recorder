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


def parse_ble_events(events_file: Path) -> dict:
    """Parse events.jsonl and group BLE events by device name.
    Returns dict: device_name -> {rssi_values, first_seen_ts, last_seen_ts, scan_count}
    Skips devices named 'Unknown'.
    """
    devices = {}
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") != "bluetooth":
                continue
            name = event.get("name", "Unknown")
            if name == "Unknown":
                continue
            rssi = event.get("rssi")
            ts = event.get("ts", 0)
            if rssi is None:
                continue
            if name not in devices:
                devices[name] = {
                    "rssi_values": [],
                    "first_seen_ts": ts,
                    "last_seen_ts": ts,
                    "scan_count": 0,
                }
            devices[name]["rssi_values"].append(rssi)
            devices[name]["last_seen_ts"] = max(devices[name]["last_seen_ts"], ts)
            devices[name]["first_seen_ts"] = min(devices[name]["first_seen_ts"], ts)
            devices[name]["scan_count"] += 1
    return devices


RSSI_THRESHOLD = -70
MIN_DURATION_S = 120
MIN_DETECTION_RATIO = 0.6


def analyze_attendance(devices: dict, session_duration_s: float) -> dict:
    """Determine which devices were physically present.
    Returns dict: device_name -> {present, median_rssi, duration_s, detection_ratio}
    """
    expected_scans = max(session_duration_s / 5, 1)
    result = {}
    for name, info in devices.items():
        rssi_vals = info["rssi_values"]
        med_rssi = median(rssi_vals) if rssi_vals else -100
        duration_ns = info["last_seen_ts"] - info["first_seen_ts"]
        duration_s = duration_ns / 1_000_000_000
        detection_ratio = info["scan_count"] / expected_scans
        present = (
            med_rssi > RSSI_THRESHOLD
            and duration_s > MIN_DURATION_S
            and detection_ratio > MIN_DETECTION_RATIO
        )
        result[name] = {
            "present": present,
            "median_rssi": round(med_rssi, 1),
            "duration_s": round(duration_s, 1),
            "detection_ratio": round(detection_ratio, 2),
        }
    return result
