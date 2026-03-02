#!/usr/bin/env python3
"""BLE & Recording session analyzer for ~/Recordings/.

Usage:
    python analyze.py summary                  # 전체 요약
    python analyze.py devices [--top N]        # 디바이스 분석
    python analyze.py locations                # 장소 fingerprint
    python analyze.py presence SESSION_NAME    # 특정 세션 시간대별 출석
    python analyze.py cooccurrence [--min N]   # 디바이스 동시 출현
    python analyze.py proximity SESSION_NAME   # RSSI 근접도 분석
    python analyze.py context [--top N]        # 컨텍스트 이벤트 분석
    python analyze.py timeline [--date YYYY-MM-DD]  # 일별 타임라인
    python analyze.py diff SESSION1 SESSION2   # 두 세션 비교
    python analyze.py person DEVICE_NAME       # 특정 디바이스 추적

Output: JSON to stdout (pipe to jq for pretty-print)
"""

import json
import os
import sys
import glob
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

RECORDINGS_DIR = os.path.expanduser("~/Recordings")
KST = timezone(timedelta(hours=9))

# Known manufacturer IDs
MANUFACTURER_NAMES = {
    "76": "Apple",
    "6": "Microsoft",
    "117": "Samsung",
    "301": "Xiaomi",
    "9474": "Unknown-9474",
    "2560": "Atmosphere",
    "1373": "LG",
    "2409": "Unknown-2409",
    "2215": "Unknown-2215",
    "14083": "Unknown-14083",
}

# Keywords that indicate fixed/stationary devices (location anchors)
FIXED_DEVICE_KEYWORDS = [
    "TV", "Soundbar", "Venus", "Jet", "LHB-", "AD_402",
    "SS_", "18220", "Air Purifier", "Robot", "Washer", "Fridge",
    "Dryer", "AC_", "Oven", "Dishwasher",
]


def find_sessions():
    """Find all recording sessions with events."""
    sessions = []
    for d in sorted(glob.glob(os.path.join(RECORDINGS_DIR, "*"))):
        if not os.path.isdir(d):
            continue
        events_file = os.path.join(d, "events.jsonl")
        bt_file = os.path.join(d, "bluetooth.jsonl")
        if os.path.exists(events_file) or os.path.exists(bt_file):
            sessions.append(d)
    return sessions


def load_events(session_dir):
    """Load all events from a session directory."""
    events = []
    for fname in ["events.jsonl", "bluetooth.jsonl"]:
        fpath = os.path.join(session_dir, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return events


def classify_event(e):
    """Classify event type."""
    return e.get("type", "bluetooth" if "rssi" in e else "unknown")


def ts_to_kst(ts_ns):
    """Convert nanosecond timestamp to KST datetime."""
    if ts_ns > 1e15:
        return datetime.fromtimestamp(ts_ns / 1e9, tz=KST)
    return None


def is_fixed_device(name):
    """Check if device name suggests a fixed/stationary device."""
    if not name or name == "Unknown":
        return False
    return any(kw.lower() in name.lower() for kw in FIXED_DEVICE_KEYWORDS)


def rssi_to_distance_category(rssi):
    """Classify RSSI into distance categories."""
    if rssi is None:
        return "unknown"
    if rssi > -40:
        return "contact"     # 접촉 거리
    if rssi > -60:
        return "near"        # 1m 이내
    if rssi > -80:
        return "room"        # 1~5m (같은 방)
    return "far"             # 5m+ (복도/옆방)


def guess_manufacturer(e):
    """Guess device manufacturer from manufacturer_data."""
    md = e.get("manufacturer_data", {})
    for mid, name in MANUFACTURER_NAMES.items():
        if mid in md:
            return name
    return "Unknown"


# ─── Commands ────────────────────────────────────────────────────────

def cmd_summary(args):
    """Overall summary of all recording sessions."""
    sessions = find_sessions()
    total_bt = 0
    total_ctx = 0
    total_other = 0
    all_devices = set()
    named_devices = Counter()
    session_list = []

    for sdir in sessions:
        events = load_events(sdir)
        bt = ctx = other = 0
        devs = set()
        names = set()

        for e in events:
            t = classify_event(e)
            if t == "bluetooth":
                bt += 1
                addr = e.get("address", "")
                devs.add(addr)
                all_devices.add(addr)
                n = e.get("name", "Unknown")
                if n != "Unknown":
                    named_devices[n] += 1
                    names.add(n)
            elif t == "context":
                ctx += 1
            else:
                other += 1

        total_bt += bt
        total_ctx += ctx
        total_other += other

        # Session time
        ref_file = os.path.join(sdir, "reference_time.json")
        iso_time = None
        if os.path.exists(ref_file):
            with open(ref_file) as f:
                ref = json.load(f)
                iso_time = ref.get("iso")

        files = os.listdir(sdir)
        session_list.append({
            "name": os.path.basename(sdir),
            "time": iso_time,
            "bt_events": bt,
            "ctx_events": ctx,
            "unique_devices": len(devs),
            "named_devices": list(names),
            "files": files,
            "complete": "COMPLETE" in files,
        })

    result = {
        "total_sessions": len(sessions),
        "total_bt_events": total_bt,
        "total_ctx_events": total_ctx,
        "total_other_events": total_other,
        "unique_devices": len(all_devices),
        "top_named_devices": [
            {"name": n, "count": c}
            for n, c in named_devices.most_common(30)
        ],
        "sessions": sorted(session_list, key=lambda x: x.get("time") or "", reverse=True),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_devices(args):
    """Detailed device analysis."""
    sessions = find_sessions()
    device_info = defaultdict(lambda: {
        "count": 0, "sessions": set(), "rssi_values": [],
        "names": Counter(), "manufacturers": Counter(),
    })

    for sdir in sessions:
        sname = os.path.basename(sdir)
        for e in load_events(sdir):
            if classify_event(e) != "bluetooth":
                continue
            addr = e.get("address", "")
            info = device_info[addr]
            info["count"] += 1
            info["sessions"].add(sname)
            name = e.get("name", "Unknown")
            info["names"][name] += 1
            rssi = e.get("rssi")
            if rssi is not None:
                info["rssi_values"].append(rssi)
            mfr = guess_manufacturer(e)
            info["manufacturers"][mfr] += 1

    devices = []
    for addr, info in device_info.items():
        best_name = info["names"].most_common(1)[0][0] if info["names"] else "Unknown"
        best_mfr = info["manufacturers"].most_common(1)[0][0] if info["manufacturers"] else "Unknown"
        rssi_vals = info["rssi_values"]
        avg_rssi = sum(rssi_vals) / len(rssi_vals) if rssi_vals else None

        devices.append({
            "address": addr,
            "name": best_name,
            "manufacturer": best_mfr,
            "total_events": info["count"],
            "session_count": len(info["sessions"]),
            "avg_rssi": round(avg_rssi, 1) if avg_rssi else None,
            "distance": rssi_to_distance_category(avg_rssi) if avg_rssi else "unknown",
            "is_fixed": is_fixed_device(best_name),
            "sessions": sorted(info["sessions"]),
        })

    devices.sort(key=lambda x: -x["total_events"])
    top = args.top if hasattr(args, "top") and args.top else 50
    result = {
        "total_unique_devices": len(devices),
        "named_count": sum(1 for d in devices if d["name"] != "Unknown"),
        "fixed_count": sum(1 for d in devices if d["is_fixed"]),
        "multi_session_count": sum(1 for d in devices if d["session_count"] >= 2),
        "manufacturer_distribution": dict(Counter(d["manufacturer"] for d in devices).most_common()),
        "distance_distribution": dict(Counter(d["distance"] for d in devices).most_common()),
        "devices": devices[:top],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_locations(args):
    """Location fingerprinting based on fixed devices."""
    sessions = find_sessions()
    session_fixed = {}
    session_all_named = {}

    for sdir in sessions:
        sname = os.path.basename(sdir)
        fixed = set()
        named = set()
        for e in load_events(sdir):
            if classify_event(e) != "bluetooth":
                continue
            n = e.get("name", "Unknown")
            if n != "Unknown":
                named.add(n)
                if is_fixed_device(n):
                    fixed.add(n)
        if fixed:
            session_fixed[sname] = fixed
        session_all_named[sname] = named

    # Group sessions by fixed device sets (exact and subset matching)
    location_groups = defaultdict(list)
    for sname, devs in session_fixed.items():
        location_groups[frozenset(devs)].append(sname)

    # Merge similar locations (>70% overlap)
    locations = []
    seen = set()
    for devs, sess_list in sorted(location_groups.items(), key=lambda x: -len(x[1])):
        key = frozenset(devs)
        if key in seen:
            continue
        seen.add(key)

        # Find mobile devices frequently seen at this location
        mobile_counter = Counter()
        for s in sess_list:
            for n in session_all_named.get(s, set()):
                if not is_fixed_device(n):
                    mobile_counter[n] += 1

        locations.append({
            "anchor_devices": sorted(devs),
            "sessions": sorted(sess_list),
            "session_count": len(sess_list),
            "frequent_visitors": [
                {"name": n, "visit_count": c}
                for n, c in mobile_counter.most_common(10)
                if c >= 1
            ],
        })

    print(json.dumps({"locations": locations}, ensure_ascii=False, indent=2))


def cmd_presence(args):
    """Time-based presence analysis for a specific session."""
    session_name = args.session
    sdir = os.path.join(RECORDINGS_DIR, session_name)
    if not os.path.isdir(sdir):
        print(json.dumps({"error": f"Session not found: {session_name}"}))
        sys.exit(1)

    events = load_events(sdir)
    minute_devices = defaultdict(set)
    minute_named = defaultdict(set)
    minute_rssi = defaultdict(lambda: defaultdict(list))
    first_ts = None

    for e in events:
        if classify_event(e) != "bluetooth":
            continue
        ts = e.get("ts", 0)
        if first_ts is None:
            first_ts = ts
        minute = int((ts - first_ts) / (60 * 1_000_000_000))
        addr = e.get("address", "")
        minute_devices[minute].add(addr)
        n = e.get("name", "Unknown")
        if n != "Unknown":
            minute_named[minute].add(n)
        rssi = e.get("rssi")
        if rssi is not None:
            minute_rssi[minute][n if n != "Unknown" else addr].append(rssi)

    if not minute_devices:
        print(json.dumps({"error": "No BLE events in session"}))
        sys.exit(1)

    total_mins = max(minute_devices.keys()) + 1
    interval = 5  # 5-minute windows

    windows = []
    for m in range(0, int(total_mins), interval):
        devs = set()
        names = set()
        for mm in range(m, min(m + interval, int(total_mins))):
            devs |= minute_devices.get(mm, set())
            names |= minute_named.get(mm, set())
        if devs:
            windows.append({
                "start_min": m,
                "end_min": min(m + interval, int(total_mins)),
                "total_devices": len(devs),
                "named_devices": sorted(names),
                "named_count": len(names),
            })

    # Detect arrivals and departures
    all_named_by_window = [set() for _ in range(len(windows))]
    for i, w in enumerate(windows):
        all_named_by_window[i] = set(w["named_devices"])

    arrivals = []
    departures = []
    for i in range(1, len(all_named_by_window)):
        entered = all_named_by_window[i] - all_named_by_window[i - 1]
        left = all_named_by_window[i - 1] - all_named_by_window[i]
        if entered:
            arrivals.append({
                "minute": windows[i]["start_min"],
                "devices": sorted(entered),
            })
        if left:
            departures.append({
                "minute": windows[i]["start_min"],
                "devices": sorted(left),
            })

    result = {
        "session": session_name,
        "duration_minutes": int(total_mins),
        "windows": windows,
        "arrivals": arrivals,
        "departures": departures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_cooccurrence(args):
    """Cross-session co-occurrence analysis."""
    sessions = find_sessions()
    device_sessions = defaultdict(set)
    device_name = {}

    for sdir in sessions:
        sname = os.path.basename(sdir)
        for e in load_events(sdir):
            if classify_event(e) != "bluetooth":
                continue
            addr = e.get("address", "")
            device_sessions[addr].add(sname)
            n = e.get("name", "Unknown")
            if n != "Unknown":
                device_name[addr] = n

    min_sessions = args.min if hasattr(args, "min") and args.min else 3
    named_multi = {
        addr: sess for addr, sess in device_sessions.items()
        if device_name.get(addr, "Unknown") != "Unknown" and len(sess) >= min_sessions
    }

    # Find co-occurring pairs
    pairs = []
    addrs = list(named_multi.keys())
    for i in range(len(addrs)):
        for j in range(i + 1, len(addrs)):
            overlap = named_multi[addrs[i]] & named_multi[addrs[j]]
            if len(overlap) >= min_sessions:
                pairs.append({
                    "device_a": device_name.get(addrs[i], "Unknown"),
                    "device_b": device_name.get(addrs[j], "Unknown"),
                    "co_sessions": len(overlap),
                    "sessions": sorted(overlap),
                })

    pairs.sort(key=lambda x: -x["co_sessions"])

    # Device groups (cliques of always-together devices)
    groups = []
    used = set()
    for p in pairs:
        a, b = p["device_a"], p["device_b"]
        found = False
        for g in groups:
            if a in g["devices"] or b in g["devices"]:
                g["devices"].add(a)
                g["devices"].add(b)
                g["min_co_sessions"] = min(g["min_co_sessions"], p["co_sessions"])
                found = True
                break
        if not found:
            groups.append({
                "devices": {a, b},
                "min_co_sessions": p["co_sessions"],
            })

    result = {
        "min_session_threshold": min_sessions,
        "pairs": pairs[:30],
        "device_groups": [
            {"devices": sorted(g["devices"]), "min_co_sessions": g["min_co_sessions"]}
            for g in groups if len(g["devices"]) >= 2
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_proximity(args):
    """RSSI proximity analysis for a session."""
    session_name = args.session
    sdir = os.path.join(RECORDINGS_DIR, session_name)
    if not os.path.isdir(sdir):
        print(json.dumps({"error": f"Session not found: {session_name}"}))
        sys.exit(1)

    device_rssi = defaultdict(list)
    device_name_map = {}

    for e in load_events(sdir):
        if classify_event(e) != "bluetooth":
            continue
        addr = e.get("address", "")
        rssi = e.get("rssi")
        name = e.get("name", "Unknown")
        if name != "Unknown":
            device_name_map[addr] = name
        if rssi is not None:
            device_rssi[addr].append(rssi)

    categories = defaultdict(list)
    for addr, rssis in device_rssi.items():
        avg = sum(rssis) / len(rssis)
        name = device_name_map.get(addr, "Unknown")
        cat = rssi_to_distance_category(avg)
        categories[cat].append({
            "name": name,
            "address": addr if name == "Unknown" else None,
            "avg_rssi": round(avg, 1),
            "min_rssi": min(rssis),
            "max_rssi": max(rssis),
            "event_count": len(rssis),
        })

    for cat in categories:
        categories[cat].sort(key=lambda x: -x["avg_rssi"])

    result = {
        "session": session_name,
        "total_devices": len(device_rssi),
        "contact": categories.get("contact", [])[:10],
        "near": categories.get("near", [])[:20],
        "room": [d for d in categories.get("room", []) if d["name"] != "Unknown"][:20],
        "far_count": len(categories.get("far", [])),
        "distance_distribution": {
            cat: len(devs) for cat, devs in categories.items()
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_context(args):
    """Context event analysis (apps, battery, idle)."""
    sessions = find_sessions()
    apps = Counter()
    meeting_apps = Counter()
    window_titles = Counter()
    power_samples = []
    idle_samples = []

    for sdir in sessions:
        for e in load_events(sdir):
            if classify_event(e) != "context":
                continue
            app = e.get("active_app", {})
            if app.get("name"):
                apps[app["name"]] += 1
            if app.get("window_title"):
                window_titles[app["window_title"]] += 1
            ma = e.get("meeting_apps", {})
            for a in ma.get("running", []):
                meeting_apps[a] += 1
            pw = e.get("power", {})
            if pw:
                power_samples.append(pw)
            idle = e.get("idle", {})
            if idle:
                idle_samples.append(idle.get("idle_seconds", 0))

    top = args.top if hasattr(args, "top") and args.top else 20
    result = {
        "total_context_events": sum(apps.values()),
        "apps": [{"name": n, "count": c} for n, c in apps.most_common(top)],
        "meeting_apps": [{"name": n, "count": c} for n, c in meeting_apps.most_common()],
        "window_titles": [{"title": t, "count": c} for t, c in window_titles.most_common(top)],
        "power": {
            "samples": len(power_samples),
            "avg_battery": round(
                sum(p.get("percent", 0) for p in power_samples) / len(power_samples), 1
            ) if power_samples else None,
            "charging_count": sum(1 for p in power_samples if p.get("charging")),
        },
        "idle": {
            "samples": len(idle_samples),
            "avg_idle_seconds": round(sum(idle_samples) / len(idle_samples), 1) if idle_samples else None,
            "max_idle_seconds": max(idle_samples) if idle_samples else None,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_timeline(args):
    """Daily timeline of sessions."""
    sessions = find_sessions()
    date_filter = args.date if hasattr(args, "date") and args.date else None

    day_sessions = defaultdict(list)
    for sdir in sessions:
        ref_file = os.path.join(sdir, "reference_time.json")
        if os.path.exists(ref_file):
            with open(ref_file) as f:
                ref = json.load(f)
                iso = ref.get("iso", "")
                day = iso[:10]
                if date_filter and day != date_filter:
                    continue
                day_sessions[day].append({
                    "name": os.path.basename(sdir),
                    "start_time": iso,
                })

    result = {
        "days": [
            {"date": day, "sessions": sorted(sess, key=lambda x: x["start_time"])}
            for day, sess in sorted(day_sessions.items())
        ]
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_diff(args):
    """Compare two sessions."""
    s1 = os.path.join(RECORDINGS_DIR, args.session1)
    s2 = os.path.join(RECORDINGS_DIR, args.session2)

    def get_devices(sdir):
        devs = {}
        for e in load_events(sdir):
            if classify_event(e) != "bluetooth":
                continue
            addr = e.get("address", "")
            name = e.get("name", "Unknown")
            if name != "Unknown":
                devs[addr] = name
            elif addr not in devs:
                devs[addr] = "Unknown"
        return devs

    d1 = get_devices(s1)
    d2 = get_devices(s2)
    addrs1 = set(d1.keys())
    addrs2 = set(d2.keys())

    only1 = addrs1 - addrs2
    only2 = addrs2 - addrs1
    both = addrs1 & addrs2

    result = {
        "session1": args.session1,
        "session2": args.session2,
        "session1_devices": len(addrs1),
        "session2_devices": len(addrs2),
        "common_devices": len(both),
        "only_in_session1": [
            {"address": a, "name": d1[a]} for a in only1 if d1[a] != "Unknown"
        ],
        "only_in_session2": [
            {"address": a, "name": d2[a]} for a in only2 if d2[a] != "Unknown"
        ],
        "common_named": [
            {"address": a, "name": d1[a]} for a in both if d1[a] != "Unknown"
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_person(args):
    """Track a specific device/person across sessions."""
    target = args.device_name.lower()
    sessions = find_sessions()

    appearances = []
    for sdir in sessions:
        sname = os.path.basename(sdir)
        events = load_events(sdir)
        matched = []
        for e in events:
            if classify_event(e) != "bluetooth":
                continue
            name = e.get("name", "Unknown")
            if target in name.lower():
                matched.append(e)

        if matched:
            rssis = [e["rssi"] for e in matched if e.get("rssi") is not None]
            first_ts = ts_to_kst(matched[0].get("ts", 0))
            last_ts = ts_to_kst(matched[-1].get("ts", 0))

            appearances.append({
                "session": sname,
                "event_count": len(matched),
                "first_seen": first_ts.isoformat() if first_ts else None,
                "last_seen": last_ts.isoformat() if last_ts else None,
                "avg_rssi": round(sum(rssis) / len(rssis), 1) if rssis else None,
                "distance": rssi_to_distance_category(
                    sum(rssis) / len(rssis) if rssis else None
                ),
            })

    result = {
        "search_term": args.device_name,
        "total_appearances": len(appearances),
        "appearances": appearances,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="BLE & Recording Analyzer")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("summary", help="Overall summary")

    p = sub.add_parser("devices", help="Device analysis")
    p.add_argument("--top", type=int, default=50)

    sub.add_parser("locations", help="Location fingerprinting")

    p = sub.add_parser("presence", help="Session presence timeline")
    p.add_argument("session", help="Session directory name")

    p = sub.add_parser("cooccurrence", help="Device co-occurrence")
    p.add_argument("--min", type=int, default=2, help="Minimum shared sessions")

    p = sub.add_parser("proximity", help="RSSI proximity analysis")
    p.add_argument("session", help="Session directory name")

    p = sub.add_parser("context", help="Context event analysis")
    p.add_argument("--top", type=int, default=20)

    p = sub.add_parser("timeline", help="Daily timeline")
    p.add_argument("--date", help="Filter by date (YYYY-MM-DD)")

    p = sub.add_parser("diff", help="Compare two sessions")
    p.add_argument("session1")
    p.add_argument("session2")

    p = sub.add_parser("person", help="Track a device/person")
    p.add_argument("device_name", help="Device name to search for")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "summary": cmd_summary,
        "devices": cmd_devices,
        "locations": cmd_locations,
        "presence": cmd_presence,
        "cooccurrence": cmd_cooccurrence,
        "proximity": cmd_proximity,
        "context": cmd_context,
        "timeline": cmd_timeline,
        "diff": cmd_diff,
        "person": cmd_person,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
