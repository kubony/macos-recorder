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

import numpy as np
import yaml

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PermissionChecker:
    """macOS 권한 상태 확인"""
    
    @staticmethod
    def check_screen_recording() -> bool:
        """화면 녹화 권한 확인"""
        try:
            from Quartz import CGPreflightScreenCaptureAccess
            return CGPreflightScreenCaptureAccess()
        except ImportError:
            # pyobjc 없으면 True 가정 (런타임에서 실패 처리)
            return True
        except:
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
    def validate_all() -> dict:
        """모든 권한 상태 확인"""
        return {
            "screen_recording": PermissionChecker.check_screen_recording(),
            "microphone": PermissionChecker.check_microphone(),
            "bluetooth": True,  # BLE는 별도 권한 불필요
        }


class SleepInhibitor:
    """Prevents macOS from sleeping during recording using IOKit."""
    
    def __init__(self, reason: str = "Recording in progress"):
        self.reason = reason
        self.assertion_id = None
        self._process = None  # Fallback to caffeinate
    
    def start(self):
        """Start preventing sleep."""
        try:
            # Try IOKit first (more efficient)
            IOKit = cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
            self.assertion_id = c_uint32(0)
            # IOPMAssertionCreateWithName is complex, fallback to caffeinate
            raise NotImplementedError("Using caffeinate fallback")
        except:
            # Fallback to caffeinate subprocess
            self._process = subprocess.Popen(
                ["caffeinate", "-dims"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info("Sleep prevention started (caffeinate)")
    
    def stop(self):
        """Allow sleep again."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except:
                self._process.kill()
            self._process = None
            logger.info("Sleep prevention stopped")


class ScreenRecorder:
    """Records screen using ffmpeg with avfoundation."""
    
    def __init__(self, output_path: Path, fps: int = 30, monitor_idx: int = 1):
        self.output_path = output_path
        self.fps = fps
        self.monitor_idx = monitor_idx
        self.process = None
        self.recording = False
        self._error = None
    
    def start(self) -> bool:
        """Start screen recording."""
        # Check ffmpeg availability
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        except FileNotFoundError:
            self._error = "ffmpeg not found. Install: brew install ffmpeg"
            logger.error(self._error)
            return False
        except subprocess.CalledProcessError as e:
            self._error = f"ffmpeg error: {e}"
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
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            logger.info(f"Screen recording started: {self.output_path}")
            return True
        except Exception as e:
            self._error = f"Failed to start screen recording: {e}"
            logger.error(self._error)
            self.recording = False
            return False
    
    def stop(self):
        """Stop screen recording."""
        self.recording = False
        if self.process:
            try:
                self.process.stdin.write(b'q')
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except:
                    self.process.kill()
            
            # Set secure file permissions
            if self.output_path.exists():
                os.chmod(self.output_path, 0o600)
            
            self.process = None
            logger.info("Screen recording stopped")
    
    def get_error(self) -> Optional[str]:
        return self._error


class AudioRecorder:
    """Records audio using sounddevice with streaming write (no memory leak)."""
    
    def __init__(self, output_path: Path, source: str = "microphone", sample_rate: int = 44100):
        self.output_path = output_path
        self.source = source
        self.sample_rate = sample_rate
        self.recording = False
        self.thread = None
        self._error = None
    
    def start(self) -> bool:
        """Start audio recording in a background thread."""
        self.recording = True
        self.thread = threading.Thread(target=self._record_thread, daemon=True)
        self.thread.start()
        return True
    
    def _record_thread(self):
        """Background thread for audio recording with streaming write."""
        try:
            import sounddevice as sd
            import soundfile as sf
            
            # Get device based on source
            device = None
            if self.source == "microphone":
                device = sd.default.device[0]
            else:
                # For system audio, need BlackHole
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if "BlackHole" in d.get("name", "") and d.get("max_input_channels", 0) > 0:
                        device = i
                        break
                
                if device is None:
                    self._error = "BlackHole not found. Install: brew install blackhole-2ch"
                    logger.warning(self._error)
                    self.recording = False
                    return
            
            # Streaming write - no memory accumulation!
            with sf.SoundFile(
                str(self.output_path), mode='w',
                samplerate=self.sample_rate,
                channels=2, subtype='PCM_16'
            ) as f:
                def callback(indata, frames, time_info, status):
                    if status:
                        logger.warning(f"Audio status: {status}")
                    if self.recording:
                        f.write(indata)  # Write directly to file
                
                with sd.InputStream(
                    device=device,
                    samplerate=self.sample_rate,
                    channels=2,
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
    
    def __init__(self, salt: str = None):
        self.salt = salt or os.urandom(16).hex()
        self.device_map = {}
    
    def anonymize(self, device_name: str) -> str:
        """Anonymize device name with consistent hash."""
        if not device_name:
            return "Unknown"
        
        if device_name not in self.device_map:
            hash_value = hashlib.sha256(
                (self.salt + device_name).encode()
            ).hexdigest()[:6]
            self.device_map[device_name] = f"Device_{hash_value}"
        
        return self.device_map[device_name]


class BluetoothMonitor:
    """Monitors Bluetooth device RSSI with anonymization."""
    
    def __init__(self, callback: Callable[[str, int], None], scan_interval: float = 1.0, anonymize: bool = True):
        self.callback = callback
        self.scan_interval = scan_interval
        self.anonymize = anonymize
        self.running = False
        self.thread = None
        self.loop = None
        self._anonymizer = BluetoothAnonymizer() if anonymize else None
        self._error = None
    
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
                        devices = await BleakScanner.discover(timeout=self.scan_interval)
                        for device in devices:
                            name = device.name or "Unknown"
                            rssi = device.rssi
                            
                            # Anonymize device name if enabled
                            if self._anonymizer:
                                name = self._anonymizer.anonymize(name)
                            
                            if rssi is not None:
                                self.callback(name, rssi)
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
        """Stop Bluetooth monitoring."""
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None
        logger.info("Bluetooth monitoring stopped")
    
    def get_error(self) -> Optional[str]:
        return self._error


def load_config(config_path: Path = None) -> dict:
    """Load configuration from YAML file."""
    default_config = {
        "recording": {
            "fps": 30,
            "quality": "high",
            "include_cursor": True,
        },
        "audio": {
            "system_audio": True,
            "microphone": True,
            "sample_rate": 44100,
        },
        "bluetooth": {
            "enabled": True,
            "scan_interval": 1.0,
            "anonymize": True,
        },
        "output": {
            "directory": str(Path.home() / "Recordings"),
            "format": "mp4",
        },
        "privacy": {
            "auto_delete_days": 30,
            "require_consent": True,
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


def cli():
    """Command-line interface for recording."""
    import click
    
    @click.command()
    @click.argument("output_name")
    @click.option("--fps", default=30, help="Frames per second for screen recording")
    @click.option("--no-screen", is_flag=True, help="Disable screen recording")
    @click.option("--no-audio", is_flag=True, help="Disable system audio recording")
    @click.option("--no-mic", is_flag=True, help="Disable microphone recording")
    @click.option("--no-bluetooth", is_flag=True, help="Disable Bluetooth monitoring")
    @click.option("--output-dir", default="~/Recordings", help="Output directory")
    @click.option("--no-anonymize", is_flag=True, help="Disable Bluetooth anonymization")
    def main(output_name, fps, no_screen, no_audio, no_mic, no_bluetooth, output_dir, no_anonymize):
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
        
        # Play start sound
        play_sound("Blow")
        
        try:
            # Start screen recording
            if not no_screen:
                screen = ScreenRecorder(session_dir / "screen.mp4", fps=fps)
                if screen.start():
                    components.append(("screen", screen))
                    print("✓ Screen recording started")
                else:
                    print(f"✗ Screen recording failed: {screen.get_error()}")
            
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
                def bt_callback(name, rssi):
                    log_event("bluetooth", {"device": name, "rssi": rssi})
                
                bt = BluetoothMonitor(
                    callback=bt_callback,
                    anonymize=not no_anonymize
                )
                if bt.start():
                    components.append(("bluetooth", bt))
                    anon_status = "enabled" if not no_anonymize else "disabled"
                    print(f"✓ Bluetooth monitoring started (anonymization: {anon_status})")
            
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
            
            # Play stop sound
            play_sound("Glass")
            
            print(f"\n✓ Recording saved to: {session_dir}")
    
    main()


if __name__ == "__main__":
    cli()
