# BLE Participant Identification Tool - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** CLI tool that analyzes BLE data from recording sessions to identify meeting participants, with interactive confirmation and automatic device-person mapping learning.

**Architecture:** Single Python file (`ble_identify.py`) with argparse subcommands. Device registry at `~/.macos-recorder/device_registry.json`. Reads `events.jsonl` from session dirs, writes `participants.json`. Standard library only.

**Tech Stack:** Python 3, argparse, json, statistics, pathlib (stdlib only)

---

### Task 1: Device Registry Data Layer

**Files:**
- Create: `ble_identify.py`
- Create: `tests/test_ble_identify.py`

**Step 1: Write failing tests for DeviceRegistry**

```python
# tests/test_ble_identify.py
import json
import pytest
from pathlib import Path

# We'll import after creating the module
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestDeviceRegistry:
    """Tests for DeviceRegistry load/save/migrate."""

    def test_load_empty_registry(self, tmp_path):
        """Empty dir should return default empty registry."""
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        assert reg.devices == {}
        assert reg.people == {}
        assert reg.ambient == []

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save then load should preserve data."""
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.devices["Galaxy Buds3 Pro (29A7)"] = {
            "owner": "서인근",
            "confidence": "certain",
            "aliases": ["Galaxy Buds3 Pro (7C68)"],
            "first_seen": "2026-02-23",
            "session_count": 8,
        }
        reg.people["서인근"] = {
            "affiliation": "마음AI WoRV",
            "devices": ["Galaxy Buds3 Pro (29A7)"],
        }
        reg.ambient.append("WH-CH720N")
        reg.save()

        reg2 = DeviceRegistry(config_dir=tmp_path)
        assert reg2.devices["Galaxy Buds3 Pro (29A7)"]["owner"] == "서인근"
        assert reg2.people["서인근"]["affiliation"] == "마음AI WoRV"
        assert "WH-CH720N" in reg2.ambient

    def test_lookup_device_by_name(self, tmp_path):
        """lookup() should find device by name or alias."""
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.devices["Galaxy Buds3 Pro (29A7)"] = {
            "owner": "서인근",
            "confidence": "certain",
            "aliases": ["Galaxy Buds3 Pro (7C68)"],
            "first_seen": "2026-02-23",
            "session_count": 8,
        }
        # Direct name lookup
        result = reg.lookup("Galaxy Buds3 Pro (29A7)")
        assert result["owner"] == "서인근"
        # Alias lookup
        result = reg.lookup("Galaxy Buds3 Pro (7C68)")
        assert result["owner"] == "서인근"
        # Unknown device
        assert reg.lookup("Unknown Device") is None

    def test_is_ambient(self, tmp_path):
        """is_ambient() should detect ambient devices."""
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.ambient = ["WH-CH720N", "LE-PurpleSand's QC45"]
        assert reg.is_ambient("WH-CH720N") is True
        assert reg.is_ambient("Galaxy Buds3 Pro") is False

    def test_migrate_from_ble_device_owners(self, tmp_path):
        """migrate() should import ble_device_owners.json format."""
        from ble_identify import DeviceRegistry
        owners_file = tmp_path / "ble_device_owners.json"
        owners_file.write_text(json.dumps({
            "device_owners": {
                "certain": [
                    {
                        "device_name": "Inkeun의 iPhone",
                        "owner": "서인근",
                        "affiliation": "마음AI WoRV",
                        "confidence": "certain",
                        "reason": "이름에 Inkeun 포함",
                    }
                ],
                "high": [],
                "medium": [],
                "unresolved": [],
                "office_ambient": [
                    {
                        "device_name": "WH-CH720N",
                        "owner": "사무실 동료",
                        "reason": "사무실 세션에서 지속 탐지",
                    }
                ],
            },
        }))
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.migrate(owners_file)
        assert reg.devices["Inkeun의 iPhone"]["owner"] == "서인근"
        assert "WH-CH720N" in reg.ambient
        assert "서인근" in reg.people
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'ble_identify'`

**Step 3: Implement DeviceRegistry**

```python
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
        """Build reverse index: alias -> canonical device name."""
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
        """Find device info by name or alias. Returns device dict or None."""
        if device_name in self.devices:
            return self.devices[device_name]
        canonical = self._alias_index.get(device_name)
        if canonical:
            return self.devices[canonical]
        return None

    def is_ambient(self, device_name: str) -> bool:
        return device_name in self.ambient

    def migrate(self, owners_file: Path):
        """Import from ble_device_owners.json format."""
        data = json.loads(owners_file.read_text())
        owners = data.get("device_owners", {})
        today = date.today().isoformat()

        # Import categorized devices
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

        # Import unresolved with candidate
        for dev in owners.get("unresolved", []):
            name = dev["device_name"]
            self.devices[name] = {
                "owner": dev.get("candidate", ""),
                "confidence": "low",
                "aliases": [],
                "first_seen": today,
                "session_count": 0,
            }

        # Import ambient
        for dev in owners.get("office_ambient", []):
            name = dev["device_name"]
            if name not in self.ambient:
                self.ambient.append(name)

        self._build_alias_index()
        self.save()
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add ble_identify.py tests/test_ble_identify.py
git commit -m "feat: add DeviceRegistry for BLE device-person mappings"
```

---

### Task 2: BLE Event Parser & Attendance Analysis

**Files:**
- Modify: `ble_identify.py` (add `parse_ble_events` and `analyze_attendance`)
- Modify: `tests/test_ble_identify.py` (add parser/analysis tests)

**Step 1: Write failing tests**

```python
# Add to tests/test_ble_identify.py

class TestBLEParser:
    """Tests for BLE event parsing from events.jsonl."""

    def test_parse_ble_events(self, tmp_path):
        """Should extract bluetooth events, skip others."""
        from ble_identify import parse_ble_events
        events_file = tmp_path / "events.jsonl"
        events_file.write_text("\n".join([
            json.dumps({"ts": 1000, "type": "recording", "action": "start"}),
            json.dumps({"ts": 1001, "type": "bluetooth", "name": "Device A", "rssi": -50, "address": "AA:BB"}),
            json.dumps({"ts": 1002, "type": "bluetooth", "name": "Unknown", "rssi": -60, "address": "CC:DD"}),
            json.dumps({"ts": 1003, "type": "bluetooth", "name": "Device A", "rssi": -55, "address": "AA:BB"}),
            json.dumps({"ts": 1004, "type": "context", "wifi": {}}),
            json.dumps({"ts": 1005, "type": "bluetooth", "name": "Device B", "rssi": -70, "address": "EE:FF"}),
        ]))
        result = parse_ble_events(events_file)
        # Should have 2 named devices (Unknown excluded)
        assert "Device A" in result
        assert "Device B" in result
        assert "Unknown" not in result
        # Device A should have 2 readings
        assert len(result["Device A"]["rssi_values"]) == 2
        assert result["Device A"]["rssi_values"] == [-50, -55]

    def test_parse_session_timestamps(self, tmp_path):
        """Should capture first and last timestamps."""
        from ble_identify import parse_ble_events
        events_file = tmp_path / "events.jsonl"
        events_file.write_text("\n".join([
            json.dumps({"ts": 1000000000000, "type": "recording", "action": "start"}),
            json.dumps({"ts": 1001000000000, "type": "bluetooth", "name": "Dev", "rssi": -50, "address": "AA"}),
            json.dumps({"ts": 1060000000000, "type": "bluetooth", "name": "Dev", "rssi": -50, "address": "AA"}),
            json.dumps({"ts": 1120000000000, "type": "recording", "action": "stop"}),
        ]))
        result = parse_ble_events(events_file)
        assert result["Dev"]["first_seen_ts"] == 1001000000000
        assert result["Dev"]["last_seen_ts"] == 1060000000000


class TestAttendanceAnalysis:
    """Tests for attendance determination logic."""

    def test_strong_signal_device_is_present(self, tmp_path):
        """Device with strong RSSI and sufficient duration should be present."""
        from ble_identify import analyze_attendance
        devices = {
            "Strong Device": {
                "rssi_values": [-50, -55, -52, -48, -53] * 20,
                "first_seen_ts": 1000000000000,
                "last_seen_ts": 1000000000000 + 600 * 1_000_000_000,  # 10 min
                "scan_count": 100,
            }
        }
        session_duration_s = 600  # 10 min
        result = analyze_attendance(devices, session_duration_s)
        assert "Strong Device" in result
        assert result["Strong Device"]["present"] is True

    def test_weak_signal_device_not_present(self, tmp_path):
        """Device with weak RSSI should not be present."""
        from ble_identify import analyze_attendance
        devices = {
            "Weak Device": {
                "rssi_values": [-85, -90, -88],
                "first_seen_ts": 1000000000000,
                "last_seen_ts": 1000000000000 + 600 * 1_000_000_000,
                "scan_count": 3,
            }
        }
        result = analyze_attendance(devices, 600)
        assert result["Weak Device"]["present"] is False

    def test_brief_device_not_present(self, tmp_path):
        """Device seen for less than 2 minutes should not be present."""
        from ble_identify import analyze_attendance
        devices = {
            "Brief Device": {
                "rssi_values": [-50, -52],
                "first_seen_ts": 1000000000000,
                "last_seen_ts": 1000000000000 + 60 * 1_000_000_000,  # 1 min
                "scan_count": 2,
            }
        }
        result = analyze_attendance(devices, 600)
        assert result["Brief Device"]["present"] is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py::TestBLEParser -v`
Expected: FAIL - `ImportError: cannot import name 'parse_ble_events'`

**Step 3: Implement parser and analysis**

Add to `ble_identify.py` after `DeviceRegistry` class:

```python
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


# Attendance thresholds (from ROADMAP.md)
RSSI_THRESHOLD = -70       # dBm, median RSSI must be stronger
MIN_DURATION_S = 120       # seconds, must be present > 2 min
MIN_DETECTION_RATIO = 0.6  # must be detected > 60% of session scans


def analyze_attendance(devices: dict, session_duration_s: float) -> dict:
    """Determine which devices were physically present.

    Returns dict: device_name -> {present, median_rssi, duration_s, detection_ratio}
    """
    # Estimate total possible scans (1 scan per ~2 seconds)
    expected_scans = max(session_duration_s / 2, 1)

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
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py -v`
Expected: All tests PASS (6 registry + 5 parser/analysis)

**Step 5: Commit**

```bash
git add ble_identify.py tests/test_ble_identify.py
git commit -m "feat: add BLE event parser and attendance analysis"
```

---

### Task 3: Analyze Command (Core Feature)

**Files:**
- Modify: `ble_identify.py` (add `cmd_analyze` function and main/argparse)
- Modify: `tests/test_ble_identify.py`

**Step 1: Write failing test for analyze pipeline**

```python
# Add to tests/test_ble_identify.py

class TestAnalyzePipeline:
    """Tests for the full analyze pipeline (non-interactive)."""

    def _make_session(self, tmp_path, devices_data):
        """Helper: create a fake session directory with events.jsonl."""
        session = tmp_path / "recording_20260227_100000"
        session.mkdir()
        lines = []
        # recording start
        lines.append(json.dumps({"ts": 1000000000000, "type": "recording", "action": "start"}))
        for dev_name, rssi, count in devices_data:
            for i in range(count):
                ts = 1000000000000 + (i * 3_000_000_000)  # every 3 sec
                lines.append(json.dumps({
                    "ts": ts,
                    "type": "bluetooth",
                    "name": dev_name,
                    "rssi": rssi,
                    "address": f"ADDR-{dev_name}",
                }))
        # recording stop (10 min session)
        lines.append(json.dumps({"ts": 1000000000000 + 600_000_000_000, "type": "recording", "action": "stop"}))
        (session / "events.jsonl").write_text("\n".join(lines))
        return session

    def test_analyze_with_known_device(self, tmp_path):
        """Known device in registry should auto-identify participant."""
        from ble_identify import DeviceRegistry, cmd_analyze

        # Setup registry
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reg = DeviceRegistry(config_dir=config_dir)
        reg.devices["Inkeun의 iPhone"] = {
            "owner": "서인근",
            "confidence": "certain",
            "aliases": [],
            "first_seen": "2026-02-23",
            "session_count": 5,
        }
        reg.people["서인근"] = {"affiliation": "마음AI WoRV", "devices": ["Inkeun의 iPhone"]}
        reg.ambient = ["WH-CH720N"]
        reg.save()

        # Create session with known + ambient + unknown devices
        session = self._make_session(tmp_path, [
            ("Inkeun의 iPhone", -45, 100),  # strong, known
            ("WH-CH720N", -60, 100),          # ambient, should be ignored
            ("New Device", -55, 100),          # unknown, strong
        ])

        result = cmd_analyze(session, config_dir=config_dir, interactive=False)

        # 서인근 should be auto-identified
        names = [p["name"] for p in result["participants"]]
        assert "서인근" in names

        # WH-CH720N should not appear as participant
        for p in result["participants"]:
            assert p["name"] != "WH-CH720N"

        # New Device should be in candidates
        candidate_devices = [c["device_name"] for c in result["candidates"]]
        assert "New Device" in candidate_devices

    def test_analyze_saves_participants_json(self, tmp_path):
        """analyze --yes should save participants.json."""
        from ble_identify import DeviceRegistry, cmd_analyze

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reg = DeviceRegistry(config_dir=config_dir)
        reg.devices["Inkeun의 iPhone"] = {
            "owner": "서인근",
            "confidence": "certain",
            "aliases": [],
            "first_seen": "2026-02-23",
            "session_count": 5,
        }
        reg.people["서인근"] = {"affiliation": "마음AI WoRV", "devices": ["Inkeun의 iPhone"]}
        reg.save()

        session = self._make_session(tmp_path, [
            ("Inkeun의 iPhone", -45, 100),
        ])

        cmd_analyze(session, config_dir=config_dir, interactive=False, save=True)

        pfile = session / "participants.json"
        assert pfile.exists()
        data = json.loads(pfile.read_text())
        assert data["participants"][0]["name"] == "서인근"
        assert data["participants"][0]["source"] == "ble"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py::TestAnalyzePipeline -v`
Expected: FAIL - `ImportError: cannot import name 'cmd_analyze'`

**Step 3: Implement cmd_analyze**

Add to `ble_identify.py`:

```python
def cmd_analyze(session_path: Path, config_dir: Path = DEFAULT_CONFIG_DIR,
                interactive: bool = True, save: bool = False) -> dict:
    """Analyze a session's BLE data to identify participants.

    Returns: {participants: [...], candidates: [...]}
    """
    session_path = Path(session_path)
    events_file = session_path / "events.jsonl"
    if not events_file.exists():
        print(f"Error: {events_file} not found", file=sys.stderr)
        return {"participants": [], "candidates": []}

    registry = DeviceRegistry(config_dir=config_dir)

    # Parse BLE events
    devices = parse_ble_events(events_file)
    if not devices:
        print("No named BLE devices found in session.")
        return {"participants": [], "candidates": []}

    # Get session duration from recording start/stop events
    session_duration_s = _get_session_duration(events_file)

    # Analyze attendance
    attendance = analyze_attendance(devices, session_duration_s)

    # Classify into: confirmed participants, candidates, ambient
    participants = []
    candidates = []

    for dev_name, att_info in attendance.items():
        if not att_info["present"]:
            continue
        if registry.is_ambient(dev_name):
            continue

        device_info = registry.lookup(dev_name)
        if device_info and device_info["owner"]:
            owner = device_info["owner"]
            person_info = registry.people.get(owner, {})
            # Avoid duplicate participants (same person, multiple devices)
            if not any(p["name"] == owner for p in participants):
                participants.append({
                    "name": owner,
                    "affiliation": person_info.get("affiliation", ""),
                    "source": "ble",
                    "ble_devices": [dev_name],
                    "median_rssi": att_info["median_rssi"],
                })
            else:
                for p in participants:
                    if p["name"] == owner:
                        p["ble_devices"].append(dev_name)
        else:
            # Unknown device — suggest as candidate
            suggestion = _suggest_owner(dev_name, device_info)
            candidates.append({
                "device_name": dev_name,
                "median_rssi": att_info["median_rssi"],
                "duration_s": att_info["duration_s"],
                "suggested_owner": suggestion,
            })

    result = {"participants": participants, "candidates": candidates}

    if interactive:
        result = _interactive_confirm(result, registry, session_path)

    if save or interactive:
        _save_participants(session_path, result)
        # Update registry with confirmed participants
        _update_registry(registry, result, devices)

    return result


def _get_session_duration(events_file: Path) -> float:
    """Get session duration in seconds from recording start/stop events."""
    start_ts = None
    stop_ts = None
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "recording":
                if event.get("action") == "start" and start_ts is None:
                    start_ts = event["ts"]
                elif event.get("action") == "stop":
                    stop_ts = event["ts"]
    if start_ts and stop_ts:
        return (stop_ts - start_ts) / 1_000_000_000
    # Fallback: estimate from first/last event
    first_ts = None
    last_ts = None
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            ts = event.get("ts", 0)
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    if first_ts and last_ts:
        return (last_ts - first_ts) / 1_000_000_000
    return 600  # default 10 min


def _suggest_owner(dev_name: str, device_info: dict | None) -> str:
    """Suggest a likely owner based on device name patterns."""
    if device_info and device_info.get("owner"):
        return device_info["owner"]
    # Check for Korean possessive patterns like "xxx의 iPhone"
    if "의 " in dev_name:
        return dev_name.split("의 ")[0]
    return ""


def _save_participants(session_path: Path, result: dict):
    """Save participants.json to session directory."""
    out = {
        "participants": [
            {k: v for k, v in p.items() if k != "median_rssi"}
            for p in result["participants"]
        ]
    }
    pfile = session_path / "participants.json"
    # If file exists, preserve meeting_title and meeting_note
    if pfile.exists():
        existing = json.loads(pfile.read_text())
        if "meeting_title" in existing:
            out["meeting_title"] = existing["meeting_title"]
        if "meeting_note" in existing:
            out["meeting_note"] = existing["meeting_note"]
    pfile.write_text(json.dumps(out, ensure_ascii=False, indent=2))


def _update_registry(registry: DeviceRegistry, result: dict, devices: dict):
    """Update registry with confirmed participants (auto-learning)."""
    today = date.today().isoformat()
    changed = False

    for p in result["participants"]:
        source = p.get("source", "manual")
        if source not in ("ble", "confirmed", "manual"):
            continue
        owner = p["name"]
        affiliation = p.get("affiliation", "")
        ble_devices = p.get("ble_devices", [])

        # Ensure person exists in registry
        if owner not in registry.people:
            registry.people[owner] = {"affiliation": affiliation, "devices": []}
            changed = True
        elif affiliation and not registry.people[owner].get("affiliation"):
            registry.people[owner]["affiliation"] = affiliation
            changed = True

        # Update device mappings
        for dev_name in ble_devices:
            if dev_name in registry.devices:
                entry = registry.devices[dev_name]
                entry["session_count"] = entry.get("session_count", 0) + 1
                entry["last_seen"] = today
                # Upgrade confidence based on session count
                count = entry["session_count"]
                if count >= 5:
                    entry["confidence"] = "certain"
                elif count >= 3:
                    entry["confidence"] = "high"
                elif count >= 1:
                    entry["confidence"] = "medium"
                changed = True
            else:
                registry.devices[dev_name] = {
                    "owner": owner,
                    "confidence": "medium" if source == "confirmed" else "low",
                    "aliases": [],
                    "first_seen": today,
                    "session_count": 1,
                }
                changed = True

            # Link device to person
            if dev_name not in registry.people[owner]["devices"]:
                registry.people[owner]["devices"].append(dev_name)
                changed = True

    if changed:
        registry.save()
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add ble_identify.py tests/test_ble_identify.py
git commit -m "feat: add analyze command with auto-learning"
```

---

### Task 4: Interactive Confirmation UX

**Files:**
- Modify: `ble_identify.py` (add `_interactive_confirm`)

**Step 1: Implement interactive confirmation**

This is a UI function, so test manually rather than unit test.

Add to `ble_identify.py`:

```python
def _interactive_confirm(result: dict, registry: DeviceRegistry, session_path: Path) -> dict:
    """Interactive confirmation of analysis results."""
    print(f"\n[BLE 참석자 분석] {session_path.name}")
    print("─" * 50)

    confirmed = list(result["participants"])
    candidates = list(result["candidates"])

    # Show confirmed participants
    if confirmed:
        print("\n✓ 확정 참석자 (device registry 기반):")
        for i, p in enumerate(confirmed, 1):
            devices_str = ", ".join(p.get("ble_devices", []))
            rssi = p.get("median_rssi", "?")
            print(f"  {i}. {p['name']} ({p.get('affiliation', '')}) — {devices_str} [{rssi}dBm]")

    # Show and confirm candidates
    if candidates:
        print("\n? 미확인 장치 (근접 감지됨):")
        for c in candidates:
            suggestion = c.get("suggested_owner", "")
            rssi = c["median_rssi"]
            prompt_text = f"  {c['device_name']} [{rssi}dBm]"
            if suggestion:
                prompt_text += f" → {suggestion}?"

            print(prompt_text)
            try:
                answer = input("    확인 [Y/n/이름 입력]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if answer == "" or answer.lower() == "y":
                name = suggestion if suggestion else ""
                if not name:
                    try:
                        name = input("    이름: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                if name:
                    affiliation = ""
                    person_info = registry.people.get(name, {})
                    if person_info:
                        affiliation = person_info.get("affiliation", "")
                    else:
                        try:
                            affiliation = input(f"    {name} 소속: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            print()
                    if not any(p["name"] == name for p in confirmed):
                        confirmed.append({
                            "name": name,
                            "affiliation": affiliation,
                            "source": "confirmed",
                            "ble_devices": [c["device_name"]],
                        })
                    else:
                        for p in confirmed:
                            if p["name"] == name:
                                p.setdefault("ble_devices", []).append(c["device_name"])
                                if p["source"] == "ble":
                                    p["source"] = "confirmed"
            elif answer.lower() == "n":
                continue
            else:
                # User typed a name directly
                name = answer
                person_info = registry.people.get(name, {})
                affiliation = person_info.get("affiliation", "")
                if not affiliation:
                    try:
                        affiliation = input(f"    {name} 소속: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                if not any(p["name"] == name for p in confirmed):
                    confirmed.append({
                        "name": name,
                        "affiliation": affiliation,
                        "source": "confirmed",
                        "ble_devices": [c["device_name"]],
                    })

    # Ask for additional participants
    print("\n? 추가 참석자 (빈 줄로 종료):")
    while True:
        try:
            name = input("  이름: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not name:
            break
        if any(p["name"] == name for p in confirmed):
            print(f"    '{name}' 이미 목록에 있습니다.")
            continue
        person_info = registry.people.get(name, {})
        affiliation = person_info.get("affiliation", "")
        if not affiliation:
            try:
                affiliation = input(f"  {name} 소속: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
        confirmed.append({
            "name": name,
            "affiliation": affiliation,
            "source": "manual",
        })

    return {"participants": confirmed, "candidates": []}
```

**Step 2: Manual test**

Run: `cd /Users/inkeun/projects/macos-recorder && python ble_identify.py analyze ~/Recordings/recording_20260225_092305 --yes`
Expected: Should print analysis and save participants.json (non-interactive mode first)

**Step 3: Commit**

```bash
git add ble_identify.py
git commit -m "feat: add interactive confirmation for BLE analysis"
```

---

### Task 5: CRUD Subcommands + Main Entry Point

**Files:**
- Modify: `ble_identify.py` (add list/add/remove/edit/registry/migrate commands + argparse main)
- Modify: `tests/test_ble_identify.py`

**Step 1: Write failing tests for CRUD**

```python
# Add to tests/test_ble_identify.py

class TestCRUD:
    """Tests for list/add/remove/edit commands."""

    def _make_session_with_participants(self, tmp_path):
        """Create session with existing participants.json."""
        session = tmp_path / "recording_test"
        session.mkdir()
        (session / "participants.json").write_text(json.dumps({
            "meeting_title": "Test Meeting",
            "participants": [
                {"name": "서인근", "affiliation": "마음AI WoRV", "source": "ble"},
                {"name": "김다혜", "affiliation": "마음AI WoRV", "source": "confirmed"},
            ]
        }, ensure_ascii=False))
        return session

    def test_cmd_list(self, tmp_path):
        """list command should return current participants."""
        from ble_identify import cmd_list
        session = self._make_session_with_participants(tmp_path)
        result = cmd_list(session)
        assert len(result) == 2
        assert result[0]["name"] == "서인근"

    def test_cmd_add(self, tmp_path):
        """add command should append a participant."""
        from ble_identify import cmd_add, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_add(session, "박유빈", affiliation="마음AI WoRV 2본부")
        result = cmd_list(session)
        assert len(result) == 3
        assert any(p["name"] == "박유빈" for p in result)

    def test_cmd_add_duplicate(self, tmp_path):
        """add duplicate name should not create second entry."""
        from ble_identify import cmd_add, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_add(session, "서인근", affiliation="마음AI")
        result = cmd_list(session)
        assert len(result) == 2  # No duplicate

    def test_cmd_remove(self, tmp_path):
        """remove command should delete a participant."""
        from ble_identify import cmd_remove, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_remove(session, "김다혜")
        result = cmd_list(session)
        assert len(result) == 1
        assert result[0]["name"] == "서인근"

    def test_cmd_edit(self, tmp_path):
        """edit command should update participant fields."""
        from ble_identify import cmd_edit, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_edit(session, "김다혜", new_name="김다혜(DH)", affiliation="WoRV 개발팀")
        result = cmd_list(session)
        dh = next(p for p in result if p["name"] == "김다혜(DH)")
        assert dh["affiliation"] == "WoRV 개발팀"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py::TestCRUD -v`
Expected: FAIL

**Step 3: Implement CRUD commands and main**

Add to `ble_identify.py`:

```python
def cmd_list(session_path: Path) -> list:
    """List participants in a session."""
    session_path = Path(session_path)
    pfile = session_path / "participants.json"
    if not pfile.exists():
        print(f"No participants.json in {session_path.name}")
        return []
    data = json.loads(pfile.read_text())
    participants = data.get("participants", [])
    for i, p in enumerate(participants, 1):
        source_icon = {"ble": "📡", "confirmed": "✓", "manual": "✎"}.get(p.get("source", ""), "?")
        print(f"  {i}. {source_icon} {p['name']} ({p.get('affiliation', '')})")
    return participants


def cmd_add(session_path: Path, name: str, affiliation: str = ""):
    """Add a participant to a session."""
    session_path = Path(session_path)
    pfile = session_path / "participants.json"
    if pfile.exists():
        data = json.loads(pfile.read_text())
    else:
        data = {"participants": []}

    # Check duplicate
    if any(p["name"] == name for p in data["participants"]):
        print(f"'{name}' already exists.")
        return

    data["participants"].append({
        "name": name,
        "affiliation": affiliation,
        "source": "manual",
    })
    pfile.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✓ Added: {name}")


def cmd_remove(session_path: Path, name: str):
    """Remove a participant from a session."""
    session_path = Path(session_path)
    pfile = session_path / "participants.json"
    if not pfile.exists():
        print(f"No participants.json in {session_path.name}")
        return
    data = json.loads(pfile.read_text())
    before = len(data["participants"])
    data["participants"] = [p for p in data["participants"] if p["name"] != name]
    if len(data["participants"]) == before:
        print(f"'{name}' not found.")
        return
    pfile.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✓ Removed: {name}")


def cmd_edit(session_path: Path, name: str, new_name: str = None, affiliation: str = None):
    """Edit a participant's info."""
    session_path = Path(session_path)
    pfile = session_path / "participants.json"
    if not pfile.exists():
        print(f"No participants.json in {session_path.name}")
        return
    data = json.loads(pfile.read_text())
    found = False
    for p in data["participants"]:
        if p["name"] == name:
            if new_name:
                p["name"] = new_name
            if affiliation is not None:
                p["affiliation"] = affiliation
            found = True
            break
    if not found:
        print(f"'{name}' not found.")
        return
    pfile.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✓ Updated: {new_name or name}")


def cmd_registry(config_dir: Path = DEFAULT_CONFIG_DIR):
    """Show device registry contents."""
    registry = DeviceRegistry(config_dir=config_dir)
    if not registry.devices and not registry.people:
        print("Device registry is empty. Run 'migrate' to import ble_device_owners.json.")
        return

    print("\n[Device Registry]")
    print("─" * 50)

    if registry.people:
        print("\n👤 People:")
        for name, info in registry.people.items():
            aff = info.get("affiliation", "")
            devices = info.get("devices", [])
            print(f"  {name} ({aff})")
            for d in devices:
                dev_info = registry.devices.get(d, {})
                conf = dev_info.get("confidence", "?")
                count = dev_info.get("session_count", 0)
                print(f"    └ {d} [{conf}, {count} sessions]")

    if registry.ambient:
        print(f"\n🏢 Ambient ({len(registry.ambient)}):")
        for d in registry.ambient:
            print(f"  - {d}")


def cmd_migrate(owners_file: Path, config_dir: Path = DEFAULT_CONFIG_DIR):
    """Migrate from ble_device_owners.json to device_registry.json."""
    owners_file = Path(owners_file)
    if not owners_file.exists():
        print(f"Error: {owners_file} not found", file=sys.stderr)
        return
    registry = DeviceRegistry(config_dir=config_dir)
    registry.migrate(owners_file)
    n_devices = len(registry.devices)
    n_people = len(registry.people)
    n_ambient = len(registry.ambient)
    print(f"✓ Migrated: {n_devices} devices, {n_people} people, {n_ambient} ambient")


def main():
    parser = argparse.ArgumentParser(
        prog="ble-id",
        description="BLE-based meeting participant identification",
    )
    sub = parser.add_subparsers(dest="command")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze BLE data to identify participants")
    p_analyze.add_argument("session", type=Path, help="Session directory path")
    p_analyze.add_argument("--yes", action="store_true", help="Non-interactive: accept all suggestions")
    p_analyze.add_argument("--json", action="store_true", help="Output JSON")

    # list
    p_list = sub.add_parser("list", help="List session participants")
    p_list.add_argument("session", type=Path, help="Session directory path")

    # add
    p_add = sub.add_parser("add", help="Add a participant")
    p_add.add_argument("session", type=Path, help="Session directory path")
    p_add.add_argument("name", help="Participant name")
    p_add.add_argument("--affiliation", default="", help="Affiliation/organization")

    # remove
    p_rm = sub.add_parser("remove", help="Remove a participant")
    p_rm.add_argument("session", type=Path, help="Session directory path")
    p_rm.add_argument("name", help="Participant name")

    # edit
    p_edit = sub.add_parser("edit", help="Edit participant info")
    p_edit.add_argument("session", type=Path, help="Session directory path")
    p_edit.add_argument("name", help="Current participant name")
    p_edit.add_argument("--name", dest="new_name", help="New name")
    p_edit.add_argument("--affiliation", help="New affiliation")

    # registry
    sub.add_parser("registry", help="Show device registry")

    # migrate
    p_migrate = sub.add_parser("migrate", help="Import ble_device_owners.json")
    p_migrate.add_argument("file", type=Path, help="Path to ble_device_owners.json")

    args = parser.parse_args()

    if args.command == "analyze":
        result = cmd_analyze(
            args.session,
            interactive=not args.yes,
            save=True,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "list":
        cmd_list(args.session)
    elif args.command == "add":
        cmd_add(args.session, args.name, affiliation=args.affiliation)
    elif args.command == "remove":
        cmd_remove(args.session, args.name)
    elif args.command == "edit":
        cmd_edit(args.session, args.name, new_name=args.new_name, affiliation=args.affiliation)
    elif args.command == "registry":
        cmd_registry()
    elif args.command == "migrate":
        cmd_migrate(args.file)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

**Step 4: Run all tests**

Run: `cd /Users/inkeun/projects/macos-recorder && python -m pytest tests/test_ble_identify.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add ble_identify.py tests/test_ble_identify.py
git commit -m "feat: add CRUD subcommands and CLI entry point"
```

---

### Task 6: Integration Test with Real Data + Migration

**Step 1: Migrate real data**

```bash
cd /Users/inkeun/projects/macos-recorder
python ble_identify.py migrate ~/Recordings/ble_device_owners.json
python ble_identify.py registry
```

Expected: Shows migrated devices, people, ambient list.

**Step 2: Analyze a real session**

```bash
python ble_identify.py analyze ~/Recordings/recording_20260225_092305 --yes --json
```

Expected: JSON output with identified participants (서인근 should appear from "Inkeun의 iPhone").

**Step 3: Test CRUD on real session**

```bash
python ble_identify.py list ~/Recordings/recording_20260225_092305
python ble_identify.py add ~/Recordings/recording_20260225_092305 "테스트유저" --affiliation "Test"
python ble_identify.py list ~/Recordings/recording_20260225_092305
python ble_identify.py remove ~/Recordings/recording_20260225_092305 "테스트유저"
python ble_identify.py list ~/Recordings/recording_20260225_092305
```

**Step 4: Test interactive mode**

```bash
python ble_identify.py analyze ~/Recordings/recording_20260226_110218
```

Expected: Interactive prompts for confirmation.

**Step 5: Commit final state**

```bash
git add -A
git commit -m "feat: BLE participant identification tool complete"
```
