# -*- coding: utf-8 -*-
"""The ear: the meeting being recorded, readable by Claude on this PC (Claude link, 2026-11-11).

A small HTTP server on 127.0.0.1 (a free port), in a thread of its own, GET only, JSON only. It answers with what the
app's `providers` return: {"/v1/meeting": fn(query) -> dict, ...}. Nothing here knows LiaApp.

Who may read: a request must carry the ear's token (`Authorization: Bearer ...`), must name this server in `Host`, and
must not come from a web page (any `Origin` is refused). The token is made at start and lives in `<data_dir>/ears.json`
as a DPAPI blob beside the port, so only a process of the same Windows user finds and reads the ear. The file is
removed at stop. The bridge Claude starts (ears_bridge.py, kept in the Lia Lite repo, one file for both apps) reads
that file.

Never in the way of a recording: a provider that raises is a 503, and the log gets numbers only (no text, no names).
The same module as Lia Lite's lialite/ears.py, with this app's logger and secret store.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets as pysecrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("Lia")

FILE_NAME = "ears.json"
HOST = "127.0.0.1"


def discovery_path(data_dir):
    return os.path.join(data_dir, FILE_NAME)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Ear:
    """start() -> True when the ear listens and its discovery file is written; stop() closes both."""

    def __init__(self, app_name, version, data_dir, providers, protect=None, actions=None):
        self.app_name, self.version, self.data_dir = str(app_name), str(version), data_dir
        self.providers = dict(providers)
        # POST paths (start / stop a recording). Empty = reading only. The app decides inside each
        # action whether its own setting allows it.
        self.actions = dict(actions or {})
        if protect is None:
            import secret_store
            protect = secret_store.protect
        self._protect = protect
        self.token = ""
        self.port = 0
        self._srv = None
        self._thread = None

    # ---- the server ----
    def start(self):
        if self._srv is not None:
            return True
        token = pysecrets.token_urlsafe(32)
        blob = self._protect(token)
        if not blob or blob == token:
            log.warning("ear not started: the token could not be protected")   # never a readable token on disk
            return False
        try:
            srv = _Server((HOST, 0), self._handler())
        except OSError as e:
            log.warning("ear not started: %s", type(e).__name__)
            return False
        self._srv, self.token, self.port = srv, token, int(srv.server_address[1])
        self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True, name="lia-ear")
        self._thread.start()
        try:
            self._write({"app": self.app_name, "version": self.version, "pid": os.getpid(), "port": self.port,
                         "token_dpapi": blob})
        except OSError as e:
            log.warning("ear not started: its file was not written (%s)", type(e).__name__)
            self.stop()
            return False
        log.info("ear listening on %s:%d", HOST, self.port)
        return True

    def stop(self):
        srv, self._srv = self._srv, None
        if srv is None:
            return
        try:
            os.remove(discovery_path(self.data_dir))
        except OSError:
            pass
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass

    def _write(self, rec):
        path = discovery_path(self.data_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(rec, f)
        os.replace(tmp, path)

    # ---- one request ----
    def _handler(self):
        ear = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LiaEar"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):          # nothing of a request goes to stderr
                return

            def _send(self, status, body=None):
                data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                if data:
                    self.wfile.write(data)
                self.close_connection = True

            def _refuse(self):                   # nothing but GET, and POST to a listed action
                self._send(405)

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _refuse

            def do_POST(self):
                path = urlsplit(self.path).path
                if path not in ear.actions:
                    return self._refuse()
                status, _n = ear._answer(self, ear.actions)
                log.info("ear action %s -> %d", path[:40], status)

            def do_GET(self):
                t0 = time.monotonic()
                status, n = ear._answer(self)
                log.debug("ear %s -> %d (%d, %.2f s)", urlsplit(self.path).path[:40], status, n, time.monotonic() - t0)

        return Handler

    def _answer(self, h, table=None):
        """-> (status, a count for the log). `table`: the paths this method may call (GET: the providers)."""
        table = self.providers if table is None else table
        if h.headers.get("Origin") is not None or (h.headers.get("Host") or "") != "%s:%d" % (HOST, self.port):
            h._send(403)
            return 403, 0
        auth = h.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].encode("utf-8"), self.token.encode("utf-8")):
            h._send(401)
            return 401, 0
        parts = urlsplit(h.path)
        fn = table.get(parts.path)
        if fn is None:
            h._send(404)
            return 404, 0
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            body = fn(query)
        except Exception as e:
            log.info("ear %s failed: %s", parts.path[:40], type(e).__name__)
            h._send(503)
            return 503, 0
        try:
            h._send(200, body)
        except OSError:
            return 499, 0                       # the reader went away
        return 200, len(body.get("lines") or ()) if isinstance(body, dict) and isinstance(body.get("lines"), list) else 0


# ---- what a provider needs: the query's numbers, and a page of lines within a size ----
def q_int(query, key, default):
    try:
        return int(float(query.get(key)))
    except (TypeError, ValueError):
        return default


def q_float(query, key, default=None):
    try:
        return float(query.get(key))
    except (TypeError, ValueError):
        return default


def page(lines, after=-1, from_s=None, to_s=None, limit=2000, max_chars=12000):
    """`lines` ([{seq, t, text, ...}] in order) -> (the page, more?): after `after`, within [from_s, to_s], the oldest
    first, at most `limit` lines and about `max_chars` of text (always at least one line)."""
    out, size = [], 0
    limit, max_chars = max(1, min(int(limit), 6000)), max(200, min(int(max_chars), 400000))
    for x in lines:
        if x["seq"] <= after:
            continue
        if from_s is not None and float(x.get("e", x["t"])) < from_s:
            continue
        if to_s is not None and float(x["t"]) > to_s:
            continue
        n = len(x.get("text") or "") + len(x.get("speaker") or "") + 12
        if out and (len(out) >= limit or size + n > max_chars):
            return out, True
        out.append(x)
        size += n
    return out, False
