#!/usr/bin/env python3
"""
Recording components for macOS Recorder.
Handles screen capture, audio recording, Bluetooth monitoring, and sleep prevention.
"""

import subprocess
import threading
import time
import json
import os
import asyncio
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime

import numpy as np


class SleepInhibitor:
    """Prevents macOS from sleeping during recording using caffeinate."""
    
    def __init__(self):
        self.process = None
    
    def start(self):
        """Start preventing sleep."""
        # caffeinate -dims: prevent display sleep, idle sleep, disk sleep, system sleep
        self.process = subprocess.Popen(
            ["caffeinate", "-dims"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    
    def stop(self):
        """Allow sleep again."""
        if self.process:
            self.process.terminate()
            self.process.wait()
            self.process = None


class ScreenRecorder:
    """Records screen using macOS screencapture or ffmpeg with avfoundation."""
    
    def __init__(self, output_path: Path, fps: int = 30):
        self.output_path = output_path
        self.fps = fps
        self.process = None
        self.recording = False
    
    def start(self):
        """Start screen recording using ffmpeg with avfoundation."""
        self.recording = True
        
        # Use ffmpeg with avfoundation for screen capture
        # List devices: ffmpeg -f avfoundation -list_devices true -i ""
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-f", "avfoundation",
            "-capture_cursor", "1",
            "-framerate", str(self.fps),
            "-i", "1:none",  # Screen index 1, no audio (captured separately)
            "-c:v", "h264_videotoolbox",  # Hardware accelerated encoding
            "-b:v", "5M",
            "-pix_fmt", "yuv420p",
            str(self.output_path)
        ]
        
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    
    def stop(self):
        """Stop screen recording."""
        self.recording = False
        if self.process:
            # Send 'q' to ffmpeg to stop gracefully
            try:
                self.process.stdin.write(b'q')
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except:
                self.process.terminate()
                self.process.wait()
            self.process = None


class AudioRecorder:
    """Records audio using sounddevice."""
    
    def __init__(self, output_path: Path, source: str = "microphone", sample_rate: int = 44100):
        self.output_path = output_path
        self.source = source
        self.sample_rate = sample_rate
        self.recording = False
        self.thread = None
        self.frames = []
    
    def start(self):
        """Start audio recording in a background thread."""
        self.recording = True
        self.frames = []
        self.thread = threading.Thread(target=self._record_thread, daemon=True)
        self.thread.start()
    
    def _record_thread(self):
        """Background thread for audio recording."""
        try:
            import sounddevice as sd
            import soundfile as sf
            
            # Get device based on source
            if self.source == "microphone":
                device = sd.default.device[0]  # Default input device
            else:
                # For system audio, need BlackHole or similar virtual audio device
                # Try to find BlackHole
                devices = sd.query_devices()
                device = None
                for i, d in enumerate(devices):
                    if "BlackHole" in d["name"] and d["max_input_channels"] > 0:
                        device = i
                        break
                
                if device is None:
                    print("Warning: BlackHole not found. System audio not recorded.")
                    print("Install BlackHole: brew install blackhole-2ch")
                    return
            
            def callback(indata, frames, time_info, status):
                if self.recording:
                    self.frames.append(indata.copy())
            
            with sd.InputStream(
                device=device,
                samplerate=self.sample_rate,
                channels=2,
                callback=callback
            ):
                while self.recording:
                    time.sleep(0.1)
            
            # Save to file
            if self.frames:
                audio_data = np.concatenate(self.frames, axis=0)
                sf.write(str(self.output_path), audio_data, self.sample_rate)
        
        except Exception as e:
            print(f"Audio recording error: {e}")
    
    def stop(self):
        """Stop audio recording."""
        self.recording = False
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None


class BluetoothMonitor:
    """Monitors Bluetooth device RSSI using bleak."""
    
    def __init__(self, callback: Callable[[str, int], None], scan_interval: float = 1.0):
        self.callback = callback
        self.scan_interval = scan_interval
        self.running = False
        self.thread = None
        self.loop = None
    
    def start(self):
        """Start Bluetooth monitoring in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._monitor_thread, daemon=True)
        self.thread.start()
    
    def _monitor_thread(self):
        """Background thread for Bluetooth monitoring."""
        try:
            from bleak import BleakScanner
            
            async def scan():
                while self.running:
                    try:
                        devices = await BleakScanner.discover(timeout=self.scan_interval)
                        for device in devices:
                            if device.name and device.rssi:
                                self.callback(device.name, device.rssi)
                    except Exception as e:
                        print(f"Bluetooth scan error: {e}")
                    
                    await asyncio.sleep(self.scan_interval)
            
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(scan())
        
        except Exception as e:
            print(f"Bluetooth monitoring error: {e}")
    
    def stop(self):
        """Stop Bluetooth monitoring."""
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None


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
    def main(output_name, fps, no_screen, no_audio, no_mic, no_bluetooth, output_dir):
        """Record screen, audio, microphone, and Bluetooth RSSI.
        
        Press Ctrl+C to stop recording.
        """
        output_dir = Path(output_dir).expanduser()
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = output_dir / f"{output_name}_{timestamp}"
        session_dir.mkdir(exist_ok=True)
        
        print(f"Recording to: {session_dir}")
        print("Press Ctrl+C to stop...")
        
        components = []
        event_file = open(session_dir / "events.jsonl", "w")
        
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
                screen.start()
                components.append(("screen", screen))
                print("✓ Screen recording started")
            
            # Start audio recording
            if not no_audio:
                audio = AudioRecorder(session_dir / "audio.wav", source="system")
                audio.start()
                components.append(("audio", audio))
                print("✓ System audio recording started")
            
            # Start microphone recording
            if not no_mic:
                mic = AudioRecorder(session_dir / "mic.wav", source="microphone")
                mic.start()
                components.append(("mic", mic))
                print("✓ Microphone recording started")
            
            # Start Bluetooth monitoring
            if not no_bluetooth:
                def bt_callback(name, rssi):
                    log_event("bluetooth", {"device": name, "rssi": rssi})
                    print(f"  📶 {name}: {rssi} dBm")
                
                bt = BluetoothMonitor(callback=bt_callback)
                bt.start()
                components.append(("bluetooth", bt))
                print("✓ Bluetooth monitoring started")
            
            log_event("recording", {"action": "start"})
            print("\n🔴 Recording...")
            
            # Wait for Ctrl+C
            start_time = time.time()
            while True:
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                print(f"\r  Duration: {mins:02d}:{secs:02d}", end="", flush=True)
                time.sleep(1)
        
        except KeyboardInterrupt:
            print("\n\nStopping...")
        
        finally:
            log_event("recording", {"action": "stop"})
            
            # Stop all components
            for name, component in components:
                print(f"Stopping {name}...")
                component.stop()
            
            sleep_inhibitor.stop()
            event_file.close()
            
            print(f"\n✓ Recording saved to: {session_dir}")
    
    main()


if __name__ == "__main__":
    cli()
