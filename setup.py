"""
py2app build script for macOS Recorder.

Usage:
    python setup.py py2app
"""
from setuptools import setup

APP = ['main.py']
DATA_FILES = []

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'icon.icns',
    'plist': {
        'CFBundleName': 'macOS Recorder',
        'CFBundleDisplayName': 'macOS Recorder',
        'CFBundleIdentifier': 'com.kubony.macos-recorder',
        'CFBundleVersion': '1.2.0',
        'CFBundleShortVersionString': '1.2.0',
        'LSUIElement': True,  # Menu bar app (no dock icon)
        'NSMicrophoneUsageDescription': 'macOS Recorder needs microphone access to record audio.',
        'NSScreenCaptureUsageDescription': 'macOS Recorder needs screen recording permission.',
        'NSBluetoothAlwaysUsageDescription': 'macOS Recorder monitors Bluetooth RSSI signals.',
        'NSCameraUsageDescription': 'macOS Recorder needs camera access to record video.',
        'NSLocationWhenInUseUsageDescription': 'macOS Recorder uses location for place labeling.',
    },
    'packages': ['bleak'],
    'includes': ['rumps'],
    'excludes': ['tkinter', 'unittest'],
}

setup(
    app=APP,
    name='macOS Recorder',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
