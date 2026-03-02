#!/usr/bin/env python3
"""
Recording components for macOS Recorder.
Handles screen capture, audio recording, Bluetooth monitoring, and sleep prevention.

Improvements based on expert review:
- Streaming audio write (memory leak fix)
- Permission checking
- Bluetooth anonymization
- Secure file permissions
- Better error handling
"""

import subprocess
import threading
import time
import json
import os
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime
from ctypes import cdll, c_uint32, byref

import yaml

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_macos_version() -> tuple:
    """Get macOS version as tuple (major, minor)."""
    import platform
    try:
        return tuple(map(int, platform.mac_ver()[0].split('.')[:2]))
    except:
        return (10, 15)  # Assume Catalina as minimum


class PermissionChecker:
    """macOS 권한 상태 확인"""
    
    @staticmethod
    def check_screen_recording() -> bool:
        """화면 녹화 권한 확인 (macOS 10.15+)"""
        try:
            version = get_macos_version()
            if version < (10, 15):
                return True  # 10.15 이전은 권한 시스템 없음
            
            from Quartz import CGPreflightScreenCaptureAccess
            return CGPreflightScreenCaptureAccess()
        except ImportError:
            logger.warning("pyobjc-framework-Quartz 설치 필요")
            return False  # False가 더 안전
        except Exception as e:
            logger.error(f"권한 확인 실패: {e}")
            return False
    
    @staticmethod
    def check_microphone() -> bool:
        """마이크 권한 확인"""
        try:
            import AVFoundation
            status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                AVFoundation.AVMediaTypeAudio
            )
            return status == 3  # AVAuthorizationStatusAuthorized
        except:
            return True  # 확인 불가 시 True 가정
    
    @staticmethod
    def request_screen_recording():
        """화면 녹화 권한 요청"""
        try:
            from Quartz import CGRequestScreenCaptureAccess
            CGRequestScreenCaptureAccess()
        except:
            pass
    
    @staticmethod
    def open_privacy_settings(pane: str = "screen"):
        """시스템 설정 열기"""
        urls = {
            "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            "bluetooth": "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth",
        }
        subprocess.run(["open", urls.get(pane, urls["screen"])])
    
    @staticmethod
    def check_bluetooth() -> bool:
        """Bluetooth 권한 확인 (macOS 10.15+)"""
        try:
            version = get_macos_version()
            if version < (10, 15):
                return True
            # Note: Full CoreBluetooth check requires async delegate
            # For now, assume True and handle runtime errors
            return True
        except:
            return True

    @staticmethod
    def check_location() -> bool:
        """위치 서비스 권한 확인"""
        try:
            import CoreLocation
            status = CoreLocation.CLLocationManager.authorizationStatus()
            # 0=NotDetermined, 3=AuthorizedAlways, 4=AuthorizedWhenInUse
            return status in (0, 3, 4)
        except ImportError:
            logger.debug("CoreLocation not available")
            return False
        except Exception:
            return False

    @staticmethod
    def validate_all() -> dict:
        """모든 권한 상태 확인"""
        return {
            "screen_recording": PermissionChecker.check_screen_recording(),
            "microphone": PermissionChecker.check_microphone(),
            "bluetooth": PermissionChecker.check_bluetooth(),
            "location": PermissionChecker.check_location(),
        }


class SleepInhibitor:
    """Prevents macOS from sleeping and App Nap during recording."""
    
    def __init__(self, reason: str = "Recording in progress"):
        self.reason = reason
        self.assertion_id = None
        self._process = None  # Fallback to caffeinate
        self._activity = None  # App Nap prevention
    
    def start(self):
        """Start preventing sleep and App Nap."""
        # 1. Prevent App Nap (P0 fix from macOS Native reviewer)
        try:
            from Foundation import NSProcessInfo
            # NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFF
            self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                0x00FFFFFF, self.reason
            )
            logger.info("App Nap prevention started")
        except Exception as e:
            logger.warning(f"App Nap prevention failed: {e}")
        
        # 2. Prevent system sleep via caffeinate
        try:
            self._process = subprocess.Popen(
                ["caffeinate", "-dims"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info("Sleep prevention started (caffeinate)")
        except Exception as e:
            logger.warning(f"caffeinate failed: {e}")
    
    def stop(self):
        """Allow sleep and App Nap again."""
        # Stop App Nap prevention
        if self._activity:
            try:
                from Foundation import NSProcessInfo
                NSProcessInfo.processInfo().endActivity_(self._activity)
                logger.info("App Nap prevention stopped")
            except:
                pass
            self._activity = None
        
        # Stop caffeinate
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except:
                self._process.kill()
            self._process = None
            logger.info("Sleep prevention stopped")


def list_avfoundation_devices() -> dict:
    """List available avfoundation video/audio devices.

    Returns dict with 'video' and 'audio' lists of (index, name) tuples.
    """
    result = {"video": [], "audio": []}
    try:
        proc = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5
        )
        # ffmpeg prints device list to stderr
        import re
        output = proc.stderr
        section = None
        for line in output.splitlines():
            if "AVFoundation video devices:" in line:
                section = "video"
            elif "AVFoundation audio devices:" in line:
                section = "audio"
            elif section:
                # Lines like: [AVFoundation indev @ 0x...] [0] Device Name
                m = re.search(r'\]\s+\[(\d+)\]\s+(.+)', line)
                if m:
                    result[section].append((int(m.group(1)), m.group(2).strip()))
    except Exception as e:
        logger.warning(f"Failed to list avfoundation devices: {e}")
    return result


def find_device_index(device_type: str, keyword: str = None) -> Optional[int]:
    """Find avfoundation device index by type and optional keyword.

    device_type: 'screen', 'camera', or 'audio'
    keyword: substring to match in device name (e.g. 'BlackHole', 'FaceTime')
    """
    devices = list_avfoundation_devices()

    if device_type == "screen":
        # Screen devices typically have 'Capture screen' in name
        for idx, name in devices["video"]:
            if "capture screen" in name.lower() or "screen" in name.lower():
                return idx
        # Fallback: last video device (screens come after cameras)
        if devices["video"]:
            return devices["video"][-1][0]

    elif device_type == "camera":
        for idx, name in devices["video"]:
            if keyword and keyword.lower() in name.lower():
                return idx
            if "camera" in name.lower() or "facetime" in name.lower():
                return idx
        # Fallback: first video device
        if devices["video"]:
            return devices["video"][0][0]

    elif device_type == "audio":
        for idx, name in devices["audio"]:
            if keyword and keyword.lower() in name.lower():
                return idx

    return None


def find_all_screens() -> list:
    """Find all screen capture device indices.

    Returns list of (index, name) tuples for all screen devices.
    """
    devices = list_avfoundation_devices()
    screens = []
    for idx, name in devices["video"]:
        if "capture screen" in name.lower() or "screen" in name.lower():
            screens.append((idx, name))
    return screens


class ScreenRecorder:
    """Records screen using ffmpeg with avfoundation."""

    # Class-level ffmpeg availability cache
    _ffmpeg_available = None
    
    def __init__(self, output_path: Path, fps: int = 30, monitor_idx: int = None):
        self.output_path = output_path
        self.fps = fps
        self.monitor_idx = monitor_idx if monitor_idx is not None else (find_device_index("screen") or 1)
        self.process = None
        self.recording = False
        self._error = None
    
    @classmethod
    def check_ffmpeg(cls) -> bool:
        """Check ffmpeg availability (cached)."""
        if cls._ffmpeg_available is None:
            try:
                subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
                cls._ffmpeg_available = True
            except:
                cls._ffmpeg_available = False
        return cls._ffmpeg_available
    
    def start(self) -> bool:
        """Start screen recording."""
        if not self.check_ffmpeg():
            self._error = "ffmpeg not found. Install: brew install ffmpeg"
            logger.error(self._error)
            return False
        
        self.recording = True
        
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "avfoundation",
            "-capture_cursor", "1",
            "-framerate", str(self.fps),
            "-i", f"{self.monitor_idx}:none",
            "-c:v", "h264_videotoolbox",
            "-b:v", "5M",
            "-pix_fmt", "yuv420p",
            str(self.output_path)
        ]
        
        try:
            self._stderr_path = str(self.output_path) + ".log"
            self._stderr_file = open(self._stderr_path, "w")
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_file
            )
            # Give ffmpeg a moment to fail on startup
            time.sleep(0.5)
            if self.process.poll() is not None:
                self._stderr_file.close()
                with open(self._stderr_path) as f:
                    err = f.read().strip().split('\n')[-3:]
                self._error = f"ffmpeg exited immediately: {' '.join(err)}"
                logger.error(self._error)
                self.recording = False
                return False
            logger.info(f"Screen recording started: {self.output_path}")
            return True
        except Exception as e:
            self._error = f"Failed to start screen recording: {e}"
            logger.error(self._error)
            self.recording = False
            return False
    
    def stop(self):
        """Stop screen recording with robust process cleanup."""
        self.recording = False
        if not self.process:
            return
        
        # P0 fix: robust multi-stage process termination
        try:
            self.process.stdin.write(b'q')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        
        # Stage 1: graceful wait
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Stage 2: terminate
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Stage 3: kill
                self.process.kill()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Stage 4: force kill via OS
                    try:
                        subprocess.run(["kill", "-9", str(self.process.pid)], 
                                       capture_output=True, timeout=2)
                    except:
                        pass
        
        # Close stderr log
        if hasattr(self, '_stderr_file') and self._stderr_file:
            self._stderr_file.close()
            self._stderr_file = None
        # Clean up log file
        if hasattr(self, '_stderr_path') and os.path.exists(self._stderr_path):
            try:
                os.remove(self._stderr_path)
            except OSError:
                pass

        # Set secure file permissions
        if self.output_path.exists():
            os.chmod(self.output_path, 0o600)

        self.process = None
        logger.info("Screen recording stopped")
    
    def get_error(self) -> Optional[str]:
        return self._error


class CameraRecorder(ScreenRecorder):
    """Records camera (webcam) using ffmpeg with avfoundation."""

    def __init__(self, output_path: Path, fps: int = 30, device_idx: int = None,
                 video_size: str = "1920x1080"):
        idx = device_idx if device_idx is not None else (find_device_index("camera") or 0)
        super().__init__(output_path, fps, monitor_idx=idx)
        self.video_size = video_size

    def start(self) -> bool:
        """Start camera recording."""
        if not self.check_ffmpeg():
            self._error = "ffmpeg not found. Install: brew install ffmpeg"
            logger.error(self._error)
            return False

        self.recording = True

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "avfoundation",
            "-video_size", self.video_size,
            "-framerate", str(self.fps),
            "-i", f"{self.monitor_idx}:none",
            "-c:v", "h264_videotoolbox",
            "-b:v", "3M",
            "-pix_fmt", "yuv420p",
            str(self.output_path)
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info(f"Camera recording started: {self.output_path}")
            return True
        except Exception as e:
            self._error = f"Failed to start camera recording: {e}"
            logger.error(self._error)
            self.recording = False
            return False


class AudioRecorder:
    """Records audio using sounddevice with streaming write (no memory leak)."""

    BUILTIN_MIC_PATTERNS = ["MacBook Pro 마이크", "MacBook Air 마이크", "MacBook Pro Microphone", "MacBook Air Microphone", "Built-in Microphone"]
    BLACKHOLE_PATTERNS = ["BlackHole"]

    def __init__(self, output_path: Path, source: str = "microphone", sample_rate: int = 44100, device_name: str = None):
        self.output_path = output_path
        self.source = source
        self.sample_rate = sample_rate
        self.device_name = device_name
        self._recording = False
        self._recording_lock = threading.Lock()  # P0 fix: thread safety
        self.thread = None
        self._error = None
    
    @property
    def recording(self) -> bool:
        with self._recording_lock:
            return self._recording
    
    @recording.setter
    def recording(self, value: bool):
        with self._recording_lock:
            self._recording = value
    
    def start(self) -> bool:
        """Start audio recording in a background thread."""
        self.recording = True
        self.thread = threading.Thread(target=self._record_thread, daemon=True)
        self.thread.start()
        return True
    
    @staticmethod
    def _find_device_by_name(name_patterns: list, require_input: bool = True) -> Optional[int]:
        """Find audio device index by name patterns. Returns first match or None."""
        import sounddevice as sd
        devices = sd.query_devices()
        for pattern in name_patterns:
            for i, d in enumerate(devices):
                if pattern in d.get("name", ""):
                    if require_input and d.get("max_input_channels", 0) < 1:
                        continue
                    return i
        return None

    def _record_thread(self):
        """Background thread for audio recording with streaming write."""
        try:
            import sounddevice as sd
            import soundfile as sf

            # Get device based on source and explicit device_name
            device = None
            if self.device_name:
                device = self._find_device_by_name([self.device_name])
                if device is None:
                    logger.warning(f"Configured device '{self.device_name}' not found, falling back to auto-detect")

            if device is None:
                if self.source == "microphone":
                    # Try built-in microphone first, fall back to system default
                    device = self._find_device_by_name(self.BUILTIN_MIC_PATTERNS)
                    if device is None:
                        device = sd.default.device[0]
                        logger.warning(f"Built-in mic not found, using system default: {sd.query_devices(device)['name']}")
                    else:
                        logger.info(f"Using built-in mic: {sd.query_devices(device)['name']}")
                else:
                    # For system audio, need BlackHole
                    device = self._find_device_by_name(self.BLACKHOLE_PATTERNS)
                    if device is None:
                        self._error = "BlackHole not found. Install: brew install blackhole-2ch"
                        logger.warning(self._error)
                        self.recording = False
                        return

            # Detect channel count from device
            device_info = sd.query_devices(device)
            channels = min(2, int(device_info.get("max_input_channels", 2)))
            if channels < 1:
                channels = 1

            # Streaming write - no memory accumulation!
            with sf.SoundFile(
                str(self.output_path), mode='w',
                samplerate=self.sample_rate,
                channels=channels, subtype='PCM_16'
            ) as f:
                def callback(indata, frames, time_info, status):
                    if status:
                        logger.warning(f"Audio status: {status}")
                    if self.recording:
                        f.write(indata)  # Write directly to file

                with sd.InputStream(
                    device=device,
                    samplerate=self.sample_rate,
                    channels=channels,
                    callback=callback
                ):
                    logger.info(f"Audio recording started ({self.source}): {self.output_path}")
                    while self.recording:
                        time.sleep(0.1)
            
            # Set secure file permissions
            if self.output_path.exists():
                os.chmod(self.output_path, 0o600)
            
            logger.info(f"Audio recording stopped ({self.source})")
        
        except ImportError as e:
            self._error = f"Missing dependency: {e}. Run: pip install sounddevice soundfile"
            logger.error(self._error)
        except Exception as e:
            self._error = f"Audio recording error: {e}"
            logger.error(self._error)
    
    def stop(self):
        """Stop audio recording."""
        self.recording = False
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None
    
    def get_error(self) -> Optional[str]:
        return self._error


class BluetoothAnonymizer:
    """Anonymizes Bluetooth device names for privacy."""
    
    MAX_DEVICES = 5000  # P1 fix (Memory): LRU cache limit
    
    def __init__(self, salt: str = None):
        from collections import OrderedDict
        self.salt = salt or os.urandom(16).hex()
        self.device_map = OrderedDict()
    
    def anonymize(self, device_name: str) -> str:
        """Anonymize device name with consistent hash (LRU cached)."""
        if not device_name:
            return "Unknown"
        
        if device_name in self.device_map:
            self.device_map.move_to_end(device_name)  # LRU update
            return self.device_map[device_name]
        
        # P1 fix: LRU eviction when cache is full
        while len(self.device_map) >= self.MAX_DEVICES:
            self.device_map.popitem(last=False)
        
        hash_value = hashlib.sha256(
            (self.salt + device_name).encode()
        ).hexdigest()[:6]
        self.device_map[device_name] = f"Device_{hash_value}"
        
        return self.device_map[device_name]


class BluetoothMonitor:
    """Monitors Bluetooth device RSSI with anonymization."""
    
    def __init__(self, callback: Callable[[dict], None], scan_interval: float = 1.0,
                 anonymize: bool = True, raw_callback: Callable[[dict], None] = None):
        self.callback = callback
        self.scan_interval = scan_interval
        self.anonymize = anonymize
        self.raw_callback = raw_callback
        # P0 fix (Concurrency): thread-safe running flag
        self._running = False
        self._running_lock = threading.Lock()
        self.thread = None
        self.loop = None
        self._anonymizer = BluetoothAnonymizer() if anonymize else None
        self._error = None
    
    @property
    def running(self) -> bool:
        with self._running_lock:
            return self._running
    
    @running.setter
    def running(self, value: bool):
        with self._running_lock:
            self._running = value
    
    def start(self) -> bool:
        """Start Bluetooth monitoring in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.thread.start()
        return True
    
    def _monitor_thread(self):
        """Background thread for Bluetooth monitoring."""
        try:
            from bleak import BleakScanner
            
            async def scan():
                while self.running:
                    try:
                        devices = await BleakScanner.discover(
                            timeout=self.scan_interval,
                            return_adv=True
                        )
                        for device, adv_data in devices.values():
                            try:
                                name = device.name or adv_data.local_name or "Unknown"
                                rssi = adv_data.rssi

                                # raw callback before anonymization (for overlay)
                                if self.raw_callback and rssi is not None:
                                    try:
                                        raw_event = {"address": device.address, "name": name, "rssi": rssi}
                                        self.raw_callback(raw_event)
                                    except Exception as e:
                                        logger.error(f"Raw BLE callback error: {e}")

                                if self._anonymizer:
                                    name = self._anonymizer.anonymize(name)

                                bt_event = {
                                    "address": device.address,
                                    "name": name,
                                    "rssi": rssi,
                                    "tx_power": adv_data.tx_power,
                                    "manufacturer_data": {
                                        str(company_id): data.hex()
                                        for company_id, data in (adv_data.manufacturer_data or {}).items()
                                    },
                                    "service_uuids": list(adv_data.service_uuids or []),
                                    "service_data": {
                                        str(uuid): data.hex()
                                        for uuid, data in (adv_data.service_data or {}).items()
                                    },
                                }

                                if rssi is not None:
                                    self.callback(bt_event)
                            except Exception as e:
                                logger.error(f"Bluetooth callback error: {e}")
                    except Exception as e:
                        logger.warning(f"Bluetooth scan error: {e}")

                    await asyncio.sleep(self.scan_interval)
            
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            logger.info("Bluetooth monitoring started")
            self.loop.run_until_complete(scan())
        
        except ImportError as e:
            self._error = f"Missing dependency: {e}. Run: pip install bleak"
            logger.error(self._error)
        except Exception as e:
            self._error = f"Bluetooth monitoring error: {e}"
            logger.error(self._error)
    
    def stop(self):
        """Stop Bluetooth monitoring with proper resource cleanup."""
        self.running = False
        
        # P0 fix (Concurrency): capture local reference to avoid race condition
        loop_ref = self.loop
        if loop_ref:
            try:
                loop_ref.call_soon_threadsafe(loop_ref.stop)
            except RuntimeError:
                pass  # Already closed
        
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Bluetooth thread did not terminate in time")
            self.thread = None
        
        # P0 fix: properly close asyncio event loop to prevent resource leak
        if loop_ref:
            try:
                loop_ref.close()
            except Exception:
                pass
        self.loop = None
        logger.info("Bluetooth monitoring stopped")
    
    def get_error(self) -> Optional[str]:
        return self._error


class ContextMonitor:
    """Monitors system context: Wi-Fi, power, active app, audio devices, etc."""

    # Known meeting app bundle identifiers
    MEETING_APPS = {
        "us.zoom.xos": "Zoom",
        "com.microsoft.teams": "Teams",
        "com.microsoft.teams2": "Teams",
        "com.tinyspeck.slackmacgap": "Slack",
        "com.cisco.webexmeetingsapp": "Webex",
        "com.google.Chrome": "Chrome",  # Google Meet runs in browser
        "com.apple.Safari": "Safari",
    }

    def __init__(self, callback: Callable[[dict], None], poll_interval: float = 10.0):
        self.callback = callback
        self.poll_interval = poll_interval
        self._running = False
        self._running_lock = threading.Lock()
        self.thread = None
        self._error = None

    @property
    def running(self) -> bool:
        with self._running_lock:
            return self._running

    @running.setter
    def running(self, value: bool):
        with self._running_lock:
            self._running = value

    def start(self) -> bool:
        """Start context monitoring in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.thread.start()
        return True

    def _monitor_thread(self):
        """Background thread for context monitoring."""
        logger.info("Context monitoring started")
        try:
            while self.running:
                context = {}
                for name, getter in [
                    ("wifi", self._get_wifi),
                    ("power", self._get_power),
                    ("active_app", self._get_active_app),
                    ("audio_devices", self._get_audio_devices),
                    ("idle", self._get_idle_time),
                    ("displays", self._get_displays),
                    ("meeting_apps", self._get_meeting_apps),
                ]:
                    try:
                        result = getter()
                        if result is not None:
                            context[name] = result
                    except Exception as e:
                        logger.warning(f"Context getter '{name}' error: {e}")

                if context:
                    self.callback(context)

                # Sleep in small increments for responsive shutdown
                deadline = time.monotonic() + self.poll_interval
                while self.running and time.monotonic() < deadline:
                    time.sleep(0.5)
        except Exception as e:
            self._error = f"Context monitoring error: {e}"
            logger.error(self._error)
        logger.info("Context monitoring stopped")

    def _get_wifi(self) -> Optional[dict]:
        """Get current Wi-Fi info via CoreWLAN."""
        try:
            from CoreWLAN import CWWiFiClient
            client = CWWiFiClient.sharedWiFiClient()
            iface = client.interface()
            if iface is None or not iface.ssid():
                return None
            return {
                "ssid": str(iface.ssid()),
                "bssid": str(iface.bssid()) if iface.bssid() else None,
                "rssi": iface.rssiValue(),
                "noise": iface.noiseMeasurement(),
                "channel": iface.wlanChannel().channelNumber() if iface.wlanChannel() else None,
            }
        except ImportError:
            logger.debug("CoreWLAN not available, skipping Wi-Fi monitoring")
            return None

    def _get_power(self) -> Optional[dict]:
        """Get power source info via pmset."""
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return None

            output = result.stdout
            info = {}

            # First line: "Now drawing from 'AC Power'" or "'Battery Power'"
            first_line = output.splitlines()[0] if output.splitlines() else ""
            if "AC Power" in first_line:
                info["source"] = "AC Power"
            elif "Battery Power" in first_line:
                info["source"] = "Battery Power"

            # Second line contains percentage and state
            import re
            match = re.search(r'(\d+)%;\s*(\w[\w\s]*?)(?:;|$)', output)
            if match:
                info["percent"] = int(match.group(1))
                state = match.group(2).strip().lower()
                info["charging"] = state in ("charging", "charged", "finishing charge")

            return info if info else None
        except Exception:
            return None

    def _get_active_app(self) -> Optional[dict]:
        """Get the frontmost application info."""
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None

            info = {
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
            }

            # Try to get window title via CGWindowListCopyWindowInfo
            try:
                from Quartz import (
                    CGWindowListCopyWindowInfo,
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID,
                    kCGWindowOwnerPID,
                    kCGWindowName,
                )
                pid = app.processIdentifier()
                windows = CGWindowListCopyWindowInfo(
                    kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                )
                for win in windows or []:
                    if win.get(kCGWindowOwnerPID) == pid:
                        title = win.get(kCGWindowName)
                        if title:
                            info["window_title"] = str(title)
                            break
            except ImportError:
                pass

            return info
        except ImportError:
            return None

    def _get_audio_devices(self) -> Optional[dict]:
        """Get default audio input/output device names."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            defaults = sd.default.device  # (input_idx, output_idx)
            info = {}
            if defaults[1] is not None and defaults[1] < len(devices):
                info["output"] = devices[defaults[1]]["name"]
            if defaults[0] is not None and defaults[0] < len(devices):
                info["input"] = devices[defaults[0]]["name"]
            return info if info else None
        except Exception:
            return None

    def _get_idle_time(self) -> Optional[dict]:
        """Get system idle time in seconds."""
        try:
            result = subprocess.run(
                ["ioreg", "-c", "IOHIDSystem"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return None

            import re
            match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
            if match:
                idle_ns = int(match.group(1))
                return {"idle_seconds": round(idle_ns / 1_000_000_000, 1)}
            return None
        except Exception:
            return None

    def _get_displays(self) -> Optional[dict]:
        """Get display count and type info."""
        try:
            from Quartz import CGGetActiveDisplayList, CGDisplayIsBuiltin
            err, display_ids, count = CGGetActiveDisplayList(16, None, None)
            if err != 0:
                return None

            builtin = False
            external = 0
            for did in display_ids[:count]:
                if CGDisplayIsBuiltin(did):
                    builtin = True
                else:
                    external += 1

            return {
                "count": count,
                "builtin": builtin,
                "external": external,
            }
        except ImportError:
            return None

    def _get_meeting_apps(self) -> Optional[dict]:
        """Detect running meeting applications."""
        try:
            from AppKit import NSWorkspace
            running = NSWorkspace.sharedWorkspace().runningApplications()
            frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
            front_bid = str(frontmost.bundleIdentifier()) if frontmost else ""

            detected = []
            foreground = False
            for app in running:
                bid = str(app.bundleIdentifier() or "")
                if bid in self.MEETING_APPS:
                    detected.append(self.MEETING_APPS[bid])
                    if bid == front_bid:
                        foreground = True

            if not detected:
                return None
            return {
                "running": detected,
                "foreground": foreground,
            }
        except ImportError:
            return None

    def stop(self):
        """Stop context monitoring."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Context monitor thread did not terminate in time")
            self.thread = None
        logger.info("Context monitor stopped")

    def get_error(self) -> Optional[str]:
        return self._error


class LocationMonitor:
    """Monitors location using CoreLocation (Wi-Fi positioning on Mac, no GPS chip)."""

    _DelegateClass = None

    @classmethod
    def _ensure_delegate(cls):
        """Lazily create the ObjC delegate class (registered once)."""
        if cls._DelegateClass is not None:
            return True
        try:
            import objc
            from Foundation import NSObject

            class _LocDelegate(NSObject):
                def initWithCallback_(self, callback):
                    self = objc.super(_LocDelegate, self).init()
                    if self is None:
                        return None
                    self._callback = callback
                    return self

                def locationManager_didUpdateLocations_(self, manager, locations):
                    for loc in locations:
                        accuracy = loc.horizontalAccuracy()
                        if accuracy < 0:
                            continue

                        # Skip cached data older than 15 seconds
                        age = abs(loc.timestamp().timeIntervalSinceNow())
                        if age > 15.0:
                            logger.debug(f"Ignored cached location (age={age:.1f}s)")
                            continue

                        event = {
                            "latitude": loc.coordinate().latitude,
                            "longitude": loc.coordinate().longitude,
                            "altitude": loc.altitude(),
                            "horizontal_accuracy": accuracy,
                            "vertical_accuracy": loc.verticalAccuracy(),
                            "speed": loc.speed(),
                        }
                        try:
                            self._callback(event)
                        except Exception as e:
                            logger.error(f"Location callback error: {e}")

                def locationManager_didFailWithError_(self, manager, error):
                    logger.warning(f"Location error: {error.localizedDescription()}")

                def locationManagerDidChangeAuthorization_(self, manager):
                    try:
                        status = manager.authorizationStatus()
                        if status in (3, 4):  # Authorized
                            manager.startUpdatingLocation()
                    except Exception:
                        pass

            cls._DelegateClass = _LocDelegate
            return True
        except ImportError:
            return False

    def __init__(self, callback: Callable[[dict], None], poll_interval: float = 60.0):
        self.callback = callback
        self.poll_interval = poll_interval  # kept for config compat, unused internally
        self._running = False
        self._running_lock = threading.Lock()
        self._manager = None
        self._delegate = None
        self._error = None

    @property
    def running(self) -> bool:
        with self._running_lock:
            return self._running

    @running.setter
    def running(self, value: bool):
        with self._running_lock:
            self._running = value

    def start(self) -> bool:
        """Start location monitoring. Must be called on main thread."""
        if not self._ensure_delegate():
            self._error = "pyobjc-framework-CoreLocation not installed"
            logger.warning(self._error)
            return False

        try:
            import CoreLocation

            self._delegate = self._DelegateClass.alloc().initWithCallback_(self.callback)
            self._manager = CoreLocation.CLLocationManager.alloc().init()
            self._manager.setDelegate_(self._delegate)
            self._manager.setDistanceFilter_(10.0)
            self._manager.setDesiredAccuracy_(CoreLocation.kCLLocationAccuracyBest)
            self._manager.setPausesLocationUpdatesAutomatically_(False)

            status = CoreLocation.CLLocationManager.authorizationStatus()
            if status == 0:  # NotDetermined
                self._manager.requestWhenInUseAuthorization()
            elif status in (1, 2):  # Restricted or Denied
                self._error = "Location permission denied"
                logger.warning(self._error)
                return False

            self._manager.startUpdatingLocation()
            self.running = True

            logger.info("Location monitoring started")
            return True
        except ImportError:
            self._error = "pyobjc-framework-CoreLocation not installed"
            logger.warning(self._error)
            return False
        except Exception as e:
            self._error = f"Location monitoring error: {e}"
            logger.error(self._error)
            return False

    def stop(self):
        """Stop location monitoring."""
        self.running = False
        if self._manager:
            try:
                self._manager.stopUpdatingLocation()
            except Exception:
                pass
            self._manager = None
        self._delegate = None
        logger.info("Location monitoring stopped")

    def get_error(self) -> Optional[str]:
        return self._error


def load_config(config_path: Path = None) -> dict:
    """Load configuration from YAML file."""
    default_config = {
        "recording": {
            "fps": 30,
            "quality": "high",
            "include_cursor": True,
            "camera": True,
        },
        "audio": {
            "system_audio": True,
            "microphone": True,
            "sample_rate": 44100,
            "mic_device": None,
            "system_device": None,
        },
        "bluetooth": {
            "enabled": True,
            "scan_interval": 1.0,
            "anonymize": False,
            "rssi_threshold": -70,
        },
        "overlay": {
            "enabled": False,
            "show_unmapped": False,
            "opacity": 0.85,
            "max_devices": 15,
            "refresh_interval": 2.0,
        },
        "context": {
            "enabled": True,
            "poll_interval": 10.0,
        },
        "gps": {
            "enabled": True,
            "poll_interval": 60.0,
        },
        "labeling": {
            "enabled": True,
            "auto_suggest_location": True,
        },
        "output": {
            "directory": str(Path.home() / "Recordings"),
            "format": "mp4",
        },
        "privacy": {
            "auto_delete_days": 30,
            "require_consent": True,
        },
        "post_processing": {
            "enabled": False,
            "command": 'claude "/rec-processor {path}"',
        }
    }
    
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    
    if config_path.exists():
        try:
            with open(config_path) as f:
                user_config = yaml.safe_load(f) or {}
            
            # Deep merge
            def merge(base, override):
                for key, value in override.items():
                    if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                        merge(base[key], value)
                    else:
                        base[key] = value
            
            merge(default_config, user_config)
            logger.info(f"Config loaded from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to load config: {e}, using defaults")
    
    return default_config


def secure_directory(path: Path):
    """Set secure permissions on directory (700)."""
    os.chmod(path, 0o700)


def secure_file(path: Path):
    """Set secure permissions on file (600)."""
    if path.exists():
        os.chmod(path, 0o600)


def play_sound(sound_name: str = "Blow"):
    """Play macOS system sound."""
    sound_path = f"/System/Library/Sounds/{sound_name}.aiff"
    if Path(sound_path).exists():
        subprocess.run(["afplay", sound_path], capture_output=True)


class AudioOutputSwitcher:
    """Switches macOS audio output device for recording.

    Requires: brew install switchaudio-osx
    """

    def __init__(self, recording_device: str = "다중 출력 기기"):
        self.recording_device = recording_device
        self._original_device = None
        self._available = self._check_available()

    @staticmethod
    def _check_available() -> bool:
        try:
            result = subprocess.run(
                ["SwitchAudioSource", "-c", "-t", "output"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except FileNotFoundError:
            logger.warning("SwitchAudioSource not found. Install: brew install switchaudio-osx")
            return False
        except Exception:
            return False

    def switch_to_recording(self):
        """Save current output and switch to multi-output device."""
        if not self._available:
            return
        try:
            result = subprocess.run(
                ["SwitchAudioSource", "-c", "-t", "output"],
                capture_output=True, text=True, timeout=5
            )
            self._original_device = result.stdout.strip()

            if self._original_device == self.recording_device:
                logger.info(f"Audio output already set to '{self.recording_device}'")
                return

            subprocess.run(
                ["SwitchAudioSource", "-s", self.recording_device, "-t", "output"],
                capture_output=True, text=True, timeout=5
            )
            logger.info(f"Audio output switched: '{self._original_device}' → '{self.recording_device}'")
        except Exception as e:
            logger.warning(f"Failed to switch audio output: {e}")

    def restore(self):
        """Restore original output device."""
        if not self._available or not self._original_device:
            return
        if self._original_device == self.recording_device:
            return
        try:
            subprocess.run(
                ["SwitchAudioSource", "-s", self._original_device, "-t", "output"],
                capture_output=True, text=True, timeout=5
            )
            logger.info(f"Audio output restored: '{self._original_device}'")
        except Exception as e:
            logger.warning(f"Failed to restore audio output: {e}")
        self._original_device = None


def cli():
    """Command-line interface for recording."""
    import click
    
    @click.command()
    @click.argument("output_name")
    @click.option("--fps", default=30, help="Frames per second for screen recording")
    @click.option("--no-screen", is_flag=True, help="Disable screen recording")
    @click.option("--no-camera", is_flag=True, help="Disable camera recording")
    @click.option("--no-audio", is_flag=True, help="Disable system audio recording")
    @click.option("--no-mic", is_flag=True, help="Disable microphone recording")
    @click.option("--no-bluetooth", is_flag=True, help="Disable Bluetooth monitoring")
    @click.option("--no-context", is_flag=True, help="Disable context monitoring")
    @click.option("--output-dir", default="~/Recordings", help="Output directory")
    @click.option("--no-anonymize", is_flag=True, help="Disable Bluetooth anonymization")
    def main(output_name, fps, no_screen, no_camera, no_audio, no_mic, no_bluetooth, no_context, output_dir, no_anonymize):
        """Record screen, audio, microphone, and Bluetooth RSSI.
        
        Press Ctrl+C to stop recording.
        """
        # Check permissions
        permissions = PermissionChecker.validate_all()
        missing = [k for k, v in permissions.items() if not v]
        if missing:
            print(f"⚠️  Missing permissions: {', '.join(missing)}")
            print("Please grant permissions in System Preferences > Privacy & Security")
            PermissionChecker.open_privacy_settings()
            return
        
        output_dir = Path(output_dir).expanduser()
        output_dir.mkdir(exist_ok=True)
        secure_directory(output_dir)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = output_dir / f"{output_name}_{timestamp}"
        session_dir.mkdir(exist_ok=True)
        secure_directory(session_dir)
        
        print(f"📁 Recording to: {session_dir}")
        print("Press Ctrl+C to stop...")
        
        components = []
        event_file = open(session_dir / "events.jsonl", "w")
        secure_file(session_dir / "events.jsonl")
        
        def log_event(event_type: str, data: dict):
            event = {"ts": time.time_ns(), "type": event_type, **data}
            event_file.write(json.dumps(event) + "\n")
            event_file.flush()
        
        # Prevent sleep
        sleep_inhibitor = SleepInhibitor()
        sleep_inhibitor.start()
        
        try:
            # Start screen recording
            if not no_screen:
                screen = ScreenRecorder(session_dir / "screen.mp4", fps=fps)
                if screen.start():
                    components.append(("screen", screen))
                    print("✓ Screen recording started")
                else:
                    print(f"✗ Screen recording failed: {screen.get_error()}")
            
            # Start camera recording
            if not no_camera:
                camera = CameraRecorder(session_dir / "camera.mp4", fps=fps)
                if camera.start():
                    components.append(("camera", camera))
                    print("✓ Camera recording started")
                else:
                    print(f"✗ Camera recording failed: {camera.get_error()}")

            # Start audio recording
            if not no_audio:
                audio = AudioRecorder(session_dir / "audio.wav", source="system")
                if audio.start():
                    components.append(("audio", audio))
                    print("✓ System audio recording started")
            
            # Start microphone recording
            if not no_mic:
                mic = AudioRecorder(session_dir / "mic.wav", source="microphone")
                if mic.start():
                    components.append(("mic", mic))
                    print("✓ Microphone recording started")
            
            # Start Bluetooth monitoring
            if not no_bluetooth:
                def bt_callback(bt_event: dict):
                    log_event("bluetooth", bt_event)
                
                bt = BluetoothMonitor(
                    callback=bt_callback,
                    anonymize=not no_anonymize
                )
                if bt.start():
                    components.append(("bluetooth", bt))
                    anon_status = "enabled" if not no_anonymize else "disabled"
                    print(f"✓ Bluetooth monitoring started (anonymization: {anon_status})")

            # Start context monitoring
            if not no_context:
                def ctx_callback(ctx_event: dict):
                    log_event("context", ctx_event)

                ctx = ContextMonitor(callback=ctx_callback)
                if ctx.start():
                    components.append(("context", ctx))
                    print("✓ Context monitoring started")

            log_event("recording", {"action": "start"})
            print("\n🔴 Recording...")
            
            # Wait for Ctrl+C
            start_time = time.time()
            while True:
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
                else:
                    time_str = f"{mins:02d}:{secs:02d}"
                print(f"\r  Duration: {time_str}", end="", flush=True)
                time.sleep(1)
        
        except KeyboardInterrupt:
            print("\n\n⏹️  Stopping...")
        
        finally:
            log_event("recording", {"action": "stop"})
            
            # Stop all components
            for name, component in components:
                print(f"  Stopping {name}...")
                component.stop()
            
            sleep_inhibitor.stop()
            event_file.close()
            secure_file(session_dir / "events.jsonl")
            
            print(f"\n✓ Recording saved to: {session_dir}")
    
    main()


if __name__ == "__main__":
    cli()
