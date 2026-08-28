# -*- coding: utf-8 -*-
"""Lia — local email index (data + query layer).

100% local. The single source of truth for the email-search feature, shared by:
  - emailsearch_indexer.py  (the Outlook COM worker — WRITES)
  - email_search.py         (the pywebview search window — READS)
  - lia.py          (tray wiring / first-run kickoff)

Deliberately lightweight: stdlib sqlite3 (FTS5) + numpy + requests only. It does
NOT import pywebview or win32com — those live in the subprocess scripts so this
module stays importable everywhere (incl. the test suite) with no heavy deps.

Storage (plaintext, per the user's choice — same exposure class as Outlook's own
.ost; user-profile ACL):
    %APPDATA%/Lia/email/
        messages.db        metadata + FTS5 full-text (WAL)
        vectors.db         chunk embeddings (1024-d float16 BLOBs)
        index_state.json   per-folder watermarks + schema/model version

Keyword search is fully functional from messages.db alone; semantic / "ask"
(added alongside, bge-m3 + local LLM) degrade off gracefully when unavailable.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _appdata_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Lia", "email")

EMAIL_DIR = _appdata_dir()
MESSAGES_DB = os.path.join(EMAIL_DIR, "messages.db")
VECTORS_DB = os.path.join(EMAIL_DIR, "vectors.db")
STATE_FILE = os.path.join(EMAIL_DIR, "index_state.json")


def ensure_dir():
    os.makedirs(EMAIL_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# FTS column layout (order matters for bm25() weights + snippet() index)
# ---------------------------------------------------------------------------
# 0 sender_name  1 sender_email  2 to_recips  3 cc_recips  4 subject  5 body_clean
_FTS_COLS = ["sender_name", "sender_email", "to_recips", "cc_recips",
             "subject", "body_clean"]
_BM25_WEIGHTS = (8.0, 8.0, 4.0, 4.0, 10.0, 1.0)   # subject/sender weighted high
_BODY_COL_IDX = 5


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def connect(path=MESSAGES_DB, readonly=False, timeout=30.0):
    """Open a WAL SQLite connection. Readers pass readonly=True (a separate
    connection from the single writer — WAL lets them run concurrently)."""
    ensure_dir()
    if readonly and os.path.exists(path):
        uri = "file:%s?mode=ro" % path.replace("?", "%3f")
        conn = sqlite3.connect(uri, uri=True, timeout=timeout,
                               check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError:
        pass  # read-only mode can't set some pragmas — fine
    return conn


def init_schema(conn):
    """Create the messages table + external-content FTS5 mirror + the triggers
    that keep them in sync. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY,
            entry_id      TEXT NOT NULL,
            store_id      TEXT,
            account       TEXT,
            folder        TEXT,
            kind          TEXT NOT NULL DEFAULT 'email',
            sender_name   TEXT,
            sender_email  TEXT,
            to_recips     TEXT,
            cc_recips     TEXT,
            subject       TEXT,
            body_clean    TEXT,
            received_ts   INTEGER,
            has_attach    INTEGER NOT NULL DEFAULT 0,
            unread        INTEGER NOT NULL DEFAULT 0,
            content_hash  TEXT,
            indexed_ts    INTEGER,
            embedded      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(entry_id, store_id)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_received ON messages(received_ts);
        CREATE INDEX IF NOT EXISTS idx_msg_folder   ON messages(account, folder, received_ts);
        CREATE INDEX IF NOT EXISTS idx_msg_attach   ON messages(has_attach);
        CREATE INDEX IF NOT EXISTS idx_msg_embedded ON messages(embedded);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            sender_name, sender_email, to_recips, cc_recips, subject, body_clean,
            content='messages', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3'
        );

        -- Keep the external-content FTS index in sync (standard SQLite pattern).
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, sender_name, sender_email, to_recips,
                                   cc_recips, subject, body_clean)
          VALUES (new.id, new.sender_name, new.sender_email, new.to_recips,
                  new.cc_recips, new.subject, new.body_clean);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, sender_name, sender_email,
                                   to_recips, cc_recips, subject, body_clean)
          VALUES ('delete', old.id, old.sender_name, old.sender_email,
                  old.to_recips, old.cc_recips, old.subject, old.body_clean);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, sender_name, sender_email,
                                   to_recips, cc_recips, subject, body_clean)
          VALUES ('delete', old.id, old.sender_name, old.sender_email,
                  old.to_recips, old.cc_recips, old.subject, old.body_clean);
          INSERT INTO messages_fts(rowid, sender_name, sender_email, to_recips,
                                   cc_recips, subject, body_clean)
          VALUES (new.id, new.sender_name, new.sender_email, new.to_recips,
                  new.cc_recips, new.subject, new.body_clean);
        END;

        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    # migrations for pre-existing DBs (idempotent). New columns default 0/'email',
    # so a fresh sync (see sync_unread) backfills the real values.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    if "kind" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'email'")
    if "unread" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN unread INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


def init_vectors(conn):
    """Vector store schema (separate DB to keep BLOB churn out of messages.db)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id      INTEGER PRIMARY KEY,
            msg_id  INTEGER NOT NULL,
            ord     INTEGER NOT NULL,
            text    TEXT,
            vec     BLOB           -- 1024 x float16
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_msg ON chunks(msg_id);
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Body cleaning — strip quoted reply chains, safelinks, collapse whitespace.
# Validated: avg body ~7,040 -> ~1,366 chars; smaller index AND better relevance.
# ---------------------------------------------------------------------------
# Earliest match of any of these starts the quoted history → cut there.
_REPLY_BOUNDARIES = [
    r"^-{2,}\s*Original Message\s*-{2,}",          # -----Original Message-----
    r"^_{5,}\s*$",                                  # Outlook's ____ separator line
    r"^From:\s.+",                                  # Outlook EN quoted header
    r"^מאת:\s.+",                                   # Outlook HE quoted header (From)
    r"^On\s.+\bwrote:\s*$",                         # Gmail/Apple "On ... wrote:"
    r"^בתאריך\s.+\bכתב",                            # Hebrew "On ... wrote"
    r"^Sent from my\b",                             # mobile signatures
    r"^נשלח מ-",                                    # Hebrew "Sent from"
]
_REPLY_RE = re.compile("|".join("(?:%s)" % p for p in _REPLY_BOUNDARIES),
                       re.IGNORECASE | re.MULTILINE)
# Microsoft SafeLinks wrapper — unwrap to the real URL (or drop the noise).
_SAFELINK_RE = re.compile(
    r"https?://\S*safelinks\.protection\.outlook\.com/\S*?[?&]url=([^&\s>]+)\S*",
    re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t]+")
_MULTINL_RE = re.compile(r"\n{3,}")


def _unwrap_safelink(m):
    import urllib.parse
    try:
        return urllib.parse.unquote(m.group(1))
    except Exception:
        return ""


def clean_body(text):
    """Return the message's own (top) text: quoted history removed, SafeLinks
    unwrapped, whitespace collapsed. Never raises — returns "" on junk."""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # Cut at the first quoted-reply boundary (keep only this message's new text).
    m = _REPLY_RE.search(s)
    if m and m.start() > 0:
        s = s[:m.start()]
    # Drop long runs of '>' quoted lines that precede a boundary we didn't catch.
    s = "\n".join(ln for ln in s.split("\n") if not ln.lstrip().startswith(">"))
    s = _SAFELINK_RE.sub(_unwrap_safelink, s)
    s = _WS_RE.sub(" ", s)
    s = _MULTINL_RE.sub("\n\n", s)
    return s.strip()


def content_hash(subject, body_clean):
    import hashlib
    h = hashlib.sha1()
    h.update((subject or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((body_clean or "").encode("utf-8", "replace"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Query parser  —  free text + operators -> (FTS MATCH, SQL WHERE, params)
# Never pass raw user text to MATCH: a bare dot (acme.co) is a syntax error, so
# every token is wrapped in double quotes. Operators map to column filters /
# SQL WHERE. Validated against SQLite 3.50.4 FTS5.
# ---------------------------------------------------------------------------
_FTS_OPS = {  # operator -> FTS column(s) it scopes to
    "from": ["sender_name", "sender_email"],
    "to": ["to_recips", "cc_recips"],
    "subject": ["subject"],
    "subj": ["subject"],
    "body": ["body_clean"],
}
_SQL_OPS = {"after", "since", "before", "until", "folder", "account", "has"}
_TOKEN_RE = re.compile(r'(\w+):("[^"]*"|\S+)|"([^"]*)"|(\S+)')


def _fts_quote(term):
    """Quote a term as an FTS5 string literal (escape embedded double-quotes)."""
    return '"' + term.replace('"', '""') + '"'


def _fts_term(term):
    """Like _fts_quote, but a trailing '*' becomes an FTS5 PREFIX operator —
    which must sit OUTSIDE the quotes ("פגיש"* not "פגיש*")."""
    if len(term) > 1 and term.endswith("*"):
        return _fts_quote(term[:-1]) + "*"
    return _fts_quote(term)


def _to_epoch(datestr):
    """Parse YYYY/MM/DD or YYYY-MM-DD (and a few relatives) -> epoch seconds."""
    s = (datestr or "").strip().strip('"').lower()
    rel = {"today": 0, "yesterday": 1}
    if s in rel:
        return int(time.time()) - rel[s] * 86400
    m = re.match(r"(\d{1,4})d$", s)  # "7d" = 7 days ago
    if m:
        return int(time.time()) - int(m.group(1)) * 86400
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            import datetime
            return int(datetime.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


def parse_query(query):
    """-> (match_str_or_None, where_sql, params). match_str None => no full-text
    constraint (pure filter/browse). where_sql is '' or 'AND ...'."""
    query = (query or "").strip()
    fts_parts = []
    where = []
    params = []
    if not query:
        return None, "", []

    for m in _TOKEN_RE.finditer(query):
        op, opval, phrase, bare = m.group(1), m.group(2), m.group(3), m.group(4)
        if op is not None:
            op = op.lower()
            val = opval.strip('"') if opval else ""
            if op in _FTS_OPS:
                cols = _FTS_OPS[op]
                colset = "{%s}" % " ".join(cols) if len(cols) > 1 else cols[0]
                fts_parts.append("%s : %s" % (colset, _fts_quote(val)))
            elif op == "has":
                if val.lower() in ("attachment", "attach", "att", "file"):
                    where.append("m.has_attach = 1")
            elif op in ("after", "since"):
                ep = _to_epoch(val)
                if ep is not None:
                    where.append("m.received_ts >= ?"); params.append(ep)
            elif op in ("before", "until"):
                ep = _to_epoch(val)
                if ep is not None:
                    where.append("m.received_ts <= ?"); params.append(ep)
            elif op == "folder":
                where.append("LOWER(m.folder) = ?"); params.append(val.lower())
            elif op == "account":
                where.append("LOWER(m.account) LIKE ?")
                params.append("%" + val.lower() + "%")
            else:  # unknown op — treat the whole thing as a free term
                fts_parts.append(_fts_quote("%s:%s" % (op, val)))
        elif phrase is not None:
            if phrase.strip():
                fts_parts.append(_fts_quote(phrase.strip()))
        elif bare is not None:
            fts_parts.append(_fts_term(bare))

    match = " ".join(fts_parts) if fts_parts else None
    where_sql = (" AND " + " AND ".join(where)) if where else ""
    return match, where_sql, params


# ---------------------------------------------------------------------------
# Keyword search (BM25, instant)
# ---------------------------------------------------------------------------
_SELECT_COLS = (
    "m.id, m.entry_id, m.store_id, m.account, m.folder, m.kind, m.sender_name, "
    "m.sender_email, m.to_recips, m.subject, m.received_ts, m.has_attach, m.unread"
)


def keyword_search(conn, query, limit=50):
    """Return ranked result dicts for a keyword query (with operators/filters).
    Empty / filter-only queries browse by recency."""
    match, where_sql, params = parse_query(query)
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    if match:
        sql = (
            "SELECT %s, "
            "snippet(messages_fts, %d, '\x02', '\x03', ' … ', 14) AS snippet, "
            "bm25(messages_fts, %s) AS rank "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ?%s "
            "ORDER BY rank, m.received_ts DESC LIMIT ?"
        ) % (_SELECT_COLS, _BODY_COL_IDX, weights, where_sql)
        args = [match] + params + [limit]
    else:
        # Default browse (no full-text query): show recent EMAIL newest-first,
        # like an Outlook inbox. Exclude meetings here — future-dated meetings
        # (upcoming holidays, recurring events) would otherwise sort to the very
        # top and bury the recent mail. Meetings still surface via a text search
        # or an explicit Calendar-folder filter.
        kind_clause = "" if "m.folder" in where_sql else " AND m.kind = 'email'"
        sql = (
            "SELECT %s, "
            "substr(m.body_clean,1,180) AS snippet, 0.0 AS rank "
            "FROM messages m WHERE 1=1%s%s "
            "ORDER BY m.received_ts DESC LIMIT ?"
        ) % (_SELECT_COLS, where_sql, kind_clause)
        args = params + [limit]
    out = []
    for r in conn.execute(sql, args):
        out.append(_row_to_result(r))
    return out


def _row_to_result(r):
    d = dict(r)
    return {
        "id": d["id"],
        "entry_id": d["entry_id"],
        "store_id": d.get("store_id"),
        "account": d.get("account"),
        "folder": d.get("folder"),
        "kind": d.get("kind") or "email",
        "sender_name": d.get("sender_name") or "",
        "sender_email": d.get("sender_email") or "",
        "to_recips": d.get("to_recips") or "",
        "subject": d.get("subject") or "",
        "received_ts": d.get("received_ts") or 0,
        "has_attach": bool(d.get("has_attach")),
        "unread": int(d.get("unread") or 0),
        "snippet": d.get("snippet") or "",
        "rank": d.get("rank") or 0.0,
    }


def get_message(conn, msg_id):
    """Full row incl. body (for the preview pane)."""
    r = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return dict(r) if r else None


def stats(conn):
    """Index counts for the UI 'last synced / N emails' banner."""
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        emb = conn.execute("SELECT COUNT(*) FROM messages WHERE embedded=1").fetchone()[0]
        return {"messages": n, "embedded": emb}
    except sqlite3.Error:
        return {"messages": 0, "embedded": 0}


# ===========================================================================
# Semantic layer — bge-m3 embeddings (local, via Ollama) + hybrid + "ask"
# bge-m3: 1024-d, L2-normalised (cosine == dot), cross-lingual (a Hebrew query
# finds an English email). Ollama has a ~2.2s fixed per-request floor, so we
# ALWAYS batch. Long emails are chunked (whole-body embedding dilutes signal).
# ===========================================================================
DEFAULT_OLLAMA = "http://localhost:11434/v1/chat/completions"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024
CHUNK_CHARS = 800
CHUNK_OVERLAP = 120


def _ollama_root(base_url):
    h = (base_url or DEFAULT_OLLAMA).rstrip("/")
    h = re.sub(r"/v1/chat/completions$", "", h)
    h = re.sub(r"/v1$", "", h)
    return h


def chunk_text(subject, body, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Split an email into overlapping windows for embedding. The subject is
    prepended to the first chunk (it carries strong signal). Short emails -> 1."""
    base = ((subject or "").strip() + "\n" + (body or "").strip()).strip()
    if not base:
        return []
    if len(base) <= size:
        return [base]
    out, i, n = [], 0, len(base)
    while i < n:
        end = min(i + size, n)
        # try not to cut mid-word
        if end < n:
            sp = base.rfind(" ", i + size - overlap, end)
            if sp > i:
                end = sp
        out.append(base[i:end].strip())
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return [c for c in out if c]


def embed_texts(texts, base_url=DEFAULT_OLLAMA, model=EMBED_MODEL, batch=64,
                timeout=180):
    """Embed a list of strings -> (N, 1024) float32 ndarray. Batches to amortise
    Ollama's fixed per-request latency floor. Raises on transport error."""
    import numpy as np
    import requests
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    url = _ollama_root(base_url) + "/api/embed"
    vecs = []
    sess = requests.Session()
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        r = sess.post(url, json={"model": model, "input": part, "truncate": True},
                      timeout=(10, timeout))
        r.raise_for_status()
        embs = r.json().get("embeddings") or []
        vecs.extend(embs)
    arr = np.asarray(vecs, dtype=np.float32)
    return arr


def _f16_blob(vec):
    import numpy as np
    return np.asarray(vec, dtype=np.float16).tobytes()


def _blob_f32(blob):
    import numpy as np
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def store_chunks(vconn, msg_id, chunks, vecs):
    """Replace a message's chunk vectors (float16 BLOBs)."""
    vconn.execute("DELETE FROM chunks WHERE msg_id = ?", (msg_id,))
    vconn.executemany(
        "INSERT INTO chunks(msg_id, ord, text, vec) VALUES(?,?,?,?)",
        [(msg_id, k, chunks[k], _f16_blob(vecs[k])) for k in range(len(chunks))])
    vconn.commit()


# In-process cache of the full vector matrix (rebuilt when the row count changes).
_VEC_CACHE = {"n": -1, "ids": None, "mat": None}


def load_vectors(vconn, force=False):
    """Return (msg_ids ndarray[int], mat ndarray[N,1024] float32) for all chunks.
    Cached; rebuilt when the chunk count changes."""
    import numpy as np
    n = vconn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not force and _VEC_CACHE["n"] == n and _VEC_CACHE["mat"] is not None:
        return _VEC_CACHE["ids"], _VEC_CACHE["mat"]
    ids = np.empty(n, dtype=np.int64)
    mat = np.empty((n, EMBED_DIM), dtype=np.float32)
    for k, row in enumerate(vconn.execute("SELECT msg_id, vec FROM chunks")):
        ids[k] = row[0]
        mat[k] = _blob_f32(row[1])
    _VEC_CACHE.update(n=n, ids=ids, mat=mat)
    return ids, mat


def semantic_search(conn, vconn, query, base_url=DEFAULT_OLLAMA, limit=50):
    """Embed the query, cosine vs all chunk vectors, best-chunk-per-message,
    return ranked result dicts. [] if no vectors / embedder unavailable."""
    import numpy as np
    ids, mat = load_vectors(vconn)
    if mat is None or len(ids) == 0:
        return []
    qv = embed_texts([query], base_url)[0]
    sims = mat @ qv  # both L2-normalised -> cosine
    best = {}
    for idx in np.argsort(-sims):
        mid = int(ids[idx])
        if mid not in best:
            best[mid] = float(sims[idx])
            if len(best) >= limit:
                break
    ranked = sorted(best.items(), key=lambda x: -x[1])
    return _fetch_results(conn, ranked)


def _fetch_results(conn, mid_score_pairs):
    """Fetch message rows for (msg_id, score) pairs, preserving order, attaching
    score + a body snippet."""
    out = []
    for mid, score in mid_score_pairs:
        r = conn.execute(
            "SELECT %s, substr(m.body_clean,1,200) AS snippet FROM messages m WHERE m.id = ?"
            % _SELECT_COLS, (mid,)).fetchone()
        if r:
            d = _row_to_result(r)
            d["score"] = round(score, 4)
            out.append(d)
    return out


def hybrid_search(conn, vconn, query, base_url=DEFAULT_OLLAMA, limit=50):
    """Merge keyword (BM25) + semantic (cosine) results with min-max normalised
    scores. Falls back to keyword-only if embeddings are unavailable."""
    kw = keyword_search(conn, query, limit=limit * 2)
    try:
        sem = semantic_search(conn, vconn, query, base_url, limit=limit * 2)
    except Exception:
        sem = []
    if not sem:
        return kw[:limit]

    def norm(vals):
        if not vals:
            return {}
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1.0
        return lambda v: (v - lo) / rng

    # BM25: lower (more negative) is better -> invert
    kw_scores = {r["id"]: -r["rank"] for r in kw}
    sem_scores = {r["id"]: r.get("score", 0.0) for r in sem}
    nk = norm(list(kw_scores.values())) if kw_scores else (lambda v: 0)
    ns = norm(list(sem_scores.values())) if sem_scores else (lambda v: 0)
    merged = {}
    rowmap = {r["id"]: r for r in kw}
    for r in sem:
        rowmap.setdefault(r["id"], r)
    for mid, row in rowmap.items():
        s = 0.6 * (ns(sem_scores[mid]) if mid in sem_scores else 0.0) \
            + 0.4 * (nk(kw_scores[mid]) if mid in kw_scores else 0.0)
        row = dict(row)
        row["score"] = round(s, 4)
        merged[mid] = row
    ranked = sorted(merged.values(), key=lambda r: -r["score"])
    return ranked[:limit]


# ---- "Ask your email" (RAG) ------------------------------------------------
def _llm_chat(system_prompt, user_content, base_url, model, api_key="ollama",
              timeout=180, return_model=False):
    """One chat-completion. Local Ollama (/api/chat, think off, num_ctx fit ~1.9
    Hebrew chars/token) or a cloud OpenAI-compatible endpoint. Mirrors the app's
    summarize() shaping so behaviour is consistent. With return_model=True returns
    (text, model_the_server_actually_used) — the server ECHOES the model it ran,
    so callers can prove which model produced the answer."""
    import re as _re
    import requests
    u = (base_url or "").lower()
    is_local = ("localhost" in u) or ("127.0.0.1" in u) or (":11434" in u)
    sess = requests.Session()
    if is_local:
        url = _ollama_root(base_url) + "/api/chat"
        num_ctx = max(8192, min(_SUMMARY_CTX_CAP,
                                int(len(user_content) / 1.9) + 4096))
        body = {"model": model, "stream": False, "think": False,
                "options": {"num_ctx": num_ctx, "temperature": 0.2},
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": user_content}]}
        r = sess.post(url, json=body, timeout=(10, timeout)); r.raise_for_status()
        jr = r.json()
        out = ((jr.get("message") or {}).get("content") or "").strip()
        used_model = jr.get("model") or model
    else:
        ml = (model or "").lower()
        body = {"model": model, "stream": False,
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": user_content}]}
        if ml.startswith(("gpt-5", "o1", "o3", "o4")):
            body["max_completion_tokens"] = 4000
        else:
            body["max_tokens"] = 4000; body["temperature"] = 0.2
        r = sess.post(base_url, json=body,
                      headers={"Authorization": "Bearer " + api_key,
                               "Content-Type": "application/json"},
                      timeout=(10, timeout)); r.raise_for_status()
        jr = r.json()
        out = (jr["choices"][0]["message"]["content"] or "").strip()
        used_model = jr.get("model") or model
    out = _re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>\s*", "", out).strip()
    return (out, used_model) if return_model else out


_SUMMARY_CTX_CAP = 32768

_ASK_PROMPT = (
    "You answer questions about the user's emails. You are given the user's "
    "QUESTION, a CORRESPONDENT MAP (their most frequent external contacts by "
    "organization, across the WHOLE mailbox), and a numbered list of RELEVANT "
    "EMAILS (sender, date, subject, excerpt). Answer in the QUESTION's language, "
    "concisely and factually. For broad/overview questions such as 'who are my "
    "clients' or 'who do I work with', SYNTHESIZE from BOTH the correspondent map "
    "and the emails — don't rely only on the handful of emails. For specific "
    "questions, rely on the emails and CITE them inline as [1], [2], … matching "
    "their numbers. If the evidence doesn't contain the answer, say so. Do not "
    "invent senders, addresses, or facts."
)

# Org/vendor noise filtered out of the correspondent map (own domain, cloud
# providers, SaaS tools, and notification/newsletter senders — not real contacts).
_DIGEST_NOISE = (
    # (add your own organization's domains here)
    "amazon", "aws", "microsoft", "sharepoint",
    "google", "gmail", "outlook", "linkedin", "atlassian", "slack", "zoom.us",
    "gitlab", "qlik", "hava.io", "sonatype", "backbox", "akamai", "openai",
    "docusign", "clickup", "postman", "smartbear", "twilio", "apple.com",
    "pearson", "blackberry", "calendly", "no-reply", "noreply", "notifications",
    "mailer", "newsletter", "mail.", "email.", "marketing.", "info.",
)


# Optional domain -> organization-name labels for the correspondent map. A
# local model cannot reliably map a domain to the right organization (Hebrew
# public-sector domains especially), so these labels keep it from mis-naming
# the people you actually work with; unknown domains fall back to the bare
# domain, which works fine.
#
# Ships EMPTY on purpose - the list is inherently personal. To add your own,
# create %APPDATA%\Lia\email\known_orgs.json:
#     {"example.gov": "Ministry of Examples", "acme.co": "ACME Ltd"}
def _load_known_orgs():
    import json
    try:
        with open(os.path.join(EMAIL_DIR, "known_orgs.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k).lower(): str(v) for k, v in data.items()} \
            if isinstance(data, dict) else {}
    except Exception:
        return {}


_KNOWN_ORGS = _load_known_orgs()


def _digest_domain(addr):
    m = re.search(r"@([a-z0-9.\-]+)", (addr or "").lower())
    return m.group(1) if m else ""


def correspondent_digest(conn, limit=35):
    """A compact 'who do you work with' map: the top external sender-organization
    domains by inbound volume (vendor/notification noise removed), each with a
    representative sender name. This gives the LLM the corpus-wide relationship
    picture that top-K semantic retrieval alone can't surface — so overview
    questions ('who are my clients') get a complete answer instead of whatever a
    dozen keyword-matched emails happen to mention."""
    import collections
    counts = collections.Counter()
    names = {}
    try:
        cur = conn.execute("SELECT sender_email, sender_name FROM messages WHERE folder='Inbox'")
    except sqlite3.Error:
        return ""
    for se, sn in cur:
        d = _digest_domain(se)
        if not d or any(x in d for x in _DIGEST_NOISE):
            continue
        counts[d] += 1
        if d not in names and sn:
            names[d] = sn
    if not counts:
        return ""
    out = []
    for d, n in counts.most_common(limit):
        org = _KNOWN_ORGS.get(d, "")
        label = ("%s — %s" % (org, d)) if org else d
        out.append("%4d  %-44s  %s" % (n, label, (names.get(d, "") or "")[:24]))
    return "\n".join(out)


def ask(conn, vconn, question, base_url=DEFAULT_OLLAMA, model="gemma4:31b-it-qat",
        api_key="ollama", top_k=20):
    """Retrieve the most relevant emails (hybrid) and have a local LLM answer
    WITH citations. A corpus-wide CORRESPONDENT MAP is included so broad
    'who do I work with' questions get a complete answer, not just whatever the
    top-K retrieval matched. Returns {'answer': str, 'sources': [result dicts]}."""
    import datetime
    hits = hybrid_search(conn, vconn, question, base_url, limit=top_k)
    digest = correspondent_digest(conn)
    if not hits and not digest:
        return {"answer": "לא נמצאו אימיילים רלוונטיים לשאלה.", "sources": []}
    blocks = []
    for i, h in enumerate(hits, 1):
        full = get_message(conn, h["id"]) or {}
        body = (full.get("body_clean") or h.get("snippet") or "")[:1200]
        dt = datetime.datetime.fromtimestamp(h["received_ts"]).strftime("%Y-%m-%d") \
            if h.get("received_ts") else ""
        blocks.append(
            "[%d] From: %s <%s> | To: %s | Date: %s\nSubject: %s\n%s"
            % (i, h["sender_name"], h["sender_email"], h["to_recips"][:120],
               dt, h["subject"], body))
    user_content = (
        "CORRESPONDENT MAP (inbound count · organization domain · a sample "
        "sender — across the whole mailbox; use for overview questions):\n%s\n\n"
        "QUESTION:\n%s\n\nRELEVANT EMAILS:\n%s"
        % (digest or "(unavailable)", question, "\n\n".join(blocks)))
    answer, used_model = _llm_chat(_ASK_PROMPT, user_content, base_url, model,
                                   api_key, return_model=True)
    return {"answer": answer, "sources": hits, "model": used_model}
