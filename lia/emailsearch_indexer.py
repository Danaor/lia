# -*- coding: utf-8 -*-
"""Lia — Outlook → local-index worker (the ONLY bulk COM holder).

Runs as its own subprocess (spawned by lia.py), so a flaky/wedged COM
call can never freeze the tray or the search UI — the parent just kills and
respawns it. It reads cached Outlook folders via COM, cleans + upserts rows into
email_index's SQLite (WAL), then embeds new/changed messages via Ollama bge-m3.

ALWAYS launch with  python -X utf8  (Hebrew stdout crashes under cp1252).

CLI:
    emailsearch_indexer.py [--full] [--limit N] [--no-embed] [--accounts a,b]
                           [--exclude f1,f2] [--current-meeting OUT.json]
  --full      reindex everything (ignore watermarks)
  --limit N   only the N most-recent items per folder (for quick tests)
  --no-embed  skip the embedding pass (keyword-only build)
  --current-meeting OUT.json
              speaker-naming helper (2026-08-27): query the calendars for the
              appointment(s) overlapping NOW, dump {subject, organizer,
              attendees[]} JSON to OUT.json, and exit (no indexing).
A status JSON is written to %APPDATA%/Lia/email/index_status.json
and a heartbeat timestamp to index_heartbeat.txt every few seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_index as ei

STATUS_FILE = os.path.join(ei.EMAIL_DIR, "index_status.json")
HEARTBEAT_FILE = os.path.join(ei.EMAIL_DIR, "index_heartbeat.txt")

# MAPI / PropertyAccessor proptags (faster than GetExchangeUser / Attachments)
PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"      # sender SMTP
PR_HASATTACH = "http://schemas.microsoft.com/mapi/proptag/0x0E1B000B"  # bool
OL_MAIL = 43          # olMail item Class
OL_APPT = 26          # olAppointment item Class
OL_FOLDER_CALENDAR = 9
OL_FOLDER_INBOX = 6
DEFAULT_FOLDERS = ("Inbox", "Sent Items", "Sent", "Archive")


# ---------------------------------------------------------------------------
def _heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def _write_status(**kw):
    kw.setdefault("ts", int(time.time()))
    try:
        ei.ensure_dir()
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def _load_state():
    try:
        with open(ei.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"watermarks": {}}


def _save_state(state):
    try:
        tmp = ei.STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, ei.STATE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
class Indexer:
    def __init__(self, accounts=None, exclude=None):
        self.accounts = [a.lower() for a in accounts] if accounts else None
        self.exclude = set(f.lower() for f in (exclude or []))
        self.app = None
        self.ns = None
        self.conn = None
        self.vconn = None

    # ---- COM ----
    def connect_outlook(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            self.app = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            # Not running. Last resort: Dispatch (launches it) — caller may prefer
            # to fail instead; we Dispatch so a scheduled refresh still works.
            self.app = win32com.client.Dispatch("Outlook.Application")
        self.ns = self.app.GetNamespace("MAPI")

    def target_folders(self):
        """[(account, folder_name, folder_obj)] for cached mail folders we index."""
        out = []
        stores = self.ns.Folders
        for i in range(1, stores.Count + 1):
            store = stores.Item(i)
            acct = store.Name
            if self.accounts and not any(a in acct.lower() for a in self.accounts):
                continue
            if "public folders" in acct.lower():
                continue
            try:
                subs = store.Folders
            except Exception:
                continue
            for j in range(1, subs.Count + 1):
                try:
                    fld = subs.Item(j)
                    name = fld.Name
                except Exception:
                    continue
                if name.lower() in self.exclude:
                    continue
                if name not in DEFAULT_FOLDERS:
                    continue
                try:
                    if getattr(fld, "DefaultItemType", 0) != 0:  # 0 = mail
                        continue
                except Exception:
                    pass
                out.append((acct, name, fld))
        return out

    def target_calendars(self):
        """[(account, folder_name, calendar_folder)] — the default calendar of
        each (non-public) store, so Teams/meeting items become searchable too."""
        out = []
        stores = self.ns.Folders
        for i in range(1, stores.Count + 1):
            root = stores.Item(i)
            try:
                acct = root.Name
            except Exception:
                continue
            if self.accounts and not any(a in acct.lower() for a in self.accounts):
                continue
            if "public folders" in acct.lower():
                continue
            cal = None
            try:
                cal = root.Store.GetDefaultFolder(OL_FOLDER_CALENDAR)
            except Exception:
                cal = None
            if cal is None:
                cal = self._find_calendar(root)
            if cal is not None:
                try:
                    nm = cal.Name
                except Exception:
                    nm = "Calendar"
                out.append((acct, nm, cal))
        return out

    @staticmethod
    def _find_calendar(folder, depth=0):
        """Fallback when Store.GetDefaultFolder isn't available: first folder
        whose default item type is appointment (1 = olAppointmentItem)."""
        try:
            if getattr(folder, "DefaultItemType", -1) == 1:
                return folder
        except Exception:
            pass
        if depth > 2:
            return None
        try:
            subs = folder.Folders
            for j in range(1, subs.Count + 1):
                f = Indexer._find_calendar(subs.Item(j), depth + 1)
                if f is not None:
                    return f
        except Exception:
            pass
        return None

    @staticmethod
    def _dt_to_epoch(v):
        """COM datetime -> epoch seconds. pywin32 returns the LOCAL wall-clock but
        newer builds mis-tag it as UTC (+00:00); calling v.timestamp() would
        over-shift by the local offset (+3h in Israel) and show a future/wrong
        time. Read the wall-clock components and treat them as local."""
        if v is None:
            return 0
        try:
            import datetime
            return int(datetime.datetime(
                v.year, v.month, v.day, v.hour, v.minute, v.second).timestamp())
        except Exception:
            return 0

    # ---- read one mail item ----
    @staticmethod
    def _read_item(item):
        def g(attr, default=""):
            try:
                v = getattr(item, attr)
                return v if v is not None else default
            except Exception:
                return default

        def prop(tag, default=""):
            try:
                v = item.PropertyAccessor.GetProperty(tag)
                return v if v is not None else default
            except Exception:
                return default

        sender_addr = prop(PR_SMTP) or g("SenderEmailAddress")
        if sender_addr and sender_addr.startswith("/"):  # unresolved EX address
            sender_addr = g("SenderEmailAddress")
        rt = 0
        for a in ("ReceivedTime", "SentOn", "CreationTime"):
            rt = Indexer._dt_to_epoch(g(a, None))
            if rt:
                break
        return {
            "entry_id": g("EntryID"),
            "sender_name": g("SenderName"),
            "sender_email": sender_addr,
            "to_recips": g("To"),
            "cc_recips": g("CC"),
            "subject": g("Subject"),
            "body": g("Body"),
            "received_ts": rt,
            "has_attach": 1 if prop(PR_HASATTACH, False) else 0,
            "unread": 1 if g("UnRead", False) else 0,
        }

    # ---- read one calendar appointment (Teams/meeting) ----
    @staticmethod
    def _read_appointment(item):
        def g(attr, default=""):
            try:
                v = getattr(item, attr)
                return v if v is not None else default
            except Exception:
                return default

        def prop(tag, default=""):
            try:
                v = item.PropertyAccessor.GetProperty(tag)
                return v if v is not None else default
            except Exception:
                return default

        req, opt = g("RequiredAttendees"), g("OptionalAttendees")
        attendees = "; ".join(x for x in (req, opt) if x)
        loc, body = g("Location"), g("Body")
        if loc:
            body = ("מיקום/Location: %s\n%s" % (loc, body)) if body else ("מיקום/Location: %s" % loc)
        start = Indexer._dt_to_epoch(g("Start", None))
        return {
            "entry_id": g("EntryID"),
            "sender_name": g("Organizer"),     # the meeting organizer
            "sender_email": "",
            "to_recips": attendees,            # invitees, searchable
            "cc_recips": "",
            "subject": g("Subject"),
            "body": body,
            "received_ts": start or Indexer._dt_to_epoch(g("CreationTime", None)),
            "has_attach": 1 if prop(PR_HASATTACH, False) else 0,
            "unread": 0,                       # meetings have no read/unread state
            "lmt": Indexer._dt_to_epoch(g("LastModificationTime", None)),
        }

    # ---- upsert ----
    def _upsert(self, store_id, account, folder, raw, kind="email"):
        body_clean = ei.clean_body(raw["body"])
        chash = ei.content_hash(raw["subject"], body_clean)
        row = self.conn.execute(
            "SELECT id, content_hash FROM messages WHERE entry_id=? AND store_id=?",
            (raw["entry_id"], store_id)).fetchone()
        now = int(time.time())
        unread = int(raw.get("unread", 0))
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO messages(entry_id,store_id,account,folder,kind,sender_name,"
                "sender_email,to_recips,cc_recips,subject,body_clean,received_ts,"
                "has_attach,unread,content_hash,indexed_ts,embedded) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (raw["entry_id"], store_id, account, folder, kind, raw["sender_name"],
                 raw["sender_email"], raw["to_recips"], raw["cc_recips"],
                 raw["subject"], body_clean, raw["received_ts"], raw["has_attach"],
                 unread, chash, now))
            return cur.lastrowid, True
        mid, old_hash = row["id"], row["content_hash"]
        if old_hash != chash:
            self.conn.execute(
                "UPDATE messages SET account=?,folder=?,kind=?,sender_name=?,sender_email=?,"
                "to_recips=?,cc_recips=?,subject=?,body_clean=?,received_ts=?,"
                "has_attach=?,unread=?,content_hash=?,indexed_ts=?,embedded=0 WHERE id=?",
                (account, folder, kind, raw["sender_name"], raw["sender_email"],
                 raw["to_recips"], raw["cc_recips"], raw["subject"], body_clean,
                 raw["received_ts"], raw["has_attach"], unread, chash, now, mid))
            return mid, True
        return mid, False

    # ---- sync ----
    def sync(self, full=False, limit=0):
        state = _load_state()
        wm = state.setdefault("watermarks", {})
        folders = self.target_folders()
        total_seen = total_new = 0
        for acct, fname, fld in folders:
            key = "%s/%s" % (acct, fname)
            store_id = ""
            try:
                store_id = fld.StoreID
            except Exception:
                pass
            watermark = 0 if full else int(wm.get(key, 0))
            items = fld.Items
            try:
                items.Sort("[ReceivedTime]", True)  # newest first
            except Exception:
                pass
            max_ts = watermark
            seen = 0
            for it in items:
                try:
                    if getattr(it, "Class", OL_MAIL) != OL_MAIL:
                        continue
                except Exception:
                    continue
                raw = self._read_item(it)
                if not raw["entry_id"]:
                    continue
                # newest-first: once we drop below the watermark we can stop
                if not full and raw["received_ts"] and raw["received_ts"] <= watermark:
                    break
                _mid, changed = self._upsert(store_id, acct, fname, raw)
                total_seen += 1
                seen += 1
                if changed:
                    total_new += 1
                if raw["received_ts"] > max_ts:
                    max_ts = raw["received_ts"]
                if seen % 200 == 0:
                    self.conn.commit()
                    _heartbeat()
                    _write_status(state="indexing", folder=key, total=total_seen,
                                  new=total_new)
                if limit and seen >= limit:
                    break
            self.conn.commit()
            wm[key] = max(max_ts, watermark)
            _save_state(state)
            _write_status(state="indexing", folder=key, total=total_seen, new=total_new)
        return total_seen, total_new

    # ---- calendar / meetings ----
    def sync_calendar(self, full=False, limit=0):
        """Index calendar appointments (incl. Teams meetings) as kind='meeting'
        rows so they're searchable alongside email. Incremental by each item's
        LastModificationTime; content_hash still dedupes unchanged items."""
        state = _load_state()
        wm = state.setdefault("watermarks", {})
        cals = self.target_calendars()
        total_seen = total_new = 0
        for acct, fname, fld in cals:
            key = "%s/__calendar__" % acct
            store_id = ""
            try:
                store_id = fld.StoreID
            except Exception:
                pass
            watermark = 0 if full else int(wm.get(key, 0))
            try:
                items = fld.Items
            except Exception:
                continue
            try:
                items.IncludeRecurrences = False   # masters only, not occurrences
            except Exception:
                pass
            sorted_ok = True
            try:
                items.Sort("[LastModificationTime]", True)  # most-recently-changed first
            except Exception:
                sorted_ok = False
            max_ts = watermark
            seen = 0
            for it in items:
                try:
                    if getattr(it, "Class", OL_APPT) != OL_APPT:
                        continue
                except Exception:
                    continue
                raw = self._read_appointment(it)
                if not raw["entry_id"]:
                    continue
                lmt = raw.get("lmt", 0)
                # only break early when the list is truly sorted newest-changed-first
                if not full and sorted_ok and lmt and lmt <= watermark:
                    break
                _mid, changed = self._upsert(store_id, acct, fname, raw, kind="meeting")
                total_seen += 1
                seen += 1
                if changed:
                    total_new += 1
                if lmt > max_ts:
                    max_ts = lmt
                if seen % 200 == 0:
                    self.conn.commit()
                    _heartbeat()
                    _write_status(state="indexing", folder=key, total=total_seen, new=total_new)
                if limit and seen >= limit:
                    break
            self.conn.commit()
            wm[key] = max(max_ts, watermark)
            _save_state(state)
        return total_seen, total_new

    # ---- read/unread reconcile ----
    def sync_unread(self):
        """Reconcile the unread flag for indexed MAIL folders. Outlook's
        Restrict("[Unread] = true") returns only the (usually few) unread items,
        so this is cheap AND watermark-independent: reading an OLD email correctly
        clears its bold, which the received_ts-watermarked sync() would never
        revisit. Only rows whose state actually changed are written, so the FTS
        sync triggers stay quiet on the untouched majority. Meetings (kind other
        than 'email') are left alone — they have no read/unread state."""
        try:
            folders = self.target_folders()
        except Exception:
            return 0
        changed = 0
        for _acct, fname, fld in folders:
            store_id = ""
            try:
                store_id = fld.StoreID
            except Exception:
                pass
            unread_ids = set()
            try:
                restricted = fld.Items.Restrict("[Unread] = true")
            except Exception:
                continue
            for it in restricted:
                try:
                    if getattr(it, "Class", OL_MAIL) != OL_MAIL:
                        continue
                    eid = it.EntryID
                    if eid:
                        unread_ids.add(eid)
                except Exception:
                    continue
            try:
                rows = self.conn.execute(
                    "SELECT id, entry_id, unread FROM messages "
                    "WHERE store_id=? AND folder=? AND kind='email'",
                    (store_id, fname)).fetchall()
            except Exception:
                continue
            for r in rows:
                want = 1 if r["entry_id"] in unread_ids else 0
                if (r["unread"] or 0) != want:
                    self.conn.execute("UPDATE messages SET unread=? WHERE id=?",
                                      (want, r["id"]))
                    changed += 1
            self.conn.commit()
        return changed

    # ---- embeddings ----
    def embed_pending(self, base_url=ei.DEFAULT_OLLAMA, batch_msgs=120):
        pending = self.conn.execute(
            "SELECT id, subject, body_clean FROM messages WHERE embedded=0 "
            "ORDER BY received_ts DESC").fetchall()
        if not pending:
            return 0
        done = 0
        # Build chunks across a batch of messages, embed in one Ollama call.
        i = 0
        while i < len(pending):
            batch = pending[i:i + batch_msgs]
            i += batch_msgs
            all_chunks, owners = [], []
            for r in batch:
                chunks = ei.chunk_text(r["subject"], r["body_clean"])
                if not chunks:
                    chunks = [(r["subject"] or "").strip() or " "]
                for c in chunks:
                    all_chunks.append(c)
                    owners.append(r["id"])
            if not all_chunks:
                continue
            vecs = ei.embed_texts(all_chunks, base_url)
            # group vectors back per message
            by_msg = {}
            for k, mid in enumerate(owners):
                by_msg.setdefault(mid, []).append(k)
            for mid, idxs in by_msg.items():
                ei.store_chunks(self.vconn, mid,
                                [all_chunks[k] for k in idxs],
                                [vecs[k] for k in idxs])
                self.conn.execute("UPDATE messages SET embedded=1 WHERE id=?", (mid,))
            self.conn.commit()
            done += len(batch)
            _heartbeat()
            _write_status(state="embedding", embedded=done, total=len(pending))
        return done


# ---------------------------------------------------------------------------
def split_attendee_names(*fields):
    """Display names out of Outlook's '; '-joined attendee strings (Required/
    Optional/Organizer). Pure + testable: strips, drops empties/dupes (case-
    insensitive), drops bare e-mail-looking entries' angle parts, keeps order."""
    out, seen = [], set()
    for field in fields:
        for part in (field or "").split(";"):
            name = part.strip().strip(",")
            # "Dana Cohen <dana@x>" -> "Dana Cohen"; a bare address stays as-is
            if "<" in name:
                name = name.split("<", 1)[0].strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def current_meeting_snapshot(idx, now=None, slack_min=5):
    """The calendar appointment(s) overlapping NOW (+-slack), as
    [{subject, organizer, attendees, start_epoch, end_epoch}] sorted by start
    (most recent first). Uses Restrict over IncludeRecurrences - the only way
    recurring Teams meetings resolve to today's occurrence."""
    import datetime as _dt
    now = now or _dt.datetime.now()
    lo = now + _dt.timedelta(minutes=slack_min)     # [Start] <= now+slack
    hi = now - _dt.timedelta(minutes=slack_min)     # [End]   >= now-slack
    # Outlook Restrict needs locale-tolerant date strings; this US form parses
    # on Hebrew Windows too (verified pattern used across Outlook automation).
    fmt = "%m/%d/%Y %I:%M %p"
    flt = ("[Start] <= '" + lo.strftime(fmt) + "' AND [End] >= '"
           + hi.strftime(fmt) + "'")
    out = []
    for _acct, _name, cal in idx.target_calendars():
        try:
            items = cal.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            found = items.Restrict(flt)
            it = found.GetFirst()
            n = 0
            while it is not None and n < 25:
                n += 1
                try:
                    if (getattr(it, "Class", OL_APPT) == OL_APPT
                            and not getattr(it, "AllDayEvent", False)):
                        # All-day items (vacations, reminders) overlap every
                        # meeting of the day - noise for speaker naming.
                        attendees = split_attendee_names(
                            getattr(it, "RequiredAttendees", ""),
                            getattr(it, "OptionalAttendees", ""))
                        out.append({
                            "subject": str(getattr(it, "Subject", "") or ""),
                            "organizer": str(getattr(it, "Organizer", "") or ""),
                            "attendees": attendees,
                            "start_epoch": Indexer._dt_to_epoch(
                                getattr(it, "Start", None)),
                            "end_epoch": Indexer._dt_to_epoch(
                                getattr(it, "End", None)),
                        })
                except Exception:
                    pass
                it = found.GetNext()
        except Exception as e:
            sys.stderr.write("current-meeting: calendar scan failed: %r\n" % (e,))
    out.sort(key=lambda m: m.get("start_epoch") or 0, reverse=True)
    return out


def _run_current_meeting(out_path):
    """--current-meeting mode: COM query only, atomic JSON dump, no indexing."""
    idx = Indexer(accounts=None, exclude=[])
    idx.connect_outlook()
    meetings = current_meeting_snapshot(idx)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"meetings": meetings, "ts": int(time.time())},
                  f, ensure_ascii=False)
    os.replace(tmp, out_path)
    sys.stdout.write("current-meeting: %d match(es)\n" % len(meetings))


def main():
    # Windowless spawns (pythonw / Lia.exe + CREATE_NO_WINDOW) hand this
    # process sys.stdout == sys.stderr == None. Any sys.stdout.write() then raises
    # "'NoneType' object has no attribute 'write'"; the first such print (the
    # "calendar:" line) aborted the run mid-way — its except handler's stderr.write
    # ALSO threw and propagated, so sync_unread() below never ran and the status was
    # stuck on "error". Route both to a log file (devnull if that fails) so writes
    # are always safe and we keep a diagnostic trail.
    if sys.stdout is None or sys.stderr is None:
        try:
            ei.ensure_dir()
            _sink = open(os.path.join(ei.EMAIL_DIR, "indexer.log"), "a",
                         encoding="utf-8")
        except Exception:
            _sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _sink
        if sys.stderr is None:
            sys.stderr = _sink

    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--accounts", default="")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--base-url", default=ei.DEFAULT_OLLAMA)
    ap.add_argument("--current-meeting", default="",
                    help="write the now-overlapping appointment(s) JSON here and exit")
    args = ap.parse_args()

    if args.current_meeting:
        try:
            _run_current_meeting(args.current_meeting)
        except Exception as e:
            sys.stderr.write("current-meeting failed: %s\n%s\n"
                             % (e, traceback.format_exc()))
            sys.exit(1)
        return

    ei.ensure_dir()
    _write_status(state="starting")
    _heartbeat()
    t0 = time.time()
    try:
        idx = Indexer(
            accounts=[a for a in args.accounts.split(",") if a.strip()] or None,
            exclude=[f for f in args.exclude.split(",") if f.strip()])
        idx.conn = ei.connect(ei.MESSAGES_DB)
        ei.init_schema(idx.conn)
        idx.vconn = ei.connect(ei.VECTORS_DB)
        ei.init_vectors(idx.vconn)
        idx.connect_outlook()

        seen, new = idx.sync(full=args.full, limit=args.limit)
        try:
            cseen, cnew = idx.sync_calendar(full=args.full, limit=args.limit)
            seen += cseen
            new += cnew
            sys.stdout.write("calendar: seen=%d new/changed=%d\n" % (cseen, cnew))
        except Exception as e:
            sys.stderr.write("calendar index skipped: %r\n" % (e,))
        try:
            uch = idx.sync_unread()
            sys.stdout.write("unread reconciled: %d changed\n" % uch)
        except Exception as e:
            sys.stderr.write("unread sync skipped: %r\n" % (e,))
        sys.stdout.write("indexed: seen=%d new/changed=%d in %.1fs\n"
                         % (seen, new, time.time() - t0))
        sys.stdout.flush()

        emb = 0
        if not args.no_embed:
            _write_status(state="embedding")
            emb = idx.embed_pending(base_url=args.base_url)
            sys.stdout.write("embedded: %d messages\n" % emb)
            sys.stdout.flush()

        st = ei.stats(idx.conn)
        _write_status(state="done", messages=st["messages"], embedded=st["embedded"],
                      last_sync=int(time.time()), elapsed=round(time.time() - t0, 1))
        sys.stdout.write("DONE total=%d embedded=%d in %.1fs\n"
                         % (st["messages"], st["embedded"], time.time() - t0))
    except Exception as e:
        _write_status(state="error", error="%s: %s" % (type(e).__name__, e))
        sys.stderr.write("indexer error: %s\n%s\n" % (e, traceback.format_exc()))
        sys.exit(1)


if __name__ == "__main__":
    main()
