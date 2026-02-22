#!/usr/bin/env python3
"""
Basic tests for macOS Recorder components.
Run with: pytest tests/ -v
"""

import pytest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder import (
    BluetoothAnonymizer,
    load_config,
    secure_directory,
    secure_file,
)


class TestBluetoothAnonymizer:
    """Tests for BluetoothAnonymizer."""
    
    def test_consistency(self):
        """Same device name should always return same hash."""
        anon = BluetoothAnonymizer(salt="test-salt")
        result1 = anon.anonymize("AirPods Pro")
        result2 = anon.anonymize("AirPods Pro")
        assert result1 == result2
    
    def test_different_devices(self):
        """Different device names should return different hashes."""
        anon = BluetoothAnonymizer(salt="test-salt")
        result1 = anon.anonymize("AirPods Pro")
        result2 = anon.anonymize("iPhone 15")
        assert result1 != result2
    
    def test_unknown_device(self):
        """Empty or None device name should return 'Unknown'."""
        anon = BluetoothAnonymizer(salt="test-salt")
        assert anon.anonymize("") == "Unknown"
        assert anon.anonymize(None) == "Unknown"
    
    def test_format(self):
        """Anonymized name should follow Device_XXXXXX format."""
        anon = BluetoothAnonymizer(salt="test-salt")
        result = anon.anonymize("MyDevice")
        assert result.startswith("Device_")
        assert len(result) == 13  # "Device_" + 6 hex chars


class TestLoadConfig:
    """Tests for configuration loading."""
    
    def test_defaults(self):
        """Should return defaults when config file doesn't exist."""
        config = load_config(Path("/nonexistent/path/config.yaml"))
        assert config["recording"]["fps"] == 30
        assert config["audio"]["sample_rate"] == 44100
        assert config["bluetooth"]["enabled"] is True
    
    def test_merge_with_user_config(self):
        """Should merge user config with defaults."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("recording:\n  fps: 60\n")
            config_path = Path(f.name)
        
        try:
            config = load_config(config_path)
            # User override
            assert config["recording"]["fps"] == 60
            # Defaults preserved
            assert config["recording"]["quality"] == "high"
            assert config["audio"]["sample_rate"] == 44100
        finally:
            config_path.unlink()


class TestSecurePermissions:
    """Tests for secure file/directory permissions."""
    
    def test_secure_directory(self):
        """Directory should have 700 permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            secure_directory(path)
            # Check permissions (700 = 0o700 = 448 in decimal)
            import stat
            mode = path.stat().st_mode & 0o777
            assert mode == 0o700
    
    def test_secure_file(self):
        """File should have 600 permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        
        try:
            secure_file(path)
            import stat
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600
        finally:
            path.unlink()


class TestScreenRecorder:
    """Tests for ScreenRecorder (mocked)."""
    
    def test_ffmpeg_check_caching(self):
        """ffmpeg availability check should be cached."""
        from recorder import ScreenRecorder
        
        # Reset cache
        ScreenRecorder._ffmpeg_available = None
        
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            
            # First call
            result1 = ScreenRecorder.check_ffmpeg()
            # Second call (should use cache)
            result2 = ScreenRecorder.check_ffmpeg()
            
            # subprocess.run should only be called once
            assert mock_run.call_count == 1
            assert result1 == result2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
