"""
Floating attendee panel for manually recording who is present during a session.
BLE device-to-person mapping is done statistically in post-processing,
not in this UI. This panel simply logs person_join/person_leave events.

UX: Enter-key driven. Mouse optional.
- Name field: Enter = join (or toggle departure if already present)
- Location field: Enter = log location change
- Note field: Enter = add note
- All actions auto-refocus to name input for continuous entry.
"""

import json
import logging
from pathlib import Path
from datetime import datetime

import objc
from Foundation import NSObject, NSMakeRect
from AppKit import (
    NSPanel, NSTextField, NSScrollView, NSTableView,
    NSTableColumn, NSButton, NSFont, NSColor, NSComboBox,
    NSFloatingWindowLevel, NSBackingStoreBuffered,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskUtilityWindow,
    NSBezelStyleRounded, NSEventModifierFlagCommand,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".macos-recorder"
KNOWN_PEOPLE_FILE = CONFIG_DIR / "known_people.json"
KNOWN_LOCATIONS_FILE = CONFIG_DIR / "known_locations.json"


class _AttendeeDelegate(NSObject):
    """NSTableView data source + action targets."""

    def initWithPanel_(self, panel):
        self = objc.super(_AttendeeDelegate, self).init()
        if self is None:
            return None
        self._panel = panel
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self._panel._attendees)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self._panel._attendees):
            return ""
        person = self._panel._attendees[row]
        col_id = str(column.identifier())
        if col_id == "name":
            if person.get("departed"):
                return f"\U0001f534 {person['name']}"
            if person.get("remote"):
                return f"\U0001f4fa {person['name']}"
            return f"\U0001f7e2 {person['name']}"
        elif col_id == "time":
            if person.get("departed"):
                return f"{person.get('join_time', '')} ~ {person.get('depart_time', '')}"
            return f"{person.get('join_time', '')} ~"
        return ""

    @objc.typedSelector(b'v@:@')
    def addPerson_(self, sender):
        self._panel._add_person_from_input()

    @objc.typedSelector(b'v@:@')
    def markDeparture_(self, sender):
        self._panel._mark_selected_departure()

    @objc.typedSelector(b'v@:@')
    def addNote_(self, sender):
        if self._panel._note_field:
            text = str(self._panel._note_field.stringValue())
            if text.strip():
                self._panel._add_note(text.strip())
                self._panel._note_field.setStringValue_("")
        self._panel._focus_name_input()

    @objc.typedSelector(b'v@:@')
    def locationChanged_(self, sender):
        self._panel._handle_location_change()
        self._panel._focus_name_input()


class LabelPanel:
    """Floating panel for manually recording attendees during a session."""

    def __init__(self, log_event_callback):
        self.log_event = log_event_callback
        self._attendees = []
        self._shown = False
        self._panel = None
        self._table_view = None
        self._table_delegate = None
        self._name_input = None
        self._remote_checkbox = None
        self._note_field = None
        self._summary_label = None
        self._location_field = None
        self._current_location = ""

        self._known_people = self._load_known_people()
        self._known_locations = self._load_known_locations()

    # --- Persistence ---

    @staticmethod
    def _load_known_people():
        if KNOWN_PEOPLE_FILE.exists():
            try:
                return json.loads(KNOWN_PEOPLE_FILE.read_text())
            except Exception:
                pass
        return []

    @staticmethod
    def _load_known_locations():
        if KNOWN_LOCATIONS_FILE.exists():
            try:
                return json.loads(KNOWN_LOCATIONS_FILE.read_text())
            except Exception:
                pass
        return []

    def _save_known_people(self):
        CONFIG_DIR.mkdir(exist_ok=True)
        temp = KNOWN_PEOPLE_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps(self._known_people, ensure_ascii=False, indent=2))
        temp.replace(KNOWN_PEOPLE_FILE)

    def _save_known_locations(self):
        CONFIG_DIR.mkdir(exist_ok=True)
        temp = KNOWN_LOCATIONS_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps(self._known_locations, ensure_ascii=False, indent=2))
        temp.replace(KNOWN_LOCATIONS_FILE)

    # --- Show / Hide ---

    def show(self):
        if self._shown:
            return
        try:
            self._create_panel()
            from AppKit import NSApp
            NSApp.activateIgnoringOtherApps_(True)
            self._panel.makeKeyAndOrderFront_(None)
            self._shown = True
            self._focus_name_input()
            logger.info("Label panel shown")
        except Exception as e:
            logger.error(f"Failed to show label panel: {e}", exc_info=True)

    def hide(self):
        if self._location_field:
            self._handle_location_change()
        if self._panel:
            self._panel.orderOut_(None)
            self._panel = None
        self._table_view = None
        self._table_delegate = None
        self._shown = False
        self._save_known_people()
        self._save_known_locations()
        logger.info("Label panel hidden")

    def update_devices(self, bt_event):
        pass

    # --- Focus ---

    def _focus_name_input(self):
        if self._panel and self._name_input:
            self._panel.makeFirstResponder_(self._name_input)

    # --- Location ---

    def _handle_location_change(self):
        if not self._location_field:
            return
        new_loc = str(self._location_field.stringValue()).strip()
        if new_loc == self._current_location:
            return
        if not new_loc and not self._current_location:
            return

        action = "location_set" if not self._current_location else "location_changed"
        self.log_event("label", {
            "action": action,
            "location": new_loc,
            "previous": self._current_location,
        })
        self._current_location = new_loc

        if new_loc and new_loc not in self._known_locations:
            self._known_locations.append(new_loc)
            self._save_known_locations()

    # --- Attendee management ---

    def _add_person_from_input(self):
        if not self._name_input:
            return
        name = str(self._name_input.stringValue()).strip()
        if not name:
            return

        # Toggle: if already present, mark departure
        for person in self._attendees:
            if person["name"] == name and not person.get("departed"):
                person["departed"] = True
                person["depart_time"] = datetime.now().strftime("%H:%M")
                self.log_event("label", {
                    "action": "person_leave",
                    "person": name,
                    "location": self._current_location,
                })
                self._name_input.setStringValue_("")
                self._refresh_ui()
                self._focus_name_input()
                return

        now = datetime.now().strftime("%H:%M")
        is_remote = bool(self._remote_checkbox and self._remote_checkbox.state())
        self._attendees.append({
            "name": name,
            "join_time": now,
            "departed": False,
            "depart_time": "",
            "remote": is_remote,
        })

        if name not in self._known_people:
            self._known_people.append(name)
            self._save_known_people()
            if self._name_input and hasattr(self._name_input, 'addItemWithObjectValue_'):
                self._name_input.addItemWithObjectValue_(name)

        self.log_event("label", {
            "action": "person_join",
            "person": name,
            "mode": "remote" if is_remote else "in_person",
            "location": self._current_location,
        })

        self._name_input.setStringValue_("")
        self._refresh_ui()
        self._focus_name_input()

    def _mark_selected_departure(self):
        if not self._table_view:
            return
        row = self._table_view.selectedRow()
        if row < 0 or row >= len(self._attendees):
            return
        person = self._attendees[row]
        if person.get("departed"):
            return
        person["departed"] = True
        person["depart_time"] = datetime.now().strftime("%H:%M")
        self.log_event("label", {
            "action": "person_leave",
            "person": person["name"],
            "location": self._current_location,
        })
        self._refresh_ui()

    def _add_note(self, text):
        if text:
            self.log_event("label", {
                "action": "note",
                "text": text,
                "location": self._current_location,
            })

    def _refresh_ui(self):
        if self._table_view:
            self._table_view.reloadData()
            if self._attendees:
                self._table_view.scrollRowToVisible_(len(self._attendees) - 1)
        if self._summary_label:
            in_person = [p["name"] for p in self._attendees
                         if not p.get("departed") and not p.get("remote")]
            remote = [p["name"] for p in self._attendees
                      if not p.get("departed") and p.get("remote")]
            parts = []
            if in_person:
                parts.append(f"\U0001f465 {len(in_person)}\uba85 \ub300\uba74: {', '.join(in_person)}")
            if remote:
                parts.append(f"\U0001f4fa {len(remote)}\uba85 \ud654\uc0c1: {', '.join(remote)}")
            if parts:
                self._summary_label.setStringValue_(" | ".join(parts))
            else:
                self._summary_label.setStringValue_("\U0001f465 \ucc38\uc11d\uc790 0\uba85")

    # --- Panel UI ---

    def _create_panel(self):
        style = (
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable | NSWindowStyleMaskUtilityWindow
        )
        # Layout constants (bottom-up in Cocoa coordinates)
        W = 360
        PAD = 10
        FIELD_H = 26
        LABEL_H = 24
        GAP = 6
        CW = W - PAD * 2  # content width

        # Calculate heights bottom-up to get exact panel size
        help_h = 20
        note_h = FIELD_H
        table_h = 180
        summary_h = LABEL_H
        name_h = FIELD_H
        loc_h = FIELD_H

        total_h = (PAD + help_h + GAP
                    + note_h + GAP * 2
                    + table_h + GAP
                    + summary_h + GAP
                    + name_h + GAP
                    + loc_h + PAD)

        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(100, 200, W, total_h),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._panel.setTitle_("\ucc38\uc11d\uc790 \uae30\ub85d")
        self._panel.setLevel_(NSFloatingWindowLevel)
        self._panel.setFloatingPanel_(True)
        self._panel.setBecomesKeyOnlyIfNeeded_(False)

        self._table_delegate = _AttendeeDelegate.alloc().initWithPanel_(self)
        content = self._panel.contentView()

        # Build bottom-up (Cocoa y=0 is bottom)
        y = PAD

        # --- Help (bottom) ---
        help_label = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, CW, help_h))
        help_label.setEditable_(False)
        help_label.setBordered_(False)
        help_label.setDrawsBackground_(False)
        help_label.setFont_(NSFont.systemFontOfSize_(11))
        help_label.setTextColor_(NSColor.secondaryLabelColor())
        help_label.setStringValue_(
            "Enter=\uc785\ub825  \uc774\ub984 \uc7ac\uc785\ub825=\ud1f4\uc7a5  \u2318D=\uc120\ud0dd \ud1f4\uc7a5"
        )
        content.addSubview_(help_label)
        y += help_h + GAP

        # --- Note input ---
        note_label = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, 42, LABEL_H))
        note_label.setStringValue_("\uba54\ubaa8:")
        note_label.setEditable_(False)
        note_label.setBordered_(False)
        note_label.setDrawsBackground_(False)
        note_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content.addSubview_(note_label)

        self._note_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD + 48, y, CW - 48, note_h))
        self._note_field.setPlaceholderString_("\uba54\ubaa8 \uc785\ub825 \ud6c4 Enter...")
        self._note_field.setTarget_(self._table_delegate)
        self._note_field.setAction_("addNote:")
        content.addSubview_(self._note_field)
        y += note_h + GAP * 2

        # --- Attendee table ---
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, y, CW, table_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)

        self._table_view = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, CW, table_h))

        name_col = NSTableColumn.alloc().initWithIdentifier_("name")
        name_col.headerCell().setStringValue_("\uc774\ub984")
        name_col.setWidth_(190)
        name_col.setEditable_(False)
        self._table_view.addTableColumn_(name_col)

        time_col = NSTableColumn.alloc().initWithIdentifier_("time")
        time_col.headerCell().setStringValue_("\uc2dc\uac04")
        time_col.setWidth_(130)
        time_col.setEditable_(False)
        self._table_view.addTableColumn_(time_col)

        self._table_view.setDataSource_(self._table_delegate)
        self._table_view.setDelegate_(self._table_delegate)

        scroll.setDocumentView_(self._table_view)
        content.addSubview_(scroll)
        y += table_h + GAP

        # --- Summary ---
        self._summary_label = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, CW, summary_h))
        self._summary_label.setStringValue_("\U0001f465 \ucc38\uc11d\uc790 0\uba85")
        self._summary_label.setEditable_(False)
        self._summary_label.setBordered_(False)
        self._summary_label.setDrawsBackground_(False)
        self._summary_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content.addSubview_(self._summary_label)
        y += summary_h + GAP

        # --- Name input + leave button ---
        name_label = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, 42, LABEL_H))
        name_label.setStringValue_("\uc774\ub984:")
        name_label.setEditable_(False)
        name_label.setBordered_(False)
        name_label.setDrawsBackground_(False)
        name_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content.addSubview_(name_label)

        btn_w = 60
        chk_w = 50
        input_w = CW - 48 - chk_w - GAP - btn_w - GAP
        self._name_input = NSComboBox.alloc().initWithFrame_(
            NSMakeRect(PAD + 48, y, input_w, name_h))
        self._name_input.setPlaceholderString_("\uc774\ub984 \uc785\ub825 \ud6c4 Enter...")
        self._name_input.setCompletes_(True)
        self._name_input.setNumberOfVisibleItems_(5)
        for name in sorted(self._known_people):
            self._name_input.addItemWithObjectValue_(name)
        self._name_input.setTarget_(self._table_delegate)
        self._name_input.setAction_("addPerson:")
        content.addSubview_(self._name_input)

        self._remote_checkbox = NSButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 48 + input_w + GAP, y, chk_w, name_h))
        self._remote_checkbox.setButtonType_(3)  # NSSwitchButton
        self._remote_checkbox.setTitle_("\ud654\uc0c1")
        self._remote_checkbox.setFont_(NSFont.systemFontOfSize_(12))
        content.addSubview_(self._remote_checkbox)

        leave_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(W - PAD - btn_w, y, btn_w, name_h))
        leave_btn.setTitle_("\ud1f4\uc7a5")
        leave_btn.setBezelStyle_(NSBezelStyleRounded)
        leave_btn.setTarget_(self._table_delegate)
        leave_btn.setAction_("markDeparture:")
        leave_btn.setKeyEquivalent_("d")
        leave_btn.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
        content.addSubview_(leave_btn)
        y += name_h + GAP

        # --- Location (top) ---
        loc_label = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, 42, LABEL_H))
        loc_label.setStringValue_("\uc7a5\uc18c:")
        loc_label.setEditable_(False)
        loc_label.setBordered_(False)
        loc_label.setDrawsBackground_(False)
        loc_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content.addSubview_(loc_label)

        self._location_field = NSComboBox.alloc().initWithFrame_(
            NSMakeRect(PAD + 48, y, CW - 48, loc_h))
        self._location_field.setPlaceholderString_("\uc7a5\uc18c\uba85 \uc785\ub825 \ud6c4 Enter...")
        self._location_field.setCompletes_(True)
        self._location_field.setNumberOfVisibleItems_(5)
        for loc in sorted(self._known_locations):
            self._location_field.addItemWithObjectValue_(loc)
        self._location_field.setTarget_(self._table_delegate)
        self._location_field.setAction_("locationChanged:")
        content.addSubview_(self._location_field)
