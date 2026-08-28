# -*- coding: utf-8 -*-
"""Lia - "Ask your meetings" index (data + query + RAG, 100% local retrieval).

Reuses the generic engine in email_index.py (sqlite FTS5 + bge-m3 via Ollama +
hybrid search + _llm_chat) against a SEPARATE database, so meetings are indexed
and searched independently of the email index and without touching it.

Shared by:
  - meetings_index.py --reindex   (the file-parsing WRITER; this file's main())
  - meetings_search.py            (the pywebview ask/search window; READS)
  - lia.py                (tray wiring / first-run kickoff)

Storage:  %APPDATA%/Lia/meetings_index/{messages.db, vectors.db,
          index_state.json, index_status.json, index_heartbeat.txt}
Source:   %APPDATA%/Lia/meetings/*_meeting.txt + *_meeting_diarized.txt

Verified against real files (2026-08-15): every line is RLM-prefixed (U+200F);
the title header is "Meeting <EM DASH> <title>" (U+2014, NOT a hyphen) and the
title may be a bare date, in which case the real title is the line under the
summary's "כותרת הדיון" section; diarized transcripts carry "[mm:ss] Speaker X:"
lines; two filename generations coexist (with/without seconds + slug).
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_index as ei

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Paths (own dir; do NOT reuse ei.EMAIL_DIR)
# ---------------------------------------------------------------------------
def _appdata_base():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


MEET_INDEX_DIR = os.path.join(_appdata_base(), "Lia", "meetings_index")
MESSAGES_DB = os.path.join(MEET_INDEX_DIR, "messages.db")
VECTORS_DB = os.path.join(MEET_INDEX_DIR, "vectors.db")
STATE_FILE = os.path.join(MEET_INDEX_DIR, "index_state.json")
STATUS_FILE = os.path.join(MEET_INDEX_DIR, "index_status.json")
HEARTBEAT_FILE = os.path.join(MEET_INDEX_DIR, "index_heartbeat.txt")
# Source transcripts live in the sibling meetings folder.
MEETINGS_SRC_DIR = os.path.join(_appdata_base(), "Lia", "meetings")


def ensure_dir():
    os.makedirs(MEET_INDEX_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Meeting-file parsing  (verified against real chunked + diarized files)
# ---------------------------------------------------------------------------
# Skip these; only *_meeting.txt and *_meeting_diarized.txt are final transcripts.
_SKIP_SUFFIXES = ("_meeting_live.txt", "_meeting_partial.txt",
                  "_meeting_audio.wav", "_meeting_audio.opus",
                  "_meeting_audio.opus.tmp", "_summary.html", ".bak.html")
# YYYY-MM-DD_HH-MM[-SS]  (seconds optional -> old May-2026 generation)
_FNAME_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})(?:-(\d{2}))?")
_EM_DASH = "—"
_DATE_TITLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")   # header title that's just a date
_SEP_CHARS = ({"═"}, {"─"})                  # ═ (thick) / ─ (thin) runs
_SPEAKER_RE = re.compile(r"Speaker\s+[A-Z0-9]+")


def _strip_marks(s):
    return s.replace("‏", "").replace("‎", "").replace("﻿", "")


def is_indexable_meeting(name):
    low = name.lower()
    if not (low.endswith("_meeting.txt") or low.endswith("_meeting_diarized.txt")):
        return False
    return not any(low.endswith(sfx) for sfx in _SKIP_SUFFIXES)


def parse_meeting_file(path):
    """Parse a meeting .txt into a row dict (or None if unparseable).
    Returns keys matching the messages schema used by _upsert()."""
    base = os.path.basename(path)
    m = _FNAME_TS_RE.match(base)
    received_ts = 0
    if m:
        y, mo, d, h, mi, s = m.groups()
        try:
            import datetime
            received_ts = int(datetime.datetime(
                int(y), int(mo), int(d), int(h), int(mi), int(s or 0)).timestamp())
        except (ValueError, OverflowError):
            received_ts = 0
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    # Python text mode already normalised CRLF -> LF; strip the per-line RLM.
    lines = [_strip_marks(l) for l in raw.splitlines()]

    # --- title ---
    title = ""
    if lines and lines[0].strip().startswith("Meeting"):
        parts = lines[0].split(_EM_DASH, 1)
        if len(parts) > 1:
            title = parts[1].strip()
    # A bare date header means "no human title" -> use the summary's discussion
    # title (the "כותרת הדיון" / "Discussion Title" header, then the next
    # non-empty line), if present. Union match (2026-08): he + en files coexist.
    if not title or _DATE_TITLE_RE.match(title):
        for i, l in enumerate(lines):
            if l.strip() in ("כותרת הדיון", "Discussion Title"):
                for j in range(i + 1, min(i + 4, len(lines))):
                    cand = lines[j].strip()
                    if cand:
                        title = cand
                        break
                break
    if not title:
        title = base

    # --- body: keep summary + transcript, drop title/meta/separator lines ---
    body_lines = []
    for l in lines[1:]:
        s = l.strip()
        if not s:
            body_lines.append("")
            continue
        cs = set(s)
        if cs <= _SEP_CHARS[0] or cs <= _SEP_CHARS[1]:
            continue                                   # ═/─ separator run
        if s.startswith("Source:") or s.startswith("Duration:"):
            continue                                   # meta line
        body_lines.append(l)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines)).strip()

    speakers = sorted(set(_SPEAKER_RE.findall(raw)))
    kind_src = "diarized" if base.lower().endswith("_meeting_diarized.txt") else "chunked"
    return {
        "entry_id": base,
        "store_id": "",
        "account": "meetings",
        "folder": kind_src,                  # 'chunked' / 'diarized' (filterable)
        "sender_name": ", ".join(speakers),  # speaker labels (diarized) or ""
        "sender_email": "",
        "to_recips": "",
        "cc_recips": "",
        "subject": title,
        "body": body,
        "received_ts": received_ts,
        "has_attach": 0,
        "unread": 0,
    }


# ---------------------------------------------------------------------------
# Writer helpers (upsert + embed) - mirror emailsearch_indexer, no COM
# ---------------------------------------------------------------------------
def _upsert(conn, raw):
    """Insert/update a meeting row. body is stored VERBATIM (no email clean_body,
    which would strip Hebrew 'מאת:'/quoted-reply markers that legitimately appear
    in transcripts). Dedup by (entry_id, store_id) + content_hash; embedded->0 on
    change. Returns (msg_id, changed)."""
    body_clean = raw["body"] or ""
    chash = ei.content_hash(raw["subject"], body_clean)
    row = conn.execute(
        "SELECT id, content_hash FROM messages WHERE entry_id=? AND store_id=?",
        (raw["entry_id"], raw["store_id"])).fetchone()
    now = int(time.time())
    if row is None:
        cur = conn.execute(
            "INSERT INTO messages(entry_id,store_id,account,folder,kind,sender_name,"
            "sender_email,to_recips,cc_recips,subject,body_clean,received_ts,"
            "has_attach,unread,content_hash,indexed_ts,embedded) "
            "VALUES(?,?,?,?, 'meeting', ?,?,?,?,?,?,?,?,?,?,?,0)",
            (raw["entry_id"], raw["store_id"], raw["account"], raw["folder"],
             raw["sender_name"], raw["sender_email"], raw["to_recips"],
             raw["cc_recips"], raw["subject"], body_clean, raw["received_ts"],
             raw["has_attach"], raw["unread"], chash, now))
        return cur.lastrowid, True
    mid, old_hash = row["id"], row["content_hash"]
    if old_hash != chash:
        conn.execute(
            "UPDATE messages SET account=?,folder=?,kind='meeting',sender_name=?,"
            "sender_email=?,to_recips=?,cc_recips=?,subject=?,body_clean=?,"
            "received_ts=?,has_attach=?,unread=?,content_hash=?,indexed_ts=?,"
            "embedded=0 WHERE id=?",
            (raw["account"], raw["folder"], raw["sender_name"], raw["sender_email"],
             raw["to_recips"], raw["cc_recips"], raw["subject"], body_clean,
             raw["received_ts"], raw["has_attach"], raw["unread"], chash, now, mid))
        return mid, True
    return mid, False


def embed_pending(conn, vconn, base_url=ei.DEFAULT_OLLAMA, batch_msgs=80,
                  on_progress=None):
    """Embed all rows WHERE embedded=0 (chunk -> bge-m3 -> vectors). Mirrors
    emailsearch_indexer.embed_pending. Returns count embedded."""
    pending = conn.execute(
        "SELECT id, subject, body_clean FROM messages WHERE embedded=0 "
        "ORDER BY received_ts DESC").fetchall()
    if not pending:
        return 0
    done = 0
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
        by_msg = {}
        for k, mid in enumerate(owners):
            by_msg.setdefault(mid, []).append(k)
        for mid, idxs in by_msg.items():
            ei.store_chunks(vconn, mid, [all_chunks[k] for k in idxs],
                            [vecs[k] for k in idxs])
            conn.execute("UPDATE messages SET embedded=1 WHERE id=?", (mid,))
        conn.commit()
        done += len(batch)
        if on_progress:
            on_progress(done, len(pending))
    return done


# ---------------------------------------------------------------------------
# Query + RAG  (retrieval always local via bge-m3; answer via chosen provider)
# ---------------------------------------------------------------------------
def open_read():
    """(conn, vconn) read-only connections, or (None, None) if no index yet."""
    if not os.path.exists(MESSAGES_DB):
        return None, None
    conn = ei.connect(MESSAGES_DB, readonly=True)
    vconn = (ei.connect(VECTORS_DB, readonly=True)
             if os.path.exists(VECTORS_DB) else None)
    return conn, vconn


def recent(conn, limit=50):
    """Newest meetings first. Own query (email_index's empty-query browse
    hard-filters kind='email' and would return NOTHING for our rows)."""
    rows = conn.execute(
        "SELECT %s, substr(m.body_clean,1,200) AS snippet "
        "FROM messages m ORDER BY m.received_ts DESC LIMIT ?"
        % ei._SELECT_COLS, (limit,))
    return [ei._row_to_result(r) for r in rows]


# [m:ss] / [mm:ss] / [h:mm:ss] transcript markers (chunked + diarized).
_TS_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
# operator tokens (after:.. / speaker:.. / subject:"..") - stripped before embedding.
_OP_TOKEN_RE = re.compile(r'\b\w+:(?:"[^"]*"|\S+)')


def _first_ts(text):
    m = _TS_RE.search(text or "")
    return m.group(1) if m else ""


def _plain_query(q):
    """Drop operator tokens so they don't pollute the embedding; keep free text."""
    return _OP_TOKEN_RE.sub(" ", q or "").strip()


def _allowed_ids(conn, where_sql, params):
    """msg ids passing the parsed WHERE filter (date/folder). None = no filter."""
    if not where_sql:
        return None
    rows = conn.execute("SELECT m.id FROM messages m WHERE 1=1 %s" % where_sql, params)
    return {r[0] for r in rows}


def _best_chunk_ts(vconn, qv, msg_id):
    """Timestamp of the message's chunk most similar to the query vector, or ''
    when the best chunk is a summary/short chunk with no [mm:ss] marker."""
    if vconn is None or qv is None:
        return ""
    import numpy as np
    best_t, best_s = "", -9.0
    for text, vb in vconn.execute("SELECT text, vec FROM chunks WHERE msg_id=?", (msg_id,)):
        try:
            s = float(np.frombuffer(vb, dtype=np.float16).astype(np.float32) @ qv)
        except Exception:
            continue
        if s > best_s:
            best_s, best_t = s, text
    return _first_ts(best_t)


def _retrieve(conn, vconn, query, embed_base_url=ei.DEFAULT_OLLAMA, limit=20):
    """Meetings hybrid retrieval. Unlike email_index.hybrid_search it applies the
    parsed date/folder filters to BOTH the keyword AND the semantic side (email's
    semantic_search ignores them), strips operator tokens from the embedded text,
    and tags each hit with the timestamp of its best-matching chunk ('ts')."""
    import numpy as np
    q = (query or "").strip()
    match, where_sql, params = ei.parse_query(q)
    allowed = _allowed_ids(conn, where_sql, params)

    # Filter-only query (e.g. "after:2026-08-01" with no free text): browse recent
    # WITHIN the filter (keyword_search's empty-match branch would filter kind='email').
    if match is None and where_sql:
        rows = conn.execute(
            "SELECT %s, substr(m.body_clean,1,200) AS snippet FROM messages m "
            "WHERE 1=1 %s ORDER BY m.received_ts DESC LIMIT ?"
            % (ei._SELECT_COLS, where_sql), params + [limit])
        res = [ei._row_to_result(r) for r in rows]
        for r in res:
            r["ts"] = ""
        return res

    kw = ei.keyword_search(conn, q, limit=limit * 3)   # already applies where_sql
    if allowed is not None:
        kw = [r for r in kw if r["id"] in allowed]

    sem, qv = [], None
    if vconn is not None:
        try:
            ids, mat = ei.load_vectors(vconn)
            if mat is not None and len(ids):
                plain = _plain_query(q) or q
                qv = ei.embed_texts([plain], embed_base_url)[0]
                sims = mat @ qv
                best = {}
                for idx in np.argsort(-sims):
                    mid = int(ids[idx])
                    if allowed is not None and mid not in allowed:
                        continue
                    if mid not in best:
                        best[mid] = float(sims[idx])
                        if len(best) >= limit * 3:
                            break
                sem = ei._fetch_results(conn, sorted(best.items(), key=lambda x: -x[1]))
        except Exception:
            sem, qv = [], None

    if not sem:
        merged = kw[:limit]
    else:
        def _norm(vals):
            if not vals:
                return lambda v: 0.0
            lo, hi = min(vals), max(vals)
            rng = (hi - lo) or 1.0
            return lambda v: (v - lo) / rng
        kw_s = {r["id"]: -r["rank"] for r in kw}          # BM25: lower is better
        sem_s = {r["id"]: r.get("score", 0.0) for r in sem}
        nk, ns = _norm(list(kw_s.values())), _norm(list(sem_s.values()))
        rows = {r["id"]: r for r in kw}
        for r in sem:
            rows.setdefault(r["id"], r)
        scored = []
        for mid, row in rows.items():
            s = 0.6 * (ns(sem_s[mid]) if mid in sem_s else 0.0) \
                + 0.4 * (nk(kw_s[mid]) if mid in kw_s else 0.0)
            row = dict(row)
            row["score"] = round(s, 4)
            scored.append(row)
        merged = sorted(scored, key=lambda r: -r["score"])[:limit]

    for r in merged:
        r["ts"] = _best_chunk_ts(vconn, qv, r["id"])
    return merged


def search(conn, vconn, query, embed_base_url=ei.DEFAULT_OLLAMA, limit=20):
    """Hybrid (keyword + semantic) over meetings, honoring date/folder filters on
    BOTH sides. Empty query -> recent list. `embed_base_url` MUST be the local
    Ollama endpoint (retrieval embeds the query via bge-m3), NOT the answer URL."""
    q = (query or "").strip()
    if not q:
        rec = recent(conn, limit)
        for r in rec:
            r["ts"] = ""
        return rec
    if vconn is None:
        kw = ei.keyword_search(conn, q, limit=limit)
        for r in kw:
            r["ts"] = ""
        return kw
    return _retrieve(conn, vconn, q, embed_base_url, limit)


_ASK_PROMPT = (
    "You answer questions about the user's OWN meeting transcripts, given below as "
    "numbered blocks. Ground every claim in those blocks only - never invent people, "
    "decisions, or numbers. Answer in the SAME language as the question (Hebrew stays "
    "Hebrew, right-to-left). Be concise and concrete. Cite the meetings you used inline "
    "as [1], [2] ... matching the block numbers; when a block header shows a ~timestamp, "
    "you may mention it so the user can find the moment. If the meetings do not contain "
    "the answer, say so plainly instead of guessing."
)

# Synthesis: a BROAD / cross-meeting question ("summarize what we decided about X
# across all meetings"). Wider, relevance-floored retrieval + an aggregate/timeline
# prompt that organizes ACROSS meetings instead of answering one fact.
_SYNTH_PROMPT = (
    "You are given numbered blocks, each an excerpt from one of the user's OWN meeting "
    "transcripts (title | date). The user is asking a BROAD, cross-meeting question. "
    "Synthesize ACROSS the meetings rather than answering from one: group the answer by "
    "theme or decision, note how things developed over time (use the dates), and call "
    "out agreements, open questions, and changes of direction. Ground every claim in the "
    "blocks only - never invent people, decisions, or numbers. Cite the meetings you use "
    "inline as [1], [2] ... matching the block numbers. Answer in the SAME language as "
    "the question (Hebrew stays Hebrew, right-to-left). Be thorough but organized - use "
    "short headings or bullets. If the meetings do not cover it, say so plainly."
)

# Action items: "what is still open?" - answered from the EXHAUSTIVE action-item
# aggregation (action_items.collect_items over every meeting), NOT similarity retrieval
# (which would miss meetings). The blocks are open tasks grouped by meeting.
_ACTION_PROMPT = (
    "The blocks below are the user's OPEN action items, grouped by meeting ([n] = one "
    "meeting, title | date). Answer the user's question using ONLY these items - never "
    "invent tasks. Preserve any owner shown inside an item (e.g. 'אחראי: <name>'). If the "
    "question is a general 'what is open' / 'מה המשימות הפתוחות', give an organized "
    "overview (grouped by meeting or by owner, kept scannable). If it is scoped (a "
    "person, a topic, or a meeting), return only the matching items. Cite the source "
    "meeting inline as [1], [2] ... matching the block numbers. Answer in the SAME "
    "language as the question (Hebrew stays Hebrew, right-to-left). If nothing matches, "
    "say so plainly."
)

# Lightweight intent router (default = 'factual', the snappy path). Action is checked
# first (most specific + most different behavior), synthesis next, else factual. These
# are heuristics tuned for mixed Hebrew+English dictation; a misroute degrades
# gracefully (synthesis on a narrow question is just more structured; the router can be
# disabled via `route=False`).
_ACTION_RE = re.compile(
    r"(משימ|מטל|טודו|מה נשאר|צריך לעשות|מה לעשות|לביצוע|מה עלי|מה פתוח|"
    r"action item|to-?do|task list|\btasks\b|open items|follow[- ]?up|"
    r"outstanding|what'?s left|what needs to be done|what do i need to do)",
    re.IGNORECASE)
_SYNTH_RE = re.compile(
    r"(סכם|סיכום|לסכם|תסכם|לאורך כל|בכל הפגיש|כל הפגיש|על פני|התקדמות|"
    r"תמונת מצב|סקיר|השווא|השווה|לאורך זמן|מגמ|כל מה ש|"
    r"summari[sz]e|overview|across all|across meetings|over time|"
    r"progress on|evolution|compare|trend|recap|big picture|all meetings)",
    re.IGNORECASE)


def classify_intent(question):
    """'action' | 'synthesis' | 'factual' from the question text (fast regex).
    Conservative: only leaves 'factual' on a clear action/synthesis signal."""
    q = (question or "").strip()
    if not q:
        return "factual"
    if _ACTION_RE.search(q):
        return "action"
    if _SYNTH_RE.search(q):
        return "synthesis"
    return "factual"


def _fmt_date(ts):
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def build_context(conn, hits, per_block_chars=1400):
    """Numbered [n] blocks from the retrieved meetings (title | date [| ~ts] |
    speakers + body excerpt). Returns (context_text, sources_list)."""
    blocks = []
    for n, h in enumerate(hits, 1):
        full = ei.get_message(conn, h["id"]) or {}
        body = (full.get("body_clean") or "")[:per_block_chars]
        ts = h.get("ts") or ""
        loc = _fmt_date(h.get("received_ts")) + ((" · ~" + ts) if ts else "")
        blocks.append(
            "[%d] %s | %s | speakers: %s\n%s"
            % (n, h.get("subject") or "(untitled)", loc, h.get("sender_name") or "-", body))
    return "\n\n".join(blocks), hits


def _prior_block(history):
    """The 'earlier in this conversation' context (last 3 turns, answers clipped)."""
    if not history:
        return ""
    prior = "\n".join(
        "Q: %s\nA: %s" % (h.get("q", ""), (h.get("a", "") or "")[:400])
        for h in history[-3:] if h.get("q"))
    return ("Earlier in this conversation (for context only):\n" + prior) if prior else ""


def _synthesis_hits(conn, vconn, question, embed_base_url=ei.DEFAULT_OLLAMA,
                    limit=30, cap=15, floor_frac=0.5, min_keep=5):
    """Wider retrieval for cross-meeting synthesis, then a RELEVANCE FLOOR to cut the
    low-similarity tail (verified live: a topic query has a clean score cliff, e.g.
    Landing Zone 0.96->0.46 relevant then a 0.40 noise tail). Falls back to a plain
    cap when the hits carry no fusion score (keyword-only / filter-only paths)."""
    hits = search(conn, vconn, question, embed_base_url, limit=limit)
    if not hits:
        return []
    scored = [h["score"] for h in hits if "score" in h]
    if not scored:
        return hits[:cap]
    floor = max(scored) * floor_frac
    keep = [h for h in hits if h.get("score", 0.0) >= floor][:cap]
    if len(keep) < min_keep:
        keep = hits[:min(cap, max(min_keep, len(hits)))]
    return keep


def _action_context(open_only=True):
    """Numbered [n] blocks of OPEN action items grouped by meeting, plus meeting
    `sources` shaped like retrieval hits. Reads action_items.collect_items() (raw-file
    parse + done-store merge) - EXHAUSTIVE across all meetings, independent of the
    vector index. Returns (context_text, sources)."""
    try:
        import action_items as ai
    except Exception:
        return "", []
    try:
        groups = ai.collect_items()
    except Exception:
        return "", []
    blocks, sources = [], []
    n = 0
    for g in groups:
        items = g.get("items", [])
        if open_only:
            items = [it for it in items if not it.get("done")]
        if not items:
            continue
        n += 1
        tasks = "\n".join("- " + (it.get("text") or "") for it in items)
        loc = g.get("date") or _fmt_date(g.get("ts") or 0)
        blocks.append("[%d] %s | %s\n%s"
                      % (n, g.get("title") or "(untitled)", loc, tasks))
        sources.append({
            "entry_id": g.get("meeting_id") or "",
            "subject": g.get("title") or "",
            "received_ts": g.get("ts") or 0,
            "sender_name": "", "folder": "", "ts": "", "score": 0.0,
        })
    return "\n\n".join(blocks), sources


def _ask_actions(question, answer_base_url, answer_model, answer_key, history):
    """Answer an action-item question from the exhaustive open-task aggregation."""
    context, sources = _action_context(open_only=True)
    if not sources:
        # Empty-state answer in the QUESTION's language (the prompts already
        # answer in the question's language; this hardcoded path must too).
        heb = sum(1 for c in question or "" if "֐" <= c <= "׿")
        lat = sum(1 for c in (question or "").lower() if "a" <= c <= "z")
        empty = ("לא נמצאו משימות פתוחות בפגישות." if heb > lat
                 else "No open action items were found in the meetings.")
        return {"answer": empty, "sources": [], "model": "", "mode": "action"}
    parts = []
    prior = _prior_block(history)
    if prior:
        parts.append(prior)
    parts.append("Open action items across meetings ([n] = one meeting):\n\n" + context)
    parts.append("Question: " + question)
    user = "\n\n".join(parts)
    try:
        answer, used = ei._llm_chat(_ACTION_PROMPT, user, answer_base_url, answer_model,
                                    api_key=(answer_key or "ollama"), return_model=True)
    except Exception as e:
        return {"answer": "Answer model failed: %s" % e, "sources": sources,
                "model": answer_model, "mode": "action"}
    return {"answer": answer, "sources": sources, "model": used, "mode": "action"}


def ask(conn, vconn, question, embed_base_url, answer_base_url, answer_model,
        answer_key="ollama", top_k=20, history=None, route=True):
    """RAG over the meeting archive. Routes the question to one of three modes and
    returns {answer, sources, model, mode}:
      - 'action'    -> exhaustive open action-item aggregation (no retrieval);
      - 'synthesis' -> wider, relevance-floored retrieval + a cross-meeting prompt;
      - 'factual'   -> the standard top-k retrieval + cited-answer prompt (default).
    `route=False` forces 'factual' (the original behavior). Retrieval and answer use
    DIFFERENT urls: `embed_base_url` = local Ollama (query embedding); `answer_base_url`
    = the chosen provider's chat endpoint. `history` (last 3 {q,a} turns) is phrasing
    context for follow-ups; each turn re-retrieves on its own question."""
    question = (question or "").strip()
    intent = classify_intent(question) if route else "factual"

    if intent == "action":
        return _ask_actions(question, answer_base_url, answer_model, answer_key, history)

    if intent == "synthesis":
        hits = _synthesis_hits(conn, vconn, question, embed_base_url)
        prompt, per_block, mode = _SYNTH_PROMPT, 2200, "synthesis"
    else:
        hits = search(conn, vconn, question, embed_base_url, limit=top_k)
        prompt, per_block, mode = _ASK_PROMPT, 1400, "factual"

    if not hits:
        return {"answer": "No indexed meetings matched. Try re-indexing or a "
                          "different question.", "sources": [], "model": "", "mode": mode}
    context, sources = build_context(conn, hits, per_block_chars=per_block)
    parts = []
    prior = _prior_block(history)
    if prior:
        parts.append(prior)
    parts.append("Meetings:\n\n" + context)
    parts.append("Question: " + question)
    user = "\n\n".join(parts)
    try:
        answer, used = ei._llm_chat(prompt, user, answer_base_url, answer_model,
                                    api_key=(answer_key or "ollama"), return_model=True)
    except Exception as e:
        return {"answer": "Answer model failed: %s" % e, "sources": sources,
                "model": answer_model, "mode": mode}
    return {"answer": answer, "sources": sources, "model": used, "mode": mode}


# ---------------------------------------------------------------------------
# Status / heartbeat / state  (same pattern as emailsearch_indexer)
# ---------------------------------------------------------------------------
def _heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def _write_status(**kw):
    import json
    kw.setdefault("ts", int(time.time()))
    try:
        ensure_dir()
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def _load_state():
    import json
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mtimes": {}}


def _save_state(state):
    import json
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reindex driver + CLI
# ---------------------------------------------------------------------------
def reindex(full=False, limit=0, do_embed=True, base_url=ei.DEFAULT_OLLAMA,
            on_status=None):
    """Enumerate MEETINGS_SRC_DIR, upsert new/changed files (mtime watermark),
    embed pending. Returns (seen, changed, embedded)."""
    ensure_dir()
    conn = ei.connect(MESSAGES_DB)
    ei.init_schema(conn)
    vconn = ei.connect(VECTORS_DB)
    ei.init_vectors(vconn)
    state = _load_state()
    mtimes = state.setdefault("mtimes", {})

    try:
        names = sorted(os.listdir(MEETINGS_SRC_DIR))
    except OSError:
        names = []
    names = [n for n in names if is_indexable_meeting(n)]

    # Prune meetings deleted from disk (self-healing index). Uses the FULL present
    # set (before any --limit slice). GUARD: never prune on an empty listing - a
    # transient listdir() failure must not wipe the whole index. FTS is cleaned
    # automatically by the messages AFTER DELETE trigger; chunks live in vectors.db.
    pruned = 0
    if names:
        present = set(names)
        gone = [(r[0], r[1]) for r in conn.execute("SELECT id, entry_id FROM messages")
                if r[1] not in present]
        for mid, eid in gone:
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            try:
                vconn.execute("DELETE FROM chunks WHERE msg_id=?", (mid,))
            except Exception:
                pass
            mtimes.pop(eid, None)
        if gone:
            conn.commit()
            try:
                vconn.commit()
            except Exception:
                pass
        pruned = len(gone)

    if limit:
        names = names[-limit:]

    seen = changed = 0
    for name in names:
        path = os.path.join(MEETINGS_SRC_DIR, name)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if not full and mtimes.get(name) == mt:
            continue                         # unchanged since last index
        raw = parse_meeting_file(path)
        if not raw or not (raw["body"] or raw["subject"]):
            continue
        _mid, ch = _upsert(conn, raw)
        seen += 1
        if ch:
            changed += 1
        mtimes[name] = mt
        if on_status:
            on_status("indexing", seen, len(names))
    conn.commit()
    _save_state(state)

    embedded = 0
    if do_embed:
        if on_status:
            on_status("embedding", 0, 0)
        embedded = embed_pending(
            conn, vconn, base_url=base_url,
            on_progress=(lambda d, t: on_status("embedding", d, t)) if on_status else None)
    conn.commit()
    return seen, changed, embedded, pruned


def main():
    import argparse
    import traceback
    # Windowless spawns hand this process stdout==stderr==None; route to a log.
    if sys.stdout is None or sys.stderr is None:
        try:
            ensure_dir()
            _sink = open(os.path.join(MEET_INDEX_DIR, "indexer.log"), "a",
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
    ap.add_argument("--base-url", default=ei.DEFAULT_OLLAMA)
    args = ap.parse_args()

    ensure_dir()
    _write_status(state="starting")
    _heartbeat()
    t0 = time.time()

    def _status(state, done, total):
        _heartbeat()
        _write_status(state=state, done=done, total=total)

    try:
        seen, changed, embedded, pruned = reindex(
            full=args.full, limit=args.limit, do_embed=not args.no_embed,
            base_url=args.base_url, on_status=_status)
        conn = ei.connect(MESSAGES_DB, readonly=True)
        st = ei.stats(conn)
        _write_status(state="done", messages=st["messages"], embedded=st["embedded"],
                      last_sync=int(time.time()), elapsed=round(time.time() - t0, 1))
        sys.stdout.write(
            "DONE seen=%d changed=%d embedded=%d pruned=%d total=%d in %.1fs\n"
            % (seen, changed, embedded, pruned, st["messages"], time.time() - t0))
        sys.stdout.flush()
    except Exception as e:
        _write_status(state="error", error="%s: %s" % (type(e).__name__, e))
        sys.stderr.write("meetings indexer error: %s\n%s\n"
                         % (e, traceback.format_exc()))
        sys.exit(1)


if __name__ == "__main__":
    main()
