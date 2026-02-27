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
            # Normalize non-breaking spaces from BLE device names
            name = name.replace("\xa0", " ")
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
    return 600


def _suggest_owner(dev_name: str, device_info: dict | None) -> str:
    """Suggest a likely owner based on device name patterns."""
    if device_info and device_info.get("owner"):
        return device_info["owner"]
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

        if owner not in registry.people:
            registry.people[owner] = {"affiliation": affiliation, "devices": []}
            changed = True
        elif affiliation and not registry.people[owner].get("affiliation"):
            registry.people[owner]["affiliation"] = affiliation
            changed = True

        for dev_name in ble_devices:
            if dev_name in registry.devices:
                entry = registry.devices[dev_name]
                entry["session_count"] = entry.get("session_count", 0) + 1
                entry["last_seen"] = today
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

            if dev_name not in registry.people[owner]["devices"]:
                registry.people[owner]["devices"].append(dev_name)
                changed = True

    if changed:
        registry.save()


def cmd_analyze(session_path: Path, config_dir: Path = DEFAULT_CONFIG_DIR,
                interactive: bool = True, save: bool = False) -> dict:
    """Analyze a session's BLE data to identify participants."""
    session_path = Path(session_path)
    events_file = session_path / "events.jsonl"
    if not events_file.exists():
        print(f"Error: {events_file} not found", file=sys.stderr)
        return {"participants": [], "candidates": []}

    registry = DeviceRegistry(config_dir=config_dir)
    devices = parse_ble_events(events_file)
    if not devices:
        print("No named BLE devices found in session.")
        return {"participants": [], "candidates": []}

    session_duration_s = _get_session_duration(events_file)
    attendance = analyze_attendance(devices, session_duration_s)

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
        _update_registry(registry, result, devices)

    return result


def _interactive_confirm(result: dict, registry: DeviceRegistry, session_path: Path) -> dict:
    """Interactive confirmation of analysis results."""
    print(f"\n[BLE 참석자 분석] {session_path.name}")
    print("─" * 50)

    confirmed = list(result["participants"])
    candidates = list(result["candidates"])

    if confirmed:
        print("\n✓ 확정 참석자 (device registry 기반):")
        for i, p in enumerate(confirmed, 1):
            devices_str = ", ".join(p.get("ble_devices", []))
            rssi = p.get("median_rssi", "?")
            print(f"  {i}. {p['name']} ({p.get('affiliation', '')}) — {devices_str} [{rssi}dBm]")

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
    session_path = Path(session_path)
    pfile = session_path / "participants.json"
    if pfile.exists():
        data = json.loads(pfile.read_text())
    else:
        data = {"participants": []}
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
        print("\nPeople:")
        for name, info in registry.people.items():
            aff = info.get("affiliation", "")
            devices = info.get("devices", [])
            print(f"  {name} ({aff})")
            for d in devices:
                dev_info = registry.devices.get(d, {})
                conf = dev_info.get("confidence", "?")
                count = dev_info.get("session_count", 0)
                print(f"    - {d} [{conf}, {count} sessions]")
    if registry.ambient:
        print(f"\nAmbient ({len(registry.ambient)}):")
        for d in registry.ambient:
            print(f"  - {d}")


def cmd_migrate(owners_file: Path, config_dir: Path = DEFAULT_CONFIG_DIR):
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

    p_analyze = sub.add_parser("analyze", help="Analyze BLE data to identify participants")
    p_analyze.add_argument("session", type=Path, help="Session directory path")
    p_analyze.add_argument("--yes", action="store_true", help="Non-interactive: accept all suggestions")
    p_analyze.add_argument("--json", action="store_true", help="Output JSON")

    p_list = sub.add_parser("list", help="List session participants")
    p_list.add_argument("session", type=Path, help="Session directory path")

    p_add = sub.add_parser("add", help="Add a participant")
    p_add.add_argument("session", type=Path, help="Session directory path")
    p_add.add_argument("name", help="Participant name")
    p_add.add_argument("--affiliation", default="", help="Affiliation/organization")

    p_rm = sub.add_parser("remove", help="Remove a participant")
    p_rm.add_argument("session", type=Path, help="Session directory path")
    p_rm.add_argument("name", help="Participant name")

    p_edit = sub.add_parser("edit", help="Edit participant info")
    p_edit.add_argument("session", type=Path, help="Session directory path")
    p_edit.add_argument("name", help="Current participant name")
    p_edit.add_argument("--name", dest="new_name", help="New name")
    p_edit.add_argument("--affiliation", help="New affiliation")

    sub.add_parser("registry", help="Show device registry")

    p_migrate = sub.add_parser("migrate", help="Import ble_device_owners.json")
    p_migrate.add_argument("file", type=Path, help="Path to ble_device_owners.json")

    args = parser.parse_args()

    if args.command == "analyze":
        result = cmd_analyze(args.session, interactive=not args.yes, save=True)
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
