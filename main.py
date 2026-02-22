#!/usr/bin/env python3
"""
macOS Recorder - Menu bar app for recording screen, audio, mic, and Bluetooth RSSI.
Inspired by ocap from the D2E project.
"""

import rumps
import threading
import time
import os
import json
import subprocess
from datetime import datetime
from pathlib import Path

from recorder import ScreenRecorder, AudioRecorder, BluetoothMonitor, SleepInhibitor


class RecorderApp(rumps.App):
    """Menu bar application for macOS recording."""
    
    def __init__(self):
        super().__init__(
            name="macOS Recorder",
            icon="🔴",
            quit_button=None  # Custom quit handling
        )
        
        self.recording = False
        self.start_time = None
        self.output_dir = Path.home() / "Recordings"
        self.output_dir.mkdir(exist_ok=True)
        
        # Recording components
        self.screen_recorder = None
        self.audio_recorder = None
        self.mic_recorder = None
        self.bluetooth_monitor = None
        self.sleep_inhibitor = None
        
        # Event log
        self.events = []
        self.event_file = None
        
        # Settings
        self.settings = {
            "fps": 30,
            "record_screen": True,
            "record_audio": True,
            "record_mic": True,
            "record_bluetooth": True,
        }
        
        # Build menu
        self.menu = [
            rumps.MenuItem("Start Recording", callback=self.toggle_recording, key="r"),
            None,  # Separator
            rumps.MenuItem("Status: Idle"),
            rumps.MenuItem("Duration: --:--"),
            None,  # Separator
            rumps.MenuItem("Settings", callback=self.open_settings),
            rumps.MenuItem("Open Recordings Folder", callback=self.open_recordings),
            None,  # Separator
            rumps.MenuItem("Quit", callback=self.quit_app, key="q"),
        ]
        
        # Timer for updating duration
        self.timer = rumps.Timer(self.update_duration, 1)
    
    def toggle_recording(self, sender):
        """Start or stop recording."""
        if not self.recording:
            self.start_recording()
            sender.title = "Stop Recording"
            self.icon = "🔴"  # Recording indicator
        else:
            self.stop_recording()
            sender.title = "Start Recording"
            self.icon = "⚫"  # Idle indicator
    
    def start_recording(self):
        """Initialize and start all recording components."""
        self.recording = True
        self.start_time = time.time()
        self.events = []
        
        # Create session directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.output_dir / f"recording_{timestamp}"
        self.session_dir.mkdir(exist_ok=True)
        
        # Open event log
        self.event_file = open(self.session_dir / "events.jsonl", "w")
        
        # Prevent sleep
        self.sleep_inhibitor = SleepInhibitor()
        self.sleep_inhibitor.start()
        
        # Start screen recording
        if self.settings["record_screen"]:
            self.screen_recorder = ScreenRecorder(
                output_path=self.session_dir / "screen.mp4",
                fps=self.settings["fps"]
            )
            self.screen_recorder.start()
        
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
                callback=self.on_bluetooth_event
            )
            self.bluetooth_monitor.start()
        
        # Update status
        self.menu["Status: Idle"].title = "Status: Recording..."
        self.timer.start()
        
        self.log_event("recording", {"action": "start"})
        rumps.notification(
            title="Recording Started",
            subtitle="",
            message=f"Saving to {self.session_dir.name}"
        )
    
    def stop_recording(self):
        """Stop all recording components and save files."""
        self.recording = False
        self.timer.stop()
        
        self.log_event("recording", {"action": "stop"})
        
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
        
        # Close event log
        if self.event_file:
            self.event_file.close()
            self.event_file = None
        
        # Update status
        self.menu["Status: Recording..."].title = "Status: Idle"
        self.menu["Duration: --:--"].title = "Duration: --:--"
        
        rumps.notification(
            title="Recording Stopped",
            subtitle="",
            message=f"Saved to {self.session_dir.name}"
        )
    
    def log_event(self, event_type: str, data: dict):
        """Log an event with timestamp."""
        event = {
            "ts": time.time_ns(),
            "type": event_type,
            **data
        }
        if self.event_file:
            self.event_file.write(json.dumps(event) + "\n")
            self.event_file.flush()
    
    def on_bluetooth_event(self, device_name: str, rssi: int):
        """Callback for Bluetooth RSSI updates."""
        self.log_event("bluetooth", {
            "device": device_name,
            "rssi": rssi
        })
    
    def update_duration(self, sender):
        """Update the duration display."""
        if self.recording and self.start_time:
            elapsed = int(time.time() - self.start_time)
            minutes, seconds = divmod(elapsed, 60)
            hours, minutes = divmod(minutes, 60)
            if hours > 0:
                duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                duration_str = f"{minutes:02d}:{seconds:02d}"
            self.menu["Duration: --:--"].title = f"Duration: {duration_str}"
    
    def open_settings(self, sender):
        """Open settings dialog."""
        # For now, just show a notification
        rumps.notification(
            title="Settings",
            subtitle="",
            message="Edit config.yaml to change settings"
        )
    
    def open_recordings(self, sender):
        """Open the recordings folder in Finder."""
        subprocess.run(["open", str(self.output_dir)])
    
    def quit_app(self, sender):
        """Clean up and quit the application."""
        if self.recording:
            self.stop_recording()
        rumps.quit_application()


def main():
    """Entry point for the menu bar app."""
    app = RecorderApp()
    app.run()


if __name__ == "__main__":
    main()
