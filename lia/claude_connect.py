# -*- coding: utf-8 -*-
"""Claude link: Lia's bridges written into the Claude apps found on this PC, with no step by the user.

Two servers, each a file of this install that Claude starts:

    lia           ears_bridge.py      reads the meeting being recorded      (when "Claude link" is on)
    lia-control   claude_control.py   starts and stops a recording          (when its own switch is on too)

Two places, each touched only when that Claude is there:

    Claude Code      <home>/.claude.json                            mcpServers  (user scope), and "mcp__lia" in
                     <home>/.claude/settings.json                   permissions.allow (those tools only read;
                                                                    lia-control is never allowed ahead: Claude asks)
    Claude Desktop   <a Claude folder>/claude_desktop_config.json   mcpServers

An entry Lia wrote carries env LIA_CONNECTOR=1 (the same mark as Lia Lite's connector). The rules:
  - an entry made by hand (no mark) is left as it is and counts as connected;
  - a marked entry whose script is another install that still exists (Lia Lite: its bridge reads this app too) is
    left as it is and counts as connected;
  - only a marked entry that points at THIS install is ever removed.
A file that is not valid JSON is never written. The first change to a file keeps a copy beside it
(<name>.lia-backup). Nothing here raises.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sys

log = logging.getLogger("Lia")

NAME = "lia"
CONTROL = "lia-control"
MARK = "LIA_CONNECTOR"
ALLOW = "mcp__lia"
DESKTOP_FILE = "claude_desktop_config.json"
DESKTOP_DIRS = ("Claude", "Claude-3p")
SCRIPTS = {NAME: "ears_bridge.py", CONTROL: "claude_control.py"}


def command(name=NAME):
    """(python.exe, args) that start one of this install's servers, or (None, None) when it cannot be started."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPTS[name])
    # beside this process's own exe (the runtime folder of an install); a renamed copy of pythonw that sits
    # elsewhere (the development tree's Lia.exe) has no python.exe beside it: the Python it was copied from
    dirs = [os.path.dirname(sys.executable), os.path.dirname(getattr(sys, "_base_executable", "") or ""),
            getattr(sys, "base_prefix", "") or ""]
    exe = next((p for p in (os.path.join(d, n) for d in dirs if d for n in ("python.exe", "pythonw.exe"))
                if os.path.exists(p)), "")
    if not exe or not os.path.exists(script):
        return None, None
    return exe, ["-X", "utf8", script]


def code_file(home):
    return os.path.join(home, ".claude.json")


def code_settings_file(home):
    return os.path.join(home, ".claude", "settings.json")


def desktop_files(appdata, localappdata):
    """The config file of every Claude Desktop folder that exists (the plain install, and the Store package's own
    roaming folder), whether or not the file is there yet."""
    dirs = [os.path.join(appdata, d) for d in DESKTOP_DIRS] if appdata else []
    if localappdata:
        for d in DESKTOP_DIRS:
            dirs += glob.glob(os.path.join(localappdata, "Packages", "Claude_*", "LocalCache", "Roaming", d))
    out = []
    for d in dirs:
        f = os.path.join(d, DESKTOP_FILE)
        # a package folder that only marks "the data lives elsewhere" is not a Claude to write to
        if os.path.isdir(d) and (os.path.exists(f) or "Packages" not in d) and f not in out:
            out.append(f)
    return out


def _read(path):
    """The file as a dict; {} when it is absent or empty; None when it cannot be read as a JSON object."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write(path, data):
    try:
        backup = path + ".lia-backup"
        if os.path.exists(path) and not os.path.exists(backup):
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".lia-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("Claude link: %s not written (%s)", os.path.basename(path), type(e).__name__)
        return False


def _marked(entry):
    return isinstance(entry, dict) and isinstance(entry.get("env"), dict) and entry["env"].get(MARK) == "1"


def _script(entry):
    """The .py file an entry starts, or ""."""
    args = entry.get("args") if isinstance(entry, dict) else None
    for a in args if isinstance(args, list) else ():
        if isinstance(a, str) and a.lower().endswith(".py"):
            return a
    return ""


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _set_server(path, name, entry):
    """-> True when the file names a `name` server after this call: ours (written or already right), one made by
    hand, or another install's that still exists."""
    data = _read(path)
    if data is None:
        return False
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        return False
    have = servers.get(name)
    if have is not None and not _marked(have):
        return True                     # made by hand: never touched
    if have == entry:
        return True
    if have is not None and not _same(_script(have), _script(entry)) and os.path.exists(_script(have)):
        return True                     # another install's bridge (Lia Lite) that is still there: it reads this app too
    servers[name] = entry
    return _write(path, data)


def _drop_server(path, name, script):
    """Remove the entry only when Lia wrote it and it points at this install. -> True when it was removed."""
    data = _read(path)
    if not data:
        return False
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and _marked(servers.get(name)) and _same(_script(servers[name]), script):
        del servers[name]
        return _write(path, data)
    return False


def _allow(path, on):
    """`mcp__lia` in Claude Code's permissions.allow: its tools only read, so Claude does not ask on each call."""
    data = _read(path)
    if data is None or (not on and not data):
        return
    perms = data.get("permissions")
    if perms is None:
        if not on:
            return
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        return
    allow = perms.get("allow")
    if allow is None:
        if not on:
            return
        allow = perms["allow"] = []
    if not isinstance(allow, list) or (ALLOW in allow) == on:
        return
    if on:
        allow.append(ALLOW)
    else:
        allow.remove(ALLOW)
    _write(path, data)


def _roots(home, appdata, localappdata):
    return (home if home is not None else os.path.expanduser("~"),
            appdata if appdata is not None else (os.environ.get("APPDATA") or ""),
            localappdata if localappdata is not None else (os.environ.get("LOCALAPPDATA") or ""))


def connect(home=None, appdata=None, localappdata=None, name=NAME, cmd=None):
    """Write one server into every Claude found. -> the places that now name it: a subset of ("code", "desktop")."""
    done = []
    try:
        home, appdata, localappdata = _roots(home, appdata, localappdata)
        exe, args = cmd if cmd is not None else command(name)
        if not exe:
            return done
        env = {MARK: "1"}
        cf = code_file(home)
        if os.path.exists(cf):          # Claude Code was used on this PC
            entry = {"type": "stdio", "command": exe, "args": list(args), "env": dict(env)}
            if _set_server(cf, name, entry):
                done.append("code")
                servers = (_read(cf) or {}).get("mcpServers")
                if name == NAME and isinstance(servers, dict) and servers.get(NAME) == entry:
                    _allow(code_settings_file(home), True)
        ok = False
        for f in desktop_files(appdata, localappdata):
            ok = _set_server(f, name, {"command": exe, "args": list(args), "env": dict(env)}) or ok
        if ok:
            done.append("desktop")
    except Exception as e:
        log.warning("Claude link: connect failed (%s)", type(e).__name__)
    return done


def disconnect(home=None, appdata=None, localappdata=None, name=NAME):
    """Remove what connect() wrote for this install (marked entries that point at it, nothing else)."""
    try:
        home, appdata, localappdata = _roots(home, appdata, localappdata)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPTS[name])
        if _drop_server(code_file(home), name, script) and name == NAME:
            _allow(code_settings_file(home), False)
        for f in desktop_files(appdata, localappdata):
            _drop_server(f, name, script)
    except Exception as e:
        log.warning("Claude link: disconnect failed (%s)", type(e).__name__)
