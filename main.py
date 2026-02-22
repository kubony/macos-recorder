#!/usr/bin/env python3
"""
macOS Recorder - Menu bar app for recording screen, audio, mic, and Bluetooth RSSI.
Inspired by ocap from the D2E project.

Improvements based on expert review:
- Permission checking and onboarding
- Config file loading
- Recording options toggle
- Status feedback (icon animation, sounds, duration)
- Consent mechanism
- Secure file handling
- P0 fixes: thread safety, event buffering, crash recovery, size update optimization
"""

import rumps
import threading
import time
import os
import json
import subprocess
import signal
import atexit
import logging
from datetime import datetime
from pathlib import Path

from recorder import (
    ScreenRecorder, AudioRecorder, BluetoothMonitor, SleepInhibitor,
    PermissionChecker, load_config, secure_directory, secure_file, play_sound
)

# Setup logging
logger = logging.getLogger(__name__)


# P0 fix: State file for crash recovery
STATE_FILE = Path.home() / ".macos-recorder" / "state.json"


class ConsentManager:
    """Manages user consent for data collection (GDPR compliance)."""
    
    CONSENT_FILE = Path.home() / ".macos-recorder" / "consent.json"
    CONSENT_VERSION = "1.0"
    
    def __init__(self):
        self.CONSENT_FILE.parent.mkdir(exist_ok=True)
    
    def has_consent(self) -> bool:
        """Check if user has given consent."""
        if not self.CONSENT_FILE.exists():
            return False
        try:
            data = json.loads(self.CONSENT_FILE.read_text())
            return data.get("granted", False) and data.get("version") == self.CONSENT_VERSION
        except (json.JSONDecodeError, KeyError, IOError) as e:
            logger.warning(f"Consent file error: {e}")
            return False
    
    def request_consent(self) -> bool:
        """Show consent dialog and save response."""
        response = rumps.alert(
            title="📹 개인정보 수집 동의",
            message=(
                "macOS Recorder는 다음 정보를 수집합니다:\n\n"
                "• 화면 녹화 (모든 화면 내용)\n"
                "• 시스템 오디오 및 마이크\n"
                "• 블루투스 기기 신호 강도 (익명화됨)\n\n"
                "수집된 데이터는 로컬에만 저장되며,\n"
                "언제든지 삭제할 수 있습니다.\n\n"
                "동의하시겠습니까?"
            ),
            ok="동의",
            cancel="거부"
        )
        
        consent_data = {
            "granted": response == 1,
            "version": self.CONSENT_VERSION,
            "timestamp": datetime.now().isoformat()
        }
        
        self.CONSENT_FILE.write_text(json.dumps(consent_data, indent=2))
        return response == 1


class RecorderApp(rumps.App):
    """Menu bar application for macOS recording."""
    
    def __init__(self):
        super().__init__(
            name="macOS Recorder",
            icon=None,
            title="⚫",
            quit_button=None
        )
        
        # P0 fix: thread-safe recording flag
        self._recording = False
        self._recording_lock = threading.Lock()
        self.start_time = None
        
        # P0 fix: event buffering
        self._event_buffer = []
        self._event_buffer_lock = threading.Lock()
        self._last_size_update = 0  # P0 fix: throttle size updates
        self._cached_size = 0
        
        # Load config
        self.config = load_config()
        self.output_dir = Path(self.config["output"]["directory"]).expanduser()
        self.output_dir.mkdir(exist_ok=True)
        secure_directory(self.output_dir)
        
        # P0 fix: recover from previous crash
        self._recover_from_crash()
        
        # P0 fix: register cleanup handlers
        atexit.register(self._cleanup_on_exit)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        # Recording settings (toggleable)
        self.settings = {
            "record_screen": True,
            "record_audio": self.config["audio"]["system_audio"],
            "record_mic": self.config["audio"]["microphone"],
            "record_bluetooth": self.config["bluetooth"]["enabled"],
            "fps": self.config["recording"]["fps"],
            "anonymize_bluetooth": self.config["bluetooth"].get("anonymize", True),
        }
        
        # Recording components
        self.screen_recorder = None
        self.audio_recorder = None
        self.mic_recorder = None
        self.bluetooth_monitor = None
        self.sleep_inhibitor = None
        
        # Event log
        self.event_file = None
        self.session_dir = None
        
        # Consent manager
        self.consent_manager = ConsentManager()
        
        # Build menu with toggles
        self._build_menu()
        
        # Timer for updating duration
        self.duration_timer = rumps.Timer(self._update_duration, 1)
        
        # Check permissions and consent on startup
        self._check_startup_requirements()
    
    @property
    def recording(self) -> bool:
        with self._recording_lock:
            return self._recording
    
    @recording.setter
    def recording(self, value: bool):
        with self._recording_lock:
            self._recording = value
    
    def _recover_from_crash(self):
        """Check for incomplete recording from previous crash."""
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                if state.get("recording") and state.get("session_dir"):
                    session_dir = Path(state["session_dir"])
                    if session_dir.exists():
                        # Mark as incomplete
                        incomplete_marker = session_dir / "INCOMPLETE"
                        incomplete_marker.write_text(f"Crashed at: {datetime.now().isoformat()}")
                        rumps.notification(
                            title="macOS Recorder",
                            subtitle="",
                            message=f"이전 녹화가 비정상 종료됨:\n{session_dir.name}"
                        )
            except:
                pass
            finally:
                STATE_FILE.unlink(missing_ok=True)
    
    def _save_state(self):
        """Save current recording state for crash recovery."""
        STATE_FILE.parent.mkdir(exist_ok=True)
        state = {
            "recording": self.recording,
            "session_dir": str(self.session_dir) if self.session_dir else None,
            "start_time": self.start_time,
            "pid": os.getpid()
        }
        # P0 fix (Data Integrity): atomic write via temp file + rename
        temp_file = STATE_FILE.with_suffix('.tmp')
        temp_file.write_text(json.dumps(state))
        temp_file.replace(STATE_FILE)  # Atomic on POSIX
    
    def _clear_state(self):
        """Clear state file after normal stop."""
        STATE_FILE.unlink(missing_ok=True)
    
    def _handle_signal(self, signum, frame):
        """Handle shutdown signals gracefully."""
        if self.recording:
            self.stop_recording()
        rumps.quit_application()
    
    def _cleanup_on_exit(self):
        """Cleanup on normal exit."""
        if self.recording:
            self.stop_recording()
        self._clear_state()
    
    def _build_menu(self):
        """Build the menu with toggle options."""
        self.start_item = rumps.MenuItem("▶️ Start Recording", callback=self.toggle_recording, key="r")
        
        # Recording option toggles
        self.screen_toggle = rumps.MenuItem("📺 Screen", callback=self._toggle_screen)
        self.screen_toggle.state = self.settings["record_screen"]
        
        self.audio_toggle = rumps.MenuItem("🔊 System Audio", callback=self._toggle_audio)
        self.audio_toggle.state = self.settings["record_audio"]
        
        self.mic_toggle = rumps.MenuItem("🎤 Microphone", callback=self._toggle_mic)
        self.mic_toggle.state = self.settings["record_mic"]
        
        self.bt_toggle = rumps.MenuItem("📶 Bluetooth", callback=self._toggle_bluetooth)
        self.bt_toggle.state = self.settings["record_bluetooth"]
        
        # Status items
        self.status_item = rumps.MenuItem("Status: Idle")
        self.duration_item = rumps.MenuItem("Duration: --:--")
        self.size_item = rumps.MenuItem("Size: --")
        
        self.menu = [
            self.start_item,
            None,  # Separator
            "Recording Options:",
            self.screen_toggle,
            self.audio_toggle,
            self.mic_toggle,
            self.bt_toggle,
            None,
            self.status_item,
            self.duration_item,
            self.size_item,
            None,
            rumps.MenuItem("📁 Open Recordings", callback=self.open_recordings),
            rumps.MenuItem("⚙️ Settings", callback=self.open_settings),
            rumps.MenuItem("❓ About", callback=self.show_about),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app, key="q"),
        ]
    
    def _toggle_screen(self, sender):
        if not self.recording:
            self.settings["record_screen"] = not self.settings["record_screen"]
            sender.state = self.settings["record_screen"]
            self._persist_settings()
    
    def _toggle_audio(self, sender):
        if not self.recording:
            self.settings["record_audio"] = not self.settings["record_audio"]
            sender.state = self.settings["record_audio"]
            self._persist_settings()
    
    def _toggle_mic(self, sender):
        if not self.recording:
            self.settings["record_mic"] = not self.settings["record_mic"]
            sender.state = self.settings["record_mic"]
            self._persist_settings()
    
    def _toggle_bluetooth(self, sender):
        if not self.recording:
            self.settings["record_bluetooth"] = not self.settings["record_bluetooth"]
            sender.state = self.settings["record_bluetooth"]
            self._persist_settings()
    
    def _persist_settings(self):
        """P0 fix (Config): Save UI settings to config file."""
        try:
            import yaml
            config_path = Path(__file__).parent / "config.yaml"
            
            # Update config with current settings
            self.config["audio"]["system_audio"] = self.settings["record_audio"]
            self.config["audio"]["microphone"] = self.settings["record_mic"]
            self.config["bluetooth"]["enabled"] = self.settings["record_bluetooth"]
            
            with open(config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
            
            logger.info("Settings persisted to config.yaml")
        except Exception as e:
            logger.warning(f"Failed to persist settings: {e}")
    
    def _check_startup_requirements(self):
        """Check permissions and consent on startup."""
        # Check consent
        if self.config["privacy"].get("require_consent", True):
            if not self.consent_manager.has_consent():
                if not self.consent_manager.request_consent():
                    rumps.notification(
                        title="macOS Recorder",
                        subtitle="",
                        message="동의가 필요합니다. 앱을 다시 시작하세요."
                    )
        
        # Check permissions
        permissions = PermissionChecker.validate_all()
        missing = [k for k, v in permissions.items() if not v]
        
        if missing:
            self._show_permission_alert(missing)
    
    def _show_permission_alert(self, missing: list):
        """Show alert for missing permissions."""
        response = rumps.alert(
            title="🔐 권한 필요",
            message=(
                f"다음 권한이 필요합니다:\n\n"
                f"• {', '.join(missing)}\n\n"
                "시스템 환경설정을 열까요?"
            ),
            ok="설정 열기",
            cancel="나중에"
        )
        
        if response == 1:
            PermissionChecker.open_privacy_settings()
    
    def toggle_recording(self, sender):
        """Start or stop recording."""
        if not self.recording:
            self.start_recording()
        else:
            self.stop_recording()
    
    def start_recording(self):
        """Initialize and start all recording components."""
        # Validate at least one recording option
        if not any([
            self.settings["record_screen"],
            self.settings["record_audio"],
            self.settings["record_mic"],
            self.settings["record_bluetooth"]
        ]):
            rumps.notification(
                title="macOS Recorder",
                subtitle="",
                message="최소 하나의 녹화 옵션을 선택하세요."
            )
            return
        
        self.recording = True
        self.start_time = time.time()
        
        # P0 fix (Real-time): Create reference timestamp for A/V sync
        self._reference_time = {
            "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            "iso": datetime.now().isoformat()
        }
        
        # Create session directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.output_dir / f"recording_{timestamp}"
        self.session_dir.mkdir(exist_ok=True)
        secure_directory(self.session_dir)
        
        # Write reference timestamp to session (for post-processing sync)
        ref_file = self.session_dir / "reference_time.json"
        ref_file.write_text(json.dumps(self._reference_time, indent=2))
        secure_file(ref_file)
        
        # Open event log
        self.event_file = open(self.session_dir / "events.jsonl", "w")
        
        # Prevent sleep
        self.sleep_inhibitor = SleepInhibitor()
        self.sleep_inhibitor.start()
        
        # Play start sound
        play_sound("Blow")
        
        errors = []
        
        # Start screen recording
        if self.settings["record_screen"]:
            self.screen_recorder = ScreenRecorder(
                output_path=self.session_dir / "screen.mp4",
                fps=self.settings["fps"]
            )
            if not self.screen_recorder.start():
                errors.append(f"Screen: {self.screen_recorder.get_error()}")
        
        # Start audio recording (system audio)
        if self.settings["record_audio"]:
            self.audio_recorder = AudioRecorder(
                output_path=self.session_dir / "audio.wav",
                source="system"
            )
            self.audio_recorder.start()
        
        # Start microphone recording
        if self.settings["record_mic"]:
            self.mic_recorder = AudioRecorder(
                output_path=self.session_dir / "mic.wav",
                source="microphone"
            )
            self.mic_recorder.start()
        
        # Start Bluetooth monitoring
        if self.settings["record_bluetooth"]:
            self.bluetooth_monitor = BluetoothMonitor(
                callback=self._on_bluetooth_event,
                anonymize=self.settings["anonymize_bluetooth"]
            )
            self.bluetooth_monitor.start()
        
        # Log recording start
        self._log_event("recording", {
            "action": "start",
            "options": {k: v for k, v in self.settings.items()}
        })
        
        # P0 fix: Save state for crash recovery
        self._save_state()
        
        # Update UI
        self.start_item.title = "⏹️ Stop Recording"
        self.title = "🔴"
        self.status_item.title = "Status: Recording..."
        self.duration_timer.start()
        
        # Disable toggles during recording
        self.screen_toggle.set_callback(None)
        self.audio_toggle.set_callback(None)
        self.mic_toggle.set_callback(None)
        self.bt_toggle.set_callback(None)
        
        # Show notification
        message = f"저장 위치: {self.session_dir.name}"
        if errors:
            message += f"\n⚠️ 오류: {'; '.join(errors)}"
        
        rumps.notification(
            title="🔴 녹화 시작",
            subtitle="",
            message=message
        )
    
    def stop_recording(self):
        """Stop all recording components and save files."""
        self.recording = False
        self.duration_timer.stop()
        
        self._log_event("recording", {"action": "stop"})
        
        # Stop all components
        if self.screen_recorder:
            self.screen_recorder.stop()
            self.screen_recorder = None
        
        if self.audio_recorder:
            self.audio_recorder.stop()
            self.audio_recorder = None
        
        if self.mic_recorder:
            self.mic_recorder.stop()
            self.mic_recorder = None
        
        if self.bluetooth_monitor:
            self.bluetooth_monitor.stop()
            self.bluetooth_monitor = None
        
        if self.sleep_inhibitor:
            self.sleep_inhibitor.stop()
            self.sleep_inhibitor = None
        
        # P0 fix: Flush remaining events before closing
        self._flush_events()
        
        # Close event log
        if self.event_file:
            self.event_file.close()
            secure_file(self.session_dir / "events.jsonl")
            self.event_file = None
        
        # P1 fix (Data Integrity): Create completion marker
        if self.session_dir:
            completion_marker = self.session_dir / "COMPLETE"
            completion_data = {
                "completed_at": datetime.now().isoformat(),
                "duration_seconds": int(time.time() - self.start_time) if self.start_time else 0,
                "files": [f.name for f in self.session_dir.iterdir() if f.is_file()]
            }
            completion_marker.write_text(json.dumps(completion_data, indent=2))
            secure_file(completion_marker)
        
        # P0 fix: Clear state file after normal stop
        self._clear_state()
        
        # Play stop sound
        play_sound("Glass")
        
        # Calculate total size
        total_size = sum(
            f.stat().st_size for f in self.session_dir.iterdir() if f.is_file()
        ) if self.session_dir else 0
        size_mb = total_size / (1024 * 1024)
        
        # Update UI
        self.start_item.title = "▶️ Start Recording"
        self.title = "⚫"
        self.status_item.title = "Status: Idle"
        self.duration_item.title = "Duration: --:--"
        self.size_item.title = "Size: --"
        
        # Re-enable toggles
        self.screen_toggle.set_callback(self._toggle_screen)
        self.audio_toggle.set_callback(self._toggle_audio)
        self.mic_toggle.set_callback(self._toggle_mic)
        self.bt_toggle.set_callback(self._toggle_bluetooth)
        
        rumps.notification(
            title="⏹️ 녹화 완료",
            subtitle="",
            message=f"{self.session_dir.name}\n크기: {size_mb:.1f} MB"
        )
    
    def _log_event(self, event_type: str, data: dict):
        """Log an event with timestamp (buffered for performance)."""
        event = {
            "ts": time.time_ns(),
            "ts_monotonic": time.monotonic_ns(),  # P0 fix: add monotonic timestamp
            "type": event_type,
            **data
        }
        
        with self._event_buffer_lock:
            self._event_buffer.append(event)
            # P0 fix: flush every 100 events, on important events, OR every 1 second
            should_flush = (
                len(self._event_buffer) >= 100 or 
                event_type == "recording" or  # Always flush start/stop
                (time.monotonic() - getattr(self, '_last_flush_time', 0)) >= 1.0
            )
        
        if should_flush:
            self._flush_events()
    
    def _flush_events(self):
        """Flush buffered events to file with error recovery."""
        if not self.event_file:
            return
        
        # P1 fix (Concurrency): update _last_flush_time inside lock
        with self._event_buffer_lock:
            events_to_write = self._event_buffer.copy()
            self._event_buffer.clear()
            self._last_flush_time = time.monotonic()
        
        # File I/O outside lock to avoid blocking
        failed_events = []
        for event in events_to_write:
            try:
                self.event_file.write(json.dumps(event) + "\n")
            except (IOError, OSError) as e:
                logger.error(f"Failed to write event: {e}")
                failed_events.append(event)
        
        try:
            self.event_file.flush()
        except (IOError, OSError) as e:
            logger.error(f"Failed to flush events: {e}")
        
        # Re-add failed events to buffer for retry
        if failed_events:
            with self._event_buffer_lock:
                self._event_buffer.extend(failed_events)
    
    def _on_bluetooth_event(self, device_name: str, rssi: int):
        """Callback for Bluetooth RSSI updates."""
        self._log_event("bluetooth", {
            "device": device_name,
            "rssi": rssi
        })
    
    def _update_duration(self, sender):
        """Update the duration and size display."""
        if self.recording and self.start_time:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            hours, mins = divmod(mins, 60)
            
            if hours > 0:
                duration_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
            else:
                duration_str = f"{mins:02d}:{secs:02d}"
            
            self.duration_item.title = f"Duration: {duration_str}"
            self.title = f"🔴 {mins:02d}:{secs:02d}"
            
            # P0 fix: Update size only every 10 seconds to reduce I/O
            current_time = time.time()
            if current_time - self._last_size_update >= 10:
                self._last_size_update = current_time
                if self.session_dir and self.session_dir.exists():
                    try:
                        self._cached_size = sum(
                            f.stat().st_size for f in self.session_dir.iterdir() if f.is_file()
                        )
                    except:
                        pass
            
            size_mb = self._cached_size / (1024 * 1024)
            self.size_item.title = f"Size: {size_mb:.1f} MB"
    
    def open_settings(self, sender):
        """Open config file for editing."""
        config_path = Path(__file__).parent / "config.yaml"
        if config_path.exists():
            subprocess.run(["open", str(config_path)])
        else:
            rumps.notification(
                title="Settings",
                subtitle="",
                message="config.yaml 파일이 없습니다."
            )
    
    def open_recordings(self, sender):
        """Open the recordings folder in Finder."""
        subprocess.run(["open", str(self.output_dir)])
    
    def show_about(self, sender):
        """Show about dialog."""
        rumps.alert(
            title="macOS Recorder",
            message=(
                "Version 1.1.0\n\n"
                "화면, 오디오, 마이크, 블루투스 신호를 녹화합니다.\n\n"
                "Inspired by ocap from the D2E project.\n"
                "https://github.com/kubony/macos-recorder"
            )
        )
    
    def quit_app(self, sender):
        """Clean up and quit the application."""
        if self.recording:
            response = rumps.alert(
                title="녹화 중",
                message="녹화 중입니다. 정말 종료하시겠습니까?",
                ok="종료",
                cancel="취소"
            )
            if response != 1:
                return
            self.stop_recording()
        
        rumps.quit_application()


def main():
    """Entry point for the menu bar app."""
    app = RecorderApp()
    app.run()


if __name__ == "__main__":
    main()
