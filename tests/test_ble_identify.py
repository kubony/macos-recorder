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
