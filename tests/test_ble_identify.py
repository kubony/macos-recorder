import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestDeviceRegistry:
    """Tests for DeviceRegistry load/save/migrate."""

    def test_load_empty_registry(self, tmp_path):
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        assert reg.devices == {}
        assert reg.people == {}
        assert reg.ambient == []

    def test_save_and_load_roundtrip(self, tmp_path):
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
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.devices["Galaxy Buds3 Pro (29A7)"] = {
            "owner": "서인근",
            "confidence": "certain",
            "aliases": ["Galaxy Buds3 Pro (7C68)"],
            "first_seen": "2026-02-23",
            "session_count": 8,
        }
        result = reg.lookup("Galaxy Buds3 Pro (29A7)")
        assert result["owner"] == "서인근"
        result = reg.lookup("Galaxy Buds3 Pro (7C68)")
        assert result["owner"] == "서인근"
        assert reg.lookup("Unknown Device") is None

    def test_is_ambient(self, tmp_path):
        from ble_identify import DeviceRegistry
        reg = DeviceRegistry(config_dir=tmp_path)
        reg.ambient = ["WH-CH720N", "LE-PurpleSand's QC45"]
        assert reg.is_ambient("WH-CH720N") is True
        assert reg.is_ambient("Galaxy Buds3 Pro") is False

    def test_migrate_from_ble_device_owners(self, tmp_path):
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


class TestBLEParser:
    """Tests for BLE event parsing from events.jsonl."""

    def test_parse_ble_events(self, tmp_path):
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
        assert "Device A" in result
        assert "Device B" in result
        assert "Unknown" not in result
        assert len(result["Device A"]["rssi_values"]) == 2
        assert result["Device A"]["rssi_values"] == [-50, -55]

    def test_parse_session_timestamps(self, tmp_path):
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

    def test_strong_signal_device_is_present(self):
        from ble_identify import analyze_attendance
        devices = {
            "Strong Device": {
                "rssi_values": [-50, -55, -52, -48, -53] * 20,
                "first_seen_ts": 1000000000000,
                "last_seen_ts": 1000000000000 + 600 * 1_000_000_000,
                "scan_count": 100,
            }
        }
        result = analyze_attendance(devices, 600)
        assert "Strong Device" in result
        assert result["Strong Device"]["present"] is True

    def test_weak_signal_device_not_present(self):
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

    def test_brief_device_not_present(self):
        from ble_identify import analyze_attendance
        devices = {
            "Brief Device": {
                "rssi_values": [-50, -52],
                "first_seen_ts": 1000000000000,
                "last_seen_ts": 1000000000000 + 60 * 1_000_000_000,
                "scan_count": 2,
            }
        }
        result = analyze_attendance(devices, 600)
        assert result["Brief Device"]["present"] is False


class TestAnalyzePipeline:
    """Tests for the full analyze pipeline (non-interactive)."""

    def _make_session(self, tmp_path, devices_data):
        session = tmp_path / "recording_20260227_100000"
        session.mkdir()
        lines = []
        lines.append(json.dumps({"ts": 1000000000000, "type": "recording", "action": "start"}))
        for dev_name, rssi, count in devices_data:
            for i in range(count):
                ts = 1000000000000 + (i * 3_000_000_000)
                lines.append(json.dumps({
                    "ts": ts, "type": "bluetooth",
                    "name": dev_name, "rssi": rssi, "address": f"ADDR-{dev_name}",
                }))
        lines.append(json.dumps({"ts": 1000000000000 + 600_000_000_000, "type": "recording", "action": "stop"}))
        (session / "events.jsonl").write_text("\n".join(lines))
        return session

    def test_analyze_with_known_device(self, tmp_path):
        from ble_identify import DeviceRegistry, cmd_analyze
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reg = DeviceRegistry(config_dir=config_dir)
        reg.devices["Inkeun의 iPhone"] = {
            "owner": "서인근", "confidence": "certain",
            "aliases": [], "first_seen": "2026-02-23", "session_count": 5,
        }
        reg.people["서인근"] = {"affiliation": "마음AI WoRV", "devices": ["Inkeun의 iPhone"]}
        reg.ambient = ["WH-CH720N"]
        reg.save()

        session = self._make_session(tmp_path, [
            ("Inkeun의 iPhone", -45, 100),
            ("WH-CH720N", -60, 100),
            ("New Device", -55, 100),
        ])

        result = cmd_analyze(session, config_dir=config_dir, interactive=False)
        names = [p["name"] for p in result["participants"]]
        assert "서인근" in names
        for p in result["participants"]:
            assert p["name"] != "WH-CH720N"
        candidate_devices = [c["device_name"] for c in result["candidates"]]
        assert "New Device" in candidate_devices

    def test_analyze_saves_participants_json(self, tmp_path):
        from ble_identify import DeviceRegistry, cmd_analyze
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reg = DeviceRegistry(config_dir=config_dir)
        reg.devices["Inkeun의 iPhone"] = {
            "owner": "서인근", "confidence": "certain",
            "aliases": [], "first_seen": "2026-02-23", "session_count": 5,
        }
        reg.people["서인근"] = {"affiliation": "마음AI WoRV", "devices": ["Inkeun의 iPhone"]}
        reg.save()

        session = self._make_session(tmp_path, [("Inkeun의 iPhone", -45, 100)])
        cmd_analyze(session, config_dir=config_dir, interactive=False, save=True)
        pfile = session / "participants.json"
        assert pfile.exists()
        data = json.loads(pfile.read_text())
        assert data["participants"][0]["name"] == "서인근"
        assert data["participants"][0]["source"] == "ble"


class TestCRUD:
    """Tests for list/add/remove/edit commands."""

    def _make_session_with_participants(self, tmp_path):
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
        from ble_identify import cmd_list
        session = self._make_session_with_participants(tmp_path)
        result = cmd_list(session)
        assert len(result) == 2
        assert result[0]["name"] == "서인근"

    def test_cmd_add(self, tmp_path):
        from ble_identify import cmd_add, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_add(session, "박유빈", affiliation="마음AI WoRV 2본부")
        result = cmd_list(session)
        assert len(result) == 3
        assert any(p["name"] == "박유빈" for p in result)

    def test_cmd_add_duplicate(self, tmp_path):
        from ble_identify import cmd_add, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_add(session, "서인근", affiliation="마음AI")
        result = cmd_list(session)
        assert len(result) == 2

    def test_cmd_remove(self, tmp_path):
        from ble_identify import cmd_remove, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_remove(session, "김다혜")
        result = cmd_list(session)
        assert len(result) == 1
        assert result[0]["name"] == "서인근"

    def test_cmd_edit(self, tmp_path):
        from ble_identify import cmd_edit, cmd_list
        session = self._make_session_with_participants(tmp_path)
        cmd_edit(session, "김다혜", new_name="김다혜(DH)", affiliation="WoRV 개발팀")
        result = cmd_list(session)
        dh = next(p for p in result if p["name"] == "김다혜(DH)")
        assert dh["affiliation"] == "WoRV 개발팀"
