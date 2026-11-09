#!/usr/bin/env -S python3 -I
"""Waybar module for Syncthing: is everything synced with the other machines?

Prints one JSON line for Waybar, then a new one whenever the state changes
(it follows Syncthing's event stream, and checks again at least every 20 s).

  synced   every folder is idle and every machine it is shared with has all of it
  syncing  scanning, receiving, or another machine still needs data from here
  offline  a machine a folder is shared with is not connected, or a folder is paused
  error    Syncthing is not running, the API key is wrong, or items failed to sync

--once prints a single line and exits (for testing).
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

RANK = {"synced": 0, "syncing": 1, "offline": 2, "error": 3}
TITLE = {
    "synced": "Syncthing: up to date",
    "syncing": "Syncthing: syncing",
    "offline": "Syncthing: cannot sync right now",
    "error": "Syncthing: error",
}
EVENTS = ",".join(
    [
        "StateChanged",
        "FolderSummary",
        "FolderCompletion",
        "FolderErrors",
        "FolderPaused",
        "FolderResumed",
        "DeviceConnected",
        "DeviceDisconnected",
        "DevicePaused",
        "DeviceResumed",
        "ConfigSaved",
    ]
)


def config_path():
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    for path in (Path(state) / "syncthing/config.xml", Path(config) / "syncthing/config.xml"):
        if path.exists():
            return path
    return None


def gui_settings():
    """Base URL and API key of the local Syncthing web interface."""
    path = config_path()
    if path is None:
        raise RuntimeError("Syncthing config not found")
    gui = ET.parse(path).getroot().find("gui")
    if gui is None:
        raise RuntimeError(f"no <gui> section in {path}")
    address = (gui.findtext("address") or "127.0.0.1:8384").strip()
    scheme = "https" if gui.get("tls") == "true" else "http"
    return f"{scheme}://{address}/rest/", (gui.findtext("apikey") or "").strip()


class Api:
    def __init__(self):
        self.base, self.key = gui_settings()

    def get(self, path, timeout=5):
        request = urllib.request.Request(self.base + path, headers={"X-API-Key": self.key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)


def size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def check(api):
    """Return (state, tooltip lines)."""
    config = api.get("config")
    me = api.get("system/status")["myID"]
    names = {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in config["devices"]}
    paused_devices = {d["deviceID"] for d in config["devices"] if d.get("paused")}
    connections = api.get("system/connections")["connections"]
    last_seen = api.get("stats/device")

    worst, lines = "synced", []

    def mark(state, line):
        nonlocal worst
        if RANK[state] > RANK[worst]:
            worst = state
        lines.append(line)

    for folder in config["folders"]:
        fid = folder["id"]
        label = folder.get("label") or fid
        if folder.get("paused"):
            mark("offline", f"{label}: paused")
            continue
        status = api.get(f"db/status?folder={urllib.parse.quote(fid)}")
        failed = status.get("pullErrors", 0) + status.get("errors", 0)
        state = status.get("state", "unknown")
        if state == "error" or failed:
            detail = status.get("error") or f"{failed} items failed to sync"
            mark("error", f"{label}: {detail}")
        elif status.get("needBytes", 0) or status.get("needFiles", 0) or status.get("needDeletes", 0):
            mark("syncing", f"{label}: receiving {size(status.get('needBytes', 0))}")
        elif state != "idle":
            mark("syncing", f"{label}: {state.replace('-', ' ')}")

        for device in folder["devices"]:
            did = device["deviceID"]
            if did == me:
                continue
            name = names.get(did, did[:7])
            if did in paused_devices:
                mark("offline", f"{label}: {name} is paused")
                continue
            if not connections.get(did, {}).get("connected"):
                seen = last_seen.get(did, {}).get("lastSeen", "")
                when = seen[:16].replace("T", " ") if seen and not seen.startswith("1970") else "never"
                mark("offline", f"{label}: {name} is offline (last seen {when})")
                continue
            completion = api.get(f"db/completion?folder={urllib.parse.quote(fid)}&device={did}")
            need = completion.get("needBytes", 0)
            if completion.get("completion", 100) < 100 or need or completion.get("needDeletes", 0):
                mark("syncing", f"{label}: sending {size(need)} to {name} ({completion.get('completion', 0):.0f}%)")
            else:
                lines.append(f"{label}: up to date with {name}")

    if not config["folders"]:
        lines.append("no folders")
    return worst, lines


def output(state, lines):
    tooltip = "\n".join([TITLE[state], ""] + lines) if lines else TITLE[state]
    return json.dumps(
        {"text": state, "alt": state, "class": state, "tooltip": html.escape(tooltip, quote=False)}
    )


def current(api):
    try:
        if api is None:
            api = Api()
        return api, output(*check(api))
    except urllib.error.HTTPError as exc:
        reason = "API key rejected" if exc.code in (401, 403) else f"HTTP {exc.code}"
        return None, output("error", [f"Syncthing web interface: {reason}"])
    except (RuntimeError, ET.ParseError) as exc:
        return None, output("error", [str(exc)])
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, output("error", [f"Syncthing is not running? ({reason})", "systemctl --user status syncthing"])


def main():
    api, line = current(None)
    print(line, flush=True)
    if "--once" in sys.argv:
        return
    last_event = 0
    while True:
        if api is None:
            time.sleep(5)
            last_event = 0
        else:
            try:
                events = api.get(f"events?events={EVENTS}&since={last_event}&timeout=20", timeout=30)
                if events:
                    last_event = events[-1]["id"]
                    time.sleep(0.5)  # let a burst of events settle
            except (urllib.error.URLError, OSError, ValueError):
                api = None
        api, new = current(api)
        if new != line:
            line = new
            print(line, flush=True)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
