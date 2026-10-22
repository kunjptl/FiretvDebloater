#!/usr/bin/env python3
"""
FiretvDebloater: find a Fire TV on your Wi-Fi, back it up, and disable
Amazon ads, telemetry and extras over ADB. Reversible: packages are
disabled with "pm disable-user", never uninstalled.

Commands:
  python firetv_debloat.py              scan, connect, back up, ask, debloat
  python firetv_debloat.py scan         list Fire TVs with ADB on your network
  python firetv_debloat.py status       show which catalog packages are off
  python firetv_debloat.py restore      undo the latest session
  python firetv_debloat.py restore sessions/<folder>   undo a specific session

Options:
  --ip 192.168.1.50     skip the scan and use this address
  --subnet 10.0.0.0/24  scan this subnet instead of the auto-detected one
  --dry-run             back up and show the plan, change nothing
  --defaults            accept every default answer without prompting
  --settle 5            seconds to wait on the home screen before each check

Only Python 3.8+ standard library. Works on Windows, macOS and Linux.
"""

import argparse
import concurrent.futures
import datetime
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "catalog.json")
SESSIONS_DIR = os.path.join(HERE, "sessions")
TOOLS_DIR = os.path.join(HERE, "platform-tools")
ADB_PORT = 5555
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_log_file = None


# ----------------------------------------------------------------- output

def say(msg=""):
    print(msg, flush=True)
    if _log_file:
        with open(_log_file, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")


def die(msg, code=1):
    say("")
    say("STOPPED: " + msg)
    sys.exit(code)


class Prompter:
    def __init__(self, use_defaults):
        self.use_defaults = use_defaults

    def yes(self, question, default):
        tag = "yes" if default else "no"
        if self.use_defaults:
            say("%s -> %s (default)" % (question, tag))
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            try:
                ans = input(question + suffix).strip().lower()
            except EOFError:
                ans = ""
            if _log_file:
                with open(_log_file, "a", encoding="utf-8") as fh:
                    fh.write("%s%s%s\n" % (question, suffix, ans or "(default " + tag + ")"))
            if not ans:
                return default
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            print("Please answer y or n.")

    def pick(self, question, count):
        if self.use_defaults or count == 1:
            return 0
        while True:
            try:
                ans = input("%s [1-%d] " % (question, count)).strip()
            except EOFError:
                ans = "1"
            if ans.isdigit() and 1 <= int(ans) <= count:
                return int(ans) - 1
            print("Enter a number from 1 to %d." % count)


# -------------------------------------------------------------------- adb

def find_adb():
    exe = "adb.exe" if os.name == "nt" else "adb"
    local = os.path.join(TOOLS_DIR, exe)
    if os.path.isfile(local):
        return local
    return shutil.which("adb")


def install_adb(prompter):
    plat = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    url = "https://dl.google.com/android/repository/platform-tools-latest-%s.zip" % plat
    say("ADB was not found on this computer.")
    if not prompter.yes("Download Google's official platform-tools (about 7 MB) from %s?" % url, True):
        die("ADB is required. Install Android platform-tools and run again.")
    zpath = os.path.join(HERE, "platform-tools.zip")
    say("Downloading...")
    urllib.request.urlretrieve(url, zpath)
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(HERE)
    os.remove(zpath)
    adb = find_adb()
    if not adb:
        die("Download finished but adb was not found in %s." % TOOLS_DIR)
    if os.name != "nt":
        for name in os.listdir(TOOLS_DIR):
            p = os.path.join(TOOLS_DIR, name)
            if os.path.isfile(p):
                os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    say("ADB installed at %s" % adb)
    return adb


class Adb:
    def __init__(self, path, serial=None):
        self.path = path
        self.serial = serial

    def run(self, *args, timeout=40, binary=False):
        cmd = [self.path]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, (b"" if binary else ""), "timed out"
        out = r.stdout if binary else r.stdout.decode("utf-8", "replace").replace("\r", "")
        return r.returncode, out, r.stderr.decode("utf-8", "replace").replace("\r", "").strip()

    def shell(self, command, timeout=40):
        _, out, err = self.run("shell", command, timeout=timeout)
        return (out or "").strip() + (("\n" + err) if err and not out.strip() else "")

    def state(self):
        _, out, err = self.run("get-state", timeout=10)
        text = (out + " " + err).lower()
        for s in ("unauthorized", "offline", "device"):
            if s in text:
                return s
        return "missing"


# -------------------------------------------------------------- discovery

def local_ipv4s():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def port_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_network(subnet=None):
    nets = []
    if subnet:
        nets.append(ipaddress.ip_network(subnet, strict=False))
    else:
        for ip in local_ipv4s():
            net = ipaddress.ip_network(ip + "/24", strict=False)
            if net not in nets:
                nets.append(net)
    if not nets:
        die("Could not detect your network. Pass --subnet 192.168.1.0/24 or --ip <address>.")
    hosts = [str(h) for n in nets for h in n.hosts()]
    say("Scanning %s for ADB (port %d)..." % (", ".join(str(n) for n in nets), ADB_PORT))
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        found = [h for h, ok in zip(hosts, pool.map(lambda h: port_open(h, ADB_PORT), hosts)) if ok]
    return found


def parse_getprop(text):
    props = {}
    for line in text.splitlines():
        m = re.match(r"\[(.+?)\]: \[(.*)\]", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def fireos_version(props):
    for key in ("ro.build.mktg.fireos", "ro.build.version.fireos"):
        if props.get(key):
            return props[key]
    for key, val in props.items():
        if "fireos" in key and val:
            return val
    return "unknown"


def connect(adb_path, address, prompter, wait=120):
    serial = address if ":" in address else "%s:%d" % (address, ADB_PORT)
    adb = Adb(adb_path, serial)
    Adb(adb_path).run("connect", serial, timeout=15)
    told = False
    deadline = time.time() + wait
    while time.time() < deadline:
        st = adb.state()
        if st == "device":
            return adb
        if st == "unauthorized" and not told:
            say("")
            say(">>> Look at your TV. Select 'Always allow from this computer', then OK.")
            say(">>> Waiting up to %d seconds..." % wait)
            told = True
        if st in ("offline", "missing"):
            Adb(adb_path).run("disconnect", serial, timeout=10)
            Adb(adb_path).run("connect", serial, timeout=15)
        time.sleep(3)
    return None


def describe(adb):
    props = parse_getprop(adb.shell("getprop"))
    return props, {
        "manufacturer": props.get("ro.product.manufacturer", ""),
        "model": props.get("ro.product.model", ""),
        "name": props.get("ro.product.name", ""),
        "serialno": props.get("ro.serialno", ""),
        "fireos": fireos_version(props),
        "sdk": props.get("ro.build.version.sdk", ""),
        "build": props.get("ro.build.display.id", ""),
    }


def is_fire_tv(info):
    return info["manufacturer"].lower() == "amazon"


def has_package_manager(adb):
    return "package:android" in adb.shell("pm list packages android")


def find_fire_tvs(adb_path, prompter, ip=None, subnet=None):
    """Return a connected Adb for the chosen Fire TV."""
    if ip:
        candidates = [ip]
    else:
        _, out, _ = Adb(adb_path).run("devices", timeout=15)
        already = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]
        candidates = already + [h for h in scan_network(subnet) if "%s:%d" % (h, ADB_PORT) not in already]
    if not candidates:
        say("")
        say("No device with ADB debugging turned on was found.")
        say("On the Fire TV: Settings > My Fire TV > Developer Options > ADB debugging: ON.")
        say("No Developer Options? Settings > My Fire TV > About, then press OK on the device")
        say("name 7 times. Then run this again, or pass --ip <TV address>.")
        say("The TV address is in Settings > My Fire TV > About > Network.")
        die("No Fire TV found.")
    tvs = []
    for c in candidates:
        say("Checking %s..." % c)
        adb = connect(adb_path, c, prompter)
        if not adb:
            say("  could not connect (did you accept the prompt on the TV?)")
            continue
        _, info = describe(adb)
        if not is_fire_tv(info):
            say("  %s %s is not an Amazon device, skipping" % (info["manufacturer"], info["model"]))
            continue
        if not has_package_manager(adb):
            say("  Amazon device without Android package manager. Vega OS devices are not supported.")
            continue
        say("  Fire TV: %s (%s), %s" % (info["model"], info["name"], info["fireos"]))
        tvs.append((adb, info))
    if not tvs:
        die("No supported Fire TV answered over ADB.")
    if len(tvs) > 1:
        say("")
        for i, (adb, info) in enumerate(tvs, 1):
            say("  %d) %s %s at %s" % (i, info["model"], info["fireos"], adb.serial))
        return tvs[prompter.pick("Which Fire TV?", len(tvs))]
    return tvs[0]


# ------------------------------------------------------------- device ops

def disabled_set(adb):
    out = adb.shell("pm list packages -d", timeout=60)
    return {l[len("package:"):].strip() for l in out.splitlines() if l.startswith("package:")}


def installed_set(adb):
    out = adb.shell("pm list packages", timeout=60)
    return {l[len("package:"):].strip() for l in out.splitlines() if l.startswith("package:")}


def get_setting(adb, ns, key):
    val = adb.shell("settings get %s %s" % (ns, key)).strip()
    return None if val in ("", "null") else val


def set_setting(adb, ns, key, value):
    if value is None:
        adb.shell("settings delete %s %s" % (ns, key))
    else:
        adb.shell("settings put %s %s %s" % (ns, key, value))


def focus_info(adb):
    out = adb.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    line = next((l for l in out.splitlines() if "mCurrentFocus" in l), out)
    m = re.search(r"\s([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", line)
    pkg = m.group(1) if m else ""
    error = bool(re.search(r"Application (Error|Not Responding)|has stopped|isn't responding", out))
    return pkg, error, line.strip()


def go_home(adb, settle):
    adb.shell("input keyevent KEYCODE_HOME")
    time.sleep(settle)


def screenshot(adb, path):
    _, data, _ = adb.run("exec-out", "screencap -p", binary=True, timeout=40)
    if data and data.startswith(PNG_MAGIC):
        with open(path, "wb") as fh:
            fh.write(data)
        return True
    return False


# ---------------------------------------------------------------- session

class Session:
    def __init__(self, path, state):
        self.path = path
        self.state = state
        self.shots = 0

    @classmethod
    def create(cls, adb, info, adb_path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_model = re.sub(r"[^A-Za-z0-9]+", "", info["model"]) or "firetv"
        path = os.path.join(SESSIONS_DIR, "%s-%s" % (stamp, safe_model))
        os.makedirs(os.path.join(path, "backup"), exist_ok=True)
        os.makedirs(os.path.join(path, "screens"), exist_ok=True)
        state = {
            "tool_version": VERSION,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "address": adb.serial,
            "adb_path": adb_path,
            "device": info,
            "home_package": "",
            "changes": [],
            "status": "started",
        }
        return cls(path, state)

    @classmethod
    def load(cls, path):
        with open(os.path.join(path, "state.json"), encoding="utf-8") as fh:
            s = cls(path, json.load(fh))
        s.shots = len(os.listdir(os.path.join(path, "screens")))
        return s

    def save(self):
        tmp = os.path.join(self.path, "state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, os.path.join(self.path, "state.json"))
        self.write_restore_scripts()

    def shot(self, adb, label):
        self.shots += 1
        name = "%02d-%s.png" % (self.shots, re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"))
        path = os.path.join(self.path, "screens", name)
        if screenshot(adb, path):
            say("  screenshot: %s" % os.path.relpath(path, HERE))
            return path
        say("  screenshot failed for %s" % label)
        return None

    def active_changes(self):
        return [c for c in self.state["changes"] if not c.get("rolled_back") and not c.get("restored")]

    def write_restore_scripts(self):
        changes = self.active_changes()
        addr = self.state["address"]
        adb_path = self.state["adb_path"]
        dev = self.state["device"]
        header = "FiretvDebloater %s restore for %s (%s), session %s" % (
            VERSION, dev.get("model"), dev.get("fireos"), os.path.basename(self.path))

        sh = ["#!/bin/sh", "# " + header,
              "# Usage: sh restore.sh [TV_ADDRESS:5555]",
              'ADB="${ADB:-%s}"' % adb_path,
              'SERIAL="${1:-%s}"' % addr,
              '"$ADB" connect "$SERIAL"']
        bat = ["@echo off", "rem " + header,
               "rem Usage: restore.bat [TV_ADDRESS:5555]",
               "setlocal",
               'if "%%ADB%%"=="" set "ADB=%s"' % adb_path,
               'set "SERIAL=%~1"',
               'if "%%SERIAL%%"=="" set "SERIAL=%s"' % addr,
               '"%ADB%" connect %SERIAL%']
        for c in reversed(changes):
            if c["type"] == "package":
                cmd = "shell pm enable %s" % c["package"]
            else:
                if c["before"] is None:
                    cmd = "shell settings delete %s %s" % (c["namespace"], c["key"])
                else:
                    cmd = "shell settings put %s %s %s" % (c["namespace"], c["key"], c["before"])
            sh.append('"$ADB" -s "$SERIAL" %s' % cmd)
            bat.append('"%%ADB%%" -s %%SERIAL%% %s' % cmd)
        sh += ['"$ADB" -s "$SERIAL" shell input keyevent KEYCODE_HOME',
               'echo "Restore finished: %d change(s) undone."' % len(changes)]
        bat += ['"%ADB%" -s %SERIAL% shell input keyevent KEYCODE_HOME',
                "echo Restore finished: %d change(s) undone." % len(changes),
                "pause"]
        with open(os.path.join(self.path, "restore.sh"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(sh) + "\n")
        with open(os.path.join(self.path, "restore.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("\n".join(bat) + "\n")
        try:
            p = os.path.join(self.path, "restore.sh")
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        except OSError:
            pass


def backup(adb, session, props_text):
    b = os.path.join(session.path, "backup")
    say("Backing up device state to %s" % os.path.relpath(b, HERE))
    files = {
        "getprop.txt": props_text,
        "packages_all.txt": adb.shell("pm list packages -f", timeout=60),
        "packages_disabled.txt": adb.shell("pm list packages -d", timeout=60),
        "packages_user_installed.txt": adb.shell("pm list packages -3", timeout=60),
        "settings_global.txt": adb.shell("settings list global"),
        "settings_secure.txt": adb.shell("settings list secure"),
        "settings_system.txt": adb.shell("settings list system"),
    }
    for name, text in files.items():
        with open(os.path.join(b, name), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if "package:" not in files["packages_all.txt"]:
        die("Backup failed: could not read the package list.")
    say("  saved %d backup files" % len(files))


# ------------------------------------------------------------ the debloat

def load_catalog():
    with open(CATALOG_PATH, encoding="utf-8") as fh:
        cat = json.load(fh)
    protected = set(cat["protected"])
    bad = [p["package"] for p in cat["packages"] if p["package"] in protected]
    if bad:
        die("catalog.json lists protected packages: %s" % ", ".join(bad))
    return cat


def build_plan(cat, installed, disabled, prompter):
    """Ask the user and return an ordered list of steps."""
    steps = []
    by_cat = {}
    for p in cat["packages"]:
        by_cat.setdefault(p["category"], []).append(p)
    settings_by_cat = {}
    for s in cat["settings"]:
        settings_by_cat.setdefault(s["category"], []).append(s)

    for cid, meta in cat["categories"].items():
        present = [p for p in by_cat.get(cid, []) if p["package"] in installed]
        already = [p for p in present if p["package"] in disabled]
        todo = [p for p in present if p["package"] not in disabled]
        tweaks = settings_by_cat.get(cid, [])
        if not todo and not tweaks:
            continue
        say("")
        say("== %s ==" % meta["title"])
        for p in already:
            say("  already off: %s (left as is)" % p["label"])
        if meta["mode"] == "group":
            for p in todo:
                say("  - %s: %s" % (p["label"], p["description"]))
            for s in tweaks:
                say("  - setting: %s" % s["label"])
            if prompter.yes("Turn off everything in this group?", meta["default"]):
                if todo:
                    steps.append({"name": meta["title"], "packages": todo, "settings": []})
                if tweaks:
                    steps.append({"name": meta["title"] + " (settings)", "packages": [], "settings": tweaks})
        else:
            for p in todo:
                say("")
                say("  %s [%s]" % (p["label"], p["package"]))
                say("  %s" % p["description"])
                if prompter.yes("  Turn this off?", meta["default"]):
                    steps.append({"name": p["label"], "packages": [p], "settings": []})

    for cid, tweaks in settings_by_cat.items():
        if cid in cat["categories"]:
            continue
        say("")
        say("== Optional tweaks ==")
        for s in tweaks:
            if prompter.yes("  %s?" % s["label"], s.get("default", False)):
                steps.append({"name": s["label"], "packages": [], "settings": [s]})
    return steps


def healthy(adb, session, settle):
    home = session.state["home_package"]
    last = ""
    for attempt in range(3):
        go_home(adb, settle if attempt == 0 else settle + 3)
        pkg, error, line = focus_info(adb)
        last = line
        if not error and (not home or pkg == home):
            return True, line
    return False, last


def apply_step(adb, session, step, settle):
    say("")
    say(">> %s" % step["name"])
    applied = []
    for p in step["packages"]:
        change = {"type": "package", "package": p["package"], "label": p["label"],
                  "before": "enabled", "step": step["name"],
                  "time": datetime.datetime.now().isoformat(timespec="seconds")}
        session.state["changes"].append(change)
        session.save()  # restore script covers it before we touch it
        out = adb.shell("pm disable-user --user 0 %s" % p["package"])
        if p["package"] in disabled_set(adb):
            change["applied"] = True
            applied.append(change)
            say("  off: %s" % p["label"])
        else:
            change["applied"] = False
            change["rolled_back"] = True
            change["error"] = out[-300:]
            say("  could not disable %s, skipped (%s)" % (p["label"], out.splitlines()[-1] if out else "no output"))
        session.save()
    for s in step["settings"]:
        keys = s.get("keys") or [s["key"]]
        for key in keys:
            before = get_setting(adb, s["namespace"], key)
            change = {"type": "setting", "namespace": s["namespace"], "key": key, "before": before,
                      "value": s["value"], "label": s["label"], "step": step["name"],
                      "time": datetime.datetime.now().isoformat(timespec="seconds")}
            session.state["changes"].append(change)
            session.save()
            set_setting(adb, s["namespace"], key, s["value"])
            now = get_setting(adb, s["namespace"], key)
            change["applied"] = (now == s["value"])
            if change["applied"]:
                applied.append(change)
                say("  set: %s %s = %s (was %s)" % (s["namespace"], key, s["value"], before if before is not None else "unset"))
            else:
                change["rolled_back"] = True
                say("  could not set %s %s" % (s["namespace"], key))
            session.save()

    if not applied:
        return "ok"
    ok, line = healthy(adb, session, settle)
    session.shot(adb, "after-" + step["name"])
    if ok:
        say("  check: home screen OK")
        return "ok"

    say("  check FAILED: home screen not in front or an error dialog appeared")
    say("  focus: %s" % line)
    say("  rolling back this step...")
    for c in reversed(applied):
        undo_change(adb, c)
        c["rolled_back"] = True
    session.save()
    ok2, line2 = healthy(adb, session, settle)
    session.shot(adb, "rollback-" + step["name"])
    say("  after rollback: %s" % ("home screen OK" if ok2 else "still not OK: " + line2))
    if len(applied) == 1:
        applied[0]["broke_home"] = True
    return "rolled_back" if ok2 else "broken"


def undo_change(adb, c):
    if c["type"] == "package":
        adb.shell("pm enable %s" % c["package"])
    else:
        set_setting(adb, c["namespace"], c["key"], c["before"])


def cmd_debloat(args, prompter):
    global _log_file
    cat = load_catalog()
    adb_path = find_adb() or install_adb(prompter)
    adb, info = find_fire_tvs(adb_path, prompter, args.ip, args.subnet)

    session = Session.create(adb, info, adb_path)
    _log_file = os.path.join(session.path, "log.txt")
    say("")
    say("Fire TV %s (%s), %s, build %s" % (info["model"], info["name"], info["fireos"], info["build"]))
    say("Session folder: %s" % os.path.relpath(session.path, HERE))

    props_text = adb.shell("getprop")
    backup(adb, session, props_text)

    go_home(adb, args.settle)
    home, error, line = focus_info(adb)
    if error or not home:
        die("The TV is not on a clean home screen (%s). Close any dialog and run again." % line)
    session.state["home_package"] = home
    session.save()
    say("Home screen app: %s" % home)
    session.shot(adb, "before")

    installed = installed_set(adb)
    disabled = disabled_set(adb)
    steps = build_plan(cat, installed, disabled, prompter)
    total = sum(len(s["packages"]) + sum(len(t.get("keys") or [t["key"]]) for t in s["settings"]) for s in steps)

    say("")
    if not steps:
        session.state["status"] = "nothing-to-do"
        session.save()
        say("Nothing to change. Your backup is in %s" % os.path.relpath(session.path, HERE))
        return
    say("Plan: %d change(s) in %d step(s). Each step is checked on the TV before the next." % (total, len(steps)))
    if args.dry_run:
        session.state["status"] = "dry-run"
        session.save()
        say("Dry run: nothing changed.")
        return
    if not prompter.yes("Apply now?", True):
        session.state["status"] = "cancelled"
        session.save()
        say("Cancelled. Nothing changed.")
        return

    failed = None
    skipped = []
    queue = list(steps)
    while queue:
        step = queue.pop(0)
        result = apply_step(adb, session, step, args.settle)
        if result == "ok":
            continue
        if result == "broken":
            failed = step["name"]
            break
        items = step["packages"] + step["settings"]
        if len(items) > 1:
            say("  retrying this group one item at a time to find the culprit...")
            queue[0:0] = [{"name": p["label"], "packages": [p], "settings": []} for p in step["packages"]] + \
                         [{"name": s["label"], "packages": [], "settings": [s]} for s in step["settings"]]
        else:
            skipped.append(step["name"])
            say("  left '%s' on. Continuing." % step["name"])

    active = session.active_changes()
    session.state["status"] = "stopped" if failed else "done"
    session.state["kept_on_after_failed_check"] = skipped
    session.save()
    say("")
    say("Changes in effect: %d" % len(active))
    if skipped:
        say("Kept on because the TV failed its check without them: %s" % ", ".join(skipped))
        say("Please report your model (%s, %s) and these items as a GitHub issue." % (info["model"], info["fireos"]))
    if failed:
        say("Stopped at '%s': the TV still failed its check after rollback." % failed)
        say("Run 'python firetv_debloat.py restore' to undo everything, then restart the TV.")
    say("Undo everything: python firetv_debloat.py restore")
    say("Without Python:  %s" % os.path.relpath(os.path.join(session.path, "restore.bat" if os.name == "nt" else "restore.sh"), HERE))
    say("Tip: restart the TV once (Settings > My Fire TV > Restart) to free memory.")


def latest_session():
    if not os.path.isdir(SESSIONS_DIR):
        return None
    dirs = sorted(d for d in os.listdir(SESSIONS_DIR)
                  if os.path.isfile(os.path.join(SESSIONS_DIR, d, "state.json")))
    return os.path.join(SESSIONS_DIR, dirs[-1]) if dirs else None


def reconnect_session(session, args, prompter):
    adb_path = find_adb() or install_adb(prompter)
    want = session.state["device"].get("serialno")
    addr = args.ip or session.state["address"]
    say("Connecting to %s..." % addr)
    adb = connect(adb_path, addr, prompter, wait=60)
    if adb:
        _, info = describe(adb)
        if not want or info.get("serialno") == want:
            return adb
        say("  that address is a different device now")
    say("Searching the network for serial %s..." % want)
    for host in scan_network(args.subnet):
        adb = connect(adb_path, host, prompter, wait=60)
        if adb and describe(adb)[1].get("serialno") == want:
            return adb
    die("Could not find the Fire TV from this session. Pass --ip <address>.")


def cmd_restore(args, prompter):
    global _log_file
    path = args.target if args.target and args.target != "latest" else latest_session()
    if not path or not os.path.isfile(os.path.join(path, "state.json")):
        die("No session found to restore.")
    session = Session.load(path)
    _log_file = os.path.join(session.path, "log.txt")
    changes = session.active_changes()
    say("Session %s: %d change(s) to undo" % (os.path.basename(path), len(changes)))
    if not changes:
        say("Nothing to restore.")
        return
    adb = reconnect_session(session, args, prompter)
    session.state["address"] = adb.serial
    if not prompter.yes("Undo all %d change(s)?" % len(changes), True):
        return
    for c in reversed(changes):
        undo_change(adb, c)
        c["restored"] = True
        say("  restored: %s" % (c.get("label") or c.get("package") or c.get("key")))
    session.state["status"] = "restored"
    session.save()
    ok, line = healthy(adb, session, args.settle)
    session.shot(adb, "after-restore")
    say("Check: %s" % ("home screen OK" if ok else line))
    still = disabled_set(adb) & {c["package"] for c in changes if c["type"] == "package"}
    say("Restore %s." % ("complete" if not still else "incomplete, still off: " + ", ".join(sorted(still))))


def cmd_status(args, prompter):
    cat = load_catalog()
    adb_path = find_adb() or install_adb(prompter)
    adb, info = find_fire_tvs(adb_path, prompter, args.ip, args.subnet)
    installed, disabled = installed_set(adb), disabled_set(adb)
    say("")
    say("%s %s" % (info["model"], info["fireos"]))
    for p in cat["packages"]:
        if p["package"] in installed:
            say("  %-4s %-40s %s" % ("OFF" if p["package"] in disabled else "on", p["label"], p["package"]))


def cmd_scan(args, prompter):
    adb_path = find_adb() or install_adb(prompter)
    find_fire_tvs(adb_path, prompter, args.ip, args.subnet)


def main():
    ap = argparse.ArgumentParser(description="Reversibly debloat Amazon Fire TV devices over ADB.")
    ap.add_argument("command", nargs="?", default="debloat", choices=["debloat", "scan", "status", "restore"])
    ap.add_argument("target", nargs="?", help="session folder for restore (default: latest)")
    ap.add_argument("--ip", help="Fire TV address, skips the network scan")
    ap.add_argument("--subnet", help="subnet to scan, e.g. 192.168.1.0/24")
    ap.add_argument("--dry-run", action="store_true", help="back up and show the plan only")
    ap.add_argument("--defaults", action="store_true", help="accept default answers without prompting")
    ap.add_argument("--settle", type=float, default=5.0, help="seconds to wait on home before checks")
    args = ap.parse_args()
    say("FiretvDebloater %s" % VERSION)
    prompter = Prompter(args.defaults)
    try:
        {"debloat": cmd_debloat, "scan": cmd_scan, "status": cmd_status, "restore": cmd_restore}[args.command](args, prompter)
    except KeyboardInterrupt:
        die("Interrupted. Any change made so far is listed in the session's restore script.", 130)


if __name__ == "__main__":
    main()
