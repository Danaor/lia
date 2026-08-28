# -*- coding: utf-8 -*-
"""Lia — Email Search window (standalone WebView2 / pywebview subprocess).

Sibling of compose_editor.py. The UI process: it queries the LOCAL index
(email_index, read-only WAL) synchronously per keystroke — never touching
Outlook COM on the hot path — and calls Ollama for the "ask" answer. Outlook COM
is touched ONLY on an explicit "Open in Outlook" (one guarded call).

Launch (spawned by lia.py):  python -X utf8 email_search.py <payload.json>
Payload: { "mode":"email_search", "title":..., "base_url":<ollama>,
           "answer_model":"gemma4:31b-it-qat",
           "models":[["Gemma 4 31B QAT","gemma4:31b-it-qat"], ...] }
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_index as ei

try:
    import webview
except Exception as e:  # pragma: no cover
    sys.stderr.write("email_search: pywebview not available: %r\n" % (e,))
    sys.exit(2)


PREFS_PATH = os.path.join(ei.EMAIL_DIR, "ui_prefs.json")

# Result caps. A filter-only BROWSE (date/folder, no free text) should show the
# whole range like an Outlook inbox — the old hard 80 made every date range look
# identical. A free-text query is ranked, so a top-N cap is plenty (nobody scrolls
# past a few hundred ranked hits) and keeps the semantic path bounded.
BROWSE_LIMIT = 5000
SEARCH_LIMIT = 500


# ---------------------------------------------------------------------------
def _fmt_date(ts):
    if not ts:
        return ""
    import datetime
    d = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%d/%m")
    return d.strftime("%d/%m/%y")


class SearchApi:
    """JS<->Python bridge. Never stores a pywebview Window (pywebview would
    recurse into Window.native forever when it introspects the api)."""

    def __init__(self, payload):
        self._payload = payload
        self._base_url = payload.get("base_url") or ei.DEFAULT_OLLAMA
        self._answer_model = payload.get("answer_model") or "gemma4:31b-it-qat"
        self._answer_key = payload.get("answer_key") or "ollama"
        self._models = payload.get("models") or [["Gemma 4 31B QAT", "gemma4:31b-it-qat"]]
        self._ready = False
        self._init_failed = False
        self._lock = threading.Lock()
        self._conn = None
        self._vconn = None
        self._prewarm_last = (None, 0.0)   # (model, ts) — de-dup repeat loads

    def _ensure_conn(self):
        if self._conn is None:
            # Bring the on-disk schema current (adds the 'unread'/'kind' columns to
            # a pre-existing DB) on a brief WRITABLE handle BEFORE opening read-only
            # — a query referencing a new column would otherwise fail on an
            # un-migrated DB, and a read-only connection can't ALTER. Idempotent.
            try:
                wconn = ei.connect(ei.MESSAGES_DB)
                ei.init_schema(wconn)
                wconn.close()
            except Exception:
                pass
            self._conn = ei.connect(ei.MESSAGES_DB, readonly=True)
        if self._vconn is None and os.path.exists(ei.VECTORS_DB):
            self._vconn = ei.connect(ei.VECTORS_DB, readonly=True)

    # ---- lifecycle ----
    def get_initial(self):
        self._ready = True
        st = self._index_status()
        accounts, folders = self._facets()
        return {
            "title": self._payload.get("title", "Email search"),
            "models": self._models,
            "answer_model": self._answer_model,
            "accounts": accounts,
            "folders": folders,
            "status": st,
            "prefs": self._load_prefs(),
        }

    def _index_status(self):
        try:
            self._ensure_conn()
            s = ei.stats(self._conn) if self._conn else {"messages": 0, "embedded": 0}
        except Exception:
            s = {"messages": 0, "embedded": 0}
        st = {}
        try:
            with open(os.path.join(ei.EMAIL_DIR, "index_status.json"), encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            pass
        s["state"] = st.get("state", "idle")
        s["error"] = st.get("error")
        s["last_sync"] = st.get("last_sync") or st.get("ts")
        s["progress"] = {k: st.get(k) for k in ("folder", "total", "embedded") if k in st}
        return s

    def _facets(self):
        try:
            self._ensure_conn()
            accts = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT account FROM messages WHERE account IS NOT NULL ORDER BY account")]
            flds = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT folder FROM messages WHERE folder IS NOT NULL ORDER BY folder")]
            return accts, flds
        except Exception:
            return [], []

    # ---- UI preference persistence (localStorage is per-subprocess in a
    # NavigateToString WebView2 doc — does NOT survive a relaunch — so the
    # window's "remembered" state lives in a tiny JSON via this bridge). ----
    def _load_prefs(self):
        try:
            with open(PREFS_PATH, encoding="utf-8") as f:
                p = json.load(f)
                return p if isinstance(p, dict) else {}
        except Exception:
            return {}

    def save_pref(self, key, value):
        try:
            ei.ensure_dir()
            prefs = self._load_prefs()
            prefs[str(key)] = value
            tmp = PREFS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False)
            os.replace(tmp, PREFS_PATH)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- search ----
    @staticmethod
    def _has_text(q):
        """True if the (post-filter) query has a free-text term. A filter-only
        browse (date/folder) parses to no FTS MATCH -> browse the whole range."""
        try:
            match, _w, _p = ei.parse_query(q)
            return bool(match)
        except Exception:
            return bool((q or "").strip())

    def search(self, query, filters=None):
        """Live keyword search. Returns ranked result dicts (date pre-formatted).
        Browse-by-filter shows the whole range; a text query is capped top-N."""
        filters = filters or {}
        q = self._apply_filters(query, filters)
        lim = SEARCH_LIMIT if self._has_text(q) else BROWSE_LIMIT
        with self._lock:
            try:
                self._ensure_conn()
                rows = ei.keyword_search(self._conn, q, limit=lim)
            except Exception as e:
                return {"error": str(e), "results": []}
        return {"results": [self._pack(r) for r in rows]}

    def smart_search(self, query, filters=None):
        """Hybrid (keyword + semantic) ranked list — on submit (embeds)."""
        filters = filters or {}
        q = self._apply_filters(query, filters)
        has_text = self._has_text(q)
        lim = SEARCH_LIMIT if has_text else BROWSE_LIMIT
        with self._lock:
            try:
                self._ensure_conn()
                # Semantic only makes sense with a free-text query; a filter-only
                # browse falls back to the (cheap) recency keyword listing.
                if self._vconn is not None and has_text:
                    rows = ei.hybrid_search(self._conn, self._vconn, q,
                                            self._base_url, limit=lim)
                else:
                    rows = ei.keyword_search(self._conn, q, limit=lim)
            except Exception as e:
                return {"error": str(e), "results": []}
        return {"results": [self._pack(r) for r in rows]}

    def _apply_filters(self, query, filters):
        q = query or ""
        if filters.get("account"):
            q += ' account:"%s"' % filters["account"]
        if filters.get("folder"):
            q += ' folder:"%s"' % filters["folder"]
        if filters.get("after"):
            q += " after:%s" % filters["after"]
        if filters.get("before"):
            q += " before:%s" % filters["before"]
        if filters.get("has_attach"):
            q += " has:attachment"
        return q.strip()

    def _pack(self, r):
        r = dict(r)
        r["date_str"] = _fmt_date(r.get("received_ts"))
        return r

    def preview(self, msg_id):
        with self._lock:
            try:
                self._ensure_conn()
                m = ei.get_message(self._conn, int(msg_id))
            except Exception as e:
                return {"error": str(e)}
        if not m:
            return {"error": "not found"}
        return {
            "id": m["id"], "entry_id": m["entry_id"], "store_id": m.get("store_id"),
            "sender_name": m.get("sender_name") or "", "sender_email": m.get("sender_email") or "",
            "to_recips": m.get("to_recips") or "", "cc_recips": m.get("cc_recips") or "",
            "subject": m.get("subject") or "", "folder": m.get("folder") or "",
            "account": m.get("account") or "", "has_attach": bool(m.get("has_attach")),
            "kind": m.get("kind") or "email",
            "date_str": _fmt_date(m.get("received_ts")),
            "body": m.get("body_clean") or "",
        }

    # ---- ask (RAG) ----
    def ask(self, question, model=None, filters=None):
        model = model or self._answer_model
        with self._lock:
            try:
                self._ensure_conn()
                if self._vconn is None:
                    return {"error": "No semantic index yet (still indexing?)"}
                res = ei.ask(self._conn, self._vconn, question,
                             base_url=self._base_url, model=model,
                             api_key=self._answer_key)
            except Exception as e:
                return {"error": str(e)}
        res["sources"] = [self._pack(s) for s in res.get("sources", [])]
        return res

    def prewarm(self, model=None):
        """Fire-and-forget: load the chosen answer model into VRAM so the FIRST
        real ask doesn't pay the cold-load. Tiny 1-token request with a 10-min
        keep_alive. Only touches the LOCAL Ollama (never a cloud key). De-duped:
        a repeat request for the same model within 90s is a no-op (prevents VRAM
        thrash when the dropdown is toggled)."""
        model = (model or self._answer_model or "").strip()
        url = (self._base_url or "").lower()
        is_local = ("localhost" in url) or ("127.0.0.1" in url) or (":11434" in url)
        if not model or not is_local:
            return {"ok": False}
        last_model, last_ts = self._prewarm_last
        if model == last_model and (time.time() - last_ts) < 90:
            return {"ok": True, "skipped": True}
        self._prewarm_last = (model, time.time())

        def _w():
            try:
                import requests
                root = ei._ollama_root(self._base_url)
                requests.post(root + "/api/chat", json={
                    "model": model, "stream": False, "keep_alive": "10m",
                    "options": {"num_predict": 1},
                    "messages": [{"role": "user", "content": "hi"}],
                }, timeout=(10, 180))
            except Exception:
                pass
        threading.Thread(target=_w, daemon=True).start()
        return {"ok": True}

    # ---- actions ----
    def outlook_action(self, entry_id, store_id, action="open", msg_id=None):
        """Resolve the item by EntryID and perform an Outlook action — open,
        reply, replyall, or forward — displaying the resulting window (a NEW
        draft for reply/forward; never auto-sent) and bringing it to the front.
        Runs on a dedicated thread with its own apartment, then joins so we can
        return a real ok/error to the UI. CoInitialize() may raise
        RPC_E_CHANGED_MODE (WebView2/.NET MTA worker threads) — tolerated; COM
        still works.

        SELF-HEALING: Outlook EntryIDs are NOT permanent — they change when an
        item is moved (e.g. to Deleted Items) or after a cached-mode resync, so a
        stored id can go stale and GetItemFromID then fails with MAPI_E_NOT_FOUND.
        When that happens we re-locate the item across the account's folders by
        subject + received-time (see _relocate_item), open THAT, and write the
        fresh id back to the index so it's instant next time."""
        result = {"ok": False, "error": None}

        def _work():
            com_inited = False
            try:
                import pythoncom
                import win32com.client
                try:
                    pythoncom.CoInitialize()
                    com_inited = True
                except Exception:
                    pass  # already in an apartment (MTA via WebView2/.NET) — OK
                try:
                    ol = win32com.client.GetActiveObject("Outlook.Application")
                except Exception:
                    ol = win32com.client.Dispatch("Outlook.Application")
                ns = ol.GetNamespace("MAPI")
                try:
                    item = ns.GetItemFromID(entry_id, store_id) if store_id \
                        else ns.GetItemFromID(entry_id)
                except Exception:
                    item = None
                if item is None:                     # stale id → self-heal
                    hint = self._hint_for(msg_id)
                    if hint:
                        found, neid, nsid, nfolder = self._relocate_item(ns, hint)
                        if found is not None:
                            item = found
                            self._writeback_entry(msg_id, neid, nsid, nfolder)
                if item is None:
                    result["error"] = ("Message not found in Outlook (it may have been "
                                       "permanently deleted or moved). Try ⟳ to refresh.")
                    return
                a = (action or "open").lower()
                if a == "reply":
                    target = item.Reply()
                elif a in ("replyall", "reply_all"):
                    target = item.ReplyAll()
                elif a == "forward":
                    target = item.Forward()
                else:
                    target = item
                target.Display(False)
                try:
                    target.GetInspector.Activate()   # pop it to the foreground
                except Exception:
                    pass
                result["ok"] = True
            except Exception as e:
                result["error"] = "%s: %s" % (type(e).__name__, e)
                sys.stderr.write("outlook_action(%s) failed: %r\n" % (action, e))
            finally:
                if com_inited:
                    try:
                        import pythoncom
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout=25)
        if t.is_alive():
            return {"ok": False, "error": "Outlook did not respond in time"}
        return result

    def open_in_outlook(self, entry_id, store_id, subject="", msg_id=None):
        """Back-compat: open the item (used by double-click / Enter)."""
        return self.outlook_action(entry_id, store_id, "open", msg_id=msg_id)

    # ---- self-heal helpers (COM thread) ----
    def _hint_for(self, msg_id):
        """Stored (account, folder, subject, received_ts) for a row — the anchors
        _relocate_item uses to find a moved/deleted item. None if unavailable."""
        if not msg_id:
            return None
        try:
            with self._lock:
                self._ensure_conn()
                r = self._conn.execute(
                    "SELECT account, folder, subject, received_ts FROM messages "
                    "WHERE id=?", (int(msg_id),)).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    @staticmethod
    def _subject_core(s):
        """Distinctive subject substring for a locale-independent DASL LIKE:
        drop emojis/punctuation, keep word chars + Hebrew, collapse spaces."""
        import re
        s = re.sub(r"[^\w֐-׿ ]+", " ", s or "")
        return " ".join(s.split())

    def _relocate_item(self, ns, hint):
        """Walk the account's folders, DASL-match the subject core, pick the item
        closest in received-time to the stored value. -> (item, entry_id, store_id,
        folder_name) or (None, None, None, None)."""
        import datetime
        core = self._subject_core(hint.get("subject"))
        if len(core) < 4:
            return None, None, None, None
        acct = hint.get("account")
        want_ts = hint.get("received_ts") or 0
        store = None
        try:
            for i in range(1, ns.Folders.Count + 1):
                f = ns.Folders.Item(i)
                try:
                    if f.Name == acct:
                        store = f
                        break
                except Exception:
                    continue
        except Exception:
            return None, None, None, None
        if store is None:
            return None, None, None, None
        store_id = ""
        try:
            store_id = store.StoreID
        except Exception:
            pass
        filt = ('@SQL="urn:schemas:httpmail:subject" LIKE '
                + "'%%%s%%'" % core.replace("'", "''"))
        best = best_diff = best_folder = None
        seen = 0
        stack = [store]
        while stack and seen < 400:
            fld = stack.pop()
            try:
                for j in range(1, fld.Folders.Count + 1):
                    stack.append(fld.Folders.Item(j))
            except Exception:
                pass
            try:
                res = fld.Items.Restrict(filt)
                fname = fld.Name
            except Exception:
                continue
            for it in res:
                seen += 1
                if seen >= 400:
                    break
                try:
                    rt = it.ReceivedTime
                    epoch = int(datetime.datetime(
                        rt.year, rt.month, rt.day, rt.hour, rt.minute,
                        rt.second).timestamp())
                except Exception:
                    epoch = 0
                diff = abs(epoch - want_ts) if (want_ts and epoch) else 10 ** 12
                if best is None or diff < best_diff:
                    best, best_diff, best_folder = it, diff, fname
        if best is None:
            return None, None, None, None
        try:
            return best, best.EntryID, store_id, best_folder
        except Exception:
            return best, None, store_id, best_folder

    def _writeback_entry(self, msg_id, entry_id, store_id, folder):
        """Persist a re-located item's fresh id/folder so the next open is instant.
        Best-effort: a UNIQUE(entry_id,store_id) clash just means we open it this
        time and skip the write."""
        if not (msg_id and entry_id):
            return
        try:
            wconn = ei.connect(ei.MESSAGES_DB)
            wconn.execute(
                "UPDATE messages SET entry_id=?, store_id=?, folder=? WHERE id=?",
                (entry_id, store_id or "", folder or "", int(msg_id)))
            wconn.commit()
            wconn.close()
        except Exception:
            pass

    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(text or "")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def refresh(self):
        """Kick an incremental reindex (spawn the indexer subprocess). COM lives
        ONLY in that child — this UI process never touches Outlook. Progress and
        the Outlook-closed case surface through poll_status()."""
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "emailsearch_indexer.py")
            import subprocess
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            subprocess.Popen([sys.executable, "-X", "utf8", script,
                              "--base-url", self._base_url],
                             creationflags=flags, env=env)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def poll_status(self):
        return self._index_status()

    def close_window(self):
        def _destroy():
            try:
                for w in list(webview.windows):
                    w.destroy()
            except Exception:
                pass
        threading.Thread(target=_destroy, daemon=True).start()
        return True


# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<style>
  :root{
    --accent:#22314F; --accent-d:#2C3E63; --accent-soft:#EDEFF3; --accent-line:#D6DCE8;
    --ink:#1B2430; --ink-soft:#2A3342; --muted:#6B7280; --faint:#9AA0AD;
    --line:#E2E5EA; --line-2:#CDD3DC; --bg:#FFFFFF; --hover:#F0F1F4;
    --sel:#EDEFF3; --hi:#FFF3BF; --chip:#EDEFF3;
    --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{background:var(--bg);color:var(--ink);font-size:14px;
       font-family:"Segoe UI Variable Text","Segoe UI",Heebo,system-ui,Arial,sans-serif;
       display:flex;flex-direction:column;-webkit-font-smoothing:antialiased;}

  /* ---- top toolbar ---- */
  .top{flex:none;background:var(--bg);border-bottom:1px solid var(--line-2);
       padding:11px 16px 10px;z-index:6;}
  .searchrow{display:flex;gap:9px;align-items:center;}
  .qwrap{flex:1;position:relative;display:flex;align-items:center;}
  .qwrap .ic{position:absolute;inset-inline-start:12px;color:var(--faint);font-size:15px;pointer-events:none;}
  #q{flex:1;width:100%;font-size:15px;padding:11px 38px;border:1px solid var(--line-2);
     border-radius:11px;outline:none;background:#F4F5F7;color:var(--ink);transition:border-color .12s,box-shadow .12s,background .12s;}
  #q::placeholder{color:var(--faint);}
  #q:focus{background:#FFFFFF;border-color:var(--accent);box-shadow:0 0 0 3px rgba(34,49,79,.13);}
  .qclear{position:absolute;inset-inline-end:10px;color:var(--faint);cursor:pointer;font-size:16px;
          width:20px;height:20px;line-height:20px;text-align:center;border-radius:50%;display:none;}
  .qclear:hover{background:var(--chip);color:var(--ink);}
  .btn{border:1px solid var(--line-2);background:#fff;border-radius:10px;padding:10px 13px;
       cursor:pointer;font-size:14px;white-space:nowrap;color:var(--ink-soft);transition:background .12s,border-color .12s,box-shadow .12s;}
  .btn:hover{background:var(--hover);border-color:var(--line-2);}
  .btn:active{transform:translateY(.5px);}
  .btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;box-shadow:0 1px 2px rgba(34,49,79,.25);}
  .btn.primary:hover{background:var(--accent-d);border-color:var(--accent-d);}
  .btn.icon{padding:10px 12px;font-size:16px;line-height:1;}
  select{padding:9px 10px;border:1px solid var(--line-2);border-radius:10px;background:#fff;
         font-size:13px;color:var(--ink-soft);cursor:pointer;outline:none;}
  select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(34,49,79,.12);}
  #model{max-width:170px;}

  .filters{display:flex;gap:7px;align-items:center;margin-top:9px;flex-wrap:wrap;
           font-size:12.5px;color:var(--muted);}
  .filters .seg{display:flex;background:var(--chip);border-radius:10px;padding:3px;gap:2px;}
  .filters .seg button{border:0;background:transparent;color:var(--muted);font-size:12.5px;
        padding:6px 12px;border-radius:8px;cursor:pointer;font-weight:600;transition:background .1s,color .1s;}
  .filters .seg button.on{background:#fff;color:var(--accent);box-shadow:var(--shadow);}
  .filters label{display:flex;gap:5px;align-items:center;color:var(--muted);}
  .filters input,.filters select{font-size:12.5px;padding:6px 8px;border-radius:8px;}
  .filters input[type=date]{color:var(--ink-soft);}
  .spacer{flex:1;}

  /* ---- answer (ask) ---- */
  .answer{flex:none;margin:12px 16px 0;padding:14px 16px;background:var(--accent-soft);
          border:1px solid var(--accent-line);border-radius:13px;line-height:1.75;display:none;}
  .ans-head{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--accent);
            font-size:13px;margin-bottom:6px;}
  .ans-sub{font-weight:500;color:var(--muted);font-size:12px;}
  .ans-body{color:var(--ink);white-space:pre-wrap;line-height:1.8;
            font-size:calc(15px*var(--z));overflow-wrap:anywhere;}
  .ans-hint{color:var(--muted);font-size:13px;}
  .ans-card{background:var(--accent-soft);border:1px solid var(--accent-line);
            border-radius:12px;padding:14px 16px;line-height:1.75;}
  .cite{color:var(--accent);cursor:pointer;font-weight:800;text-decoration:none;}
  .cite:hover{text-decoration:underline;}
  .spin{width:14px;height:14px;border:2px solid var(--accent-line);border-top-color:var(--accent);
        border-radius:50%;display:inline-block;animation:sp .7s linear infinite;}
  @keyframes sp{to{transform:rotate(360deg);}}

  /* ---- main split: LIST (left) | splitter | PREVIEW (right) ---- */
  .main{flex:1;display:flex;direction:ltr;min-height:0;}      /* ltr layout: first child = left */
  .results{width:42%;overflow:auto;direction:rtl;
           border-right:1px solid var(--line-2);background:var(--bg);}
  .splitter{flex:none;width:6px;cursor:col-resize;background:transparent;position:relative;}
  .splitter::after{content:"";position:absolute;inset:0 2px;border-radius:3px;background:transparent;transition:background .12s;}
  .splitter:hover::after,.splitter.drag::after{background:var(--accent-line);}
  .preview{flex:1;overflow:auto;direction:rtl;background:var(--bg);}

  .listhead{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
            padding:8px 14px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between;
            align-items:center;z-index:2;}
  .row{padding:11px 14px;border-bottom:1px solid var(--line);cursor:pointer;position:relative;
       transition:background .08s;}
  .row:hover{background:var(--hover);}
  .row.sel{background:var(--sel);}
  .row.sel::before{content:"";position:absolute;inset-inline-start:0;top:0;bottom:0;width:3px;background:var(--accent);}
  .row .r1{display:flex;justify-content:space-between;gap:8px;align-items:baseline;}
  .row .who{font-weight:650;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .row .dt{color:var(--faint);font-size:11.5px;flex:none;font-variant-numeric:tabular-nums;}
  .row .subj{color:var(--ink-soft);margin:3px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .row .snip{color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  /* unread mail: bold + blue (Outlook-style); read stays regular black */
  .row.unread .who{font-weight:800;color:var(--accent);}
  .row.unread .subj{font-weight:700;color:var(--accent);}
  mark{background:var(--hi);padding:0 1px;border-radius:2px;color:inherit;}

  /* ---- preview pane (fills the pane; --z zooms the email text) ---- */
  .pv{--z:1;padding:22px 32px;}
  .pv-actions{display:flex;gap:8px;margin-bottom:16px;align-items:center;flex-wrap:wrap;}
  .zoomgrp{display:flex;gap:4px;align-items:center;margin-inline-start:auto;}
  .btn.zoom{padding:7px 11px;font-size:13px;font-weight:700;line-height:1;}
  .pv-tip{color:var(--faint);font-size:11.5px;margin-inline-start:12px;}
  .pv-h{font-size:calc(21px*var(--z));font-weight:700;margin:0 0 8px;line-height:1.35;color:var(--ink);overflow-wrap:anywhere;}
  .pv-meta{color:var(--muted);font-size:calc(12.5px*var(--z));margin-bottom:16px;line-height:1.85;overflow-wrap:anywhere;}
  .pv-meta b{color:var(--ink-soft);}
  .pv-from{display:flex;align-items:center;gap:9px;margin-bottom:6px;}
  .avatar{width:34px;height:34px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
          font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center;flex:none;}
  .chip{display:inline-block;font-size:11px;background:var(--chip);color:var(--ink-soft);
        border-radius:20px;padding:2px 9px;margin-inline-start:6px;}
  .chip.acc{background:var(--accent-soft);color:var(--accent);}
  .pv-divider{height:1px;background:var(--line);margin:0 0 16px;}
  .pv-body{white-space:pre-wrap;line-height:1.8;color:var(--ink-soft);font-size:calc(14px*var(--z));overflow-wrap:anywhere;}

  .empty{color:var(--muted);padding:54px 24px;text-align:center;display:flex;flex-direction:column;
         align-items:center;gap:10px;}
  .empty .big{font-size:34px;opacity:.55;}
  .empty .sub{font-size:12.5px;color:var(--faint);max-width:280px;line-height:1.6;}

  /* ---- status bar ---- */
  .status{flex:none;font-size:12px;color:var(--muted);padding:7px 16px;border-top:1px solid var(--line-2);
          background:#F4F5F7;display:flex;justify-content:space-between;align-items:center;gap:10px;}
  .status .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#22c55e;margin-inline-end:6px;}
  .status .dot.warn{background:#b06a00;} .status .dot.err{background:#c0263c;} .status .dot.busy{background:var(--accent);animation:pulse 1s infinite;}
  @keyframes pulse{50%{opacity:.35;}}
  ::-webkit-scrollbar{width:11px;height:11px;}
  ::-webkit-scrollbar-thumb{background:#CDD3DC;border-radius:8px;border:3px solid var(--bg);}
  ::-webkit-scrollbar-thumb:hover{background:#9AA0AD;}
</style></head>
<body>
  <div class="top">
    <div class="searchrow">
      <div class="qwrap">
        <span class="ic">🔍</span>
        <input id="q" autocomplete="off" spellcheck="false"
               dir="auto" placeholder="Search your email…  (try from: / after:2026/06/01 / has:attachment)" />
        <span class="qclear" id="qclear" title="Clear">✕</span>
      </div>
      <button class="btn primary" id="askBtn" title="Ctrl+Enter">💬 Ask</button>
      <button class="btn" id="ansBtn" title="Show the last answer again" style="display:none">💬 Answer</button>
      <select id="model" title="Answer model"></select>
      <button class="btn icon" id="refresh" title="Refresh the index (Outlook must be open)">⟳</button>
    </div>
    <div class="filters">
      <div class="seg" id="accSeg"></div>
      <label>Folder <select id="fFolder"><option value="">All</option></select></label>
      <label>From <input type="date" id="fAfter"></label>
      <label>To <input type="date" id="fBefore"></label>
      <label><input type="checkbox" id="fAttach"> 📎 Attachment</label>
      <span class="spacer"></span>
    </div>
  </div>

  <div class="main" id="main">
    <div class="results" id="results">
      <div class="empty"><span class="big">✉️</span><div>Type to search</div>
        <div class="sub">Instant search across all your mail. Double-click a message to open it in Outlook.</div></div>
    </div>
    <div class="splitter" id="splitter"></div>
    <div class="preview" id="preview">
      <div class="empty"><span class="big">📭</span><div>Select a message to preview</div>
        <div class="sub">The preview shows here. Double-click to open it in Outlook and reply.</div></div>
    </div>
  </div>

  <div class="status">
    <span id="stat"></span>
    <span id="stat2"></span>
  </div>

<script>
var api, results=[], sel=-1, model="", debounce=null, account="";
var lastPrewarmed="", prewarmTimer=null, flashTimer=null, refreshTimer=null, refreshPreMsgs=0;
var lastStatus={}, refreshPreTs=0, baseStat="", statusSeq=0, askedModels={}, pvZoom=1;
var lastRefreshKick=0, askActive=false, lastAns=null;
function $(id){return document.getElementById(id);}
function esc(s){return (s||"").replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function hl(s){ return esc(s).replace(/\x02/g,'<mark>').replace(/\x03/g,'</mark>'); }
function initials(s){ s=(s||'').trim(); if(!s) return '?'; var p=s.split(/[\s<]+/).filter(Boolean);
  return ((p[0]||'?')[0] + (p.length>1?(p[1][0]||''):'')).toUpperCase(); }
function acctLabel(a){
  if(!a) return 'All';
  if(/cloud-professional/i.test(a)) return 'Cloud Professional';
  var m=a.match(/^([^@]+)@/); if(m) return m[1];
  return a;
}
function filters(){
  return {account:account, folder:$('fFolder').value,
          after:$('fAfter').value.replace(/-/g,'/'), before:$('fBefore').value.replace(/-/g,'/'),
          has_attach:$('fAttach').checked};
}
/* status bar: setStat returns a sequence token; delayed restores only apply if
   nothing else has painted the bar since (prevents stale timers stomping live
   refresh/flash status). */
function setStat(html, cls){ statusSeq++; $('stat').innerHTML=(cls?('<span class="dot '+cls+'"></span>'):'')+html; return statusSeq; }
function baseStatus(s){
  return ((s.messages||0)).toLocaleString()+' emails · '+((s.embedded||0)).toLocaleString()+' with smart search';
}
function setBase(s){ baseStat=baseStatus(s); return baseStat; }
function flashStat(msg){
  var my=setStat(esc(msg),'busy'); clearTimeout(flashTimer);
  flashTimer=setTimeout(function(){ if(statusSeq===my) setStat(baseStat,'ok'); }, 1800);
}

function clearPreview(){
  window._pv=null;
  $('preview').innerHTML='<div class="empty"><span class="big">📭</span><div>Select a message to preview</div>'
    +'<div class="sub">The preview shows here. Double-click to open it in Outlook and reply.</div></div>';
}

function renderResults(){
  var el=$('results');
  if(!results.length){
    el.innerHTML='<div class="empty"><span class="big">🔎</span><div>No results</div>'
      +'<div class="sub">Try different wording, or clear the filters.</div></div>'; return;
  }
  var head='<div class="listhead"><span>'+results.length+' results</span><span></span></div>';
  el.innerHTML = head + results.map(function(r,i){
    var meet = r.kind==='meeting';
    var sentish = r.folder==='Sent'||r.folder==='Sent Items';
    var who = meet ? ('Meeting · '+esc(r.sender_name||''))
            : (sentish ? ('To: '+esc(r.to_recips)) : esc(r.sender_name||r.sender_email));
    var subj = (meet?'📅 ':'')+esc(r.subject||'(no subject)');
    var cls = 'row'+(i===sel?' sel':'')+(r.unread?' unread':'');
    return '<div class="'+cls+'" data-i="'+i+'">'
      +'<div class="r1"><span class="who">'+who+(r.has_attach?' 📎':'')+'</span><span class="dt">'+esc(r.date_str)+'</span></div>'
      +'<div class="subj">'+subj+'</div>'
      +'<div class="snip">'+hl(r.snippet)+'</div></div>';
  }).join('');
  Array.prototype.forEach.call(el.querySelectorAll('.row'), function(rw){
    var i=+rw.dataset.i;
    rw.addEventListener('click', function(){ select(i); });
    rw.addEventListener('dblclick', function(){ openOut(i); });
  });
}

async function select(i){
  if(i<0||i>=results.length) return;
  sel=i;
  Array.prototype.forEach.call($('results').querySelectorAll('.row'), function(rw){
    rw.classList.toggle('sel', +rw.dataset.i===i);
  });
  var row=$('results').querySelector('.row[data-i="'+i+'"]'); if(row) row.scrollIntoView({block:'nearest'});
  var r=results[i];
  var pv=await api.preview(r.id);
  if(pv.error){ $('preview').innerHTML='<div class="empty"><span class="big">⚠️</span><div>'+esc(pv.error)+'</div></div>'; return; }
  var cc = pv.cc_recips?('<br>Cc: '+esc(pv.cc_recips)):'';
  var bodyHtml = (pv.body && pv.body.trim())
    ? esc(pv.body)
    : '<span style="color:var(--faint)">- no text content'+(pv.has_attach?' (it is probably in the attachment - open in Outlook)':' (the message may be HTML/image only)')+' -</span>';
  var meet = pv.kind==='meeting';
  var olBtns = '<button class="btn primary" onclick="olAct(\'open\')">↪ Open</button>';
  if(!meet){
    olBtns += '<button class="btn" onclick="olAct(\'reply\')">↩ Reply</button>'
            + '<button class="btn" onclick="olAct(\'replyall\')">↩ Reply All</button>'
            + '<button class="btn" onclick="olAct(\'forward\')">→ Forward</button>';
  }
  $('preview').innerHTML =
    '<div class="pv" style="--z:'+pvZoom+'">'
    +'<div class="pv-actions">'
      + olBtns
      +'<button class="btn" onclick="copyBody()">⧉ Copy</button>'
      +'<span class="zoomgrp"><button class="btn zoom" onclick="zoomPv(-1)" title="Smaller text">A−</button>'
      +'<button class="btn zoom" onclick="zoomPv(1)" title="Larger text">A+</button></span>'
    +'</div>'
    +'<div class="pv-h" dir="auto">'+esc(pv.subject||'(no subject)')+'</div>'
    +'<div class="pv-from">'
      +'<span class="avatar">'+esc(initials(pv.sender_name||pv.sender_email))+'</span>'
      +'<div class="pv-meta" dir="auto" style="margin:0"><b>'+esc(pv.sender_name||pv.sender_email)+'</b>'
        +(pv.sender_email?(' &lt;'+esc(pv.sender_email)+'&gt;'):'')+' · '+esc(pv.date_str)
        +'<br>To: '+esc(pv.to_recips)+cc+'</div>'
    +'</div>'
    +'<div style="margin-bottom:14px">'+(pv.kind==='meeting'?'<span class="chip acc">📅 Meeting</span>':'')
      +'<span class="chip acc">'+esc(acctLabel(pv.account))+'</span>'
      +'<span class="chip">'+esc(pv.folder)+'</span>'+(pv.has_attach?'<span class="chip">📎 Attachment</span>':'')+'</div>'
    +'<div class="pv-divider"></div>'
    +'<div class="pv-body" dir="auto" id="pvbody">'+bodyHtml+'</div>'
    +'</div>';
  window._pv=pv;
}
async function openOut(i){
  var r=results[i]; if(!r) return;
  flashStat('Opening in Outlook…');
  var res; try{ res=await api.open_in_outlook(r.entry_id, r.store_id||"", r.subject||"", r.id); }catch(e){ res={ok:false,error:''+e}; }
  if(res && res.ok){ flashStat('Opened in Outlook ✓'); }
  else { setStat('Could not open in Outlook'+((res&&res.error)?(' - '+esc(res.error)):' (is Outlook running?)'),'err'); }
}
async function olAct(action){
  var r=results[sel]; if(!r) return;
  var labels={open:'Opening',reply:'Reply',replyall:'Reply All',forward:'Forward'};
  flashStat((labels[action]||'Opening')+' in Outlook…');
  var res; try{ res=await api.outlook_action(r.entry_id, r.store_id||"", action, r.id); }catch(e){ res={ok:false,error:''+e}; }
  if(res && res.ok){ flashStat('Opened in Outlook ✓'); }
  else { setStat('Could not open in Outlook'+((res&&res.error)?(' - '+esc(res.error)):' (is Outlook running?)'),'err'); }
}
function copyBody(){ if(window._pv) api.copy((window._pv.subject||'')+'\n\n'+(window._pv.body||'')); }
function zoomPv(d){
  pvZoom=Math.max(0.85, Math.min(2.2, Math.round((pvZoom + d*0.12)*100)/100));
  var pv=document.querySelector('.pv'); if(pv) pv.style.setProperty('--z', pvZoom);
  try{ api.save_pref('pv_zoom', pvZoom); }catch(_){}
}

async function doSearch(){
  askActive=false;
  var q=$('q').value;
  $('qclear').style.display = q?'block':'none';
  var r=await api.search(q, filters());
  results=(r&&r.results)||[]; sel=-1; renderResults(); clearPreview();
  $('stat2').textContent = results.length+' results';
}
function liveSearch(){ clearTimeout(debounce); debounce=setTimeout(doSearch,130); }

function pvAnswer(inner){ $('preview').innerHTML='<div class="pv"><div class="ans-card">'+inner+'</div></div>'; }

async function doAsk(){
  var q=$('q').value.trim();
  if(!q){
    pvAnswer('<div class="ans-head">💬 Ask your email</div>'
      +'<div class="ans-hint">Type a question in the search box (for example "who at acme.co did we talk to about architecture?") and press Ask, or Ctrl+Enter.</div>');
    $('q').focus(); return;
  }
  var mlabel = $('model').options[$('model').selectedIndex] ? $('model').options[$('model').selectedIndex].text : model;
  var warm = !!askedModels[model] || model===lastPrewarmed;
  var sub = warm ? '' : ' <span class="ans-sub">('+esc(mlabel)+' · loading the model for the first time, ~20-40s)</span>';
  pvAnswer('<div class="ans-head"><span class="spin"></span> Thinking…'+sub+'</div>');
  try{
    var r=await api.ask(q, model, filters());
    if(!r || r.error){
      pvAnswer('<div class="ans-head">⚠️ Error</div><div class="ans-hint">'+esc((r&&r.error)||'No answer returned')+'</div>');
      return;
    }
    askedModels[model]=true;
    var ansHtml=esc(r.answer).replace(/\[(\d+)\]/g,'<span class="cite" data-c="$1">[$1]</span>');
    // Prefer the model the SERVER actually ran (echoed by Ollama) over the
    // dropdown label, so the header is ground-truth proof of which model answered.
    lastAns={ html:ansHtml, text:(r.answer||''), model:(r.model||mlabel), sources:(r.sources||[]) };
    $('ansBtn').style.display='';
    showAnswer();   // right pane = answer; list = its sources (citations stay valid)
  }catch(e){
    pvAnswer('<div class="ans-head">⚠️ Error</div><div class="ans-hint">'+esc(''+e)+'</div>');
  }
}

/* Show the last answer in the RIGHT pane and restore its source list on the left
   (so the [n] citations stay valid). The list stays usable; a long answer scrolls
   inside this pane instead of pushing everything down. */
function showAnswer(){
  if(!lastAns) return;
  askActive=true;
  results=(lastAns.sources||[]).slice(); sel=-1; renderResults();
  $('stat2').textContent=results.length+' sources';
  $('preview').innerHTML =
    '<div class="pv" style="--z:'+pvZoom+'">'
    +'<div class="pv-actions">'
      +'<button class="btn" onclick="copyAns()">⧉ Copy answer</button>'
      +'<span class="pv-tip">Click a [number] to jump to the source email</span>'
      +'<span class="zoomgrp"><button class="btn zoom" onclick="zoomPv(-1)" title="Smaller text">A−</button>'
      +'<button class="btn zoom" onclick="zoomPv(1)" title="Larger text">A+</button></span>'
    +'</div>'
    +'<div class="ans-card">'
      +'<div class="ans-head">💬 Answer <span class="ans-sub">'+esc(lastAns.model||'')+'</span></div>'
      +'<div class="ans-body" dir="auto">'+lastAns.html+'</div>'
    +'</div></div>';
  Array.prototype.forEach.call($('preview').querySelectorAll('.cite'), function(c){
    c.addEventListener('click', function(){ var n=+c.dataset.c-1; if(n>=0&&n<results.length) select(n); });
  });
}
function copyAns(){ if(lastAns) api.copy(lastAns.text||''); }

/* ---- refresh + status polling (COM lives in the indexer child; we read the
   status file. Use last_sync(ts) to tell THIS run from a stale prior status). ---- */
async function doRefresh(){
  refreshPreTs = (lastStatus && lastStatus.last_sync) || 0;
  refreshPreMsgs = (lastStatus && lastStatus.messages) || 0;
  setStat('Looking for new mail…','busy');
  var r=await api.refresh();
  if(r && r.ok===false){ setStat('Refresh error: '+esc(r.error||''),'err'); return; }
  clearTimeout(refreshTimer); pollRefresh(0, Date.now());
}
function pollRefresh(n, startMs){
  refreshTimer=setTimeout(async function(){
    var s; try{ s=await api.poll_status(); }catch(e){ if(n<240) pollRefresh(n+1,startMs); return; }
    lastStatus=s;
    var fresh = (s.last_sync||0) > refreshPreTs;     // belongs to THIS run
    var elapsed = Date.now()-startMs;
    if(fresh && s.state==='error'){
      var com=(s.error||'').toLowerCase();
      if(com.indexOf('com_error')>=0||com.indexOf('server execution')>=0||com.indexOf('-2146959355')>=0||com.indexOf('rejected by callee')>=0)
        setStat('Refresh failed - open Outlook and press ⟳ again','err');
      else setStat('Refresh failed: '+esc(s.error||''),'err');
      return;
    }
    if(fresh && s.state==='done'){
      var added=(s.messages||0)-refreshPreMsgs;
      setBase(s);
      setStat(baseStat+' · '+(added>0?('added '+added+' items ✓'):'nothing new ✓'),'ok');
      $('stat2').textContent='';
      if(added>0) quietRerun();     // surface the new mail in the visible list
      return;
    }
    var p=s.progress||{};
    var msg;
    if(s.state==='embedding') msg='Building smart search… '+((p.embedded||0))+(p.total?('/'+p.total):'');
    else if(s.state==='indexing') msg='Indexing… '+esc(p.folder||'')+(p.total?(' · '+p.total):'');
    else if(!fresh) msg='Starting a refresh…';
    else msg='Indexing…';
    setStat(msg,'busy');
    if(elapsed < 600000) pollRefresh(n+1, startMs);    // poll up to 10 min
    else setStat('The refresh continues in the background…','busy');
  }, n===0?1200:2500);
}

/* ---- auto-refresh while the window is open (quiet) ----
   Keeps the index current on its own: an incremental refresh every ~2 min and
   whenever the window regains focus, debounced. When new mail lands it updates
   the counts and refreshes the visible list WITHOUT disturbing the selection or
   scroll, and never while you're reading an "ask" answer. */
function maybeAutoRefresh(){
  var t=Date.now();
  if(t - lastRefreshKick < 90000) return;                 // debounce
  var st=(lastStatus && lastStatus.state) || '';
  if(st==='starting'||st==='indexing'||st==='embedding') return;  // one already running
  lastRefreshKick=t;
  var preTs=(lastStatus && lastStatus.last_sync) || 0;
  Promise.resolve(api.refresh()).then(function(r){
    if(r && r.ok===false) return;
    pollAuto(0, t, preTs);
  }).catch(function(){});
}
function pollAuto(n, startMs, preTs){
  setTimeout(function(){
    Promise.resolve(api.poll_status()).then(function(s){
      if(!s) return;
      var fresh=(s.last_sync||0) > preTs;
      if(fresh && s.state==='done'){
        var grew=(s.messages||0) !== ((lastStatus&&lastStatus.messages)||0);
        lastStatus=s; setBase(s); setStat(baseStat,'ok');
        if(grew) quietRerun();
        return;
      }
      if(fresh && s.state==='error'){ lastStatus=s; return; }  // stay quiet on auto errors
      if(Date.now()-startMs < 180000) pollAuto(n+1, startMs, preTs);
    }).catch(function(){});
  }, n===0?1500:3000);
}
function quietRerun(){
  if(askActive) return;
  if($('q').value.trim()==='') return;
  var prevId=(sel>=0 && results[sel]) ? results[sel].id : null;
  var sc=$('results').scrollTop;
  Promise.resolve(api.search($('q').value, filters())).then(function(r){
    if(!r || r.error) return;
    results=r.results||[];
    var ni=-1; for(var k=0;k<results.length;k++){ if(prevId!=null && results[k].id===prevId){ ni=k; break; } }
    sel=ni; renderResults();
    $('results').scrollTop=sc;
    $('stat2').textContent=results.length+' results';
  }).catch(function(){});
}

function key(e){
  var t=e.target, tag=(t&&t.tagName)||'', inQ=(t===$('q'));
  var inField=(tag==='INPUT'||tag==='SELECT'||tag==='TEXTAREA'||(t&&t.isContentEditable));
  if(e.ctrlKey && e.key==='Enter'){ e.preventDefault(); doAsk(); return; }
  if(inField && !inQ) return;        // let date/folder/checkbox use their native keys
  if(e.key==='ArrowDown'){ e.preventDefault(); select(Math.min(sel+1,results.length-1)); }
  else if(e.key==='ArrowUp'){ e.preventDefault(); select(Math.max(sel-1,0)); }
  else if(e.key==='Enter'){ if(sel>=0) openOut(sel); }
  else if(e.key==='Escape'){ if($('q').value){ $('q').value=''; doSearch(); $('q').focus(); } else api.close_window(); }
}

/* ---- draggable splitter (list width). JS is the single source of truth for
   the width (no CSS min/max-width to fight); persisted via the Python bridge. ---- */
function initSplitter(prefs){
  var sp=$('splitter'), res=$('results'), main=$('main'), dragging=false;
  var saved=parseInt((prefs&&prefs.list_w)||'',10);
  if(saved>=260) res.style.width=saved+'px';
  function clampW(w, rectW){ return Math.max(260, Math.min(rectW-340, w)); }
  sp.addEventListener('mousedown', function(e){ dragging=true; sp.classList.add('drag');
    document.body.style.userSelect='none'; document.body.style.cursor='col-resize'; e.preventDefault(); });
  document.addEventListener('mousemove', function(e){
    if(!dragging) return;
    var rect=main.getBoundingClientRect();
    res.style.width=clampW(e.clientX-rect.left, rect.width)+'px';   /* list on the LEFT */
  });
  document.addEventListener('mouseup', function(){
    if(!dragging) return; dragging=false; sp.classList.remove('drag');
    document.body.style.userSelect=''; document.body.style.cursor='';
    try{ api.save_pref('list_w', parseInt(res.offsetWidth,10)); }catch(_){}
  });
}

function buildAccountSeg(accounts, current){
  var seg=$('accSeg');
  var opts=[['','All']].concat((accounts||[]).map(function(a){ return [a, acctLabel(a)]; }));
  seg.innerHTML=opts.map(function(o){
    return '<button data-a="'+esc(o[0])+'"'+(o[0]===current?' class="on"':'')+'>'+esc(o[1])+'</button>';
  }).join('');
  Array.prototype.forEach.call(seg.querySelectorAll('button'), function(b){
    b.addEventListener('click', function(){
      account=b.dataset.a;
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function(x){ x.classList.toggle('on', x===b); });
      try{ api.save_pref('last_account', account); }catch(_){}
      doSearch();
    });
  });
}

window.addEventListener('pywebviewready', async function(){
  api=window.pywebview.api;
  var d=await api.get_initial();
  document.title=d.title;
  var prefs=d.prefs||{};
  pvZoom=parseFloat(prefs.pv_zoom)||1;

  var ms=$('model');
  (d.models||[]).forEach(function(m){ var o=document.createElement('option'); o.value=m[1]; o.textContent=m[0]; ms.appendChild(o); });
  var want=prefs.last_model||d.answer_model||(d.models&&d.models[0]&&d.models[0][1])||'';
  if(Array.prototype.some.call(ms.options,function(o){return o.value===want;})) ms.value=want;
  model=ms.value;
  ms.addEventListener('change', function(){
    model=ms.value;
    try{ api.save_pref('last_model', model); }catch(_){}
    if(model===lastPrewarmed) return;       // de-dup: don't reload the same model
    lastPrewarmed=model;
    api.prewarm(model);
    var my=setStat('Warming up '+esc(ms.options[ms.selectedIndex].text)+'… (so the first answer is fast)','busy');
    clearTimeout(prewarmTimer);
    prewarmTimer=setTimeout(function(){ if(statusSeq===my) setStat(baseStat,'ok'); }, 6000);
  });

  account = prefs.last_account || '';
  buildAccountSeg(d.accounts, account);
  (d.folders||[]).forEach(function(f){ var o=document.createElement('option'); o.value=f;o.textContent=f; $('fFolder').appendChild(o); });

  var s=d.status||{}; lastStatus=s; refreshPreTs=s.last_sync||0;
  setBase(s); setStat(baseStat, s.state==='error'?'warn':'ok');

  $('q').addEventListener('input', liveSearch);
  $('qclear').addEventListener('click', function(){ $('q').value=''; doSearch(); $('q').focus(); });
  ['fFolder','fAfter','fBefore','fAttach'].forEach(function(id){ $(id).addEventListener('change',doSearch); });
  $('askBtn').addEventListener('click', doAsk);
  $('ansBtn').addEventListener('click', showAnswer);
  $('refresh').addEventListener('click', doRefresh);
  document.addEventListener('keydown', key);
  initSplitter(prefs);
  // Auto-refresh: the app already kicked an index refresh on open, so start the
  // debounce clock now and let the timer / focus events keep it current.
  lastRefreshKick = Date.now();
  setInterval(maybeAutoRefresh, 120000);                  // every ~2 min
  window.addEventListener('focus', maybeAutoRefresh);
  document.addEventListener('visibilitychange', function(){ if(!document.hidden) maybeAutoRefresh(); });
  if(account) doSearch();
  $('q').focus();
});
</script>
</body></html>"""


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("email_search: missing payload path\n")
        sys.exit(2)
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        sys.stderr.write("email_search: bad payload: %r\n" % (e,))
        sys.exit(2)

    api = SearchApi(payload)
    try:
        webview.create_window(payload.get("title", "Email search"),
                              html=HTML, js_api=api,
                              width=1180, height=780, min_size=(820, 560))

        def _watchdog():
            time.sleep(30)
            if not api._ready:
                api._init_failed = True
                try:
                    for w in list(webview.windows):
                        w.destroy()
                except Exception:
                    pass
        threading.Thread(target=_watchdog, daemon=True).start()
        import ui_kit as uk  # Lia window icon
        uk.webview_start(webview)
    except Exception as e:
        sys.stderr.write("email_search: failed: %r\n" % (e,))
        sys.exit(3)
    if api._init_failed:
        sys.exit(3)


if __name__ == "__main__":
    main()
