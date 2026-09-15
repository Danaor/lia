# -*- coding: utf-8 -*-
"""Lia - personal task-note store (the frictionless "things I need to do" pad).

A tiny, local, plain-JSON to-do store, DELIBERATELY separate from
action_items.py (which aggregates tasks EXTRACTED from meeting summaries and is
read-only over immutable meeting files). This one holds items the user jots
DIRECTLY - by voice (a hotkey -> speak -> it is added) or by typing in the
sticky window - so it owns its own mutable list.

Stdlib-only (like vocab_learn / lang_pack) so both the parent app (lia.py, the
voice-add path) and the sticky child window (tasknote_window.py) can import it
and read/write the same file directly - no IPC needed for CRUD. Writes are
atomic (tmp + os.replace); a per-process lock serialises this process's own
writes. Cross-process the two writers race only for the sub-millisecond of a
read-modify-write, and last-writer-wins on the whole file is acceptable for a
personal task pad (nothing critical is lost).

Store: %APPDATA%/Lia/tasks.json = {"tasks": [
    {"id": str, "text": str, "done": bool, "created": int, "done_ts": int}, ...
]}  - newest first.

ALWAYS launch a window that imports this with  python -X utf8  (Hebrew stdout
crashes under cp1252).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid


def _base():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lia")


# Module global so tests can point it at a tmp dir (tasks_store.STORE_PATH = ...);
# _load/_save read it at call time.
STORE_PATH = os.path.join(_base(), "tasks.json")

_LOCK = threading.Lock()
MAX_TASKS = 200          # cap so a runaway never bloats the file / the window
MAX_LEN = 500            # a task is one short line, not a paragraph


def _load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("tasks"), list):
            # keep only well-formed rows (defensive against a hand-edited file)
            d["tasks"] = [t for t in d["tasks"]
                          if isinstance(t, dict) and t.get("id") and "text" in t]
            return d
    except (OSError, ValueError):
        pass
    return {"tasks": []}


def _save(data):
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STORE_PATH)
        return True
    except OSError:
        return False


def _clean_text(text):
    """One tidy line: collapse all whitespace/newlines, trim, cap length."""
    return " ".join((text or "").split())[:MAX_LEN].strip()


def add(text):
    """Add a task (newest first). Returns the new task dict, or None if the text
    was empty after cleaning."""
    t = _clean_text(text)
    if not t:
        return None
    with _LOCK:
        d = _load()
        task = {"id": uuid.uuid4().hex[:12], "text": t, "done": False,
                "created": int(time.time()), "done_ts": 0}
        d["tasks"].insert(0, task)
        del d["tasks"][MAX_TASKS:]
        _save(d)
        return task


def set_done(task_id, done):
    """Mark a task done/undone. Returns the new done-state, or None if not found."""
    done = bool(done)
    with _LOCK:
        d = _load()
        for it in d["tasks"]:
            if it.get("id") == task_id:
                it["done"] = done
                it["done_ts"] = int(time.time()) if done else 0
                _save(d)
                return done
        return None


def toggle(task_id):
    """Flip a task's done-state. Returns the new state, or None if not found."""
    with _LOCK:
        d = _load()
        for it in d["tasks"]:
            if it.get("id") == task_id:
                nd = not it.get("done")
                it["done"] = nd
                it["done_ts"] = int(time.time()) if nd else 0
                _save(d)
                return nd
        return None


def edit(task_id, text):
    """Replace a task's text. Returns True on change; ignores an empty edit."""
    t = _clean_text(text)
    if not t:
        return False
    with _LOCK:
        d = _load()
        for it in d["tasks"]:
            if it.get("id") == task_id:
                it["text"] = t
                _save(d)
                return True
        return False


def delete(task_id):
    """Remove a task. Returns True if one was removed."""
    with _LOCK:
        d = _load()
        n = len(d["tasks"])
        d["tasks"] = [it for it in d["tasks"] if it.get("id") != task_id]
        if len(d["tasks"]) != n:
            _save(d)
            return True
        return False


def clear_done():
    """Remove every done task. Returns how many were cleared."""
    with _LOCK:
        d = _load()
        keep = [it for it in d["tasks"] if not it.get("done")]
        cleared = len(d["tasks"]) - len(keep)
        if cleared:
            d["tasks"] = keep
            _save(d)
        return cleared


def all_tasks():
    """Every task, newest first (open + done)."""
    return _load()["tasks"]


def open_tasks():
    return [t for t in _load()["tasks"] if not t.get("done")]


def counts():
    ts = _load()["tasks"]
    open_n = sum(1 for t in ts if not t.get("done"))
    return {"open": open_n, "done": len(ts) - open_n, "total": len(ts)}


def version():
    """A cheap change-token (mtime in ns) so a poller can detect an external
    write - e.g. the parent's voice-add - and reload. 0 when the file is absent."""
    try:
        return os.stat(STORE_PATH).st_mtime_ns
    except OSError:
        return 0
