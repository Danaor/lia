"""
Thorough test suite for Lia.

Covers everything testable without real mic / keyboard / Windows events:
- Syntax + imports
- Config load/save round-trip + migrations
- History read/write + thread safety
- strip_hallucinated_tail edge cases
- Transcribers construct correctly + custom_vocabulary wired
- GroqLLMCleaner live call for each style + guards
- MeetingSession end-to-end (mocked chunks)
- _do_paste + _undo_last_paste state machine
- _validate_hotkey
- Icon generation for all states
"""
import ast
import io
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A portable build may ship config.seed.json next to lia.py (gitignored).
# If present in the tree it would make every first-run load_config() in the config
# tests pick up the portable cloud defaults, breaking determinism. Neutralize the
# seed globally here; the seed mechanism itself is validated in t_config_seed_portable.
try:
    import lia as _wt_seed_guard
    _wt_seed_guard._bundled_seed_cache = {}
except Exception:
    pass


PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
results = []


class SkipTest(Exception):
    """Raise inside a test to mark it SKIPPED (not failed) — e.g. a live-API
    test with no key configured, or LIA_SKIP_LIVE=1 set to run the suite
    for free."""


def _skip_live(reason="live API call"):
    """Skip helper for tests that hit paid APIs. Set LIA_SKIP_LIVE=1 to run
    the whole suite without network/cost."""
    if os.environ.get("LIA_SKIP_LIVE") or os.environ.get("WT_SKIP_LIVE"):
        raise SkipTest(f"LIA_SKIP_LIVE set — skipping {reason}")


def _test(name, fn):
    """Run one test, collect pass/fail/skip + error."""
    try:
        fn()
        results.append((PASS, name, ""))
        print(f"  [PASS] {name}")
    except SkipTest as e:
        results.append((SKIP, name, str(e)))
        print(f"  [SKIP] {name} — {e}")
    except AssertionError as e:
        msg = f"{type(e).__name__}: {e}"
        results.append((FAIL, name, msg))
        print(f"  [FAIL] {name}")
        print(f"    {msg}")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        results.append((FAIL, name, msg))
        print(f"  [ERR ] {name}")
        print(f"    {msg.splitlines()[0]}")


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ============================================================
# 1. Syntax + imports
# ============================================================
section("1. Syntax + Imports")


def t_syntax():
    # Path relative to THIS file, not the CWD — the suite should pass from
    # the repo root too, not only from inside the app folder.
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia.py")
    with open(src, "r", encoding="utf-8") as f:
        ast.parse(f.read())


_test("lia.py syntax valid", t_syntax)


def t_import_module():
    import lia  # noqa
    assert hasattr(lia, "LiaApp")
    assert hasattr(lia, "MeetingSession")
    assert hasattr(lia, "GroqLLMCleaner")
    assert hasattr(lia, "FasterWhisperTranscriber")
    assert hasattr(lia, "AudioRecorder")
    assert hasattr(lia, "_validate_hotkey")
    assert hasattr(lia, "_fmt_relative_ts")


_test("all public classes/functions export", t_import_module)


# ============================================================
# 2. Config load/save/migrations
# ============================================================
section("2. Config Load/Save/Migrations")


def t_config_roundtrip():
    import lia as w
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    original_dir = w.CONFIG_DIR
    original_file = w.CONFIG_FILE
    w.CONFIG_DIR = tmp
    w.CONFIG_FILE = cfg_path
    try:
        cfg = w.load_config()  # empty file → defaults
        assert cfg["hotkey"] == "ctrl+space", cfg["hotkey"]
        assert "cleanup_style" in cfg
        assert "custom_vocabulary" in cfg
        assert "clipboard_auto_restore" in cfg
        assert "undo_hotkey" in cfg
        cfg["hotkey"] = "ctrl+alt+x"
        w.save_config(cfg)
        cfg2 = w.load_config()
        assert cfg2["hotkey"] == "ctrl+alt+x"
    finally:
        w.CONFIG_DIR = original_dir
        w.CONFIG_FILE = original_file


_test("config load → modify → save → reload", t_config_roundtrip)


def t_config_migrate_direct_type():
    import lia as w
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w") as f:
        json.dump({"paste_mode": "direct_type", "model_size": "x"}, f)
    original_dir = w.CONFIG_DIR
    original_file = w.CONFIG_FILE
    w.CONFIG_DIR = tmp
    w.CONFIG_FILE = cfg_path
    try:
        cfg = w.load_config()
        assert cfg["paste_mode"] == "auto_paste", cfg["paste_mode"]
    finally:
        w.CONFIG_DIR = original_dir
        w.CONFIG_FILE = original_file


_test("config migrates legacy direct_type → auto_paste", t_config_migrate_direct_type)


def t_config_corrupt():
    import lia as w
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w") as f:
        f.write("not valid json {")
    original_dir = w.CONFIG_DIR
    original_file = w.CONFIG_FILE
    w.CONFIG_DIR = tmp
    w.CONFIG_FILE = cfg_path
    try:
        cfg = w.load_config()
        assert cfg["hotkey"] == "ctrl+space", "should fall back to defaults"
    finally:
        w.CONFIG_DIR = original_dir
        w.CONFIG_FILE = original_file


_test("corrupt config falls back to defaults gracefully", t_config_corrupt)


def t_secret_store_config_encryption():
    """Secrets are DPAPI-encrypted at rest in config.json (2026-08-28 audit)
    and transparently decrypted on load; plaintext values in an existing file
    are accepted (automatic migration) and encrypted on the next save; a
    foreign/corrupt blob maps to '' (unusable but non-fatal)."""
    import base64
    import lia as w
    import secret_store as ss
    # module basics: passthrough + no-double-wrap + corrupt handling
    assert ss.protect("") == "" and ss.unprotect("") == ""
    assert ss.unprotect("plain-key") == "plain-key"       # migration path
    if not ss.available():
        return   # non-Windows dev box: encryption inactive, fail-open
    blob = ss.protect("sk-SECRET")
    assert ss.is_protected(blob) and "sk-SECRET" not in blob
    assert ss.protect(blob) == blob                       # already protected
    assert ss.unprotect(blob) == "sk-SECRET"
    assert ss.unprotect("dpapi:!!!not-base64") == ""      # corrupt → unset
    junk = ss.PREFIX + base64.b64encode(b"not-a-dpapi-blob").decode()
    assert ss.unprotect(junk) == ""                       # undecryptable → unset
    # config roundtrip: plaintext in → encrypted on disk → plaintext in memory
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    od, of = w.CONFIG_DIR, w.CONFIG_FILE
    w.CONFIG_DIR, w.CONFIG_FILE = tmp, cfg_path
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"groq_api_key": "gsk_PLAIN", "model_size": "x"}, f)
        cfg = w.load_config()
        assert cfg["groq_api_key"] == "gsk_PLAIN"         # plaintext accepted
        w.save_config(cfg)
        with open(cfg_path, encoding="utf-8") as f:
            raw = f.read()
        assert "gsk_PLAIN" not in raw, "plaintext key leaked to disk"
        assert json.loads(raw)["groq_api_key"].startswith(ss.PREFIX)
        assert cfg["groq_api_key"] == "gsk_PLAIN", "save_config mutated caller dict"
        cfg2 = w.load_config()
        assert cfg2["groq_api_key"] == "gsk_PLAIN"        # decrypts on load
        assert cfg2["openai_api_key"] == ""               # empty stays empty
        # FAIL CLOSED on save (audit follow-up): if protection fails, the
        # save must abort - a plaintext secret never reaches the disk.
        orig_protect = ss.protect
        ss.protect = lambda t: t                          # simulate DPAPI failure
        try:
            try:
                w.save_config({"groq_api_key": "gsk_WOULD_LEAK"})
                assert False, "save_config accepted a plaintext secret"
            except RuntimeError as e:
                assert "plaintext" in str(e)
            with open(cfg_path, encoding="utf-8") as f:
                assert "gsk_WOULD_LEAK" not in f.read()
        finally:
            ss.protect = orig_protect
    finally:
        w.CONFIG_DIR, w.CONFIG_FILE = od, of


_test("secret_store: DPAPI at-rest config encryption + migration",
      t_secret_store_config_encryption)


def t_secret_payload_wiring():
    """Child-window payload files must never hold a plaintext API key on disk:
    the chat payload + the meetings-ask provider keys are DPAPI-protected at
    write and unprotected in the child; the HF token rides in env only (argv
    is visible to every same-user process)."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert "secret_store.protect(openai_key)" in src, "chat payload key unprotected"
    assert "dict(p, key=blob)" in src, "meetask provider keys unprotected"
    # FAIL CLOSED at the payload boundary too: an unprotectable key disables
    # the cloud path instead of ever reaching disk in plaintext.
    assert 'openai_key, protected_key = "", ""' in src
    assert "dropping cloud provider" in src
    assert '"--hf-token", hf' not in src, "HF token passed on argv"
    chat = open(os.path.join(here, "chat_window.py"), encoding="utf-8").read()
    assert 'secret_store.unprotect(payload.get("openai_key")' in chat
    ms = open(os.path.join(here, "meetings_search.py"), encoding="utf-8").read()
    assert 'secret_store.unprotect(p.get("key")' in ms


_test("secret_store: no plaintext keys in child payloads / argv",
      t_secret_payload_wiring)


def t_log_privacy():
    """The log records only SIZES of user content by default (2026-08-28
    audit): dictation text, voice-ask questions, detected meeting titles and
    correction hits appear only with log_transcripts:true."""
    import lia as w
    assert w.DEFAULT_CONFIG.get("log_transcripts") is False
    assert w._fmt_user_text({}, "secret dictation") == "[16 chars]"
    assert w._fmt_user_text(None, "") == "[0 chars]"
    assert w._fmt_user_text({"log_transcripts": True}, "abc") == "abc"
    assert w._fmt_user_text({"log_transcripts": True}, "abcdef", 3) == "abc"
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert "len(display_text), display_text)" not in src, \
        "raw dictation text logged unconditionally"
    assert 'log.info("Voice-ask question: %s", question' not in src
    assert src.count("_fmt_user_text(self.config") >= 5, \
        "expected the 5 gated user-content log sites"


_test("log privacy: user content gated behind log_transcripts", t_log_privacy)


def t_config_seed_portable():
    """Portable config.seed.json seeds first-run defaults but never touches an
    existing config; absent seed keeps the keyless-local defaults."""
    import lia as w
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    original_dir, original_file = w.CONFIG_DIR, w.CONFIG_FILE
    original_seed = w._bundled_seed_cache
    w.CONFIG_DIR, w.CONFIG_FILE = tmp, cfg_path
    try:
        # Simulate a portable build's seed (inject via the module cache so we
        # don't depend on an on-disk config.seed.json).
        w._bundled_seed_cache = {
            "transcription_backend": "openai",
            "openai_model": "gpt-transcribe",
            "meeting_model": "openai_gpt_transcribe",
            "summary_model": "gpt-5.6-sol",
        }
        cfg = w.load_config()  # no config file → first run → seed applies
        assert cfg["transcription_backend"] == "openai", cfg["transcription_backend"]
        assert cfg["openai_model"] == "gpt-transcribe", cfg["openai_model"]
        assert cfg["meeting_model"] == "openai_gpt_transcribe", cfg["meeting_model"]
        assert cfg["summary_model"] == "gpt-5.6-sol", cfg["summary_model"]
        # An existing saved config MUST win over the seed (seed is first-run only).
        w.save_config({"transcription_backend": "local",
                       "meeting_model": "local_hebrew_turbo"})
        cfg2 = w.load_config()
        assert cfg2["transcription_backend"] == "local", "existing config must beat seed"
        assert cfg2["meeting_model"] == "local_hebrew_turbo", cfg2["meeting_model"]
        # No seed (main installer / dev source) → keyless-local defaults preserved.
        w._bundled_seed_cache = {}
        os.remove(cfg_path)
        cfg3 = w.load_config()
        assert cfg3["transcription_backend"] == "local", cfg3["transcription_backend"]
    finally:
        w.CONFIG_DIR, w.CONFIG_FILE = original_dir, original_file
        w._bundled_seed_cache = original_seed


_test("config: portable seed applies on first run, never overrides existing", t_config_seed_portable)


def t_gpt_transcribe_registered():
    """gpt-transcribe wired into the OpenAI json-family + dictation + meeting
    registries, and is the default OpenAI model."""
    import lia as w
    assert "gpt-transcribe" in w.OpenAITranscriber._NO_VERBOSE_JSON_MODELS
    assert w.DEFAULT_CONFIG["openai_model"] == "gpt-transcribe", w.DEFAULT_CONFIG["openai_model"]
    # Dictation registry: a row whose OpenAI sub-model is gpt-transcribe.
    dict_openai = [row[4] for row in w.LiaApp._MENU_MODELS_ORDERED]
    assert "gpt-transcribe" in dict_openai, dict_openai
    # Meeting registry: the openai_gpt_transcribe key gated behind an OpenAI key.
    meet = {k: req for _l, k, req in w.LiaApp._MEETING_MODELS}
    assert "openai_gpt_transcribe" in meet, list(meet)
    assert meet["openai_gpt_transcribe"] == ["openai_api_key"], meet["openai_gpt_transcribe"]
    # And the builder maps it to the right model id.
    import inspect
    src = inspect.getsource(w.LiaApp._build_meeting_transcriber)
    assert '"openai_gpt_transcribe": "gpt-transcribe"' in src, "builder mapping missing"


_test("models: gpt-transcribe registered for dictation + meetings + default", t_gpt_transcribe_registered)


def t_gemini_summary_option():
    """Gemini 3.6 Flash is registered as a FREE summary option and routed with
    its OWN key (not the OpenAI key / Ollama placeholder)."""
    import lia as w
    App = w.LiaApp
    assert w._is_gemini_url(w.GEMINI_CHAT_URL)
    assert "gemini_api_key" in w.DEFAULT_CONFIG
    assert "gemini-3.7-flash" in [m for _l, m, _u in App._SUMMARY_MODELS]
    assert "gemini-3.6-flash" not in [m for _l, m, _u in App._SUMMARY_MODELS]
    assert hasattr(App, "_set_gemini_api_key")
    app = App.__new__(App); app._summary_cleaner = None
    app.config = {"summary_model": "gemini-3.7-flash",
                  "summary_base_url": w.GEMINI_CHAT_URL,
                  "gemini_api_key": "AIza_TEST", "openai_api_key": ""}
    c = app._get_summary_cleaner()
    assert c is not None and c.api_key == "AIza_TEST" \
        and c.chat_url == w.GEMINI_CHAT_URL, "gemini not routed to its own key/url"
    assert app._summary_available() is True
    # No Gemini key → degrade (no cleaner, summary unavailable).
    app.config["gemini_api_key"] = ""; app._summary_cleaner = None
    assert app._get_summary_cleaner() is None
    assert app._summary_available() is False
    # OpenAI cloud path still routes to the OpenAI endpoint.
    app.config = {"summary_model": "gpt-5.6-terra", "summary_base_url": "",
                  "gemini_api_key": "", "openai_api_key": "sk-X"}
    app._summary_cleaner = None
    assert app._get_summary_cleaner().chat_url == w.OpenAILLMCleaner.CHAT_URL


_test("summary: Gemini free option registered + routed with its own key", t_gemini_summary_option)


def t_config_atomic_write():
    """Atomic writes use .tmp + rename — verify no leftover .tmp file."""
    import lia as w
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    original_dir = w.CONFIG_DIR
    original_file = w.CONFIG_FILE
    w.CONFIG_DIR = tmp
    w.CONFIG_FILE = cfg_path
    try:
        w.save_config({"hotkey": "ctrl+space", "model_size": "x"})
        files = os.listdir(tmp)
        assert "config.json" in files
        assert "config.json.tmp" not in files, "leftover .tmp file!"
    finally:
        w.CONFIG_DIR = original_dir
        w.CONFIG_FILE = original_file


_test("config save is atomic (no leftover .tmp)", t_config_atomic_write)


# ============================================================
# 3. History round-trip + thread safety
# ============================================================
section("3. History Round-trip + Thread Safety")


def t_history_roundtrip():
    import lia as w
    tmp = tempfile.mkdtemp()
    original_hf = w.HISTORY_FILE
    original_cd = w.CONFIG_DIR
    w.CONFIG_DIR = tmp
    w.HISTORY_FILE = os.path.join(tmp, "history.json")
    try:
        w.save_history([])
        assert w.load_history() == []
        w.add_history_entry("Hello world", duration_sec=2.5, model="test")
        w.add_history_entry("שלום עולם", duration_sec=1.5, model="test")
        h = w.load_history()
        assert len(h) == 2, len(h)
        assert h[0]["text"] == "Hello world"
        assert h[1]["text"] == "שלום עולם"
    finally:
        w.HISTORY_FILE = original_hf
        w.CONFIG_DIR = original_cd


_test("history: add 2 entries, read back in order", t_history_roundtrip)


def t_history_concurrent():
    """Thread safety: 10 threads writing concurrently should not lose or corrupt entries."""
    import lia as w
    tmp = tempfile.mkdtemp()
    original_hf = w.HISTORY_FILE
    original_cd = w.CONFIG_DIR
    w.CONFIG_DIR = tmp
    w.HISTORY_FILE = os.path.join(tmp, "history.json")
    w.save_history([])
    try:
        threads = []
        N = 20
        for i in range(N):
            t = threading.Thread(target=lambda i=i: w.add_history_entry(f"entry-{i}"))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        h = w.load_history()
        assert len(h) == N, f"expected {N} entries, got {len(h)}"
    finally:
        w.HISTORY_FILE = original_hf
        w.CONFIG_DIR = original_cd


_test("history: 20 concurrent writes don't lose entries", t_history_concurrent)


def t_history_ignores_empty():
    import lia as w
    tmp = tempfile.mkdtemp()
    original_hf = w.HISTORY_FILE
    original_cd = w.CONFIG_DIR
    w.CONFIG_DIR = tmp
    w.HISTORY_FILE = os.path.join(tmp, "history.json")
    w.save_history([])
    try:
        w.add_history_entry("")       # empty
        w.add_history_entry("   ")    # whitespace
        w.add_history_entry("\u200F") # just RTL marker
        assert w.load_history() == []
    finally:
        w.HISTORY_FILE = original_hf
        w.CONFIG_DIR = original_cd


_test("history: skips empty/whitespace-only entries", t_history_ignores_empty)


# ============================================================
# 4. Hallucination stripping
# ============================================================
section("4. Hallucination Stripping")


def t_strip_whole_hallucination():
    import lia as w
    assert w.strip_hallucinated_tail("Thank you.") == ""
    assert w.strip_hallucinated_tail("תודה רבה") == ""
    assert w.strip_hallucinated_tail("bye!") == ""
    assert w.strip_hallucinated_tail("Thank you for watching.") == ""


_test("strip: whole-text hallucinations → empty", t_strip_whole_hallucination)


def t_strip_tail_only():
    import lia as w
    assert w.strip_hallucinated_tail("Hello world. Thank you.") == "Hello world"
    assert w.strip_hallucinated_tail("שלום עולם. תודה רבה") == "שלום עולם"
    assert w.strip_hallucinated_tail("Real content here. bye.") == "Real content here"


_test("strip: tail hallucinations removed, prefix kept", t_strip_tail_only)


def t_strip_preserves_real_text():
    import lia as w
    assert w.strip_hallucinated_tail("Hello, this is a real message.") == "Hello, this is a real message."
    assert w.strip_hallucinated_tail("") == ""
    assert w.strip_hallucinated_tail("Short") == "Short"


_test("strip: real content preserved untouched", t_strip_preserves_real_text)


# ============================================================
# 5. Transcriber construction + custom_vocabulary wiring
# ============================================================
section("5. Transcriber Construction")


def t_faster_whisper_construct():
    import lia as w
    t = w.FasterWhisperTranscriber(model_size="large-v3-turbo", cpu_threads=4)
    assert t.model is None
    assert t.custom_vocabulary == ""
    t.custom_vocabulary = "React, git, Kubernetes"
    assert t.custom_vocabulary == "React, git, Kubernetes"


_test("FasterWhisperTranscriber constructs + custom_vocab wire-up", t_faster_whisper_construct)


def t_whisper_gpu_wiring():
    """GPU (CUDA) plumbing is present and the CPU path resolves correctly — no
    model load, no GPU required (so this runs anywhere)."""
    import lia as w
    assert hasattr(w, "_ensure_cuda_dll_path"), "DLL-path helper missing"
    assert w.DEFAULT_CONFIG.get("whisper_device") == "auto"
    assert "cuda_compute_type" in w.DEFAULT_CONFIG
    t = w.FasterWhisperTranscriber(model_size="tiny", cpu_threads=4,
                                   device="cpu", compute_type="int8_float16")
    assert hasattr(t, "_infer_lock"), "per-model inference lock missing"
    assert hasattr(t, "_demote_to_cpu"), "runtime CPU-fallback missing"
    # device='cpu' must resolve to CPU/int8 without touching CUDA
    assert t._resolve_device() == ("cpu", "int8"), t._resolve_device()
    # an explicit 'auto' transcriber still resolves (cpu or cuda) without error
    t2 = w.FasterWhisperTranscriber(model_size="tiny", cpu_threads=4, device="auto")
    dev, ct = t2._resolve_device()
    assert dev in ("cpu", "cuda") and isinstance(ct, str)


_test("faster-whisper GPU/CUDA wiring + CPU fallback plumbing", t_whisper_gpu_wiring)


def t_summary_module_wiring():
    """Meeting Summary Module: cloud vs local-Ollama cleaner endpoint, the model
    list, Ollama tag matching, and Compose's separate cleaner getter. No network."""
    import lia as w
    assert "summary_base_url" in w.DEFAULT_CONFIG
    models = [m for _, m, _ in w.LiaApp._SUMMARY_MODELS]
    assert "gpt-5.5" not in models, "ChatGPT 5.5 was removed from the summary picker"
    assert any(m.startswith("gemma4") for m in models), models
    assert not any("qwen" in m.lower() for m in models), models
    assert not any("dicta" in m.lower() for m in models), models
    # gpt-5.6-SOL/Terra are LIVE-reachable (verified 2026-07-11). The id is
    # "terra" (two r's) — "tera" 404s, so pin the correct one.
    assert "gpt-5.6-sol" in models and "gpt-5.6-terra" in models, models
    assert "gpt-5.6-tera" not in models, "wrong id: it's gpt-5.6-terra, not -tera"
    # cloud cleaner → OpenAI URL; a chat_url override (Ollama) points elsewhere
    assert w.OpenAILLMCleaner(api_key="k", model="gpt-5.5").chat_url == \
        w.OpenAILLMCleaner.CHAT_URL
    loc = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                             chat_url=w.LiaApp._OLLAMA_CHAT_URL)
    assert loc.chat_url.endswith("/v1/chat/completions")
    # Ollama tag match tolerates the ':latest' suffix Ollama appends
    H = w.LiaApp._ollama_has
    assert H("gemma4:31b-it-qat", {"gemma4:31b-it-qat:latest"})
    assert not H("gemma4:31b-it-qat", {"llama3:8b"})
    # Compose has its OWN cleaner getter (so a local summary model can't drag it)
    assert hasattr(w.LiaApp, "_get_compose_cleaner")


_test("Meeting Summary Module: cloud/local wiring + Ollama match", t_summary_module_wiring)


def t_summary_marker_localizer():
    """_localize_summary_markers normalizes markers to the summary's OWN
    language: English TL;DR/DECIDED in a Hebrew summary become Hebrew, and
    (2026-08) variant/Hebrew headers in an English summary become the canonical
    EN set. Header lines only - body text is never touched."""
    import lia as w
    he = "## TL;DR\nשורה על הפגישה.\n\n## נקודות מפתח\n- **DECIDED:** הוחלט X\n- DECIDED: גם Y"
    out = w._localize_summary_markers(he)
    assert "TL;DR" not in out and "תקציר" in out, out
    assert "DECIDED" not in out and "הוחלט" in out, out
    # An English summary: header variants canonicalized, body untouched.
    en = "## TL;DR\nThe meeting was about X.\n## Action items\n- [ ] do Y"
    out_en = w._localize_summary_markers(en)
    assert "## Summary" in out_en and "## Tasks" in out_en, out_en
    assert "The meeting was about X." in out_en and "- [ ] do Y" in out_en
    # A stray Hebrew header in an EN summary maps to its EN counterpart.
    mixed = "## משימות\n- [ ] mostly english body text here in this summary"
    assert "## Tasks" in w._localize_summary_markers(mixed)
    # Already-canonical EN headers stay put.
    canon = "## Summary\nAll good.\n## Tasks\n- [ ] x"
    assert w._localize_summary_markers(canon) == canon


_test("summary marker localizer (he DECIDED/TL;DR; en canonical headers)",
      t_summary_marker_localizer)


def t_groq_transcriber_bias_prompt():
    import lia as w
    t = w.GroqTranscriber(model_size="whisper-large-v3-turbo", api_key="fake")
    t.he_en_bias = True
    t.custom_vocabulary = "git, push, Kafka"
    prompt = t._build_bias_prompt()
    assert prompt is not None
    assert "git, push, Kafka" in prompt
    assert "Hebrew" in prompt or "שלום" in prompt  # he_en bias still present

    t.he_en_bias = False
    prompt2 = t._build_bias_prompt()
    assert "git, push, Kafka" in prompt2
    assert "Hebrew" not in prompt2  # no bias


_test("GroqTranscriber bias prompt composes vocab + he_en", t_groq_transcriber_bias_prompt)


class _FakeGemResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        import json as _json
        self.content = _json.dumps(body).encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class _FakeGemSession:
    """Captures the last POST body so tests can assert the request shape without
    hitting the network. GET returns a benign model resource (verify_key)."""
    def __init__(self, post_body):
        self._post_body = post_body
        self.last = None

    def post(self, url, headers=None, json=None, timeout=None):
        self.last = {"url": url, "headers": headers, "json": json}
        return _FakeGemResp(200, self._post_body)

    def get(self, url, headers=None, timeout=None):
        return _FakeGemResp(200, {"name": "models/gemini-3.5-transcribe"})


def t_gemini_transcriber_request_shape():
    """GeminiTranscriber posts the LIVE-verified Interactions shape: audio inline,
    language_codes whitelist, custom_vocabulary list, mode.type=verbatim, and an
    RLM prefix on Hebrew output. A forced language pins a single code."""
    import lia as w
    import numpy as _np
    resp = {"steps": [{"type": "model_output",
                       "content": [{"type": "text", "text": "שלום עולם"}]}]}
    t = w.GeminiTranscriber(api_key="fake", language_codes=["he-IL", "en-US"])
    t._session = _FakeGemSession(resp)
    t.custom_vocabulary = "git, push, React"
    t.load_model()
    audio = (_np.random.randn(16000).astype(_np.float32) * 0.1)
    out = t.transcribe(audio, language=None)
    body = t._session.last["json"]
    assert body["model"] == "gemini-3.5-transcribe", body["model"]
    assert body["input"][0]["type"] == "audio"
    assert body["input"][0]["mime_type"] == "audio/wav"
    tc = body["generation_config"]["transcription_config"]
    assert tc["language_codes"] == ["he-IL", "en-US"], tc["language_codes"]
    assert tc["custom_vocabulary"] == ["git", "push", "React"], tc["custom_vocabulary"]
    assert tc["mode"]["type"] == "verbatim"
    assert "diarization_mode" not in tc["mode"]
    assert t._session.last["headers"]["x-goog-api-key"] == "fake"
    assert out.startswith("‏") and "שלום עולם" in out, repr(out)
    # forced language -> a single pinned code
    t2 = w.GeminiTranscriber(api_key="fake")
    t2._session = _FakeGemSession(resp)
    t2.load_model()
    t2.transcribe(audio, language="en")
    tc2 = t2._session.last["json"]["generation_config"]["transcription_config"]
    assert tc2["language_codes"] == ["en-US"], tc2["language_codes"]


_test("GeminiTranscriber request shape (Interactions API, language_codes, vocab)",
      t_gemini_transcriber_request_shape)


def t_gemini_diarize_parsing():
    """Diarized request carries diarization_mode + word timestamps; the documented
    word-annotation response aggregates into speaker utterances with ms timings.
    Multi-segment audio renumbers labels per segment; prose-only degrades to a
    single utterance (graceful fallback)."""
    import lia as w
    import numpy as _np
    diar = {"steps": [{"type": "model_output", "content": [{
        "type": "text", "text": "hello there general",
        "annotations": [
            {"type": "word_info", "text": "hello", "speaker": "spk_1",
             "start_offset": "0.10s", "end_offset": "0.40s"},
            {"type": "word_info", "text": "there", "speaker": "spk_1",
             "start_offset": "0.45s", "end_offset": "0.80s"},
            {"type": "word_info", "text": "general", "speaker": "spk_2",
             "start_offset": "1.00s", "end_offset": "1.50s"}]}]}]}
    t = w.GeminiTranscriber(api_key="fake")
    t._session = _FakeGemSession(diar)
    t.load_model()
    audio = (_np.random.randn(16000).astype(_np.float32) * 0.1)
    utts = t.transcribe_diarized(audio, language=None)
    tc = t._session.last["json"]["generation_config"]["transcription_config"]
    assert tc["mode"]["diarization_mode"] == "speaker"
    assert tc["mode"]["timestamp_granularities"] == ["word"]
    assert len(utts) == 2, utts
    assert utts[0]["speaker"] == "spk_1" and utts[0]["text"] == "hello there"
    assert utts[0]["start"] == 100.0 and utts[0]["end"] == 800.0, utts[0]
    assert utts[1]["speaker"] == "spk_2" and utts[1]["start"] == 1000.0
    # multi-segment: force 2 segments and check per-segment speaker prefixes
    t2 = w.GeminiTranscriber(api_key="fake")
    t2._session = _FakeGemSession(diar)
    t2.load_model()
    t2.INLINE_MAX_S = 1.0
    t2.SPLIT_TARGET_S = 0.5
    long_audio = (_np.random.randn(16000 * 3).astype(_np.float32) * 0.1)
    utts2 = t2.transcribe_diarized(long_audio, language=None)
    assert any("-" in u["speaker"] and u["speaker"].startswith("S") for u in utts2), utts2
    # prose-only fallback
    prose = {"steps": [{"type": "model_output",
                        "content": [{"type": "text", "text": "just text"}]}]}
    t3 = w.GeminiTranscriber(api_key="fake")
    t3._session = _FakeGemSession(prose)
    t3.load_model()
    utts3 = t3.transcribe_diarized(audio, language=None)
    assert len(utts3) == 1 and utts3[0]["text"] == "just text", utts3


_test("GeminiTranscriber diarization request + utterance parsing (+ fallbacks)",
      t_gemini_diarize_parsing)


def t_gemini_meeting_registered():
    """Gemini meeting models wired: both keys gated behind gemini_api_key, the
    builder returns is_diarized correctly, the diarize dispatch pins the 'gemini'
    backend, and the wnote shows free-tier (not the hardcoded ~$0.4/hr)."""
    import inspect
    import lia as w
    App = w.LiaApp
    meet = {k: req for _l, k, req in App._MEETING_MODELS}
    assert meet.get("gemini_transcribe") == ["gemini_api_key"], list(meet)
    assert meet.get("gemini_diarize") == ["gemini_api_key"], list(meet)
    assert "gemini_transcribe" in App._MEETING_TRANSCRIBE_NAMES
    assert "gemini_diarize" in App._MEETING_TRANSCRIBE_NAMES
    # builder returns is_diarized = (key == gemini_diarize)
    bsrc = inspect.getsource(App._build_meeting_transcriber)
    assert 'key == "gemini_diarize"' in bsrc, "builder is_diarized mapping missing"
    assert "_ensure_gemini_transcriber" in bsrc
    # diarize backend dispatch maps gemini_diarize -> "gemini" (a LiaApp method)
    src = inspect.getsource(App)
    assert 'diarize_backend = "gemini"' in src, "gemini diarize_backend dispatch missing"
    # _run_diarize_job (module-level MeetingSession) has a gemini branch
    dsrc = inspect.getsource(w.MeetingSession._run_diarize_job)
    assert "is_gemini" in dsrc and "transcribe_diarized" in dsrc, "diarize job gemini branch missing"


_test("Gemini meeting models registered + diarize backend wired",
      t_gemini_meeting_registered)


def t_gemini_key_decrypt_and_error_surface():
    """2026-09-02 diarized-meeting failure: a dpapi:-encrypted key reaching the
    Gemini client was POSTed verbatim → 400 'API key not valid', and both the
    real reason AND the backend name were hidden (the card said 'AssemblyAI
    failed'). Regression guards:
    (1) the constructor defensively decrypts a dpapi: blob (idempotent on
        plaintext) so ciphertext never reaches Google;
    (2) _api_error_message surfaces Google's message + machine reason from both
        the object and list-wrapped error shapes;
    (3) _raise_for_api_error raises that detail on a 400 (not a bare status);
    (4) the diarize-job error mapping labels Gemini as Gemini and points key
        errors at the right Settings card."""
    import inspect
    import lia as w
    import secret_store

    # (1) plaintext passes through unchanged; a dpapi blob is decrypted.
    assert w.GeminiTranscriber(api_key="AIzaPlain").api_key == "AIzaPlain"
    assert w.GeminiTranscriber(api_key="").api_key == ""
    if secret_store.available():
        blob = secret_store.protect("AIzaSECRET-key-value")
        assert secret_store.is_protected(blob), "test setup: expected a dpapi blob"
        g = w.GeminiTranscriber(api_key=blob)
        assert g.api_key == "AIzaSECRET-key-value", "constructor must decrypt the blob"
        assert not g.api_key.startswith("dpapi:")

    # (2)/(3) real error surfaced from both response shapes.
    class _Resp:
        def __init__(self, code, payload=None, text=""):
            self.status_code = code
            self._payload = payload
            self.text = text
        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload
    err_obj = {"error": {"code": 400, "message": "API key not valid.",
                         "status": "INVALID_ARGUMENT",
                         "details": [{"reason": "API_KEY_INVALID"}]}}
    G = w.GeminiTranscriber
    m_list = G._api_error_message(_Resp(400, [err_obj]))     # list-wrapped
    m_obj = G._api_error_message(_Resp(400, err_obj))        # object
    assert "API key not valid." in m_list and "API_KEY_INVALID" in m_list, m_list
    assert "API key not valid." in m_obj and "API_KEY_INVALID" in m_obj, m_obj
    # non-JSON body falls back to text
    assert "boom" in G._api_error_message(_Resp(500, None, "boom"))
    t = G(api_key="fake")
    try:
        t._raise_for_api_error(_Resp(400, [err_obj]))
        assert False, "expected a raise on 400"
    except RuntimeError as e:
        assert "400" in str(e) and "API_KEY_INVALID" in str(e), str(e)
    for code, needle in ((401, "Invalid Gemini API key"),
                         (429, "rate limit")):
        try:
            t._raise_for_api_error(_Resp(code))
            assert False, "expected a raise on %d" % code
        except RuntimeError as e:
            assert needle in str(e), (code, str(e))
    assert t._raise_for_api_error(_Resp(200)) is None      # 2xx: no raise

    # (4) the diarize-job maps a Gemini key error to a Gemini-labelled message,
    #     never "AssemblyAI failed". Anchor on the except-block comment so the
    #     'elif is_gemini:' of the STAGE selection higher up isn't matched.
    dsrc = inspect.getsource(w.MeetingSession._run_diarize_job)
    err_block = dsrc[dsrc.index("Map known errors"):]
    assert "elif is_gemini:" in err_block, "diarize job lost its gemini error branch"
    gi = err_block.index("elif is_gemini:")
    gseg = err_block[gi:gi + 700]
    assert "Gemini key rejected" in gseg and "API Keys" in gseg, gseg[:300]
    assert "api_key_invalid" in gseg.lower()
    # the gemini branch must resolve to a Gemini-labelled message, never fall
    # through to the AssemblyAI label.
    assert "AssemblyAI" not in gseg[:gseg.index("elif", 5)], gseg[:300]


_test("Gemini: key decrypt guard + real API-error surfaced + backend-labelled",
      t_gemini_key_decrypt_and_error_surface)


def t_gemini_diarize_request_constraints():
    """More diarized-meeting failures surfaced once the real error was visible
    (2026-09-02): (1) custom_vocabulary + WORD timestamps is a hard 400
    ('incompatible with timestamps') - vocab must be dropped when word_ts is on,
    kept otherwise; (2) a 40-min meeting = ~7 requests on a ~25/min tier, so the
    diarized path retries a 429 with the server's retryDelay instead of aborting;
    (3) the SAVED transcript credits the actual diarizer (Gemini), never a blanket
    'AssemblyAI'."""
    import inspect
    import lia as w
    G = w.GeminiTranscriber
    t = G(api_key="fake")
    t.custom_vocabulary = "AWS, Bedrock, EKS"
    # (1) vocab present without word timestamps; DROPPED with them.
    tc_plain = t._build_transcription_config(language=None, diarize=False, word_ts=False)
    tc_word = t._build_transcription_config(language=None, diarize=True, word_ts=True)
    assert tc_plain.get("custom_vocabulary"), "vocab should be sent when no word ts"
    assert "custom_vocabulary" not in tc_word, \
        "vocab must be dropped with word timestamps (API rejects the combo)"
    assert tc_word["mode"].get("diarization_mode") == "speaker"
    assert tc_word["mode"].get("timestamp_granularities") == ["word"]

    # (2) retry-after parsing + a 429→200 retry (no real sleep).
    class _R:
        def __init__(self, code, payload=None, headers=None):
            self.status_code = code
            self._p = payload
            self.headers = headers or {}
            self.text = ""
        def json(self):
            if self._p is None:
                raise ValueError("no json")
            return self._p
    rl = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}
    assert G._retry_after_seconds(_R(429, rl)) == 7.0
    assert G._retry_after_seconds(_R(429, None, {"Retry-After": "12"})) == 12.0
    assert G._retry_after_seconds(_R(429, {"error": {}})) is None

    class _FakeSession:
        def __init__(self, responses):
            self.responses = responses
            self.calls = 0
        def post(self, *a, **k):
            r = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return r
    ok_payload = {"steps": [{"content": [{"type": "text", "text": "ok"}]}]}
    sess = _FakeSession([_R(429, rl), _R(200, ok_payload)])
    t._ensure_session = lambda: sess
    waits = []
    orig_sleep = time.sleep
    try:
        time.sleep = lambda s: waits.append(s)
        out = t._post_interaction_retrying(b"wavbytes", {}, (10, 10), attempts=3)
    finally:
        time.sleep = orig_sleep
    assert out == ok_payload, out
    assert sess.calls == 2, "should retry once then succeed"
    assert waits and 5.0 <= waits[0] <= 90.0, waits
    # a persistent 429 eventually raises the friendly message
    sess2 = _FakeSession([_R(429, rl)])
    t._ensure_session = lambda: sess2
    try:
        time.sleep = lambda s: None
        raised = False
        try:
            t._post_interaction_retrying(b"x", {}, (10, 10), attempts=2)
        except RuntimeError as e:
            raised = "rate limit" in str(e).lower()
    finally:
        time.sleep = orig_sleep
    assert raised, "persistent 429 must surface a rate-limit error"

    # the diarized loop uses the retrying POST.
    dsrc = inspect.getsource(G.transcribe_diarized)
    assert "_post_interaction_retrying" in dsrc, "diarize loop must retry 429s"

    # (3) saved transcript credits Gemini, not AssemblyAI.
    wsrc = inspect.getsource(w.MeetingSession._write_diarized_markdown)
    assert '"gemini": "Gemini 3.5 transcribe"' in wsrc, "diarizer label for gemini missing"
    assert "local_pyannote" in wsrc


_test("Gemini diarize: vocab/timestamp constraint + 429 retry + diarizer label",
      t_gemini_diarize_request_constraints)


def t_gemini_key_clear_reverts():
    """Clearing the Gemini key reverts a gemini meeting/file model to local and
    drops cached transcribers, so the picker never keeps a dead selection."""
    import lia as w
    App = w.LiaApp
    app = App.__new__(App)
    app.config = {"gemini_api_key": "AQ.fake", "meeting_model": "gemini_diarize",
                  "file_transcribe_model": "gemini_transcribe"}
    app._summary_cleaner = None
    app._gemini_transcriber = object()
    app._meeting_xcribers = {"gemini_diarize": ("x", None, True),
                             "gemini_transcribe": ("y", None, False)}
    app._make_cleanup_cleaner = lambda: None
    _saved = {}
    import lia as _w
    _orig_save = _w.save_config
    _w.save_config = lambda c: _saved.update(c)
    try:
        ok, msg = app._clear_credential("gemini")
    finally:
        _w.save_config = _orig_save
    assert ok
    assert app.config["gemini_api_key"] == ""
    assert app.config["meeting_model"] == "local_hebrew_turbo", app.config["meeting_model"]
    assert app.config["file_transcribe_model"] == "", app.config["file_transcribe_model"]
    assert app._gemini_transcriber is None
    assert "gemini_diarize" not in app._meeting_xcribers
    assert "gemini_transcribe" not in app._meeting_xcribers


_test("Gemini key-clear reverts meeting/file model + drops cache",
      t_gemini_key_clear_reverts)


def t_gemini_transcribe_live():
    """LIVE: the configured Gemini key authenticates against the transcribe model
    (verify_key). Skipped under LIA_SKIP_LIVE or when no key is set."""
    _skip_live("Gemini verify_key call")
    import lia as w
    cfg = w.load_config()
    key = (cfg.get("gemini_api_key") or "").strip()
    if not key:
        raise SkipTest("No Gemini API key configured")
    t = w.GeminiTranscriber(api_key=key)
    ok, msg = t.verify_key(timeout=15)
    assert ok, "verify_key failed: %s" % msg


_test("LIVE: Gemini key verifies against gemini-3.5-transcribe",
      t_gemini_transcribe_live)


class _FakeWS:
    """Minimal websocket-client stand-in: records sends, yields scripted recvs."""
    def __init__(self, script):
        self.script = list(script)   # list of JSON strings to hand back on recv()
        self.sent = []
    def send(self, s):
        self.sent.append(s)
    def recv(self):
        if not self.script:
            raise OSError("closed")
        return self.script.pop(0)
    def settimeout(self, t):
        pass
    def close(self):
        pass


def t_gemini_live_stream_protocol():
    """GeminiLiveStream builds the verified Live-API messages and parses the
    server transcript shapes (interim vs final)."""
    import json as _json
    import lia as w
    s = w.GeminiLiveStream(api_key="fake", language_codes=["he-IL", "en-US"])
    s._ws = _FakeWS([])
    # audio + end message shapes
    s.feed(b"\x01\x02\x03\x04")
    s.end_audio()
    a = _json.loads(s._ws.sent[0])
    assert a["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert a["realtimeInput"]["audio"]["data"], "missing base64 audio"
    b = _json.loads(s._ws.sent[1])
    assert b["realtimeInput"]["audioStreamEnd"] is True
    # _extract: interim, final, none
    assert w.GeminiLiveStream._extract(
        {"serverContent": {"interimInputTranscription": {"text": "hi"}}}) == ("hi", False)
    assert w.GeminiLiveStream._extract(
        {"serverContent": {"inputTranscription": {"text": "final"}}}) == ("final", True)
    assert w.GeminiLiveStream._extract({"serverContent": {}}) == (None, None)
    # collect_final over a scripted turn: interims then final then generationComplete
    seen = []
    s2 = w.GeminiLiveStream(api_key="fake")
    s2._ws = _FakeWS([
        _json.dumps({"serverContent": {"interimInputTranscription": {"text": "אני"}}}),
        _json.dumps({"serverContent": {"interimInputTranscription": {"text": "אני גם"}}}),
        _json.dumps({"serverContent": {"inputTranscription": {"text": "אני גם עובד"}}}),
        _json.dumps({"serverContent": {"generationComplete": True}}),
    ])
    final = s2.collect_final(on_interim=seen.append)
    assert final == "אני גם עובד", final
    assert seen == ["אני", "אני גם"], seen


_test("GeminiLiveStream protocol messages + transcript parsing",
      t_gemini_live_stream_protocol)


def t_gemini_dictation_registered():
    """The dictation Gemini backend is the BATCH 'gemini' (gemini-3.5-transcribe):
    a Menu row, startup selection, _set_backend + key-clear all handle it. The
    old streaming GeminiLiveTranscriber / gemini_live backend is gone (moved to
    backlog); GeminiLiveStream is kept as the foundation for that future work."""
    import inspect
    import lia as w
    App = w.LiaApp
    rows = {r[2] for r in App._MENU_MODELS_ORDERED}
    assert "gemini" in rows and "gemini_live" not in rows, rows
    assert not hasattr(w, "GeminiLiveTranscriber"), "GeminiLiveTranscriber should be removed"
    assert hasattr(w, "GeminiLiveStream"), "GeminiLiveStream kept for the backlog"
    src = inspect.getsource(App)
    assert 'backend == "gemini" and self._gemini_transcriber is not None' in src
    assert 'elif backend == "gemini":' in inspect.getsource(App._set_backend)
    # key-clear reverts a gemini dictation backend to local + drops the client
    app = App.__new__(App)
    app.config = {"gemini_api_key": "AQ.x", "transcription_backend": "gemini",
                  "meeting_model": "local_hebrew_turbo", "file_transcribe_model": ""}
    app._summary_cleaner = None
    app._gemini_transcriber = object()
    app._local_transcriber = "LOCAL"
    app._meeting_xcribers = {}
    app._make_cleanup_cleaner = lambda: None
    _orig = w.save_config
    w.save_config = lambda c: None
    try:
        ok, msg = app._clear_credential("gemini")
    finally:
        w.save_config = _orig
    assert ok
    assert app.config["transcription_backend"] == "local", app.config["transcription_backend"]
    assert app.transcriber == "LOCAL"
    assert app._gemini_transcriber is None


_test("gemini dictation backend (batch) registered + key-clear reverts",
      t_gemini_dictation_registered)


def t_audio_recorder_construct():
    import lia as w
    r = w.AudioRecorder(input_device_index=None)
    assert r.is_recording is False
    assert r.audio_data == []


_test("AudioRecorder constructs clean", t_audio_recorder_construct)


# ============================================================
# 6. GroqLLMCleaner — live API calls (requires key)
# ============================================================
section("6. GroqLLMCleaner Live Calls")


def _cleaner():
    _skip_live("Groq cleaner call")
    import lia as w
    cfg = w.load_config()
    key = cfg.get("groq_api_key", "")
    if not key:
        raise SkipTest("No Groq API key configured")
    return w.GroqLLMCleaner(api_key=key)


def t_cleaner_off_bypasses():
    cl = _cleaner()
    t = "אה אז אני בעצם"
    assert cl.clean(t, style="off") == t
    assert cl.clean(t, style="verbatim") == t


_test("cleaner: off/verbatim bypass without HTTP call", t_cleaner_off_bypasses)


def t_cleaner_short_bypassed():
    cl = _cleaner()
    # <4 chars → returns untouched
    assert cl.clean("hi", style="casual") == "hi"
    assert cl.clean("", style="casual") == ""


_test("cleaner: ultra-short text is not sent to LLM", t_cleaner_short_bypassed)


def t_cleaner_casual_hebrew():
    cl = _cleaner()
    raw = "אה אז אני הולק לפגישה אממ עם הצוות"
    out = cl.clean(raw, style="casual")
    # Casual is now typos-only: must fix הולק→הולך, but fillers stay
    # and overall length must stay within ±15% of the input.
    assert "הולך" in out, f"expected הולך in {out!r}"
    assert "אממ" in out, f"filler should be preserved in casual mode: {out!r}"
    length_ratio = len(out) / len(raw)
    assert 0.85 <= length_ratio <= 1.15, \
        f"casual mode should stay near-identical length (got ratio {length_ratio:.2f}): {out!r}"


_test("cleaner: casual fixes spelling typos without stripping fillers", t_cleaner_casual_hebrew)
def t_cleaner_spoken_selfcorrect():
    """Spoken style: strip fillers + resolve self-corrections, keep wording."""
    cl = _cleaner()
    raw = ("היום אני הולך לדבר על שימוש בתמלול על מנת לחסוך 20% מהזמן, "
           "אמממ בעצם 30% מהזמן שלי")
    out = cl.clean(raw, style="spoken")
    assert "30%" in out, f"kept corrected value: {out!r}"
    assert "20%" not in out, f"dropped abandoned value: {out!r}"
    assert "אמממ" not in out and "בעצם" not in out, f"filler/cue removed: {out!r}"
    assert "תמלול" in out and "לחסוך" in out, f"content words kept: {out!r}"
    assert len(out) <= len(raw) + 3, f"must not expand: {out!r}"
    # clean input stays essentially unchanged (no rewriting)
    clean_in = "זה משפט נקי לגמרי בלי שום מילת מילוי"
    out2 = cl.clean(clean_in, style="spoken")
    assert "משפט נקי" in out2 and len(out2) <= len(clean_in) + 3, out2


_test("cleaner: spoken strips fillers + resolves self-corrections (LIVE)",
      t_cleaner_spoken_selfcorrect)


def t_cleaner_casual_adds_question_mark():
    """Casual should convert a clear question ending with '.' or nothing to '?'."""
    cl = _cleaner()
    raw = "אז בעצם מה שאתה אומר לי, זה שנעשה תיקון בקוד ואז זה יעבוד."
    out = cl.clean(raw, style="casual")
    assert out.rstrip().endswith("?"), \
        f"casual should turn the trailing '.' into '?' for a question: {out!r}"


_test("cleaner: casual adds '?' to an unpunctuated question", t_cleaner_casual_adds_question_mark)


def t_cleaner_casual_fixes_word_boundary_mishear():
    """Casual should unpack 'בעלך' → 'בא לך' when the context is about wanting something."""
    cl = _cleaner()
    raw = "אם בעלך לעשות את זה בוא נעשה"
    out = cl.clean(raw, style="casual")
    assert "בא לך" in out, \
        f"expected 'בא לך' reconstruction from context: {out!r}"
    assert "בעלך" not in out, \
        f"joined form should have been replaced: {out!r}"


_test("cleaner: casual fixes 'בעלך' → 'בא לך' by context", t_cleaner_casual_fixes_word_boundary_mishear)


def t_cleaner_casual_fixes_nonword_to_real_word():
    """Casual should replace a non-existent Hebrew word with the phonetically
    similar real word when one exists — e.g. 'להיבלה' is not a Hebrew word,
    the intended word is 'להיבלע'."""
    cl = _cleaner()
    raw = "המילה עלולה להיבלה בתוך הרעש"
    out = cl.clean(raw, style="casual")
    assert "להיבלע" in out, \
        f"expected 'להיבלה' (non-word) → 'להיבלע' (real word): {out!r}"


_test("cleaner: casual fixes non-word Hebrew to real word", t_cleaner_casual_fixes_nonword_to_real_word)


def t_cleaner_expansion_blocked():
    """The AWS-expansion failure mode: must NOT expand >1.5×."""
    cl = _cleaner()
    raw = ("I want to review the code and refine it to create a standardized "
           "AWS Landing Zone. I want to examine the existing code and identify "
           "areas for improvement.")
    out = cl.clean(raw, style="code")
    ratio = len(out) / len(raw)
    assert ratio <= 1.5, f"expansion ratio {ratio:.2f}× > 1.5×! out={out!r}"


_test("cleaner: task-like input does NOT expand (was 6× before fix)", t_cleaner_expansion_blocked)


def t_cleaner_rtl_preserved():
    cl = _cleaner()
    raw = "\u200F" + "אה אני רוצה לבדוק את הקוד לפני שאני עושה commit"
    out = cl.clean(raw, style="casual")
    # Our code re-prepends the RTL marker if the input had it
    assert out.startswith("\u200F"), f"RTL marker lost: {out!r}"


_test("cleaner: U+200F RTL marker preserved across cleanup", t_cleaner_rtl_preserved)


def t_cleaner_vocab_fixes_mistranscription():
    cl = _cleaner()
    raw = "תן בגד פושע שלי"  # the 'git push' → 'בגד פושע' case
    vocab = "git, push, pull, commit, merge, branch"
    out = cl.clean(raw, style="casual", vocabulary=vocab)
    assert "git push" in out.lower() or "git" in out.lower(), \
        f"vocab didn't replace mistranscription: {out!r}"


_test("cleaner: vocab corrects 'בגד פושע' → 'git push'", t_cleaner_vocab_fixes_mistranscription)


# ============================================================
# 7. MeetingSession end-to-end
# ============================================================
section("7. Meeting Session End-to-end")


def t_meeting_markdown_output():
    import lia as w
    cfg = w.load_config()

    class MockApp:
        config = cfg
        _llm_cleaner = w.GroqLLMCleaner(api_key=cfg.get("groq_api_key", ""))

        def _transcribe_with_fallback(self, *a, **kw):
            return "mock"

        def _get_language(self):
            return "he"

        def _vocab_apply_corrections(self, text, **kw):
            return text

        def _vocab_take_corrections(self, **kw):
            return []

    session = w.MeetingSession(MockApp())
    session.start_time = time.time() - 120
    session.stop_time = time.time()
    # Title is resolved at stop in production; set it here so _write_output_file
    # doesn't block on _title_event and so we can assert it lands in the filename.
    session.title = "פגישת אטלס"
    session._title_event.set()
    with session._chunks_lock:
        session.chunks = [
            {"index": 0, "timestamp_rel": 0, "text": "דיברנו על פרויקט אטלס", "status": "ok"},
            {"index": 1, "timestamp_rel": 45, "text": "", "status": "failed"},
            {"index": 2, "timestamp_rel": 90, "text": "נאור יסיים עד יום שלישי", "status": "ok"},
        ]
    path = session._write_output_file(120)
    assert os.path.exists(path)
    assert path.endswith(".txt"), f"expected .txt file, got {path}"
    # Chunked (non-diarized) meetings must carry the title in the filename too.
    assert "פגישת אטלס" in os.path.basename(path), \
        f"meeting title missing from chunked filename: {os.path.basename(path)}"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Meeting —" in content
    assert "Full transcript" in content
    assert "פרויקט אטלס" in content
    assert "transcription failed" in content
    # No markdown markup should survive into the .txt
    assert "# Meeting" not in content
    assert "## " not in content
    # Cleanup the test output file
    try:
        os.remove(path)
    except Exception:
        pass


_test("MeetingSession writes well-formed transcript file", t_meeting_markdown_output)


def t_chunk_silence_not_failed():
    """A near-silent chunk (empty text, low energy) is OMITTED — NOT mislabeled
    '[transcription failed]', which stays reserved for real errors / lost speech."""
    import lia as w
    import numpy as np
    import threading
    # _rms_energy classifier: silence vs speech, int16 normalisation.
    assert w._rms_energy(np.zeros(16000, dtype=np.float32)) < 0.005
    assert w._rms_energy((np.random.RandomState(0).randn(16000) * 0.05
                          ).astype(np.float32)) >= 0.005
    assert w._rms_energy(np.zeros(1000, dtype=np.int16)) == 0.0
    # Assembler: 'empty' omitted, 'failed' keeps the marker, 'ok' kept.
    s = w.MeetingSession.__new__(w.MeetingSession)
    s._chunks_lock = threading.Lock()
    s.chunks = [
        {"index": 0, "timestamp_rel": 0,   "text": "שלום",  "status": "ok"},
        {"index": 1, "timestamp_rel": 45,  "text": "",      "status": "empty"},
        {"index": 2, "timestamp_rel": 90,  "text": "",      "status": "failed"},
        {"index": 3, "timestamp_rel": 135, "text": "סיכום", "status": "ok"},
    ]
    md = w.MeetingSession._assemble_transcript_markdown(s)
    assert "שלום" in md and "סיכום" in md
    assert md.count("[transcription failed]") == 1, "only the real failure is marked"
    # Summary input only takes 'ok' chunks — silence/failure never reach the LLM.
    assert w.MeetingSession._assemble_transcript_plain(s) == "שלום\nסיכום"


_test("meeting: silent chunk omitted, not '[transcription failed]'", t_chunk_silence_not_failed)


def t_meeting_timestamp_format():
    import lia as w
    assert w._fmt_relative_ts(0) == "0:00"
    assert w._fmt_relative_ts(45) == "0:45"
    assert w._fmt_relative_ts(125) == "2:05"
    assert w._fmt_relative_ts(3725) == "1:02:05"


_test("meeting timestamp formatter correct", t_meeting_timestamp_format)


# ============================================================
# 8. Paste / Undo state machine
# ============================================================
section("8. Paste / Undo State Machine")


def t_copy_with_retry():
    import lia as w
    import pyperclip
    saved = ""
    try:
        saved = pyperclip.paste() or ""
    except Exception:
        pass
    try:
        ok, msg = w._copy_with_retry("hello world", retries=3)
        assert ok, msg
        assert pyperclip.paste() == "hello world"
    finally:
        # Put the user's REAL clipboard back — the suite used to leave
        # 'hello world' in it.
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


_test("_copy_with_retry writes + verifies readback", t_copy_with_retry)


def t_undo_state_consumed():
    import lia as w
    import pyperclip

    saved_clip = ""
    try:
        saved_clip = pyperclip.paste() or ""
    except Exception:
        pass

    app = w.LiaApp.__new__(w.LiaApp)
    app.config = w.DEFAULT_CONFIG.copy()
    app._last_paste = None
    app._last_paste_lock = threading.Lock()

    class NoopOverlay:
        def show(self, *a, **kw): pass
        def show_error(self, *a, **kw): pass
    app.overlay = NoopOverlay()

    # Simulate a paste recorded
    app._last_paste = {
        "text": "TEST", "old_clipboard": "PREV",
        "timestamp": time.time(),
    }
    # Mock keyboard module so we don't actually send Ctrl+Z
    import sys as _sys
    class FakeKB:
        def send(self, *a, **kw): pass
    real_kb = _sys.modules.get("keyboard")
    _sys.modules["keyboard"] = FakeKB()
    try:
        pyperclip.copy("pasted")
        app._undo_last_paste()
        assert app._last_paste is None, "undo should clear state"
        assert pyperclip.paste() == "PREV", "clipboard should restore to PREV"
        # Second undo should be no-op
        app._undo_last_paste()
    finally:
        if real_kb:
            _sys.modules["keyboard"] = real_kb
        try:
            pyperclip.copy(saved_clip)   # leave the user's clipboard intact
        except Exception:
            pass


_test("undo consumes state + restores clipboard", t_undo_state_consumed)


def t_undo_stale_refused():
    import lia as w

    app = w.LiaApp.__new__(w.LiaApp)
    app.config = w.DEFAULT_CONFIG.copy()
    app._last_paste_lock = threading.Lock()

    class NoopOverlay:
        def show(self, *a, **kw): pass
        def show_error(self, *a, **kw): pass
    app.overlay = NoopOverlay()

    app._last_paste = {
        "text": "OLD", "old_clipboard": "prev",
        "timestamp": time.time() - 120,  # 2 min ago
    }
    import sys as _sys
    class FakeKB:
        def send(self, *a, **kw):
            raise RuntimeError("should NOT be called on stale undo")
    real_kb = _sys.modules.get("keyboard")
    _sys.modules["keyboard"] = FakeKB()
    try:
        app._undo_last_paste()  # should log warning but not raise
        # state IS cleared (we consume it regardless — can't replay)
    finally:
        if real_kb:
            _sys.modules["keyboard"] = real_kb


_test("undo: stale paste (>60s) refused gracefully", t_undo_stale_refused)


# ============================================================
# 9. Hotkey validation
# ============================================================
section("9. Hotkey Validation")


def t_hotkey_valid():
    import lia as w
    assert w._validate_hotkey("ctrl+space")
    assert w._validate_hotkey("ctrl+alt+z")
    assert w._validate_hotkey("win+h")
    assert w._validate_hotkey("shift+f1")
    assert w._validate_hotkey("f13")  # function key alone OK


_test("hotkey validator accepts modifier+key + F-keys", t_hotkey_valid)


def t_hotkey_invalid():
    import lia as w
    assert not w._validate_hotkey("")
    assert not w._validate_hotkey("a")
    assert not w._validate_hotkey("space")
    assert not w._validate_hotkey("b")


_test("hotkey validator rejects bare keys without modifier", t_hotkey_invalid)


# ============================================================
# 10. Icon generation
# ============================================================
section("10. Icon Generation")


def t_icons_all_states():
    import lia as w
    app = w.LiaApp.__new__(w.LiaApp)
    for state in ["idle", "recording", "processing", "loading", "error", "meeting"]:
        img = app._create_icon(state)
        assert img is not None
        # Square, tray-sane size — 64 (drawn fallback) or 128 (shipped PNG art).
        assert img.size[0] == img.size[1] and img.size[0] in (64, 128), \
            f"{state} size wrong: {img.size}"


_test("all 6 tray icon states render successfully", t_icons_all_states)


# ============================================================
# 11. Summarisation (gpt-5.5 engine + meeting flag + wiring)
# ============================================================
section("11. Summarisation")


def t_summary_config_and_dir():
    import lia as w
    # Assert the DEFAULT (not the user's live config.json — asserting the
    # user's choice made the suite go red the day they repoint the model).
    assert w.DEFAULT_CONFIG.get("summary_model"), "summary_model default missing"
    cfg = w.load_config()
    assert "summary_model" in cfg
    assert hasattr(w, "SUMMARIES_DIR")
    assert w.SUMMARIES_DIR.endswith("summaries")


_test("config has a summary_model + SUMMARIES_DIR exists", t_summary_config_and_dir)


def t_summary_methods_exist():
    import lia as w
    for m in ("_run_summary", "_get_summary_cleaner", "_summarize_text_dialog",
              "_do_text_summary", "_save_summary_to_file"):
        assert hasattr(w.LiaApp, m), f"LiaApp missing {m}"
    for m in ("show_summary", "_create_summary_toplevel",
              "show_summarize_input", "_create_summarize_input_toplevel"):
        assert hasattr(w.OverlayNotification, m), f"OverlayNotification missing {m}"
    assert hasattr(w.GroqLLMCleaner, "summarize")
    assert hasattr(w.OpenAILLMCleaner, "summarize")  # inherited
    assert isinstance(w._SUMMARY_PROMPT_MEETING, str)
    assert isinstance(w._SUMMARY_PROMPT_GENERAL, str)


_test("summary engine + popup + dialog methods all present", t_summary_methods_exist)


class _FakeResp:
    status_code = 200

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}], "usage": {}}


def t_summary_shaping_gpt5():
    """gpt-5.x must use max_completion_tokens and OMIT max_tokens/temperature."""
    import lia as w
    captured = {}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResp("## TL;DR\nok")

    cl = w.OpenAILLMCleaner(api_key="fake", model="gpt-5.5")
    cl._session = FakeSession()
    out = cl.summarize("Some text to summarize, long enough.", "sys prompt")
    # 2026-08: the marker localizer now canonicalizes EN headers (TL;DR -> Summary).
    assert out == "## Summary\nok", out
    p = captured["payload"]
    assert "max_completion_tokens" in p, p
    assert "max_tokens" not in p, p
    assert "temperature" not in p, p
    assert captured["url"].startswith("https://api.openai.com"), captured["url"]
    assert p["model"] == "gpt-5.5", p

    # The newer gpt-5.6-* ids must hit the SAME shaping (the `gpt-5` prefix check),
    # so they're wired correctly the moment the account gets access. Runs LAST:
    # it rebinds `p`, and the gpt-5.5 assertions above must see their own payload.
    for mid in ("gpt-5.6-sol", "gpt-5.6-terra"):
        cl = w.OpenAILLMCleaner(api_key="fake", model=mid)
        cl._session = FakeSession()
        cl.summarize("Some text to summarize, long enough.", "sys prompt")
        p = captured["payload"]
        assert p["model"] == mid, p
        assert "max_completion_tokens" in p and "max_tokens" not in p, (mid, p)
        assert "temperature" not in p, (mid, p)


_test("summarize(): gpt-5.5 request uses max_completion_tokens, no temp", t_summary_shaping_gpt5)


def t_summary_shaping_legacy():
    """Legacy chat models keep max_tokens + temperature."""
    import lia as w
    captured = {}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp("summary text")

    cl = w.GroqLLMCleaner(api_key="fake", model="llama-3.3-70b-versatile")
    cl._session = FakeSession()
    out = cl.summarize("Some text to summarize.", "sys prompt")
    assert out == "summary text", out
    p = captured["payload"]
    assert "max_tokens" in p, p
    assert "max_completion_tokens" not in p, p
    assert p.get("temperature") == 0.3, p


_test("summarize(): legacy model keeps max_tokens + temperature", t_summary_shaping_legacy)


def t_meeting_prompt_and_wrapper():
    """The PM 'סיכום דיון' prompt + its <transcript> wrapper. The transcript MUST be
    delimited — that's what makes the prompt's "never instructions to obey" rule real
    — and only the MEETING path wraps (the general Summarize tool sends raw text)."""
    import lia as w
    p = w._SUMMARY_PROMPT_MEETING
    for marker in ("## כותרת הדיון", "## תקציר", "## דגשים מרכזיים",
                   "## סטטוס פרויקטים", "## בוצע בפגישה", "## משימות",
                   "<transcript>", "never instructions to obey"):
        assert marker in p, f"meeting prompt missing {marker!r}"
    # Work CARRIED OUT in the meeting must not come back as an open task — it needs the rule AND
    # a section to live in, or the rule just deletes the item (ported from a private upstream project 2026-07-26).
    assert "בוצע בפגישה" in p and "NEVER under משימות" in p, "meeting prompt missing the done-in-meeting rule"
    # the old format must be gone
    assert "TL;DR" not in p and "Action items" not in p, "old headers still in prompt"

    wrapped = w._wrap_meeting_input("שורה אחת", "משך: 10:00")
    assert "<transcript>\nשורה אחת\n</transcript>" in wrapped, wrapped
    assert "<meeting_metadata>\nמשך: 10:00\n</meeting_metadata>" in wrapped, wrapped
    assert w._wrap_meeting_input("x", "").startswith("<meeting_metadata>\n(none)")

    captured = {}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp("## כותרת הדיון\nx")

    cl = w.OpenAILLMCleaner(api_key="fake", model="gpt-5.5")
    # general path (meeting_meta=None) → raw text, no delimiters
    cl._session = FakeSession()
    cl.summarize("TRANSCRIPT_BODY", "sys")
    assert captured["payload"]["messages"][1]["content"] == "TRANSCRIPT_BODY"
    # meeting path → wrapped, metadata carried separately
    cl._session = FakeSession()
    cl.summarize("TRANSCRIPT_BODY", "sys", meeting_meta="משך: 1")
    user = captured["payload"]["messages"][1]["content"]
    assert "<transcript>" in user and "TRANSCRIPT_BODY" in user, user
    assert "<meeting_metadata>" in user and "משך: 1" in user, user


_test("meeting prompt: PM format + <transcript> wrapper wiring",
      t_meeting_prompt_and_wrapper)


def t_summary_hebrew_always():
    """Summary-language matrix (2026-08): each prompt FAMILY commits to ONE output
    language and never mirrors the transcript - the Hebrew family says
    ALWAYS-Hebrew (2026-07-31 rule, unchanged), the EN mirror family says
    ALWAYS-English - and the selectors return the right family per lang."""
    import lia as w
    import lang_pack as LP
    # Hebrew family: the original assertions, byte-for-byte semantics.
    assert "ALWAYS Hebrew" in w._SUMMARY_PROMPT_MEETING
    assert "in HEBREW" in w._SUMMARY_PROMPT_GENERAL
    assert "## תקציר" in w._SUMMARY_PROMPT_GENERAL, "general prompt lost Hebrew headers"
    assert "HEBREW" in w._SUMMARY_PROMPT_MAP
    for p in (w._SUMMARY_PROMPT_GENERAL, w._SUMMARY_PROMPT_MAP):
        assert "PRIMARY LANGUAGE" not in p, "old follow-the-text-language rule survives"
    # English family: the mirror commitments + the canonical EN headers.
    assert "ALWAYS English" in LP.SUMMARY_PROMPT_MEETING_EN
    assert "in ENGLISH" in LP.SUMMARY_PROMPT_GENERAL_EN
    assert "## Summary" in LP.SUMMARY_PROMPT_GENERAL_EN
    assert "ENGLISH" in LP.SUMMARY_PROMPT_MAP_EN
    for hdr in ("## Discussion Title", "## Summary", "## Key Highlights",
                "## Project Status", "## Done in This Meeting", "## Tasks"):
        assert hdr in LP.SUMMARY_PROMPT_MEETING_EN, "EN meeting prompt lost " + hdr
    # The EN meeting prompt mirrors every load-bearing rule block of the GOLD.
    for rule in ("TONE:", "SUPERSEDED", "NAMES ARE THE POINT", "NO SILENT DROPS",
                 "DONE IN THE MEETING", "«NT_UP»", "PERSON, NOT ENTITY",
                 "Owner: <name> | Due: <date> | Status: <status>"):
        assert rule in LP.SUMMARY_PROMPT_MEETING_EN, "EN meeting prompt lost " + rule
    # Selectors return the right family (rendered with the notetaker identity,
    # so compare against the rendered template - not object identity).
    assert w._p_summary_meeting("he") == w._render_nt(w._SUMMARY_PROMPT_MEETING)
    assert w._p_summary_meeting("en") == w._render_nt(LP.SUMMARY_PROMPT_MEETING_EN)
    assert w._p_summary_general("en") == w._render_nt(LP.SUMMARY_PROMPT_GENERAL_EN)
    assert w._p_tasks_pass("en") == w._render_nt(LP.LOCAL_TASKS_PASS_PROMPT_EN)
    assert w._p_consolidate("en") == w._render_nt(LP.CONSOLIDATE_PROMPT_EN)
    assert w._p_task_done("en") == w._render_nt(LP.TASK_DONE_PROMPT_EN)
    assert w._p_parity_addendum("en") == w._render_nt(LP.CLOUD_PARITY_ADDENDUM_EN)
    # No Hebrew may leak into any EN generation prompt.
    for p in (LP.SUMMARY_PROMPT_MEETING_EN, LP.SUMMARY_PROMPT_GENERAL_EN,
              LP.SUMMARY_PROMPT_MAP_EN, LP.LOCAL_TASKS_PASS_PROMPT_EN,
              LP.CONSOLIDATE_PROMPT_EN, LP.TASK_DONE_PROMPT_EN,
              LP.CLOUD_PARITY_ADDENDUM_EN, LP.SPEAKER_NAME_PASS_PROMPT_EN):
        assert not any("֐" <= c <= "׿" for c in p), "Hebrew leaked into an EN prompt"


_test("summaries: language matrix (he ALWAYS-Hebrew, en ALWAYS-English, selectors)",
      t_summary_hebrew_always)


def t_summary_english_pipeline():
    """The FULL deterministic backstop chain works on an ENGLISH summary the
    way it does on a Hebrew one: title backstop, speaker-label owners, tone,
    paren-owners, task dedup, done marks, tasks-section merge, HTML direction.
    Mirrors the Hebrew coverage; every helper takes lang='en' here."""
    import lia as w
    # 1. title backstop demotes an unknown first header under the EN title.
    fixed = w._fix_title_header("## Budget sync for Q3\nbody\n", "en")
    assert fixed.startswith("## Discussion Title\nBudget sync for Q3"), fixed
    # known EN section headers are NOT demoted.
    same = "## Summary\ntext\n"
    assert w._fix_title_header(same, "en") == same
    # 2. speaker-label owners stripped (EN 'Owner:' + Speaker label).
    s = "- [ ] send the report - Owner: Speaker B"
    assert "Owner:" not in w._strip_speaker_label_owners(s)
    keep = "- [ ] send the report - Owner: Dana"
    assert w._strip_speaker_label_owners(keep) == keep
    # 3. tone softening: EN frames only on prose lines.
    toned = w._soften_tone("a dispute arose about the budget", "en")
    assert "dispute" not in toned and "discussion" in toned, toned
    task_line = "- [ ] resolve the vendor dispute over licensing"
    assert w._soften_tone(task_line, "en") == task_line, "task lines are immune"
    # 4. paren owner promoted to an EN dash-form owner.
    p = w._normalize_paren_owners("- [ ] ship the fix (Owner: Dana)", "en")
    assert p.endswith("- Owner: Dana") and "(" not in p, p
    # 5. tasks-section machinery on EN headers: span + merge + dedupe.
    summary = ("## Discussion Title\nT\n\n## Summary\nS\n\n"
               "## Tasks\n- [ ] old task\n")
    span = w._tasks_section_span(summary)
    assert span and "old task" in summary[span[0]:span[1]]
    merged = w._merge_tasks_section(
        summary, "- [ ] new task - Owner: Dana\n- [ ] second", "en")
    assert "old task" not in merged and "new task" in merged
    assert "## Tasks" in merged and "## משימות" not in merged
    # dedupe keeps the richer (owner-carrying) variant of a near-duplicate.
    dup = ("## Tasks\n- [ ] send the summary to the client\n"
           "- [ ] send the summary to the client - Owner: Dana\n")
    deduped = w._dedupe_tasks_section(dup)
    assert deduped.count("send the summary") == 1 and "Owner: Dana" in deduped
    # 6. done votes + marks in EN ('N: done', marker, bot-line immunity).
    votes = w._parse_done_votes("1: done\n2: nope\nnone", 3)
    assert votes == {1}
    body = ("## Tasks\n- [ ] configure the firewall\n"
            "- [ ] Request for the AI assistant (Lia): share notes\n")
    marked = w._apply_done_marks(body, {1, 2}, "en")
    assert "- [x] configure the firewall - done during the meeting" in marked
    assert "- [ ] Request for the AI assistant" in marked, "bot line must stay open"
    # 7. condense guards, EN: new completion claim / Hebrew leak both trip.
    assert w._condense_guard("decided to set up the VPN", "the VPN was deployed",
                             set(), 0.3, 2.0, "en") is None
    assert w._condense_guard("keep the budget", "keep the תקציב budget",
                             set(), 0.3, 2.0, "en") is None
    ok = w._condense_guard("- a\n- b\n- c", "- a\n- b and c", set(), 0.3, 1.2, "en")
    assert ok is not None
    # 8. EN header canonicalization (marker localization, EN direction).
    canon = w._localize_summary_markers("## Action items\n- [ ] do a thing\nplain english text")
    assert "## Tasks" in canon, canon
    # 9. HTML direction follows the summary content.
    html_en = w._summary_to_html("## Summary\nAll in English here")
    assert 'dir="ltr"' in html_en and 'lang="en"' in html_en
    html_he = w._summary_to_html("## תקציר\nסיכום בעברית")
    assert 'dir="rtl"' in html_he and 'lang="he"' in html_he
    # 10. metadata labels follow the summary language.
    class _S:
        source = "both"
        attendees = ["Dana Cohen"]
    meta = w.MeetingSession._meta_for_summary(_S(), 120, 3, lang="en")
    assert "Recording duration" in meta and "Calendar invitees" in meta
    assert not any("֐" <= c <= "׿" for c in meta)
    # 11. english near-dup morphology (plural tail) in summary_dedup.
    import summary_dedup as d
    assert d.is_near_duplicate("configure the VLANs for the lab",
                               "configure the VLAN for the lab")
    assert not d.is_near_duplicate("configure VLAN 10 for the lab",
                                   "configure VLAN 20 for the lab")


_test("summaries: ENGLISH pipeline (backstops, tasks machinery, HTML, metadata)",
      t_summary_english_pipeline)


def t_parakeet_registration():
    """Parakeet (2026-08): the English local backend is registered at every
    site the plumbing map lists - model tables, dictation/meeting/file
    pickers, transcribe-names, enhance map, config default, requirements -
    and the class honors the BaseTranscriber contract without loading."""
    import lia as w
    # model tables
    assert w.MODELS.get("parakeet-tdt-0.6b-v2"), "MODELS entry missing"
    assert w.MODEL_LANGUAGE.get("parakeet-tdt-0.6b-v2") == "en"
    # dictation picker row (local backend, no translate, no openai model)
    row = next((r for r in w.LiaApp._MENU_MODELS_ORDERED
                if r[1] == "parakeet-tdt-0.6b-v2"), None)
    assert row and row[2] == "local" and row[3] is False and row[4] == ""
    # meeting + file keys (file picker inherits _MEETING_MODELS)
    keys = [k for _, k, _ in w.LiaApp._MEETING_MODELS]
    assert "local_parakeet_english" in keys and "local_pyannote_parakeet" in keys
    reqs = {k: r for _, k, r in w.LiaApp._MEETING_MODELS}
    assert reqs["local_parakeet_english"] == [] and reqs["local_pyannote_parakeet"] == []
    for k in ("local_parakeet_english", "local_pyannote_parakeet"):
        assert k in w.LiaApp._MEETING_TRANSCRIBE_NAMES, k
    # enhance map + diarize backend + file diarize route (source-level checks)
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert '"local_pyannote_parakeet": "local_parakeet_english"' in src
    assert src.count('"local_pyannote_parakeet")') >= 1  # diarize_backend tuple
    # config default + availability probe + packaging
    assert w.DEFAULT_CONFIG.get("parakeet_device") == "cpu"
    assert isinstance(w.LiaApp._parakeet_available(), bool)
    base = os.path.dirname(os.path.abspath(w.__file__))
    assert "onnx-asr" in open(os.path.join(base, "requirements.txt"),
                              encoding="utf-8").read()
    import download_models as dm
    assert "parakeet-tdt-0.6b-v2" in dm.MODELS
    # contract, without loading anything
    t = w.ParakeetTranscriber()
    assert t.model is None and hasattr(t, "custom_vocabulary")
    assert hasattr(t, "_infer_lock")
    try:
        t.transcribe(__import__("numpy").zeros(16000, dtype="float32"))
        assert False, "must raise when model not loaded"
    except RuntimeError:
        pass
    try:
        t.transcribe(__import__("numpy").zeros(16000, dtype="float32"),
                     task="translate")
        assert False, "must refuse translate"
    except RuntimeError as e:
        assert "translate" in str(e)
    # the local factory routes parakeet model ids to the new class
    app = w.LiaApp.__new__(w.LiaApp)
    app.config = {"cpu_threads": 4, "parakeet_device": "cpu"}
    made = w.LiaApp._make_local_transcriber(app, "parakeet-tdt-0.6b-v2")
    assert isinstance(made, w.ParakeetTranscriber), type(made)


_test("parakeet: registration N/N + BaseTranscriber contract", t_parakeet_registration)


def t_primary_language_cascade():
    """The one-click language switch (2026-08): applies the language-flavored
    LOCAL model defaults (flavor preserved), flips the he/en bias, upgrades
    the router's en child - and leaves cloud choices + everything else alone.
    Round-trips cleanly. First-run locale derivation is present."""
    import inspect
    import lia as w
    if not w.LiaApp._parakeet_available():
        raise SkipTest("onnx-asr not installed")
    app = w.LiaApp.__new__(w.LiaApp)
    app.config = {"primary_language": "he",
                  "model_size": "ivrit-ai/whisper-large-v3-turbo-ct2",
                  "transcription_backend": "local",
                  "meeting_model": "local_pyannote_hebrew",
                  "file_transcribe_model": "",
                  "groq_he_en_bias": True}
    app._groq_transcriber = None
    app._openai_transcriber = None
    calls = []
    app._set_model = calls.append
    ok, msg = w.LiaApp._set_primary_language(app, "en")
    assert ok and "English" in msg, msg
    assert app.config["meeting_model"] == "local_pyannote_parakeet", \
        "diarized flavor must be preserved"
    assert app.config["groq_he_en_bias"] is False
    assert app.config["bilingual_english_model"] == "parakeet"
    assert calls == ["parakeet-tdt-0.6b-v2"], calls
    # round-trip back to Hebrew restores the he defaults
    app.config["model_size"] = "parakeet-tdt-0.6b-v2"   # as _set_model would
    calls.clear()
    ok, _ = w.LiaApp._set_primary_language(app, "he")
    assert ok
    assert app.config["meeting_model"] == "local_pyannote_hebrew"
    assert app.config["groq_he_en_bias"] is True
    assert calls == ["ivrit-ai/whisper-large-v3-turbo-ct2"], calls
    # a CLOUD dictation backend is never switched by the cascade
    app.config.update({"transcription_backend": "openai",
                       "model_size": "ivrit-ai/whisper-large-v3-turbo-ct2"})
    calls.clear()
    w.LiaApp._set_primary_language(app, "en")
    assert calls == [], "cloud dictation must not be switched"
    # an explicit cloud meeting model is left alone too
    app.config.update({"primary_language": "he",
                       "meeting_model": "openai_gpt_transcribe"})
    w.LiaApp._set_primary_language(app, "en")
    assert app.config["meeting_model"] == "openai_gpt_transcribe"
    # unknown language rejected
    ok, _ = w.LiaApp._set_primary_language(app, "fr")
    assert ok is False
    # first-run locale derivation + en first-run model defaults (source check)
    src = inspect.getsource(w.load_config)
    assert "GetUserDefaultUILanguage" in src and "0x0D" in src
    assert '"local_hebrew_turbo": "local_parakeet_english"' in src
    # chat window: an en-primary user gets the EN default system prompt
    import lang_pack as LP
    assert "English" in LP.CHAT_SYSTEM_EN
    chat_src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                                 "lia.py"), encoding="utf-8").read()
    assert "lang_pack.CHAT_SYSTEM_EN" in chat_src


_test("language switch: one-click cascade + locale first-run", t_primary_language_cascade)


def t_meetings_consumers_bilingual():
    """2026-08: the meeting-file CONSUMERS parse both languages - action_items
    finds an English Tasks section, meetings_index falls back to the EN
    discussion title, intent routing catches English queries, and the
    action-ask empty state answers in the question's language."""
    import tempfile
    import action_items as ai
    import meetings_index as mi
    # action_items: an ENGLISH meeting file round-trips through the parser.
    en_file = (
        "Meeting — 2026-08-28 10:00\n"
        "Duration: 10m 0s · 2 chunks · source: both\n\n"
        "─" * 60 + "\n"
        "Summary\n" + "─" * 60 + "\n"
        "Discussion Title\nBudget sync\n\n"
        "Tasks\n- [ ] send the budget to Dana\n- [x] book the room\n\n"
        + "═" * 60 + "\n"
        "Full transcript\n" + "═" * 60 + "\n"
        "[00:00] Speaker A: hello\n")
    fd, p = tempfile.mkstemp(suffix="_meeting.txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(en_file)
    try:
        items = ai.parse_action_items(p)
        assert items == ["send the budget to Dana", "book the room"], items
        assert "משימות" in ai._TASK_HEADERS and "Tasks" in ai._TASK_HEADERS
        meta = mi.parse_meeting_file(p)
        assert meta and meta["subject"] == "Budget sync", meta
    finally:
        os.unlink(p)
    # intent routing catches English phrasings.
    assert mi.classify_intent("what are my open tasks") == "action"
    assert mi.classify_intent("summarize the progress on Atlas across meetings") == "synthesis"
    assert mi.classify_intent("when is the VPN cutover") == "factual"
    assert mi.classify_intent("מה המשימות הפתוחות") == "action"
    # empty action-ask answers in the question's language (source check
    # for the branch; running it needs an empty meetings dir).
    import inspect
    src = inspect.getsource(mi._ask_actions)
    assert "No open action items" in src and "לא נמצאו משימות" in src


_test("meetings consumers: bilingual parsing (action items, title, intent)",
      t_meetings_consumers_bilingual)


def t_parakeet_live():
    """LIVE: transcribe a Windows-SAPI-generated English WAV with the real
    Parakeet int8 model (cached; downloads ~670MB on the very first run)."""
    _skip_live("Parakeet local inference")
    import importlib.util
    if importlib.util.find_spec("onnx_asr") is None:
        raise SkipTest("onnx-asr not installed")
    import tempfile
    import numpy as np
    import lia as w
    wav = os.path.join(tempfile.gettempdir(), "lia_parakeet_test.wav")
    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format.Type = 34   # SAFT16kHz16BitMono
        stream.Open(wav, 3)
        voice.AudioOutputStream = stream
        voice.Speak("The quick brown fox jumps over the lazy dog near the river bank.")
        stream.Close()
    except Exception as e:
        raise SkipTest(f"SAPI TTS unavailable: {e}")
    t = w.ParakeetTranscriber()
    t.load_model()
    text = t.transcribe_file(wav)
    low = text.lower()
    for word in ("quick", "brown", "fox", "lazy", "river"):
        assert word in low, (word, text)


_test("parakeet: LIVE local English transcription (SAPI wav)", t_parakeet_live)


def t_lang_pack():
    """lang_pack (english-support (2026-08) P0): the marker table is complete in both
    languages, detection/resolution behave, the union alternations really match
    BOTH generated forms, and the new config keys exist with sane defaults."""
    import re
    import inspect
    import lang_pack as LP
    import lia as w
    # 1. table completeness - every canonical id exists in BOTH languages.
    he_keys, en_keys = set(LP.TERMS["he"]), set(LP.TERMS["en"])
    assert he_keys == en_keys, "TERMS he/en key mismatch: %s" % (he_keys ^ en_keys)
    for k in he_keys:
        assert LP.TERMS["he"][k] and LP.TERMS["en"][k], "empty term: " + k
    # 2. detection: Hebrew-majority -> he, English -> en, Latin-heavy mixed -> en.
    assert LP.detect_text_lang("שלום, נדבר על ה-VPN מחר") == "he"
    assert LP.detect_text_lang("We will discuss the VPN tomorrow") == "en"
    assert LP.detect_text_lang("") == "en"  # no letters at all -> en (neutral)
    # 3. resolution matrix.
    assert LP.resolve_summary_lang({"summary_language": "he"}, "abc") == "he"
    assert LP.resolve_summary_lang({"summary_language": "en"}, "שלום") == "en"
    assert LP.resolve_summary_lang(
        {"summary_language": "auto"}, "שלום לכולם") == "he"
    assert LP.resolve_summary_lang(
        {"summary_language": "auto"}, "hello everyone") == "en"
    assert LP.resolve_summary_lang(
        {"summary_language": "primary", "primary_language": "en"}, "שלום") == "en"
    assert LP.resolve_summary_lang({}, "hello") == "he", \
        "default must be primary->he (today's behavior)"
    # 4. chars/token: English must be roughly double Hebrew (window sizing).
    assert LP.chars_per_token("he") == 1.9
    assert LP.chars_per_token("en") > LP.chars_per_token("he") * 1.8
    assert LP.chars_per_token("??") == 1.9  # unknown -> Hebrew-safe
    # 5. union alternations match BOTH generated forms.
    hdr_re = re.compile(r"^## " + LP.TASKS_HDR_ALT + r"\s*$")
    assert hdr_re.match("## משימות") and hdr_re.match("## Tasks")
    own_re = re.compile(LP.OWNER_ALT + ":")
    assert own_re.search("אחראי: דנה") and own_re.search("Owner: Dana")
    fld_re = re.compile(LP.FIELD_ALT + ":")
    for form in ("יעד: מחר", "סטטוס: פתוח", "Due: tomorrow", "Status: open"):
        assert fld_re.search(form), form
    done_re = re.compile(r"^\s*(\d+)\s*[:.]\s*" + LP.DONE_VOTE_ALT, re.I)
    assert done_re.match("3: בוצע") and done_re.match("3: done")
    # 6. section-title sets carry both languages.
    assert "משימות" in LP.KNOWN_SECTION_TITLES and "Tasks" in LP.KNOWN_SECTION_TITLES
    assert "תקציר" in LP.PROSE_SECTIONS and "Summary" in LP.PROSE_SECTIONS
    # 7. new config keys + the ensemble default-mismatch fix stay fixed.
    assert w.DEFAULT_CONFIG.get("primary_language") == "he"
    assert w.DEFAULT_CONFIG.get("summary_language") == "primary"
    src = inspect.getsource(w.LiaApp._run_summary)
    assert 'get("summary_local_ensemble", False)' in src, \
        "_run_summary fallback default must match DEFAULT_CONFIG (False)"


_test("lang_pack: bilingual marker table + detection + union regexes",
      t_lang_pack)


def t_file_transcribe_model():
    """Transcribe File has its OWN model, decoupled from the meeting model:
    "" follows meeting_model; an explicit key wins; the local-pyannote (meetings
    only) model is not offered for files."""
    import lia as w
    assert "file_transcribe_model" in w.DEFAULT_CONFIG
    assert w.DEFAULT_CONFIG["file_transcribe_model"] == "", "must default to follow"

    import types

    class Fake:
        pass
    f = Fake()
    f.config = {"meeting_model": "openai_gpt4o", "file_transcribe_model": ""}
    f._MEETING_MODELS = w.LiaApp._MEETING_MODELS
    for m in ("_file_model_key", "_file_model_label"):
        setattr(f, m, types.MethodType(getattr(w.LiaApp, m), f))
    assert f._file_model_key() == "openai_gpt4o", "empty must follow meeting_model"
    f.config["file_transcribe_model"] = "local_hebrew_turbo"
    assert f._file_model_key() == "local_hebrew_turbo", "explicit file model must win"
    assert "Hebrew Turbo Local" in f._file_model_label(), f._file_model_label()

    # the file picker must offer Hebrew Turbo Local and omit meetings-only pyannote
    offered = [k for (_l, k, _r) in w.LiaApp._MEETING_MODELS
               if k != "local_pyannote_hebrew"]
    assert "local_hebrew_turbo" in offered
    assert "local_pyannote_hebrew" not in offered


_test("Transcribe File: dedicated model, decoupled from meeting model",
      t_file_transcribe_model)


def t_transcribe_file_worker_runs():
    """_transcribe_file's worker must actually RUN end-to-end: resolve the file
    model, apply the model's language, write the transcript. Regression: assigning
    to the enclosing `language` param inside the nested worker made it a local, so
    reading it raised UnboundLocalError and killed the thread SILENTLY (stderr is
    None under pythonw) — the user saw "nothing happened" after picking a file."""
    import sys as _sys
    import types
    import tempfile
    import lia as w

    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "clip.wav")
    with open(src, "wb") as fh:
        fh.write(b"RIFF____WAVEfmt ")          # content irrelevant; transcriber is fake

    calls = {"errors": []}

    class FakeTranscriber:
        api_key = ""

        def transcribe_file(self, path, language=None, task="transcribe"):
            calls["path"] = path
            calls["language"] = language
            return "שלום עולם"

    class Overlay:
        def show(self, *a, **k):
            pass

        def show_error(self, m):
            calls["errors"].append(m)

        def show_done(self, **k):
            calls["done"] = True

    class _Root:
        def withdraw(self): pass
        def attributes(self, *a): pass
        def destroy(self): pass

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = lambda: _Root()
    fake_fd = types.ModuleType("tkinter.filedialog")
    fake_fd.askopenfilename = lambda **kw: src
    fake_tk.filedialog = fake_fd

    class _SyncThread:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()

    class App:
        pass
    app = App()
    app.model_loaded = True
    app.tray_icon = None
    app.overlay = Overlay()
    app.config = {"meeting_model": "openai_gpt4o",
                  "file_transcribe_model": "local_hebrew_turbo"}
    app._MEETING_MODELS = w.LiaApp._MEETING_MODELS
    for m in ("_file_model_key", "_file_model_label"):
        setattr(app, m, types.MethodType(getattr(w.LiaApp, m), app))
    app._resolve_meeting_model = lambda k: (FakeTranscriber(), "he", False)
    app._refresh_tray = lambda **k: None
    app._play_done_beep = lambda: None
    app._create_icon = lambda s: None

    saved_mods = {k: _sys.modules.get(k) for k in ("tkinter", "tkinter.filedialog")}
    orig = (w.threading.Thread, w.add_history_entry,
            w._apply_dpi_scaling_to_tk, os.startfile)
    try:
        _sys.modules["tkinter"] = fake_tk
        _sys.modules["tkinter.filedialog"] = fake_fd
        w.threading.Thread = _SyncThread
        w.add_history_entry = lambda *a, **k: None
        w._apply_dpi_scaling_to_tk = lambda r: None
        os.startfile = lambda p: calls.__setitem__("opened", p)
        w.LiaApp._transcribe_file(app)      # language=None → model's "he"
    finally:
        (w.threading.Thread, w.add_history_entry,
         w._apply_dpi_scaling_to_tk, os.startfile) = orig
        for k, v in saved_mods.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v

    assert not calls["errors"], calls["errors"]
    assert calls.get("path") == src, calls
    # the Hebrew model's language hint must reach the transcriber (not None/auto)
    assert calls.get("language") == "he", calls
    out = os.path.splitext(src)[0] + "_transcription_he.txt"
    assert os.path.exists(out), "transcript file was not written"


_test("Transcribe File: worker runs end-to-end (no silent thread death)",
      t_transcribe_file_worker_runs)


def t_overlay_tk_self_heal():
    """The overlay Tk thread self-heals: a supervisor rebuilds the root if the
    mainloop ever exits (a Tcl fault after long uptime + sleep/wake used to kill
    it SILENTLY under pythonw, leaving the recording waveform + every popup
    invisible until an app restart). Structural + logic check — no live Tk here;
    the actual rebuild-and-render was verified live."""
    import lia as w
    for m in ("_tk_supervisor", "_reset_tk_state", "_run_tk"):
        assert hasattr(w.OverlayNotification, m), "overlay missing %r" % m

    class Stub:
        pass
    s = Stub()
    for a in ("_root", "_canvas", "_visible", "_waveform_mode", "_wave_anim_running",
              "_wave_base", "_pill_photo", "_hide_after_id", "_recording_pill_top",
              "_meeting_card_top", "_meeting_card"):
        setattr(s, a, "STALE")
    w.OverlayNotification._reset_tk_state(s)
    assert s._root is None and s._canvas is None, "reset must drop the dead root"
    assert s._visible is False and s._wave_anim_running is False
    assert s._recording_pill_top is None and s._meeting_card_top is None

    # The supervisor must honour the shutdown flag (no rebuild-spin after quit).
    s._shutting_down = True
    s._tk_restarts = 0
    w.OverlayNotification._tk_supervisor(s)   # returns at once; never calls _run_tk
    assert s._tk_restarts == 0, "supervisor must not restart when shutting down"


_test("Overlay: Tk mainloop self-heals (no silent overlay death)",
      t_overlay_tk_self_heal)


def t_summary_local_ollama_shaping():
    """A LOCAL (Ollama) summary routes to the native /api/chat with thinking
    control + num_ctx — NOT the OpenAI payload — and cloud URLs stay cloud.
    Thinking is OFF for the local summary models we ship (fast)."""
    import lia as w

    class _NativeResp:
        status_code = 200
        def json(self):
            return {"message": {"content": "## תקציר\nok"}}

    def capture(model):
        cap = {}

        class FakeSession:
            def post(self, url, headers=None, json=None, timeout=None):
                cap["url"] = url
                cap["body"] = json
                return _NativeResp()

        cl = w.OpenAILLMCleaner(api_key="ollama", model=model,
                                chat_url=w.LiaApp._OLLAMA_CHAT_URL)
        cl._session = FakeSession()
        out = cl.summarize("טקסט לסיכום, מספיק ארוך לבדיקה.", "sys prompt")
        return cap, out

    cap, out = capture("gemma4:31b-it-qat")
    assert out == "## תקציר\nok", out
    assert cap["url"].endswith("/api/chat"), cap["url"]        # native, not /v1/...
    assert cap["body"].get("think") is False                  # local: thinking off
    assert "num_ctx" in cap["body"].get("options", {})
    assert "max_tokens" not in cap["body"] and "max_completion_tokens" not in cap["body"]
    capg, _ = capture("gemma4:31b-it-qat")
    assert capg["body"].get("think") is False                 # Gemma: thinking off too


_test("summarize(): local Ollama → native /api/chat + model-aware thinking",
      t_summary_local_ollama_shaping)


# ----- Compose Mode (voice → professional piece) -----
def t_compose_methods_and_config():
    import lia as w
    for m in ("_toggle_compose", "_start_compose_record", "_stop_compose_and_generate",
              "_do_compose_generate", "_compose_revise", "_compose_regenerate",
              "_compose_instr_start", "_compose_instr_stop_and_transcribe",
              "_save_composed_to_file", "_reset_tray_idle"):
        assert hasattr(w.LiaApp, m), f"LiaApp missing {m}"
    for m in ("show_compose_editor", "_create_compose_editor_toplevel"):
        assert hasattr(w.OverlayNotification, m), f"OverlayNotification missing {m}"
    for m in ("compose", "revise", "_chat"):
        assert hasattr(w.GroqLLMCleaner, m), f"GroqLLMCleaner missing {m}"
        assert hasattr(w.OpenAILLMCleaner, m), f"OpenAILLMCleaner missing {m} (inherit)"
    assert isinstance(w._COMPOSE_PROMPT, str)
    assert isinstance(w._REVISE_PROMPT, str)
    assert set(("summary", "email", "message")).issubset(w._COMPOSE_STYLE_RULES.keys())
    # Defaults (not the user's live choice — that's theirs to change):
    assert w.DEFAULT_CONFIG.get("compose_style") in w._COMPOSE_STYLE_RULES
    assert "compose_max_seconds" in w.DEFAULT_CONFIG
    cfg = w.load_config()
    assert cfg.get("compose_style") in w._COMPOSE_STYLE_RULES, cfg.get("compose_style")
    assert "compose_max_seconds" in cfg


_test("compose: app + overlay + cleaner methods, prompts, config keys present",
      t_compose_methods_and_config)


def t_compose_prompt_building():
    """compose() wraps the draft + injects the style rule + vocab; revise()
    wraps current/instruction. (Monkeypatch _chat to capture, no API.)"""
    import lia as w
    cl = w.GroqLLMCleaner(api_key="fake", model="gpt-5.5")
    cap = {}

    def fake_chat(system, user, max_completion=4000, timeout=120):
        cap["system"] = system
        cap["user"] = user
        return "RESULT"
    cl._chat = fake_chat

    out = cl.compose("buy more milk and call Dana", style="email", vocabulary="Dana")
    assert out == "RESULT", out
    assert "<draft>" in cap["user"] and "buy more milk" in cap["user"], cap["user"]
    assert "EMAIL" in cap["system"], cap["system"]
    assert "Dana" in cap["system"], "vocabulary not injected"

    cap.clear()
    out = cl.revise("Dear team, ...", "make it warmer", vocabulary="")
    assert out == "RESULT", out
    assert "<current>" in cap["user"] and "Dear team" in cap["user"], cap["user"]
    assert "<instruction>" in cap["user"] and "make it warmer" in cap["user"], cap["user"]

    # empty instruction → returns current unchanged, no _chat call
    cap.clear()
    same = cl.revise("unchanged piece", "   ")
    assert same == "unchanged piece", same
    assert not cap, "revise() with empty instruction should not call _chat"


_test("compose()/revise(): prompt wrappers + vocab + empty-instruction guard",
      t_compose_prompt_building)


def t_compose_chat_shaping():
    """_chat (via compose/revise): gpt-5.x → max_completion_tokens (no temp);
    legacy → max_tokens + temperature; OpenAI URL for the OpenAI cleaner."""
    import lia as w

    cap = {}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            cap["url"] = url
            cap["payload"] = json
            return _FakeResp("done")

    cl = w.OpenAILLMCleaner(api_key="fake", model="gpt-5.5")
    cl._session = FakeSession()
    out = cl.compose("a rough spoken draft about the quarterly plan", style="summary")
    assert out == "done", out
    p = cap["payload"]
    assert "max_completion_tokens" in p and "max_tokens" not in p, p
    assert "temperature" not in p, p
    assert cap["url"].startswith("https://api.openai.com"), cap["url"]
    assert p["model"] == "gpt-5.5"

    cap.clear()
    cl2 = w.GroqLLMCleaner(api_key="fake", model="llama-3.3-70b-versatile")
    cl2._session = FakeSession()
    out = cl2.revise("some current piece of text", "shorten it")
    assert out == "done", out
    p = cap["payload"]
    assert "max_tokens" in p and "max_completion_tokens" not in p, p
    assert p.get("temperature") == 0.4, p


_test("compose/revise: _chat request-shaping (gpt-5.x vs legacy)", t_compose_chat_shaping)


# ============================================================
# Session-17 regression tests (audit fixes)
# ============================================================
section("Session-17 regression fixes")


def t_snippet_rlm_prefix():
    """gpt-4o prepends RLM (U+200F) to Hebrew results — Hebrew cues must
    still match (they were silently dead before the bidi-strip fix)."""
    import lia as w
    sn = [{"cue": "חתימה", "text": "בברכה,\\nנאור"}]
    assert w.expand_snippet("‏חתימה", sn) == "בברכה,\nנאור"
    assert w.expand_snippet("‏הכנס חתימה", sn) == "בברכה,\nנאור"
    assert w.expand_snippet("sig", [{"cue": "sig", "text": "Best"}]) == "Best"
    assert w.expand_snippet("טקסט רגיל לגמרי", sn) == "טקסט רגיל לגמרי"


_test("snippets: RLM-prefixed Hebrew cues expand", t_snippet_rlm_prefix)


def t_char_runs_keep_digits():
    """Letter runs collapse; dictated numbers/amounts must survive."""
    import lia as w
    assert w.collapse_repetition_hallucinations("0505555555") == "0505555555"
    assert w.collapse_repetition_hallucinations("100000 שקל") == "100000 שקל"
    out = w.collapse_repetition_hallucinations("אהההההההה")
    assert "אהההה" not in out and "…" in out, out
    assert w.collapse_repetition_hallucinations("aaaaaaaa ok").startswith("aaa…")


_test("char-runs: digits survive, letter runs collapse", t_char_runs_keep_digits)


def t_tail_strip_word_boundary():
    """Tail strip must never cut MID-WORD (Hebrew prefix letters), and the
    common real words שלום/בהצלחה only strip as whole-text."""
    import lia as w
    assert w.strip_hallucinated_tail("הכל עבר בשלום") == "הכל עבר בשלום"
    assert w.strip_hallucinated_tail("אני מסיים בתודה") == "אני מסיים בתודה"
    assert w.strip_hallucinated_tail("הפגישה עברה בהצלחה") == "הפגישה עברה בהצלחה"
    assert w.strip_hallucinated_tail("תודה רבה") == ""           # whole-text → empty
    assert w.strip_hallucinated_tail("שלום") == ""
    assert w.strip_hallucinated_tail("נדבר מחר תודה רבה") == "נדבר מחר"
    # Leading RLM (gpt-4o emits bidi marks in raw Hebrew output) must not
    # defeat the anchored patterns.
    assert w.strip_hallucinated_tail("‏תודה רבה") == ""
    assert w.strip_hallucinated_tail("‏הכל עבר בשלום") == "הכל עבר בשלום"


_test("tail-strip: word boundaries + whole-text-only common words",
      t_tail_strip_word_boundary)


def t_unexpected_script_punctuation():
    """Typographic punctuation must not trigger the forced-he retry."""
    import lia as w
    f = w.OpenAITranscriber._has_unexpected_script
    assert f("it’s fine — really…") is False
    assert f("המחיר הוא 100₪ בלבד… נכון?") is False
    assert f("Привет как дела") is True


_test("script check: typographic punctuation whitelisted",
      t_unexpected_script_punctuation)


def t_extract_meeting_name_prefer():
    """prefer=<detected kind> beats the fixed Teams-first precedence."""
    import lia as w
    windows = [
        (1, "Dana Cohen | Org | Microsoft Teams", 11, "ms-teams.exe"),
        (2, "Zoom Meeting - Quarterly Sync", 22, "zoom.exe"),
    ]
    assert w.extract_meeting_name(windows) == "Dana Cohen"          # legacy order
    assert w.extract_meeting_name(windows, prefer="zoom") == "Quarterly Sync"
    assert w.extract_meeting_name(windows, prefer="teams") == "Dana Cohen"


_test("auto-title: prefer=detected-kind overrides Teams-first",
      t_extract_meeting_name_prefer)


def t_slug_strips_bidi():
    import lia as w
    assert "‏" not in w._slug_title("‏פגישת צוות‎")
    assert w._slug_title("‏פגישת צוות") == "פגישת צוות"


_test("slug: bidi control chars stripped from filenames", t_slug_strips_bidi)


def t_prompt_on_timeout_param():
    """show_prompt accepts the separate on_timeout callback (stop-prompt
    timeout semantics fix)."""
    import inspect
    import lia as w
    sig = inspect.signature(w.OverlayNotification.show_prompt)
    assert "on_timeout" in sig.parameters


_test("show_prompt has on_timeout parameter", t_prompt_on_timeout_param)


def _meeting_mock_app(summary_return):
    """A minimal app stub recording whether _run_summary was called."""
    import lia as w

    class _NoopOverlay:
        # The meeting writers now report progress to overlay.meeting_status_*;
        # swallow any such call in tests (no Tk windows).
        def __getattr__(self, _name):
            return lambda *a, **k: None

    class MockApp:
        def __init__(self):
            self.config = w.load_config()
            self.summary_calls = []
            self.overlay = _NoopOverlay()

        def _transcribe_with_fallback(self, *a, **kw):
            return "mock"

        def _get_language(self):
            return "he"

        def _run_summary(self, text, mode="meeting", metadata="",
                         collect_corrections=False):
            self.summary_calls.append((text, mode))
            return summary_return

        def _vocab_apply_corrections(self, text, **kw):
            return text

        def _vocab_take_corrections(self, **kw):
            return []

    return MockApp()


def t_meeting_regular_no_summary():
    """Regular meeting (summarize=False): clean transcript, NO LLM call."""
    import lia as w
    app = _meeting_mock_app("## TL;DR\nסיכום בדיקה")
    s = w.MeetingSession(app, summarize=False)
    assert s.summarize is False
    s.start_time = time.time() - 60
    s.stop_time = time.time()
    s._title_event.set()   # title resolved at stop in production; unblock the writer
    with s._chunks_lock:
        s.chunks = [{"index": 0, "timestamp_rel": 0, "text": "שלום עולם", "status": "ok"}]
    path = s._write_output_file(60)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert app.summary_calls == [], "regular meeting must NOT call _run_summary"
        assert "סיכום בדיקה" not in content
        assert "שלום עולם" in content
        assert "Full transcript" in content
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


_test("meeting regular mode: no summary, clean transcript only", t_meeting_regular_no_summary)


def t_meeting_summarize_writes_and_stores():
    """Summarize meeting (summarize=True): calls _run_summary, embeds it, and
    stores last_summary for the popup."""
    import lia as w
    app = _meeting_mock_app("## TL;DR\nסיכום בדיקה\n\n## נקודות\n- נקודה אחת")
    s = w.MeetingSession(app, summarize=True)
    assert s.summarize is True
    s.start_time = time.time() - 60
    s.stop_time = time.time()
    s._title_event.set()   # title resolved at stop in production; unblock the writer
    with s._chunks_lock:
        s.chunks = [{"index": 0, "timestamp_rel": 0, "text": "שלום עולם", "status": "ok"}]
    path = s._write_output_file(60)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert len(app.summary_calls) == 1, "summarize meeting must call _run_summary once"
        assert "סיכום בדיקה" in content, content
        assert s.last_summary, "last_summary must be stored for the popup"
        assert "סיכום בדיקה" in s.last_summary
    finally:
        for p in (path, getattr(s, "summary_html_path", None)):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


_test("meeting summarize mode: runs the summary model, embeds + stores summary", t_meeting_summarize_writes_and_stores)


def t_summary_live_gpt5():
    """LIVE gpt-5.5 call (requires openai_api_key). Hebrew+English transcript →
    a Hebrew, markdown-structured summary."""
    _skip_live("OpenAI gpt-5.5 summary call")
    import lia as w
    cfg = w.load_config()
    key = cfg.get("openai_api_key", "")
    if not key:
        raise SkipTest("No OpenAI API key configured")
    cl = w.OpenAILLMCleaner(api_key=key, model=cfg.get("summary_model", "gpt-5.5"))
    transcript = (
        "דיברנו על המיגרציה של ה-database ל-AWS. "
        "החלטנו להשתמש ב-RDS במקום self-managed Postgres. "
        "נאור יבדוק את העלויות עד מחר. נשאר פתוח מי לוקח את משימת ה-Redis."
    )
    # Exercise the REAL meeting path: transcript wrapped in <transcript> delimiters.
    out = cl.summarize(transcript, w._SUMMARY_PROMPT_MEETING,
                       meeting_meta="משך ההקלטה: 00:04:10")
    assert out and len(out) > 20, f"empty/short summary: {out!r}"
    assert "##" in out, f"expected markdown headers: {out!r}"
    assert any('֐' <= c <= '׿' for c in out), f"expected Hebrew output: {out!r}"
    # The PM prompt's fixed Hebrew skeleton — headline + summary must be present.
    assert "## כותרת הדיון" in out, f"missing כותרת הדיון: {out!r}"
    assert "## תקציר" in out, f"missing תקציר: {out!r}"
    # A clear decision ("החלטנו להשתמש ב-RDS") must surface as a decision bullet.
    assert "הוחלט" in out, f"expected a הוחלט decision bullet: {out!r}"


_test("LIVE: gpt-5.5 summarizes a Hebrew transcript", t_summary_live_gpt5)


def t_bidi_visual_reorder():
    """_to_visual_rtl reorders Hebrew to visual order for Tk (no-op for English/
    when python-bidi is absent)."""
    import lia as w
    he = "שלום עולם"
    vis = w._to_visual_rtl(he)
    assert isinstance(vis, str)
    assert w._to_visual_rtl("hello world") == "hello world"
    assert w._to_visual_rtl("") == ""
    if getattr(w, "_bidi_get_display", None) is not None:
        assert vis != he, f"expected Hebrew reorder, got {vis!r}"
        # round-trip sanity: same character multiset, different order
        assert sorted(vis) == sorted(he)


_test("_to_visual_rtl reorders Hebrew for Tk display", t_bidi_visual_reorder)


def t_meeting_status_card_api():
    """The persistent status-card API + the progress callbacks it relies on
    are all present and wired (no Tk windows are created here)."""
    import lia as w
    import inspect
    for m in ("meeting_status_start", "meeting_status_step", "meeting_status_detail",
              "meeting_status_finish", "meeting_status_error", "meeting_status_hide",
              "_create_meeting_card", "_meeting_card_animate", "_meeting_card_buttons"):
        assert hasattr(w.OverlayNotification, m), f"OverlayNotification missing {m}"
    for m in ("_open_meetings_folder", "_meeting_done_notify",
              "_edit_meeting_summary", "_open_summary_editor"):
        assert hasattr(w.LiaApp, m), f"LiaApp missing {m}"
    # Completion card exposes an Edit action for the WYSIWYG summary editor.
    assert "on_edit" in inspect.signature(
        w.OverlayNotification.meeting_status_finish).parameters, \
        "meeting_status_finish missing on_edit"
    # Progress hooks threaded into the pipeline.
    assert "on_status" in inspect.signature(
        w.AssemblyAITranscriber.poll).parameters, "poll missing on_status"
    assert "on_progress" in inspect.signature(
        w.MeetingSession._rerun_utterances).parameters, \
        "_rerun_utterances missing on_progress"
    # Spinner frames present for the animation.
    assert len(w.OverlayNotification._SPIN_FRAMES) >= 4


_test("meeting status card API + progress hooks present", t_meeting_status_card_api)


def t_summary_html_render():
    """The shareable meeting-summary HTML renders RTL with headers/bullets/
    tasks/bold and escapes unsafe input."""
    import lia as w
    md = "## תקציר\nשורה אחת\n\n## משימות\n- [ ] לעשות X\n- בולט **מודגש**"
    html = w._summary_to_html(md, title="2026-05-27", meta="10m")
    assert html.startswith("<!DOCTYPE html>"), html[:40]
    assert 'dir="rtl"' in html
    assert "<h2>" in html and "</h2>" in html
    assert '<li class="task">' in html
    assert "<li>" in html
    assert "<strong>" in html
    # No raw HTML injection from the summary text.
    assert "<script>" not in w._summary_to_html("## <script>x</script>")


_test("shareable HTML summary renders RTL + escapes input", t_summary_html_render)


def t_summary_editor_roundtrip():
    """compose_editor.SummaryApi loads a meeting-summary *.html, serves its own
    CSS + card interior, and writes edits back into the SAME file shell with a
    one-time .bak.html — the WYSIWYG meeting-summary editor (Phase 1)."""
    import lia as w
    import tempfile
    try:
        import compose_editor as ce  # imports `webview`; skip if unavailable
    except Exception as e:
        raise SkipTest(f"compose_editor import failed (pywebview?): {e}")

    md = ("## תקציר\nשורה אחת על הפגישה.\n\n## משימות\n"
          "- [ ] נאור יכין מסמך\n- בולט **מודגש**")
    doc = w._summary_to_html(md, title="פגישת בדיקה", meta="10m · diarized")
    tmpdir = tempfile.mkdtemp(prefix="wt_se_test_")
    path = os.path.join(tmpdir, "2026-06-26_10-00-00_meeting_summary.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    api = ce.SummaryApi({"mode": "summary_html", "path": path, "title": "x"})
    init = api.get_initial()
    assert init["css"].strip() and "--accent" in init["css"], "CSS not extracted"
    assert init["dir"] == "rtl"
    assert "תקציר" in init["card_html"], "Hebrew body missing from card_html"
    assert '<div class="footer">' not in init["card_html"], "footer leaked into card"

    edited = init["card_html"] + '\n      <p>טקסט <b>חדש</b></p>'
    res = api.save_summary(edited)
    assert res.get("ok"), res
    saved = open(path, encoding="utf-8").read()
    assert "טקסט" in saved, "edit not written"
    assert "<strong>חדש</strong>" in saved, "<b> not cleaned to <strong>"
    assert saved.startswith("<!DOCTYPE html>") and 'dir="rtl"' in saved
    assert saved.count('<div class="card">') == 1, "card duplicated/lost"
    assert '<div class="footer">' in saved, "footer chrome lost"

    bak = path[:-5] + ".bak.html"
    assert os.path.exists(bak), "backup not created"
    assert open(bak, encoding="utf-8").read() == doc, "backup != original"

    # Second save must NOT clobber the first-original backup.
    api.save_summary(edited + "\n      <p>עוד</p>")
    assert open(bak, encoding="utf-8").read() == doc, "backup overwritten"

    # Reload round-trips the edit; malformed input raises (-> exit 3 -> fallback).
    api2 = ce.SummaryApi({"mode": "summary_html", "path": path})
    assert "טקסט" in api2.get_initial()["card_html"], "edit didn't persist"
    assert ce.SummaryApi._clean_body("<b>a</b>") == "<strong>a</strong>"
    bad = os.path.join(tmpdir, "bad.html")
    open(bad, "w", encoding="utf-8").write("<html><body>no card</body></html>")
    try:
        ce.SummaryApi({"mode": "summary_html", "path": bad})
        assert False, "malformed file should raise"
    except ValueError:
        pass

    # Depth-balanced card parse: a footer-less doc with a nested body <div> must
    # NOT absorb the .wrap closing </div> (regression: the old rfind approach).
    nf = ('<!DOCTYPE html><html dir="rtl"><head><style>.card{}</style></head>'
          '<body><div class="wrap"><div class="card">'
          '<h1>כ</h1><div class="note">הערה</div><p>סוף</p>'
          '</div></div></body></html>')
    nfp = os.path.join(tmpdir, "nofooter_meeting_summary.html")
    open(nfp, "w", encoding="utf-8").write(nf)
    nfapi = ce.SummaryApi({"mode": "summary_html", "path": nfp})
    ci = nfapi.get_initial()["card_html"]
    assert "סוף" in ci and 'class="note"' in ci, "footer-less: body lost"
    assert 'class="wrap"' not in ci, "footer-less: .wrap absorbed into card"
    nfapi.save_summary(ci)
    nfsaved = open(nfp, encoding="utf-8").read()
    assert nfsaved.count("<div") == nfsaved.count("</div>"), "footer-less: unbalanced divs"

    # Concurrent-edit guard: a stale second writer is refused, not silently lost.
    a, b = (ce.SummaryApi({"mode": "summary_html", "path": path}),
            ce.SummaryApi({"mode": "summary_html", "path": path}))
    assert a.save_summary(a.get_initial()["card_html"] + "\n      <p>A</p>")["ok"]
    rb = b.save_summary(b.get_initial()["card_html"] + "\n      <p>B</p>")
    assert not rb["ok"] and "changed on disk" in rb["error"], "concurrent clobber not blocked"

    # "Save & email" helpers (no Outlook launched here): subject derives from the
    # edited <h1>, falls back to the filename, and the API surface exists.
    assert hasattr(ce.SummaryApi, "email_summary"), "email_summary missing"
    assert hasattr(ce.SummaryApi, "email_body"), "email_body missing"
    assert api._subject_from("<h1>פגישת בדיקה</h1><p>x</p>") == "פגישת בדיקה"
    assert "_meeting_summary" not in api._subject_from("<p>no heading</p>")


_test("summary editor: load/edit/save round-trip + backup", t_summary_editor_roundtrip)


# ============================================================
# Email search — local Outlook index (keyword + body cleaner + parser + wiring)
# ============================================================
section("Email search — local index")


def _email_test_db():
    import tempfile
    import time as _t
    import email_index as ei
    p = os.path.join(tempfile.gettempdir(), "wt_emailtest_%d.db" % os.getpid())
    for s in (p, p + "-wal", p + "-shm"):
        try:
            os.remove(s)
        except OSError:
            pass
    conn = ei.connect(p)
    ei.init_schema(conn)
    now = int(_t.time())
    rows = [
        ("e1", "s1", "primary", "Inbox", "Yaacov Levi", "yaacov@uni.example",
         "benny@acme.co", "eran@acme.co", "RE: התקנה UNI<>ACME",
         "היי בני, מבקש לקדם את ההתקנה.\r\nFrom: x\r\n> מצוטט ישן", now - 86400, 0),
        ("e2", "s1", "primary", "Sent", "Dana", "dana@example.com",
         "dana@acme.co", "", "דוח תקציב", "תוכן ההודעה על הפרויקט.", now - 2 * 86400, 1),
    ]
    for r in rows:
        bc = ei.clean_body(r[9])
        conn.execute(
            "INSERT INTO messages(entry_id,store_id,account,folder,sender_name,"
            "sender_email,to_recips,cc_recips,subject,body_clean,received_ts,"
            "has_attach,content_hash,indexed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], bc, r[10], r[11],
             ei.content_hash(r[8], bc), now))
    conn.commit()
    return conn


def t_email_keyword_hebrew():
    import email_index as ei
    conn = _email_test_db()
    assert len(ei.keyword_search(conn, "acme")) >= 1, "acme not found"
    assert len(ei.keyword_search(conn, "התקנה")) == 1, "Hebrew token search failed"
    assert len(ei.keyword_search(conn, "התק*")) >= 1, "Hebrew prefix failed"
    z = ei.keyword_search(conn, "from:yaacov")
    assert len(z) == 1 and z[0]["sender_email"] == "yaacov@uni.example", "from: operator"
    assert len(ei.keyword_search(conn, "to:acme.co")) >= 1, "domain-in-recipient search"
    assert len(ei.keyword_search(conn, "has:attachment")) == 1, "has:attachment"
    assert len(ei.keyword_search(conn, "folder:sent")) == 1, "folder: filter"


_test("email index: FTS5 keyword + Hebrew + operators", t_email_keyword_hebrew)


def t_email_clean_body():
    import email_index as ei
    cb = ei.clean_body("שלום\r\nFrom: a@b.com\r\nSent: now\r\n> ישן")
    assert "From:" not in cb and "ישן" not in cb and "שלום" in cb, "quoted chain not stripped"
    sl = ei.clean_body("ראו https://eur01.safelinks.protection.outlook.com/"
                       "?url=https%3A%2F%2Fexample.com%2Fx&data=1 תודה")
    assert "safelinks" not in sl, "safelink not unwrapped"


_test("email index: body cleaner strips quotes + safelinks", t_email_clean_body)


def t_email_query_parser():
    import email_index as ei
    m, where, params = ei.parse_query("acme.co from:dana after:2026/06/01 has:attachment")
    assert '"acme.co"' in m, "dot not quoted (would crash FTS)"
    assert "received_ts >= ?" in where and "has_attach = 1" in where, "operators -> SQL"
    m2, _, _ = ei.parse_query("פגיש*")
    assert m2 == '"פגיש"*', "prefix asterisk must sit OUTSIDE the quotes"


_test("email index: query parser (dot-quote, operators, prefix)", t_email_query_parser)


def t_email_module_wiring():
    import email_index
    import emailsearch_indexer  # noqa: F401  (no webview/win32com at import time)
    assert hasattr(email_index, "keyword_search") and hasattr(email_index, "ask")
    base = os.path.dirname(os.path.abspath(__file__))
    es = os.path.join(base, "email_search.py")
    assert os.path.exists(es) and "class SearchApi" in open(es, encoding="utf-8").read()
    import lia as wt
    assert hasattr(wt.LiaApp, "_open_email_search"), "_open_email_search missing"
    assert hasattr(wt.LiaApp, "_kick_email_indexer"), "_kick_email_indexer missing"
    assert "email_search_hotkey" in wt.DEFAULT_CONFIG, "config key missing"


_test("email search: modules import + lia wiring", t_email_module_wiring)


def t_email_unread_and_selfheal():
    """Unread flag flows through the query layer; browse/search limits + the
    self-heal open path + the indexer's sync_unread reconciler are wired."""
    import tempfile
    import email_index as ei
    # schema + query layer carry 'unread'
    assert "m.unread" in ei._SELECT_COLS, "unread not in _SELECT_COLS"
    dbp = os.path.join(tempfile.gettempdir(), "wt_unread_%d.db" % os.getpid())
    for s in (dbp, dbp + "-wal", dbp + "-shm"):
        try:
            os.remove(s)
        except OSError:
            pass
    conn = ei.connect(dbp)
    ei.init_schema(conn)
    assert "unread" in [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    conn.execute("INSERT INTO messages(entry_id,store_id,account,folder,kind,subject,"
                 "body_clean,received_ts,has_attach,unread,content_hash,indexed_ts,embedded)"
                 " VALUES('e1','S','a','Inbox','email','hi','body',10,0,1,'h',0,0)")
    conn.commit()
    rows = ei.keyword_search(conn, "", limit=50)
    assert rows and rows[0]["unread"] == 1, "unread not surfaced by keyword_search"
    conn.close()
    # limits + self-heal + reconciler present in source
    base = os.path.dirname(os.path.abspath(__file__))
    es = open(os.path.join(base, "email_search.py"), encoding="utf-8").read()
    assert "BROWSE_LIMIT" in es and "SEARCH_LIMIT" in es and "_has_text" in es
    assert "_relocate_item" in es and "msg_id" in es, "self-heal open not wired"
    idx = open(os.path.join(base, "emailsearch_indexer.py"), encoding="utf-8").read()
    assert "def sync_unread" in idx and "[Unread] = true" in idx


_test("email: unread flag + limits + self-heal open + reconciler", t_email_unread_and_selfheal)


def t_chat_wiring():
    """Chat window module + lia tray/hotkey/config wiring."""
    base = os.path.dirname(os.path.abspath(__file__))
    cw = os.path.join(base, "chat_window.py")
    assert os.path.exists(cw), "chat_window.py missing"
    src = open(cw, encoding="utf-8").read()
    for marker in ("class ChatApi", "def start_stream", "def _stream_local",
                   "function md(t)", "window.__chat", "HTML = r"):
        assert marker in src, "chat_window missing %r" % marker
    import lia as wt
    assert hasattr(wt.LiaApp, "_open_chat_window"), "_open_chat_window missing"
    assert "chat_hotkey" in wt.DEFAULT_CONFIG and "chat_model" in wt.DEFAULT_CONFIG


_test("chat: window module + lia wiring", t_chat_wiring)


def t_local_diarization_wiring():
    """Local pyannote diarization option: subprocess module + lia wiring."""
    base = os.path.dirname(os.path.abspath(__file__))
    dl = os.path.join(base, "diarize_local.py")
    assert os.path.exists(dl), "diarize_local.py missing"
    src = open(dl, encoding="utf-8").read()
    for marker in ('def _load_pipeline', 'itertracks', '"speaker"', '"start"', '"ok"',
                   '--models-dir', '--download', 'HF_HUB_OFFLINE'):
        assert marker in src, "diarize_local missing %r" % marker
    # subprocess reports pyannote-not-installed as clean JSON (verified live in tests)
    import lia as wt
    for m in ("_run_local_diarization", "_set_hf_token", "_diarize_bundle_dir",
              "_download_diarization_model"):
        assert hasattr(wt.LiaApp, m), "%s missing" % m
    keys = [k for _l, k, _r in wt.LiaApp._MEETING_MODELS]
    assert "local_pyannote_hebrew" in keys, "local diarize model not in selector"
    for c in ("hf_token", "diarize_local_model", "diarize_models_dir"):
        assert c in wt.DEFAULT_CONFIG, "config key %s missing" % c
    # MeetingSession must accept + store the backend
    import inspect
    sig = inspect.signature(wt.MeetingSession.__init__)
    assert "diarize_backend" in sig.parameters, "MeetingSession missing diarize_backend"


_test("diarize: local pyannote option + lia wiring", t_local_diarization_wiring)


def t_bilingual_route_decisions():
    """BilingualRouterTranscriber._decide_route: the pure policy table.
    Hebrew-biased by design — a he→en mistake garbles Hebrew badly, an
    en→he mistake only costs a little English accuracy."""
    import lia as wt
    d = wt.BilingualRouterTranscriber._decide_route
    # (top_lang, top_prob, p_en, p_he, last_route) -> expected
    # Fail-safe policy (2026-08-03): English ONLY on confident English; every
    # uncertain / other-language / garbage case → Hebrew. NO hysteresis.
    cases = [
        # confident single-language segments
        (("en", 0.97, 0.97, 0.01, "he"), "en"),
        (("he", 0.95, 0.01, 0.95, "en"), "he"),
        # English is the top guess, reasonably sure → en
        (("en", 0.60, 0.60, 0.10, "he"), "en"),
        # top en but below the soft floor → Hebrew (safe)
        (("en", 0.45, 0.45, 0.10, "en"), "he"),
        # NO sticky-English hysteresis: last_route en must NOT force en
        (("en", 0.40, 0.40, 0.28, "en"), "he"),
        # confident third language still honored
        (("ru", 0.90, 0.03, 0.02, "he"), "ru"),
        # the LIVE-failure cases — scattered low-conf non-Hebrew on Hebrew
        # audio must fall back to Hebrew, NOT cascade to English
        (("tr", 0.26, 0.13, 0.00, "en"), "he"),
        (("it", 0.66, 0.12, 0.00, "en"), "he"),
        (("fr", 0.42, 0.09, 0.00, "en"), "he"),
        (("ja", 0.29, 0.24, 0.00, "en"), "he"),
        # overwhelmingly English even if labeled oddly → en
        (("nl", 0.50, 0.88, 0.00, "he"), "en"),
        # ambiguous first segment: conservative Hebrew default
        (("en", 0.40, 0.40, 0.28, None), "he"),
    ]
    for args, want in cases:
        got = d(*args)
        assert got == want, "decide%r -> %r, want %r" % (args, got, want)
    # primary="he" explicitly must be byte-identical to the default.
    for args, want in cases:
        assert d(*args, "he") == want, args
    # primary="en" (2026-08): the exact MIRROR - Hebrew only on confident Hebrew
    # evidence, everything uncertain/garbage defaults to English.
    en_cases = [
        (("he", 0.95, 0.01, 0.95, "en"), "he"),   # confident Hebrew
        (("en", 0.97, 0.97, 0.01, "he"), "en"),   # confident English
        (("he", 0.60, 0.10, 0.60, "en"), "he"),   # he top guess, soft floor
        (("he", 0.45, 0.10, 0.45, "he"), "en"),   # below the soft floor -> en
        (("nl", 0.50, 0.00, 0.88, "en"), "he"),   # overwhelmingly Hebrew
        (("ru", 0.90, 0.03, 0.02, "en"), "ru"),   # confident third language
        (("tr", 0.26, 0.13, 0.00, "he"), "en"),   # garbage detections -> en
        (("en", 0.40, 0.40, 0.28, None), "en"),   # ambiguous -> en default
    ]
    for args, want in en_cases:
        got = d(*args, "en")
        assert got == want, "decide%r primary=en -> %r, want %r" % (args, got, want)


_test("bilingual: route decision policy (he-bias + en mirror + hysteresis)",
      t_bilingual_route_decisions)


def t_bilingual_short_clip_clamp():
    """SHORT-CLIP CLAMP (2026-08-29 field failure): a press-to-talk clip
    that language-detects as a 'confident' third language (de/ru/...) must
    be clamped to the primary language - noisy laptop mics fool the
    detector far more often than anyone dictates German. Long audio (the
    split path) keeps third-language routing."""
    import numpy as np
    import lia as wt

    class _Child:
        def __init__(self):
            self.model = object()   # "loaded"
            self.custom_vocabulary = ""
            self.calls = []
        def transcribe(self, audio_np, language=None, beam_size=3,
                       task="transcribe", bias_ok=True):
            self.calls.append(language)
            return "ok"

    he, gen = _Child(), _Child()
    r = wt.BilingualRouterTranscriber(he_transcriber=he,
                                      general_transcriber=gen)
    # Force the router's detection to claim confident German
    r._route = lambda audio: "de"
    audio = np.zeros(4 * 16000, dtype=np.float32)   # 4s: short path
    out = r.transcribe(audio)                        # language=None -> router decides
    assert out == "ok"
    # The clamp must send the clip to the HEBREW child with language="he",
    # never to the general child as German.
    assert he.calls == ["he"], "he child calls: %r" % (he.calls,)
    assert gen.calls == [], "general child must not get the clip: %r" % (gen.calls,)
    assert r._last_route == "he", "clamp must also reset _last_route"
    # primary="en" mirror: the clamp lands on English
    he2, gen2 = _Child(), _Child()
    r2 = wt.BilingualRouterTranscriber(he_transcriber=he2,
                                       general_transcriber=gen2, primary="en")
    r2._route = lambda audio: "ru"
    r2.transcribe(audio)
    assert gen2.calls == ["en"] or he2.calls == [], \
        "primary=en clamp: gen2=%r he2=%r" % (gen2.calls, he2.calls)


_test("bilingual: short-clip third-language clamp (the German-dictation fix)",
      t_bilingual_short_clip_clamp)


def t_vocab_prompt_bias_gate():
    """2026-09-03 regression: the English-dominant vocab initial_prompt was
    copied into SHORT / LOW-SIGNAL Hebrew clips, emitting Latin-letter
    hallucinations ("text, Teaching Center", "-M-M"). The gate
    (_vocab_prompt_bias_ok) decides per clip, FasterWhisperTranscriber.transcribe
    drops initial_prompt when bias_ok is False, and the bilingual router
    forwards the flag and no longer reuses a stale route on a sub-3s clip."""
    import types, threading as _th
    import numpy as _np
    import lia as w
    # -- pure gate: long+loud keeps the prompt; short OR quiet drops it
    assert w._vocab_prompt_bias_ok(30.0, 0.10, 5.0, 0.02) is True
    assert w._vocab_prompt_bias_ok(3.9, 0.12, 5.0, 0.02) is False    # the 11:42 clip
    assert w._vocab_prompt_bias_ok(1.6, 0.03, 5.0, 0.02) is False    # the 11:22 clip
    assert w._vocab_prompt_bias_ok(30.0, 0.005, 5.0, 0.02) is False  # long but near-silent
    assert w._vocab_prompt_bias_ok(None, None) is True               # unknown -> keep
    # -- FasterWhisperTranscriber.transcribe honors bias_ok: capture the prompt
    captured = {}
    class _Seg:
        text = "שלום עולם"
    class _Info:
        language = "he"
    class _FakeModel:
        def transcribe(self, audio, **kw):
            captured["initial_prompt"] = kw.get("initial_prompt")
            return ([_Seg()], _Info())
    T = w.FasterWhisperTranscriber
    tr = T.__new__(T)
    tr.model = _FakeModel()
    tr.custom_vocabulary = "AWS, Kubernetes, Docker"
    tr._infer_lock = _th.Lock()
    tr._demote_to_cpu = lambda *a, **k: False
    audio = _np.zeros(16000, dtype=_np.float32)
    tr.transcribe(audio, language="he", bias_ok=True)
    assert captured["initial_prompt"] == "Common terms: AWS, Kubernetes, Docker", captured
    tr.transcribe(audio, language="he", bias_ok=False)
    assert captured["initial_prompt"] is None, captured
    # -- the router forwards bias_ok to the chosen child
    R = w.BilingualRouterTranscriber
    r = R.__new__(R)
    seen = {}
    class _Child:
        model = object()
        def transcribe(self, audio, language=None, beam_size=3, task="transcribe", bias_ok=True):
            seen["bias_ok"] = bias_ok
            return "x"
    r.he = _Child(); r.general = _Child(); r.en = None
    r.primary = "he"; r._last_route = "he"
    r.transcribe(_np.zeros(1600, dtype=_np.float32), language="he", bias_ok=False)
    assert seen.get("bias_ok") is False, seen
    # -- WP-C: a sub-MIN_DETECT_SEC clip falls back to primary, never a stale route
    r2 = R.__new__(R)
    r2.he = _Child(); r2.general = _Child(); r2.en = None
    r2.primary = "he"; r2._last_route = "en"        # a stale English lean
    route = R._route(r2, _np.zeros(int(1.0 * 16000), dtype=_np.float32))
    assert route == "he", ("short clip must fall back to primary, got %r" % route)


_test("dictation: vocab-prompt confidence gate (no English letters on short/quiet clips)",
      t_vocab_prompt_bias_gate)


def t_parakeet_cache_self_heal():
    """Parakeet SELF-HEAL (2026-08-29 field failure): a truncated first
    download leaves the HF cache corrupt and every load fails with
    INVALID_PROTOBUF until the cache is cleared by hand. load_model must
    detect the signature, wipe, and retry ONCE; unrelated errors still
    raise immediately."""
    import sys
    import types
    import lia as wt

    old = sys.modules.get("onnx_asr")

    # Corrupt-cache error on the first call -> retry succeeds
    calls = []
    fake = types.ModuleType("onnx_asr")
    def _load(name, **kw):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError(
                "[ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Load model "
                "from encoder.onnx failed: Protobuf parsing failed.")
        return object()
    fake.load_model = _load
    sys.modules["onnx_asr"] = fake
    try:
        p = wt.ParakeetTranscriber()
        p.load_model()
        assert p.model is not None, "self-heal retry did not load"
        assert len(calls) == 2, "expected exactly 1 retry, got %d calls" % len(calls)
        assert not p._loading
    finally:
        sys.modules.pop("onnx_asr", None)
        if old is not None:
            sys.modules["onnx_asr"] = old

    # A non-corrupt error (network) must raise with NO retry
    calls2 = []
    fake2 = types.ModuleType("onnx_asr")
    def _load2(name, **kw):
        calls2.append(name)
        raise RuntimeError("connection refused")
    fake2.load_model = _load2
    sys.modules["onnx_asr"] = fake2
    try:
        p2 = wt.ParakeetTranscriber()
        raised = False
        try:
            p2.load_model()
        except RuntimeError:
            raised = True
        assert raised, "non-corrupt error must raise"
        assert len(calls2) == 1, "non-corrupt error must not retry"
        assert p2.model is None and not p2._loading
    finally:
        sys.modules.pop("onnx_asr", None)
        if old is not None:
            sys.modules["onnx_asr"] = old


_test("parakeet: corrupt-cache self-heal (wipe + one retry)",
      t_parakeet_cache_self_heal)


def t_bilingual_router_wiring():
    """Router class surface + meeting-builder wiring + config default."""
    import inspect
    import lia as wt
    R = wt.BilingualRouterTranscriber
    # BaseTranscriber-compatible surface (MeetingSession/_rerun_utterances/
    # Transcribe File all call these polymorphically)
    for m in ("transcribe", "transcribe_file", "load_model", "_route",
              "_decide_route", "_child_for"):
        assert hasattr(R, m), "router missing %s" % m
    assert R.GENERAL_MODEL == "large-v3-turbo"
    # Bilingual auto is ON by default, with an escape hatch
    assert wt.DEFAULT_CONFIG.get("meeting_bilingual_auto") is True
    # Vocabulary propagates to BOTH children; model proxies the he child;
    # a dead general child degrades routing to he (never raises).
    class _Stub:
        def __init__(self):
            self.custom_vocabulary = ""
            self.model = None
            self._loading = False
    he, gen = _Stub(), _Stub()
    r = R(he_transcriber=he, general_transcriber=gen)
    r.custom_vocabulary = "git, push, Kubernetes"
    assert he.custom_vocabulary == gen.custom_vocabulary == "git, push, Kubernetes"
    assert r.model is None            # he not loaded yet
    he.model = object()
    assert r.model is he.model
    import numpy as np
    assert r._route(np.zeros(16000 * 3, dtype=np.float32)) == "he", \
        "dead general child must degrade to he"
    # The meeting builder must consult the flag (source-level check)
    src = inspect.getsource(wt.LiaApp._build_meeting_transcriber)
    assert "meeting_bilingual_auto" in src and "BilingualRouterTranscriber" in src
    # 2026-08: the optional dedicated ENGLISH child (Parakeet).
    assert wt.DEFAULT_CONFIG.get("bilingual_english_model") == "whisper"
    en = _Stub()
    r2 = R(he_transcriber=_Stub(), general_transcriber=gen, en_transcriber=en,
           primary="en")
    assert r2.primary == "en" and r2._last_route == "en"
    r2.custom_vocabulary = "vlan"
    assert en.custom_vocabulary == "vlan", "vocab must reach the en child"
    # en child not loaded -> confident-en segments stay on the general child;
    # loaded -> they move to the dedicated child. he always stays on he.
    gen.model = object()
    assert r2._child_for("en")[0] is gen
    en.model = object()
    assert r2._child_for("en") == (en, "en")
    assert r2._child_for("he")[0] is r2.he
    assert r2._child_for("fr")[0] is gen, "third languages stay on general"
    # construction sites pass the en child + primary (source-level checks)
    for fn in (wt.LiaApp._make_local_transcriber,
               wt.LiaApp._build_meeting_transcriber):
        s = inspect.getsource(fn)
        assert "en_transcriber" in s and "primary" in s, fn.__name__
    assert "bilingual_english_model" in inspect.getsource(
        wt.LiaApp._router_en_child)
    # OpenAI post-hoc retry follows the primary language.
    o = wt.OpenAITranscriber(api_key="k")
    assert o.preferred_language == "he"
    assert '"language": getattr(self, "preferred_language", "he")' in \
        inspect.getsource(wt.OpenAITranscriber._retry_with_forced_language)


_test("bilingual: router wiring + en child + vocab/model proxy + degrade",
      t_bilingual_router_wiring)


def t_bilingual_dictation_and_split():
    """Dictation-side routing + mid-chunk code-switch splitting."""
    import inspect
    import lia as wt
    R = wt.BilingualRouterTranscriber
    # Press-to-talk: router is built by the local-transcriber factory,
    # gated on its own config key (escape hatch), and _get_language must
    # yield None when the router is active (an explicit "he" would bypass it)
    assert wt.DEFAULT_CONFIG.get("dictation_bilingual_auto") is True
    src = inspect.getsource(wt.LiaApp._make_local_transcriber)
    assert "dictation_bilingual_auto" in src and "BilingualRouterTranscriber" in src
    assert "BilingualRouterTranscriber" in inspect.getsource(
        wt.LiaApp._get_language)
    # Both construction sites go through the factory (no stray direct builds
    # of the press-to-talk local transcriber)
    app_src = inspect.getsource(wt.LiaApp)
    assert app_src.count("_make_local_transcriber(") >= 3  # def + 2 call sites
    # Splitting: long audio (meeting chunks) goes through _transcribe_split;
    # short dictation clips are routed whole (mixed sentences stay intact)
    assert hasattr(R, "_transcribe_split")
    assert R.SPLIT_MIN_SEC >= 15, "dictation clips must NOT be split"
    assert 0.3 <= R.GROUP_GAP_SEC <= 1.5
    # Meeting builder shares the dictation router (no duplicate models)
    src = inspect.getsource(wt.LiaApp._build_meeting_transcriber)
    assert "isinstance(main, BilingualRouterTranscriber)" in src


_test("bilingual: dictation routing + code-switch split wiring", t_bilingual_dictation_and_split)


def t_recap_and_continue():
    """Recap & Continue: mid-meeting checkpoint — transcript + summary up to
    now, meeting keeps recording; final stop still yields the full meeting."""
    import inspect
    import tempfile
    import threading as _th
    import time as _time
    import lia as wt

    # snapshot filtering: max_index bounds both assemblers
    s = wt.MeetingSession.__new__(wt.MeetingSession)
    s.chunks = [
        {"index": 0, "timestamp_rel": 0, "text": "אחד", "status": "ok"},
        {"index": 1, "timestamp_rel": 45, "text": "שניים", "status": "ok"},
        {"index": 2, "timestamp_rel": 90, "text": "שלוש", "status": "ok"},
    ]
    s._chunks_lock = _th.Lock()
    full = s._assemble_transcript_plain()
    cutp = s._assemble_transcript_plain(max_index=2)
    assert "שלוש" in full and "שניים" in cutp and "שלוש" not in cutp
    assert "שלוש" not in s._assemble_transcript_markdown(max_index=2)

    # functional recap() with stubs: writes txt + summary html to a temp
    # MEETINGS_DIR, returns all three, and leaves the session state intact
    class _App:
        def _run_summary(self, text, mode="meeting", metadata=""):
            # The interim note must reach the model in EITHER language, so it
            # summarizes only what has been said so far.
            import lang_pack as _lp
            assert any(_lp.term("interim_note", lg).strip()[:20] in metadata
                       for lg in ("he", "en")), \
                "the interim recap must flag itself in metadata"
            return "## תקציר\nסיכום ביניים לבדיקה"
    s.app = _App()
    s._active, s.diarize_mode, s._cancelled = True, False, False
    s._recap_active = False
    s._next_chunk_index = 3
    s._pending_jobs, s._pending_lock = [], _th.Lock()
    s.start_time = _time.time() - 600
    s.title, s.title_guess, s.source = None, "בדיקת ריקאפ", "both"
    s._drain_audio = lambda: None          # no live recorders in the test
    old_dir = wt.MEETINGS_DIR
    wt.MEETINGS_DIR = tempfile.mkdtemp(prefix="wt_recap_")
    try:
        res = s.recap()
        assert res, "recap returned nothing"
        txt_path, html_path, summary_md = res
        assert os.path.exists(txt_path) and "_recap_" in os.path.basename(txt_path)
        body = open(txt_path, encoding="utf-8").read()
        assert "meeting continues" in body and "סיכום ביניים" in body
        assert "אחד" in body and "שלוש" in body     # transcript up to now
        assert html_path and os.path.exists(html_path)
        assert summary_md.startswith("## תקציר")
        # the meeting is untouched: still active, chunks intact
        assert s._active and len(s.chunks) == 3
    finally:
        wt.MEETINGS_DIR = old_dir

    # app wiring: tray item + handler guards (diarized refused, double-run
    # guarded, delivery via the summary editor)
    src = inspect.getsource(wt.LiaApp)
    assert "Recap & Continue" in src and "_recap_available" in src
    hs = inspect.getsource(wt.LiaApp._recap_meeting)
    for marker in ("diarize_mode", "_recap_active", "_open_summary_editor"):
        assert marker in hs, f"_recap_meeting missing {marker}"


_test("meeting: Recap & Continue (snapshot + files + wiring)", t_recap_and_continue)


def t_live_transcript():
    """Rolling LIVE transcript: chunk-ordered atomic rewrite, deleted once
    the final file lands (or on cancel), tray button wired."""
    import inspect
    import tempfile
    import threading as _th
    import time as _time
    import lia as wt

    s = wt.MeetingSession.__new__(wt.MeetingSession)
    s.diarize_mode, s._cancelled = False, False
    s.start_time = _time.time()
    s._chunks_lock, s._live_lock = _th.Lock(), _th.Lock()
    s._live_warned = False
    # completion order != chunk order — the file must still be chunk-ordered
    s.chunks = [
        {"index": 1, "timestamp_rel": 45, "text": "שניים", "status": "ok"},
        {"index": 0, "timestamp_rel": 0, "text": "אחד", "status": "ok"},
    ]
    d = tempfile.mkdtemp(prefix="wt_live_")
    s._live_path = os.path.join(d, "x_meeting_LIVE.txt")
    s._write_live_file()
    body = open(s._live_path, encoding="utf-8").read()
    assert "LIVE transcript" in body
    assert body.index("אחד") < body.index("שניים"), "live file not chunk-ordered"
    assert not os.path.exists(s._live_path + ".tmp"), "atomic replace left tmp"
    s._delete_live_file()
    assert s._live_path is None
    assert not os.path.exists(os.path.join(d, "x_meeting_LIVE.txt"))
    # wiring: worker refresh, delete on final write + cancel, tray button
    assert "_write_live_file" in inspect.getsource(wt.MeetingSession._submit_chunk)
    assert "_delete_live_file" in inspect.getsource(wt.MeetingSession._write_output_file)
    assert "_delete_live_file" in inspect.getsource(wt.MeetingSession.cancel)
    # Discard drops the transcript but KEEPS the audio (WAV + Opus archive),
    # honouring keep_meeting_audio — it must not unconditionally delete the WAV.
    _cancel_src = inspect.getsource(wt.MeetingSession.cancel)
    assert "_transcode_wav_to_opus" in _cancel_src and "keep_meeting_audio" in _cancel_src, \
        "cancel() should keep + archive the audio, not delete it outright"
    assert hasattr(wt.LiaApp, "_open_live_transcript")
    assert "Live Transcript" in inspect.getsource(wt.LiaApp)


_test("meeting: rolling LIVE transcript (order + atomic + cleanup)", t_live_transcript)


def t_chunked_wav_safety_net():
    """Chunked meetings keep a full-audio WAV so a failed / mis-modeled chunk
    can be re-transcribed (2026-08-03: FAILED chunks were unrecoverable —
    chunked mode kept no audio)."""
    import inspect
    import tempfile
    import threading as _th
    import wave
    import numpy as np
    import lia as wt

    assert wt.DEFAULT_CONFIG.get("keep_meeting_audio") is True
    # append (float32 → int16) then close writes a valid, non-empty WAV
    s = wt.MeetingSession.__new__(wt.MeetingSession)
    s.diarize_mode = False
    s._wav_writer_lock = _th.Lock()
    d = tempfile.mkdtemp(prefix="wt_wav_")
    s._wav_path = os.path.join(d, "x_meeting_audio.wav")
    s._wav_writer = wave.open(s._wav_path, "wb")
    s._wav_writer.setnchannels(1); s._wav_writer.setsampwidth(2); s._wav_writer.setframerate(16000)
    tone = (np.sin(np.arange(16000) / 5.0) * 0.3).astype(np.float32)   # 1s
    s._append_chunk_to_wav(tone)
    s._append_chunk_to_wav(tone)

    class _App:
        config = {"keep_meeting_audio": True}
    s.app = _App()
    s._close_chunk_wav()
    assert s._wav_writer is None
    assert os.path.exists(s._wav_path), "safety-net WAV not kept"
    with wave.open(s._wav_path) as wf:
        assert wf.getnframes() == 32000, "WAV should hold both appended seconds"
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1

    # keep_meeting_audio off → WAV deleted on close
    s2 = wt.MeetingSession.__new__(wt.MeetingSession)
    s2.diarize_mode = False
    s2._wav_writer_lock = _th.Lock()
    p2 = os.path.join(d, "y_meeting_audio.wav")
    s2._wav_path = p2
    s2._wav_writer = wave.open(p2, "wb")
    s2._wav_writer.setnchannels(1); s2._wav_writer.setsampwidth(2); s2._wav_writer.setframerate(16000)
    s2._append_chunk_to_wav(tone)
    s2.app = type("A", (), {"config": {"keep_meeting_audio": False}})()
    s2._close_chunk_wav()
    assert not os.path.exists(p2), "WAV should be deleted when keep is off"

    # Opus transcode: ~much smaller AND still decodable by faster-whisper
    # (this is what makes it a valid re-transcription safety net). It KEEPS
    # the source WAV (tiered archive: WAV short-term + Opus long-term).
    s3 = wt.MeetingSession.__new__(wt.MeetingSession)
    wpath = os.path.join(d, "z_meeting_audio.wav")
    ww = wave.open(wpath, "wb")
    ww.setnchannels(1); ww.setsampwidth(2); ww.setframerate(16000)
    ww.writeframes((tone * 32767).astype(np.int16).tobytes() * 5)   # ~5s
    ww.close()
    opath = s3._transcode_wav_to_opus(wpath)
    assert opath and opath.endswith(".opus") and os.path.exists(opath)
    assert os.path.exists(wpath), "transcode must KEEP the WAV (tiered archive)"
    assert not os.path.exists(opath + ".tmp"), "atomic transcode left a .tmp"
    assert os.path.getsize(opath) < os.path.getsize(wpath), "Opus not smaller than WAV"
    from faster_whisper.audio import decode_audio
    dec = decode_audio(opath, sampling_rate=16000)
    assert len(dec) > 16000, "Opus not decodable back to audio for re-transcription"
    # picker accepts .opus so the kept file can be re-transcribed
    assert "*.opus" in inspect.getsource(wt.LiaApp._transcribe_file)

    # wiring: start() opens it, _submit_chunk appends in BOTH modes, stop closes
    assert "keep_meeting_audio" in inspect.getsource(wt.MeetingSession.start)
    sub = inspect.getsource(wt.MeetingSession._submit_chunk)
    assert "_append_chunk_to_wav" in sub and "if self._wav_writer is not None" in sub
    assert "_close_chunk_wav" in inspect.getsource(wt.MeetingSession.stop)


_test("meeting: chunked WAV safety net (keep + re-transcribe)", t_chunked_wav_safety_net)


def t_meeting_audio_retention():
    """Tiered retention: WAV ~1 month, Opus ~2 years, transcripts forever."""
    import inspect
    import tempfile
    import time as _time
    import lia as wt

    assert wt.DEFAULT_CONFIG.get("meeting_wav_retention_days") == 30
    assert wt.DEFAULT_CONFIG.get("meeting_opus_retention_days") == 730
    d = tempfile.mkdtemp(prefix="wt_retain_")
    def mk(name, age_days):
        p = os.path.join(d, name)
        open(p, "w").close()
        t = _time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p
    wav_45d = mk("2026-01-01_10-00-00_meeting_audio.wav", 45)     # WAV past 30 → prune
    wav_10d = mk("2026-07-20_10-00-00_meeting_audio.wav", 10)     # WAV under 30 → keep
    opus_400d = mk("2025-06-01_10-00-00_meeting_audio.opus", 400) # Opus under 730 → keep
    opus_800d = mk("2024-05-01_10-00-00_meeting_audio.opus", 800) # Opus past 730 → prune
    transcript = mk("2024-01-01_10-00-00_meeting.txt", 900)       # ancient transcript → keep

    app = wt.LiaApp.__new__(wt.LiaApp)
    app.config = {"meeting_wav_retention_days": 30, "meeting_opus_retention_days": 730}
    old_dir = wt.MEETINGS_DIR
    wt.MEETINGS_DIR = d
    try:
        app._prune_meeting_audio()
        assert not os.path.exists(wav_45d), "45-day WAV should be pruned (>30)"
        assert os.path.exists(wav_10d), "10-day WAV must be kept (<30)"
        assert os.path.exists(opus_400d), "400-day Opus must be kept (<730)"
        assert not os.path.exists(opus_800d), "800-day Opus should be pruned (>730)"
        assert os.path.exists(transcript), "transcripts must NEVER be pruned"
        # 0 for a format = keep it forever
        app.config["meeting_wav_retention_days"] = 0
        wav_old = mk("2020-01-01_10-00-00_meeting_audio.wav", 999)
        app._prune_meeting_audio()
        assert os.path.exists(wav_old), "wav retention 0 must keep WAVs forever"
        # OPT-IN transcript retention (default 0 = keep forever; audit P2e)
        assert wt.DEFAULT_CONFIG.get("meeting_transcript_retention_days") == 0
        app.config["meeting_transcript_retention_days"] = 90
        old_sum = mk("2024-02-01_10-00-00_meeting_summary.html", 120)
        new_txt = mk("2026-08-20_10-00-00_meeting_diarized.txt", 10)
        app._prune_meeting_audio()
        assert not os.path.exists(transcript), "opt-in retention must prune old transcripts"
        assert not os.path.exists(old_sum), "opt-in retention must prune old summaries"
        assert os.path.exists(new_txt), "fresh transcript must survive"
    finally:
        wt.MEETINGS_DIR = old_dir
    # runs at startup
    assert "_prune_meeting_audio" in inspect.getsource(wt.LiaApp.run)


_test("meeting: tiered audio retention (WAV 1mo, Opus 2yr, transcripts opt-in)",
      t_meeting_audio_retention)


def t_remote_ws_policy():
    """ws:// plaintext only to private/tailnet hosts (2026-08-28 audit): a
    public ws:// URL is refused - at save time and at load_model - unless
    remote_allow_insecure_ws overrides (still warned); wss:// always fine."""
    import lia as w
    for url in ("ws://localhost:9090", "ws://127.0.0.1:9090",
                "ws://192.168.1.20:9090", "ws://10.1.2.3:9090",
                "ws://100.101.7.9:9090",            # CGNAT / Tailscale
                "ws://box.tail1234.ts.net:9090", "ws://nas.local:9090",
                "wss://stt.example.com"):
        ok, warn = w.check_ws_url_security(url)
        assert ok and not warn, url
    for url in ("ws://8.8.8.8:9090", "ws://stt.example.com:9090"):
        ok, warn = w.check_ws_url_security(url)
        assert not ok and "wss://" in warn, url
    ok, warn = w.check_ws_url_security("ws://8.8.8.8:9090", allow_insecure=True)
    assert ok and "INSECURE" in warn
    assert w.DEFAULT_CONFIG.get("remote_allow_insecure_ws") is False
    # load_model refuses BEFORE any network I/O
    t = w.RemoteTranscriber(url="ws://8.8.8.8:9090")
    try:
        t.load_model()
        assert False, "public ws:// not refused"
    except RuntimeError as e:
        assert "wss://" in str(e)
    # the settings save path refuses too
    app = w.LiaApp.__new__(w.LiaApp)
    app.config = {"remote_allow_insecure_ws": False}
    ok, msg = app._apply_remote_server("ws://8.8.8.8:9090", "")
    assert not ok and "wss://" in msg


def t_remote_fail_reason_surfaced():
    """A handshake failure surfaces the REAL reason in the Test/warm-up error,
    not a generic 'server not ready' (which hid a missing websocket-client for
    a whole debugging session, 2026-08-31)."""
    import threading
    import lia as w
    # _fail records the reason on the stream object.
    s = w.WhisperLiveStream("ws://127.0.0.1:9090")
    assert s.fail_reason == ""
    s._fail("websocket-client not installed: No module named 'websocket'")
    assert s.failed and "websocket-client not installed" in s.fail_reason

    # load_model bubbles that reason into its RuntimeError.
    class FakeStream:
        def __init__(self, *a, **k):
            self.failed = True
            self.fail_reason = "boom: the real cause"
            # SET so load_model's _ready.wait() returns at once - failed=True
            # still raises (with the reason); leaving it unset would block the
            # full WARMUP_TIMEOUT_SEC (90s) and drag the whole suite.
            self._ready = threading.Event()
            self._ready.set()
        def start(self): pass
        def abort(self): pass
    orig = w.WhisperLiveStream
    w.WhisperLiveStream = FakeStream
    try:
        t = w.RemoteTranscriber(url="ws://127.0.0.1:9090")   # private -> passes policy
        try:
            t.load_model()
            assert False, "load_model should have raised"
        except RuntimeError as e:
            assert "boom: the real cause" in str(e), str(e)
            assert "not ready" in str(e)
    finally:
        w.WhisperLiveStream = orig


_test("remote: ws:// plaintext policy (private-only, override + warning)",
      t_remote_ws_policy)

_test("remote: Test surfaces the real failure reason (not generic 'not ready')",
      t_remote_fail_reason_surfaced)


def t_serve_host():
    """Serve mode HOST: port resolution + a not-listening probe, and the server
    turns raw faster-whisper segments into the WhisperLive `segments` shape the
    client expects (completed:true), never raising into the event loop."""
    import numpy as np
    import lia as w

    # ServeController.port(): config value, then default.
    sc = w.ServeController({"serve_port": 9191})
    assert sc.port() == 9191
    assert w.ServeController({}).port() == 9090
    assert w.ServeController({"serve_port": "bad"}).port() == 9090
    # A port nothing is listening on -> not running (no child, no bind).
    assert sc._port_listening(9) is False        # port 9 (discard) - closed here
    assert sc.owns_child() is False
    assert sc.is_running() is False

    # LiaTranscriptionServer._transcribe: raw segments -> completed segments.
    class FakeTr:
        model_size = "x"
        def transcribe_segments(self, audio, language=None, beam_size=3,
                                use_vocabulary=True):
            return [{"start": 0.0, "end": 1.2, "text": "שלום"},
                    {"start": 1.2, "end": 2.0, "text": "עולם"}]
    srv = w.LiaTranscriptionServer(FakeTr())
    out = srv._transcribe(np.zeros(16000, dtype=np.float32), "he")
    assert out == [{"start": 0.0, "end": 1.2, "text": "שלום", "completed": True},
                   {"start": 1.2, "end": 2.0, "text": "עולם", "completed": True}]
    # Empty audio -> no segments.
    assert srv._transcribe(np.zeros(0, dtype=np.float32), "he") == []

    # A transcriber that raises must yield [] (client falls back), never crash.
    class BoomTr:
        model_size = "x"
        def transcribe_segments(self, audio, language=None, beam_size=3,
                                use_vocabulary=True):
            raise RuntimeError("gpu gone")
    assert w.LiaTranscriptionServer(BoomTr())._transcribe(
        np.zeros(16000, dtype=np.float32), "he") == []

    # Token gate: no token -> always authorized.
    assert w.LiaTranscriptionServer(FakeTr(), token="")._authorized(None) is True


_test("serve: HOST port/probe + segment shape + fail-safe",
      t_serve_host)

def t_serve_secure_defaults():
    """WP1 security: serve mode is secure by default - resolve never yields
    0.0.0.0 implicitly, a non-loopback bind requires a token, auth is
    constant-time, only same-origin is allowed, the host vocab is gated to
    trusted clients, and the settings surface exposes the choice + a generator."""
    import types, threading as _th
    import numpy as _np
    import lia as w
    orig = w._tailscale_ipv4
    try:
        w._tailscale_ipv4 = lambda: ""
        assert w._resolve_serve_host({"serve_host": "auto"})[0] == "127.0.0.1"
        assert w._resolve_serve_host({"serve_host": "tailscale"})[0] is None
        w._tailscale_ipv4 = lambda: "100.70.1.2"
        assert w._resolve_serve_host({"serve_host": "auto"})[0] == "100.70.1.2"
        assert w._resolve_serve_host({"serve_host": "tailscale"})[0] == "100.70.1.2"
    finally:
        w._tailscale_ipv4 = orig
    assert w._resolve_serve_host({"serve_host": "loopback"})[0] == "127.0.0.1"
    assert w._resolve_serve_host({"serve_host": "all"})[0] == "0.0.0.0"
    assert w._serve_policy_check("127.0.0.1", "")[0] is True
    assert w._serve_policy_check("0.0.0.0", "")[0] is False
    assert w._serve_policy_check("0.0.0.0", "tok")[0] is True
    # default constructor binds loopback, never 0.0.0.0
    assert w.LiaTranscriptionServer(None).host == "127.0.0.1"
    srv = w.LiaTranscriptionServer(None, host="0.0.0.0", token="s3cret")
    R = lambda h: types.SimpleNamespace(request=types.SimpleNamespace(headers=h))
    assert srv._authorized(R({"Authorization": "Bearer s3cret"})) is True
    assert srv._authorized(R({"Authorization": "Bearer no"})) is False
    assert srv._authorized(R({})) is False
    assert srv._origin_ok(R({"Host": "h:9090", "Origin": "http://h:9090"})) is True
    assert srv._origin_ok(R({"Host": "h:9090"})) is True
    assert srv._origin_ok(R({"Host": "h:9090", "Origin": "http://evil.example"})) is False
    assert srv._request_trusted(R({"Authorization": "Bearer s3cret"})) is True
    assert srv._request_trusted(R({})) is False
    assert w.LiaTranscriptionServer(None, host="127.0.0.1", token="")._request_trusted(R({})) is True
    # transcribe_segments honors use_vocabulary (WP1 #19 vocab gating)
    cap = {}
    class _M:
        def transcribe(self, a, **kw):
            cap["p"] = kw.get("initial_prompt"); return ([], None)
    F = w.FasterWhisperTranscriber.__new__(w.FasterWhisperTranscriber)
    F.model = _M(); F.custom_vocabulary = "AWS, Docker"
    F._infer_lock = _th.Lock(); F._demote_to_cpu = lambda *a, **k: False
    F.transcribe_segments(_np.zeros(16000, dtype=_np.float32), use_vocabulary=True)
    assert cap["p"] and "AWS" in cap["p"]
    F.transcribe_segments(_np.zeros(16000, dtype=_np.float32), use_vocabulary=False)
    assert cap["p"] is None
    # settings surface
    App = w.LiaApp; app = App.__new__(App)
    app.config = dict(w.DEFAULT_CONFIG); app._serve = None
    ok, _m = App._set_serve_host(app, "all")
    assert ok and app.config["serve_host"] == "all"
    assert App._set_serve_host(app, "bogus")[0] is False
    ok, tok = App._gen_serve_token(app)
    assert ok and len(tok) >= 20 and app.config["serve_token"] == tok
    # ServeController refuses to start a network bind without a token
    app.config["serve_host"] = "all"; app.config["serve_token"] = ""
    sc = w.ServeController(app.config)
    ok, msg = sc.start()
    assert ok is False and "token" in msg.lower(), (ok, msg)


_test("serve: secure-by-default bind + token policy + origin + vocab gate (WP1)",
      t_serve_secure_defaults)

def t_privilege_boundaries():
    """WP3: system tools resolve to absolute trusted paths (no cwd/app-dir
    binary planting), elevated RunLevel-Highest autostart is refused from a
    user-writable install root, and a de-elevated serve child is tracked by pid
    for owns_child/stop."""
    import os as _os
    import lia as w
    ps = w._sys_exe("powershell")
    assert _os.path.isabs(ps) and ps.lower().endswith("powershell.exe")
    assert "system32" in ps.lower()
    exp = w._sys_exe("explorer")
    assert _os.path.isabs(exp) and exp.lower().endswith("explorer.exe")
    assert w._sys_exe("no_such_tool_xyz123") == "no_such_tool_xyz123"
    assert w._which_trusted("no_such_tool_xyz123") is None
    pf = _os.environ.get("ProgramFiles", r"C:\Program Files")
    assert w._install_root_is_protected(_os.path.join(pf, "Lia")) is True
    home = _os.environ.get("LOCALAPPDATA") or _os.path.join(
        _os.environ.get("USERPROFILE") or _os.path.expanduser("~"), "AppData", "Local")
    assert w._install_root_is_protected(_os.path.join(home, "Programs", "Lia")) is False
    downloads = _os.path.join(_os.environ.get("USERPROFILE") or _os.path.expanduser("~"),
                              "Downloads", "WhisperType", "lia")
    assert w._install_root_is_protected(downloads) is False
    sc = w.ServeController({})
    sc._pid = _os.getpid(); sc._proc = None
    assert sc.owns_child() is True
    sc._pid = 0x7FFFFFF0; sc._proc = None
    assert sc.owns_child() is False
    # .bat launchers hardened (#7/#13): absolute powershell, no %~f0 / %VAR%
    # interpolated into a single-quoted PowerShell string.
    here = _os.path.dirname(_os.path.abspath(__file__))
    for b in ("run.bat", "add_to_startup.bat"):
        txt = open(_os.path.join(here, b), encoding="utf-8").read()
        assert ("WindowsPowerShell" in txt and "powershell.exe" in txt), b + ": bare powershell"
        assert "-Verb RunAs" not in txt or "$env:LIA_SELF" in txt, b + ": %~f0 interpolated into PS"
        assert "CreateShortcut('%" not in txt, b + ": %VAR% interpolated into PS"


_test("security WP3: absolute system-tool paths + elevated-autostart ACL + serve pid track",
      t_privilege_boundaries)

def t_build_supply_chain_pins():
    """WP4: the shipped CPython embeddable AND get-pip.py are hash-pinned (no
    silent execution of an updated download), the working-tree copy fallback is
    gone, every offered faster-whisper model is revision-pinned, and
    make_checksums emits a correct SHA256SUMS.txt."""
    import os as _os, re as _re, hashlib, tempfile
    import lia as w
    import make_checksums as mc
    here = _os.path.dirname(_os.path.abspath(__file__))
    br = open(_os.path.join(here, "build_runtime.py"), encoding="utf-8").read()
    assert _re.search(r'PYTHON_EMBED_SHA256 = "[0-9a-f]{64}"', br), "embeddable SHA unpinned"
    assert _re.search(r'GET_PIP_SHA256 = "[0-9a-f]{64}"', br), "get-pip SHA unpinned"
    assert "falling back to direct copy" not in br, "working-tree copy fallback must be gone (#12)"
    for mid in w.MODELS:
        if mid.startswith("parakeet"):
            continue                     # onnx-asr: no revision API (documented)
        assert mid in w.MODEL_REVISIONS, "unpinned model offered: " + mid
    d = tempfile.mkdtemp()
    open(_os.path.join(d, "Lia-Setup-9.9.9.exe"), "wb").write(b"abc")
    open(_os.path.join(d, "Lia-Portable-9.9.9.zip"), "wb").write(b"xyz")
    out, files = mc.write_sha256sums([d])
    assert len(files) == 2 and _os.path.basename(out) == "SHA256SUMS.txt"
    body = open(out, encoding="utf-8").read()
    assert hashlib.sha256(b"abc").hexdigest() in body
    assert hashlib.sha256(b"xyz").hexdigest() in body


_test("security WP4: build supply-chain pins (embeddable/get-pip) + checksums",
      t_build_supply_chain_pins)

def t_client_hardening():
    """WP5/WP6: pasted text is control-char sanitized (keeps newlines + bidi
    marks), a summary base_url that would leak an API key over cleartext http to
    a public host is refused, no API key rides a URL query string, and the
    compose editor sanitizes saved summary HTML on load."""
    import os as _os
    import lia as w
    s = w._sanitize_for_paste
    assert s("hello" + chr(10) + "world" + chr(9) + "!") == "hello" + chr(10) + "world" + chr(9) + "!"
    assert s("a" + chr(0) + "b" + chr(7) + "c" + chr(27) + "d") == "abcd"
    assert s(chr(0x200f) + "\u05e9\u05dc\u05d5\u05dd") == chr(0x200f) + "\u05e9\u05dc\u05d5\u05dd"
    assert s("") == "" and s(None) is None
    ok = w._summary_base_url_ok
    assert ok("")[0] is True
    assert ok("https://api.openai.com/v1")[0] is True
    assert ok("http://localhost:11434/v1")[0] is True
    assert ok("http://127.0.0.1:11434")[0] is True
    assert ok("http://192.168.1.9:11434")[0] is True
    assert ok("http://evil.example.com/v1")[0] is False
    here = _os.path.dirname(_os.path.abspath(__file__))
    src = open(_os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert '"?key=" + self.api_key' not in src, "GeminiLiveStream still puts the key in the URL"
    ce = open(_os.path.join(here, "compose_editor.py"), encoding="utf-8").read()
    assert "sanitizeCardHtml(d.card_html" in ce, "compose editor must sanitize card_html on load"


_test("security WP5/WP6: paste sanitizer + base_url guard + no key-in-URL + compose sanitize",
      t_client_hardening)

def t_ollama_has_staticmethod():
    """_ollama_has is a @staticmethod: called as self._ollama_has(model, pulled)
    it must NOT bind self to `model` (the 'takes 2 positional arguments but 3
    were given' crash when a local summary model was selected, 2026-09-01)."""
    import lia as w
    app = w.LiaApp.__new__(w.LiaApp)
    pulled = {"gemma4:31b", "llama3.3:70b"}
    assert app._ollama_has("gemma4:31b", pulled) is True      # exact
    assert app._ollama_has("gemma4", pulled) is True          # base-tag match
    assert app._ollama_has("qwen:7b", pulled) is False        # absent
    # also callable off the class directly (staticmethod)
    assert w.LiaApp._ollama_has("llama3.3:70b", pulled) is True


_test("ollama: _ollama_has is a staticmethod (self+model+pulled crash)",
      t_ollama_has_staticmethod)


def t_model_revision_pins():
    """Supply-chain: every faster-whisper model offered in MODELS is pinned to
    a reviewed HF commit; the pin is passed at load. Parakeet (onnx-asr) has
    no revision API - a documented gap, not a silent one."""
    import re as _re
    import lia as w
    for mid in w.MODELS:
        if mid.startswith("parakeet"):
            continue   # onnx-asr: no revision parameter (documented)
        assert w.MODEL_REVISIONS.get(mid), "unpinned model: " + mid
        assert _re.fullmatch(r"[0-9a-f]{40}", w.MODEL_REVISIONS[mid]), mid
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert 'kwargs["revision"] = rev' in src, "revision not passed to WhisperModel"
    assert "documented in SECURITY.md" in src or "onnx-asr" in src


_test("models: HF downloads pinned to reviewed revisions", t_model_revision_pins)


def t_delete_all_data():
    """Settings -> Advanced -> Delete all my data: allowlisted, guarded
    (recording/meeting active, unexpected folder), and the accept path hands
    the CONFIG_DIR to the wipe worker."""
    import time as _time
    import lia as w
    App = w.LiaApp
    assert "delete_all_data" in App._settings_action_map(App.__new__(App))
    # guard: active meeting
    app = App.__new__(App)
    app._is_meeting_active = lambda: True
    ok, msg = app._settings_delete_all_data()
    assert not ok and "meeting" in msg.lower()
    # guard: CONFIG_DIR that is not a Lia folder is refused
    app2 = App.__new__(App)
    app2._is_meeting_active = lambda: False
    app2.is_recording = False
    old_dir = w.CONFIG_DIR
    w.CONFIG_DIR = tempfile.mkdtemp(prefix="not_lia_")
    try:
        ok, msg = app2._settings_delete_all_data()
        assert not ok and "Unexpected" in msg
        # accept path: a dir actually named Lia -> worker gets it (wipe mocked)
        lia_dir = os.path.join(tempfile.mkdtemp(prefix="wipe_"), "Lia")
        os.makedirs(lia_dir)
        w.CONFIG_DIR = lia_dir
        calls = []
        app2._wipe_data_and_quit = lambda target: calls.append(target)
        ok, msg = app2._settings_delete_all_data()
        assert ok, msg
        for _ in range(30):
            if calls:
                break
            _time.sleep(0.1)
        assert calls == [lia_dir], calls
    finally:
        w.CONFIG_DIR = old_dir
    # the settings page offers it with a typed confirm
    here = os.path.dirname(os.path.abspath(__file__))
    sw = open(os.path.join(here, "settings_window.py"), encoding="utf-8").read()
    assert "data-wipe" in sw and 'call("delete_all_data"' in sw
    assert "DELETE" in sw, "typed confirmation missing"


_test("settings: delete-all-data action (guards + wiring + typed confirm)",
      t_delete_all_data)


def t_models_engine_menu():
    """Models live on the Settings → Models page now (2026-08-15 makeover): the
    dictation / meeting / summary / file model pickers + the Auto/GPU/CPU device
    radio are produced by _settings_tables and driven by the SETTINGS_ACTIONS
    allowlist, not by tray submenus."""
    import inspect
    import lia as wt
    App = wt.LiaApp
    app = App.__new__(App)
    actions = App._settings_action_map(app)
    for a in ("set_dictation_model", "set_meeting_model", "set_summary_model",
              "set_file_model", "set_whisper_device"):
        assert a in actions and callable(actions[a][0]), a
    # _settings_tables yields every model group + the device radio.
    app.config = {"openai_api_key": "sk", "model_size": "large-v3-turbo",
                  "transcription_backend": "local", "whisper_device": "auto"}
    app._ollama_cache = None
    t = App._settings_tables(app, ollama=False)
    for grp in ("dictation", "meeting", "summary", "file", "device"):
        assert t.get(grp), "empty model group: " + grp
    assert any(r["key"] == "auto" and r["checked"] for r in t["device"]), t["device"]
    hs = inspect.getsource(App._set_whisper_device)
    # switch guards + full effect: cache cleared, transcriber rebuilt+reloaded
    for marker in ("is_recording", "_is_meeting_active", "_meeting_xcribers.clear",
                   "_make_local_transcriber", "_load_model"):
        assert marker in hs, f"_set_whisper_device missing {marker}"


_test("settings: Models page (dictation/meeting/summary/file/device) + actions",
      t_models_engine_menu)


def t_tray_lean_layout():
    """Lean tray (2026-08-15 makeover): a status line + capture toggles +
    meeting group + History + a single "Settings…" (default=True) + Quit.
    Everything configurable moved to the Settings window; the deep Options
    submenus are gone; Email/Chat handlers are still present (hotkey-only)."""
    import inspect
    import lia as wt
    App = wt.LiaApp
    run_src = inspect.getsource(App.run)
    # the lean set is present
    for lbl in ('"Record my microphone"', '"Record system audio"', '"History"',
                '"Settings…"', '"Quit"'):
        assert lbl in run_src, "lean tray missing " + lbl
    assert "self._open_settings_window()" in run_src, "Settings… not wired"
    # the old deep submenus are gone from the tray
    for gone in ('"Behavior"', '"Input Selection"', '"Model Selection"',
                 '"API Keys"', '"Custom Vocabulary"', '"Beep Output"'):
        assert gone not in run_src, "old submenu still in tray: " + gone
    # Email + Chat stay out of the tray but the handlers remain (hotkey-only)
    assert '"🔎  Email search"' not in run_src and '"💬  Chat"' not in run_src
    assert hasattr(App, "_open_email_search") and hasattr(App, "_open_chat_window")
    # History once, after Cancel Meeting, before Settings…
    assert run_src.count('"History"') == 1
    hist = run_src.index('"History"')
    assert hist > run_src.index("Cancel Meeting")
    assert hist < run_src.index('"Settings…"')
    # exactly one REAL default=True (on Settings…), ignoring comment lines
    real_default = sum(1 for l in run_src.splitlines()
                       if "default=True" in l and not l.strip().startswith("#"))
    assert real_default == 1, "exactly one default=True item expected, got %d" % real_default


_test("tray: lean layout (status + capture + meeting + History + Settings + Quit)",
      t_tray_lean_layout)


def t_tray_onboarding():
    """First-run tray onboarding (2026-08-28): Windows 11 hides new tray
    icons in the ^ overflow, so a first-time user concludes the app did not
    start. BEHAVIORAL, with promote/save stubbed: the balloon fires exactly
    once and BEFORE the promote poll (it must not wait behind up to 15s of
    Explorer lag), the promote flag persists only on verified success (a
    failed promote must retry next launch), both flags share ONE config
    save, a second run is a complete no-op, and a save_config raise
    (fail-closed DPAPI) never escapes. Plus: the real _promote_tray_icon
    carries the shared-interpreter identity gate (a python.exe-hosted run
    must never promote OTHER apps' NotifyIconSettings entries), and the
    hook is wired from the tray-ready callback."""
    import inspect
    import types
    import lia as wt
    calls = []

    class Icon:
        def notify(self, msg, title):
            calls.append(("notify", msg, title))

    orig_promote, orig_save = wt._promote_tray_icon, wt.save_config
    try:
        # Failed promote: balloon still shows (first!), welcome flag set,
        # promote flag NOT persisted, one save for the dirty welcome flag.
        wt._promote_tray_icon = (
            lambda *a, **k: (calls.append(("promote",)), False)[1])
        wt.save_config = lambda cfg: calls.append(("save",))
        fake = types.SimpleNamespace(config={})
        wt.LiaApp._tray_first_run_onboarding(fake, Icon())
        # (2026-09-01) A failed promote also tells the user WHERE the icon
        # is - once - since the overflow is exactly why "it didn't start".
        assert [c[0] for c in calls] == ["notify", "promote", "notify", "save"], calls
        assert fake.config.get("_first_run_welcome_shown") is True
        assert "_tray_icon_promoted" not in fake.config, \
            "a failed promote must not persist the flag (no retry ever)"
        assert fake.config.get("_tray_hint_shown") is True
        assert "overflow" in calls[2][1] and "Lia" in calls[2][2], calls[2]
        assert "ctrl+space" in calls[0][1]

        # Second launch after that failure: promote retries, hint does NOT.
        calls.clear()
        wt.LiaApp._tray_first_run_onboarding(fake, Icon())
        assert [c[0] for c in calls] == ["promote"], calls

        # Successful promote: both flags, exactly ONE save, hotkey honored,
        # no "where is it" hint.
        calls.clear()
        wt._promote_tray_icon = lambda *a, **k: True
        fake2 = types.SimpleNamespace(config={"hotkey": "f9"})
        wt.LiaApp._tray_first_run_onboarding(fake2, Icon())
        assert fake2.config.get("_tray_icon_promoted") is True
        assert fake2.config.get("_first_run_welcome_shown") is True
        assert [c[0] for c in calls].count("save") == 1, calls
        assert [c[0] for c in calls].count("notify") == 1, calls
        assert "f9" in calls[0][1]

        # Both flags set: complete no-op (no balloon spam, no save).
        calls.clear()
        wt.LiaApp._tray_first_run_onboarding(types.SimpleNamespace(config={
            "_tray_icon_promoted": True, "_first_run_welcome_shown": True}),
            Icon())
        assert calls == [], calls

        # A fail-closed save_config must be swallowed, not crash the thread.
        def _boom(cfg):
            raise RuntimeError("dpapi down")
        wt.save_config = _boom
        wt.LiaApp._tray_first_run_onboarding(
            types.SimpleNamespace(config={}), Icon())
    finally:
        wt._promote_tray_icon, wt.save_config = orig_promote, orig_save

    promo_src = inspect.getsource(wt._promote_tray_icon)
    for needle in ("NotifyIconSettings", "IsPromoted", "ExecutablePath"):
        assert needle in promo_src, "_promote_tray_icon missing " + needle
    assert "_lia_owned_executable" in promo_src, \
        "shared-interpreter identity gate missing"
    # A freshly written promotion is applied by re-registering the live icon.
    assert "_reregister_tray_icon" in promo_src and "icon=None" in promo_src
    assert "_tray_first_run_onboarding" in inspect.getsource(wt.LiaApp.run), \
        "onboarding not wired in run()"


_test("tray: first-run onboarding (overflow promote + welcome balloon)",
      t_tray_onboarding)


def t_menu_ai_cleanup_surfaced():
    """AI Cleanup now lives on the Settings → AI Cleanup page.

    Regression guard (the feature once shipped with NO way to turn it on):
    (1) the style + provider actions are in the SETTINGS_ACTIONS allowlist;
    (2) _settings_tables yields the style radios (Spoken included) and, when a
    key is set, the provider/model radios; (3) the cleanup label helper works;
    (4) the tray no longer wires the old builders — and the only orphaned
    _build_*_menu methods are the eight tray builders retired pending the
    tk-consolidation phase (no NEW accidental orphan)."""
    import inspect, re
    import lia as wt
    App = wt.LiaApp
    app = App.__new__(App)
    actions = App._settings_action_map(app)
    assert "set_cleanup_style" in actions and "set_cleanup_provider_model" in actions
    # (2) style rows (Spoken + Off) + provider rows when a key is present.
    app.config = {"openai_api_key": "sk-X", "cleanup_style": "off",
                  "cleanup_provider": "auto", "cleanup_llm_model_openai": "gpt-5.6-luna"}
    t = App._settings_tables(app, ollama=False)
    styles = [r["style"] for r in t["cleanup_styles"]]
    assert "spoken" in styles and "off" in styles, styles
    assert t["cleanup_models"], "provider/model rows expected when a key is set"
    assert app._cleanup_model_label() == "gpt-5.6-luna"
    # no key → no provider rows (the page shows the empty-state link to Keys).
    app.config = {}
    assert App._settings_tables(app, ollama=False)["cleanup_models"] == []
    # (4) the 8 tray _build_*_menu builders were deleted in P4 (their surface now
    #     lives in _settings_tables). Assert none linger orphaned; if a future
    #     builder is (re)introduced it must be wired (referenced beyond its def).
    cls_src = inspect.getsource(App)
    builders = re.findall(r"^\s*def (_build_\w*menu\w*)\(", cls_src, re.M)
    orphans = {b for b in builders
               if len(re.findall(r"\b%s\b" % re.escape(b), cls_src)) < 2}
    assert not orphans, "orphaned menu builder(s) - defined but never wired: %s" % orphans


_test("settings: AI Cleanup page (styles + provider) + no new orphan builders",
      t_menu_ai_cleanup_surfaced)


def t_cleanup_provider_and_models():
    """AI Cleanup provider picker: Groq (free) selectable alongside OpenAI,
    gpt-4o removed, the gpt-5.6 tier (luna/sol/terra) added, and the effective
    provider resolves correctly from cleanup_provider + which keys exist."""
    import lia as wt
    App = wt.LiaApp
    ids = [m for _l, m in App.CLEANUP_OPENAI_MODELS]
    for m in ("gpt-4o", "gpt-4.1", "gpt-5.1"):
        assert m not in ids, f"{m} should be removed from the cleanup picker: {ids}"
    for m in ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"):
        assert m in ids, f"{m} missing from cleanup models: {ids}"
    assert wt.DEFAULT_CONFIG.get("cleanup_provider") == "auto"
    assert wt.DEFAULT_CONFIG.get("cleanup_llm_model_openai") == "gpt-5.6-luna"
    # cleanup ships OFF — the default model only matters once turned on.
    assert wt.DEFAULT_CONFIG.get("cleanup_style") == "off"
    # ⭐ default marker sits on luna, not another row.
    starred = [m for l, m in App.CLEANUP_OPENAI_MODELS if "⭐" in l]
    assert starred == ["gpt-5.6-luna"], starred

    app = App.__new__(App)
    # auto + both keys → OpenAI (pay-as-you-go, no daily cap).
    app.config = {"openai_api_key": "sk-X", "groq_api_key": "gsk_Y",
                  "cleanup_provider": "auto", "cleanup_llm_model_openai": "gpt-5.6-sol"}
    assert app._effective_cleanup_provider() == "openai"
    assert app._cleanup_model_label() == "gpt-5.6-sol"
    # explicit groq wins even with an OpenAI key present (the free path).
    app.config["cleanup_provider"] = "groq"
    assert app._effective_cleanup_provider() == "groq"
    assert "free" in app._cleanup_model_label().lower()
    # a pinned provider whose key is gone falls back gracefully.
    app.config = {"groq_api_key": "gsk_Y", "cleanup_provider": "openai"}
    assert app._effective_cleanup_provider() == "groq"
    app.config = {"cleanup_provider": "groq"}   # no keys at all
    assert app._effective_cleanup_provider() is None
    assert app._cleanup_model_label() == "no key"

    # The Settings AI-Cleanup provider picker (_settings_tables.cleanup_models)
    # shows a Groq row when a Groq key is set + the OpenAI rows when that key is
    # set — both present with both keys, and no gpt-4o.
    app.config = {"openai_api_key": "sk-X", "groq_api_key": "gsk_Y",
                  "cleanup_provider": "groq", "cleanup_llm_model_openai": "gpt-5.6-luna"}
    app._ollama_cache = None
    picker = [r["label"] for r in App._settings_tables(app, ollama=False)["cleanup_models"]]
    assert any("Groq" in l for l in picker), picker
    assert any("ChatGPT 5.6 Sol" in l for l in picker), picker
    assert not any("GPT-4o" in l for l in picker), picker

    # _make_cleanup_cleaner routes to the right client class.
    app.config = {"openai_api_key": "sk-X", "cleanup_provider": "openai",
                  "cleanup_llm_model_openai": "gpt-5.6-luna"}
    c = app._make_cleanup_cleaner()
    assert isinstance(c, wt.OpenAILLMCleaner) and c.model == "gpt-5.6-luna"
    app.config = {"groq_api_key": "gsk_Y", "cleanup_provider": "groq",
                  "cleanup_llm_model": "llama-3.3-70b-versatile"}
    c = app._make_cleanup_cleaner()
    assert isinstance(c, wt.GroqLLMCleaner)
    app.config = {"cleanup_provider": "auto"}
    assert app._make_cleanup_cleaner() is None

    # --- Gemini (free) provider ---
    gm_ids = [m for _l, m in App.CLEANUP_GEMINI_MODELS]
    assert "gemini-3.5-flash-lite" in gm_ids, gm_ids      # fastest AND most accurate
    # 3.7 Flash dropped from CLEANUP (thinking -> ~3.7s + returned text uncleaned);
    # it stays a SUMMARY option, which uses a different config key.
    assert "gemini-3.7-flash" not in gm_ids, gm_ids
    assert "gemini-3.7-flash-lite" not in gm_ids, "no such model exists"
    assert wt.DEFAULT_CONFIG.get("cleanup_llm_model_gemini") == "gemini-3.5-flash-lite"
    # explicit gemini pick wins; label reads free; cleaner points at the
    # Gemini OpenAI-compat endpoint with the gemini key.
    app.config = {"gemini_api_key": "AIza_Z", "openai_api_key": "sk-X",
                  "cleanup_provider": "gemini",
                  "cleanup_llm_model_gemini": "gemini-3.5-flash-lite"}
    assert app._effective_cleanup_provider() == "gemini"
    assert app._cleanup_model_label() == "Gemini · free"
    c = app._make_cleanup_cleaner()
    assert isinstance(c, wt.OpenAILLMCleaner) and c.model == "gemini-3.5-flash-lite"
    assert c.chat_url == wt.GEMINI_CHAT_URL
    # auto falls back to Gemini only when it's the ONLY key present.
    app.config = {"gemini_api_key": "AIza_Z", "cleanup_provider": "auto"}
    assert app._effective_cleanup_provider() == "gemini"
    # the Settings cleanup picker shows the Gemini row when the key is set.
    app.config = {"gemini_api_key": "AIza_Z", "cleanup_provider": "gemini",
                  "cleanup_llm_model_gemini": "gemini-3.5-flash-lite"}
    app._ollama_cache = None
    picker = [r["label"] for r in App._settings_tables(app, ollama=False)["cleanup_models"]]
    assert any("Flash-Lite" in l for l in picker), picker
    assert not any("3.7 Flash" in l for l in picker), picker   # dropped from cleanup

    # Orphan-pin migration must be DATA-DRIVEN off the picker lists, so a model
    # dropped from the picker (gpt-4o/4o-mini, gpt-5.1, gpt-4.1, gemini-3.7-flash)
    # can't keep silently running with no checked radio row. The block lives in
    # __init__ (not callable in isolation) -> wiring asserted at source level.
    import os
    wsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "lia.py"), encoding="utf-8").read()
    assert "_oa_ids = [m for _l, m in self.CLEANUP_OPENAI_MODELS]" in wsrc, \
        "OpenAI cleanup-model normalisation must be data-driven off the picker list"
    assert "if _ocm not in _oa_ids:" in wsrc
    assert "_gm_ids = [m for _l, m in self.CLEANUP_GEMINI_MODELS]" in wsrc, \
        "Gemini cleanup-model normalisation must be data-driven off the picker list"
    assert "if _gcm and _gcm not in _gm_ids:" in wsrc
    # the summary + meetings pickers KEEP 3.7 (separate keys, thinking is an asset)
    assert wt.DEFAULT_CONFIG.get("meetings_answer_model_gemini") == "gemini-3.7-flash"


_test("cleanup: provider picker (Groq + Gemini free + gpt-5.6 tier, gpt-4o dropped)",
      t_cleanup_provider_and_models)


def t_cleanup_gpt5_no_temperature():
    """Regression: gpt-5.x / o-series cleanup must NOT send `temperature` — those
    models reject any non-default value with HTTP 400, which clean() swallowed into a
    raw-text fallback ('OpenAI cleanup does nothing', verified live 2026-08-15). Legacy
    chat models (Groq llama / Gemini / gpt-4.1) keep max_tokens + temperature."""
    import lia as w
    sample = "this is a test sentence long enough to clear the cleanup length floor guards"
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": sample}}]}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResp()

    def shaping(model):
        c = w.OpenAILLMCleaner("sk-test", model=model)
        c._ensure_session = lambda: FakeSession()
        captured.clear()
        c.clean(sample, style="proofread")
        return captured["payload"]

    for m in ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.1", "o3-mini"):
        p = shaping(m)
        assert "temperature" not in p, (m, "must omit temperature", list(p))
        assert "max_completion_tokens" in p, (m, list(p))
    for m in ("llama-3.3-70b-versatile", "gemini-3.7-flash", "gpt-4.1"):
        p = shaping(m)
        assert p.get("temperature") == 0.1, (m, "legacy models keep temp", p.get("temperature"))
        assert "max_tokens" in p, (m, list(p))


_test("cleanup: gpt-5.x/o-series omit temperature (OpenAI 400 fix)",
      t_cleanup_gpt5_no_temperature)


def t_email_semantic_live():
    _skip_live("bge-m3 embeddings")
    import tempfile
    import email_index as ei
    conn = _email_test_db()
    vp = os.path.join(tempfile.gettempdir(), "wt_emailvec_%d.db" % os.getpid())
    for s in (vp, vp + "-wal", vp + "-shm"):
        try:
            os.remove(s)
        except OSError:
            pass
    vconn = ei.connect(vp)
    ei.init_vectors(vconn)
    for r in conn.execute("SELECT id, subject, body_clean FROM messages"):
        ch = ei.chunk_text(r["subject"], r["body_clean"])
        vv = ei.embed_texts(ch)
        ei.store_chunks(vconn, r["id"], ch, vv)
    ei._VEC_CACHE["n"] = -1
    res = ei.semantic_search(conn, vconn, "פרויקט הגירה לענן", limit=2)
    assert len(res) >= 1, "semantic search returned nothing"


_test("email index: bge-m3 embed + semantic (LIVE)", t_email_semantic_live)


def t_vocab_miner():
    """vocab_learn.mine_texts: Latin-in-Hebrew extraction, adjacent-pair
    phrases, stopword/known/punctuation filtering."""
    import vocab_learn as vl
    texts = [
        "דיברנו על Terraform ועל Terraform וגם Terraform בענן",
        "יש לנו API Gateway וגם API Gateway ועוד API Gateway פה",
        "אמרנו the and is וגם AWS. פעם AWS. ועוד AWS: כאן",
    ]
    cands = vl.mine_texts(texts, known=(), min_freq=3)
    terms = {c["term"] for c in cands}
    assert "Terraform" in terms, terms
    assert "API Gateway" in terms, terms          # adjacent-pair phrase
    assert "AWS" in terms, terms                  # trailing punctuation stripped
    assert "the" not in terms and "and" not in terms, terms  # stopwords
    # known-suppression is case-insensitive and post-normalization
    cands2 = vl.mine_texts(texts, known={"terraform", "aws"}, min_freq=3)
    t2 = {c["term"] for c in cands2}
    assert "Terraform" not in t2 and "AWS" not in t2, t2


_test("vocab: miner heuristics (latin-in-hebrew, phrases, filters)", t_vocab_miner)


def t_vocab_store_roundtrip():
    """VocabStore: manual migration (idempotent), candidates → review →
    compose (budget + manual-at-tail), rejected never resurfaces, persistence."""
    import tempfile
    import vocab_learn as vl
    path = os.path.join(tempfile.gettempdir(), "wt_vocab_rt_%d.json" % os.getpid())
    try:
        os.remove(path)
    except OSError:
        pass
    s = vl.VocabStore(path)
    assert s.migrate_manual("git, push, Kubernetes") == 3
    assert s.migrate_manual("git, push, Kubernetes") == 0     # idempotent
    added = s.add_candidates(
        [{"term": "Terraform", "count": 9, "sample": "x"},
         {"term": "Datadog", "count": 4, "sample": "y"}], source="test")
    assert added == 2 and s.pending_count() == 2
    s.resolve(["Terraform"], approve=True)
    s.resolve(["Datadog"], approve=False)
    p = s.compose_prompt(600)
    assert "Terraform" in p and "Datadog" not in p and "Kubernetes" in p, p
    # manual terms sit at the TAIL (Whisper keeps the prompt tail)
    assert p.index("Terraform") < p.index("git"), p
    # usage bump with word-ish boundaries: "gitlab" must NOT bump "git"
    assert s.observe_text("ראן git push וגם gitlab שם") >= 2
    s.save()
    s2 = vl.VocabStore(path)                                  # reload from disk
    assert s2.pending_count() == 0
    assert "Terraform" in s2.compose_prompt(600)
    again = vl.mine_texts(["Datadog Datadog Datadog"],
                          known=s2.known_keys(), min_freq=3)
    assert not any(c["term"] == "Datadog" for c in again), "rejected resurfaced"
    os.remove(path)


_test("vocab: store roundtrip (migrate/review/compose/persist/reject)",
      t_vocab_store_roundtrip)


def t_vocab_wiring():
    """lia wiring: config keys, store methods, menu items, and the
    composed-vocabulary choke point actually feeding the transcriber pushes."""
    import lia as w
    for key, default in (("vocab_autolearn", True),
                         ("vocab_prompt_budget_chars", 600),
                         ("vocab_min_freq", 3)):
        assert w.DEFAULT_CONFIG.get(key) == default, key
    for m in ("_composed_vocabulary", "_push_vocabulary_live",
              "_vocab_rebuild", "_vocab_observe",
              "_vocab_mine_texts_async", "_vocab_pending_count"):
        assert hasattr(w.LiaApp, m), m
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert src.count("self._composed_vocabulary()") >= 15, \
        "transcriber push-sites should read the composed vocabulary"
    # the vocabulary surface now lives on the Settings → Vocabulary page.
    App = w.LiaApp
    actions = App._settings_action_map(App.__new__(App))
    for a in ("save_vocabulary", "vocab_pending_list", "vocab_resolve",
              "vocab_rebuild", "toggle_vocab_autolearn"):
        assert a in actions, "Settings vocab action missing: " + a
    # meeting auto-learn hook present in the chunked output path
    assert "_vocab_mine_texts_async([content]" in src


_test("vocab: lia wiring (config, methods, settings actions, choke point)",
      t_vocab_wiring)


def t_vocab_corrections():
    """Corrections engine: summary-tail splitter, whole-word applier, store
    guards (never rewrite an approved term), seed-once flag, meeting wiring."""
    import tempfile
    import vocab_learn as vl
    import lia as w
    # splitter: clean / no-marker / malformed
    s, p = w._split_summary_corrections(
        'סיכום\n===CORRECTIONS===\n[{"wrong": "Bedrook", "right": "Bedrock"}]')
    assert s == "סיכום" and p == [{"wrong": "Bedrook", "right": "Bedrock"}], (s, p)
    s, p = w._split_summary_corrections("סיכום בלי בלוק")
    assert s == "סיכום בלי בלוק" and p == []
    s, p = w._split_summary_corrections("סיכום\n===CORRECTIONS===\nbroken [")
    assert s == "סיכום" and p == [], (s, p)
    # applier: whole-word, case-insensitive, multiword; partial words untouched
    t, c = vl.apply_corrections(
        "על Bedrook ועל bedrook וגם Bedrooks ו-Lending Zone",
        [{"wrong": "Bedrook", "right": "Bedrock"},
         {"wrong": "Lending Zone", "right": "Landing Zone"}])
    assert t == "על Bedrock ועל Bedrock וגם Bedrooks ו-Landing Zone", t
    assert c == {"bedrook": 2, "lending zone": 1}, c
    # store guards + seed-once + persistence
    path = os.path.join(tempfile.gettempdir(), "wt_corr_t_%d.json" % os.getpid())
    try:
        os.remove(path)
    except OSError:
        pass
    st = vl.VocabStore(path)
    st.migrate_manual("Bedrock")
    n = st.add_corrections([
        {"wrong": "Bedrook", "right": "Bedrock"},
        {"wrong": "Bedrock", "right": "Bedrook"},   # approved term → refused
        {"wrong": "x", "right": "y"},               # too short → refused
    ])
    assert n == 1, n
    assert st.seed_once("f", [{"wrong": "CloudTrain", "right": "CloudTrail"}]) == 1
    assert st.seed_once("f", [{"wrong": "Q", "right": "R"}]) == 0
    assert len(vl.VocabStore(path).corrections()) == 2
    os.remove(path)
    # wiring: summarize takes the new params; meeting path applies + collects
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert "vocab=None, collect_corrections=False" in src
    assert "collect_corrections=True" in src            # meeting call site
    assert "_vocab_apply_corrections(" in src
    # corrections run on DICTATION/file output (both fallback choke points)
    # and on meeting chunks (live transcript gets fixed text in real time)
    assert src.count('label="dictation"') == 2, "both fallback sites"
    assert 'label="chunk"' in src
    assert "_VOCAB_CORRECTIONS_SEED" in src
    assert w.DEFAULT_CONFIG.get("vocab_corrections") is True
    # corrections surface now lives on the Settings → Vocabulary page.
    App = w.LiaApp
    actions = App._settings_action_map(App.__new__(App))
    for a in ("vocab_corrections_list", "vocab_add_correction",
              "vocab_remove_correction", "vocab_learned_list", "vocab_remove_learned"):
        assert a in actions, "Settings corrections action missing: " + a
    # store API for the settings page + the (retained-pending-P4) dialogs
    import vocab_learn as vl2
    for fn in ("remove_corrections", "approved_autos"):
        assert hasattr(vl2.VocabStore, fn), fn
    # manual force-add overrides the approved-term guard + updates right side
    import tempfile as tf2
    p2 = os.path.join(tf2.gettempdir(), "wt_corrf_%d.json" % os.getpid())
    try:
        os.remove(p2)
    except OSError:
        pass
    st2 = vl2.VocabStore(p2)
    st2.migrate_manual("Bedrock")
    assert st2.add_corrections([{"wrong": "Bedrock", "right": "Bedroc2"}]) == 0
    assert st2.add_corrections([{"wrong": "Bedrock", "right": "Bedroc2"}],
                               source="manual", force=True) == 1
    assert st2.add_corrections([{"wrong": "Bedrock", "right": "Bedroc3"}],
                               source="manual", force=True) == 1  # update right
    assert st2.corrections()[0]["right"] == "Bedroc3"
    assert st2.remove_corrections(["Bedrock"]) == 1
    assert st2.corrections() == []
    os.remove(p2)


_test("vocab: corrections engine (splitter/applier/guards/seed/wiring)",
      t_vocab_corrections)


# ---- Ask your meetings (RAG index) ----------------------------------------
# Synthetic fixtures reproduce the REAL on-disk format verified 2026-08-15:
# every line RLM-prefixed (U+200F), title header "Meeting <EM DASH> ...", a
# bare-date header whose real title lives under "כותרת הדיון", ═/─ separators,
# meta lines, and "[mm:ss] Speaker X:" transcript lines.
_MEET_DIARIZED = (
    "\u200fMeeting \u2014 IAA-AWS\n"
    "\u200fSource: both (diarized via pyannote + ivrit.ai (local))\n"
    "\u200fDuration: 1:11:37\n"
    "\u200f\n"
    "\u200f" + "\u2500" * 60 + "\n"
    "\u200fSummary\n"
    "\u200f" + "\u2500" * 60 + "\n"
    "\u200fכותרת הדיון\n"
    "\u200fבחינת ארכיטקטורת האבטחה של Landing Zone\n"
    "\u200f\n"
    "\u200f" + "\u2550" * 60 + "\n"
    "\u200fTranscript\n"
    "\u200f" + "\u2550" * 60 + "\n"
    "\u200f\n"
    "\u200f[0:00] Speaker A:\n"
    "\u200fצריך הרשאות read-only לחשבון ה-Management עבור GuardDuty\n"
    "\u200f[0:21] Speaker B:\n"
    "\u200fאני מסכים\n"
)
# Old May-2026 generation: no seconds / no slug in the name, header is a bare
# date, and there is NO "כותרת הדיון" -> title falls back to the date string.
_MEET_OLD = (
    "\u200fMeeting \u2014 2026-05-28 14:24\n"
    "\u200fSource: both (diarized via AssemblyAI)\n"
    "\u200fDuration: 0:15\n"
    "\u200f\n"
    "\u200f" + "\u2500" * 60 + "\n"
    "\u200fSummary\n"
    "\u200f" + "\u2500" * 60 + "\n"
    "\u200fבקצרה\n"
    "\u200fדוגמה קצרה\n"
)


def t_meetings_parser():
    """meetings_index parser: file filtering + title/date/speaker/body extraction
    across BOTH real filename generations, with RLM + em-dash handling."""
    import meetings_index as mi
    # file filtering
    assert mi.is_indexable_meeting("2026-08-13_15-55-17_x_meeting.txt")
    assert mi.is_indexable_meeting("2026-08-13_15-55-17_x_meeting_diarized.txt")
    for bad in ("a_meeting_LIVE.txt", "a_meeting_PARTIAL.txt", "a_meeting_audio.wav",
                "a_summary.html", "a_meeting_summary.bak.html"):
        assert not mi.is_indexable_meeting(bad), bad

    import tempfile, os, datetime
    d = tempfile.mkdtemp(prefix="meetparse_")
    # new diarized with a real title
    p1 = os.path.join(d, "2026-08-13_15-55-17_IAA-AWS_meeting_diarized.txt")
    open(p1, "w", encoding="utf-8").write(_MEET_DIARIZED)
    r = mi.parse_meeting_file(p1)
    assert r["subject"] == "IAA-AWS", r["subject"]                 # em-dash title
    assert r["folder"] == "diarized"
    assert r["sender_name"] == "Speaker A, Speaker B", r["sender_name"]
    assert r["received_ts"] == int(datetime.datetime(2026, 8, 13, 15, 55, 17).timestamp())
    assert "\u200f" not in r["body"], "RLM leaked into body"
    assert "GuardDuty" in r["body"] and "Landing Zone" in r["body"]
    assert "\u2500" not in r["body"] and "Source:" not in r["body"], "separator/meta leaked"
    # bare-date header -> title from "כותרת הדיון"
    p2 = os.path.join(d, "2026-08-13_15-55-17_x_meeting.txt")
    open(p2, "w", encoding="utf-8").write(_MEET_DIARIZED.replace("Meeting \u2014 IAA-AWS",
                                                                 "Meeting \u2014 2026-08-13 15:55"))
    r2 = mi.parse_meeting_file(p2)
    assert r2["subject"] == "בחינת ארכיטקטורת האבטחה של Landing Zone", r2["subject"]
    assert r2["folder"] == "chunked"
    # old generation: no seconds, no כותרת הדיון -> title stays the date
    p3 = os.path.join(d, "2026-05-28_14-24_meeting_diarized.txt")
    open(p3, "w", encoding="utf-8").write(_MEET_OLD)
    r3 = mi.parse_meeting_file(p3)
    assert r3["received_ts"] == int(datetime.datetime(2026, 5, 28, 14, 24, 0).timestamp())
    assert r3["subject"] == "2026-05-28 14:24", r3["subject"]
    assert r3["sender_name"] == ""   # no speaker labels


def t_meetings_index_db():
    """meetings_index DB layer (no Ollama): upsert + content_hash change detection,
    recent() ordering, empty-query search returns recent (NOT the email kind
    filter that would return nothing), and ask() with no index degrades cleanly."""
    import tempfile, os
    import meetings_index as mi
    import email_index as ei
    d = tempfile.mkdtemp(prefix="meetdb_")
    conn = ei.connect(os.path.join(d, "m.db"))
    ei.init_schema(conn)

    def row(entry, title, body, ts):
        return {"entry_id": entry, "store_id": "", "account": "meetings",
                "folder": "chunked", "sender_name": "", "sender_email": "",
                "to_recips": "", "cc_recips": "", "subject": title, "body": body,
                "received_ts": ts, "has_attach": 0, "unread": 0}

    _mid, ch = mi._upsert(conn, row("a.txt", "Alpha", "landing zone decided", 100))
    assert ch is True
    _mid2, ch2 = mi._upsert(conn, row("a.txt", "Alpha", "landing zone decided", 100))
    assert ch2 is False, "unchanged file must not re-flag"
    _mid3, ch3 = mi._upsert(conn, row("a.txt", "Alpha", "landing zone REVISED", 100))
    assert ch3 is True, "content change must re-flag (embedded=0)"
    assert conn.execute("SELECT embedded FROM messages WHERE entry_id='a.txt'").fetchone()[0] == 0
    mi._upsert(conn, row("b.txt", "Beta", "unrelated notes", 200))
    conn.commit()

    # recent() newest-first, and empty-query search === recent (the bug the plan
    # flagged: email_index's empty-query browse filters kind='email').
    rec = mi.recent(conn, 10)
    assert [r["subject"] for r in rec] == ["Beta", "Alpha"], rec
    empty = mi.search(conn, None, "", limit=10)
    assert [r["subject"] for r in empty] == ["Beta", "Alpha"], "empty query lost meetings"
    # keyword search still works with no vector DB (vconn=None -> keyword only)
    kw = mi.search(conn, None, "landing zone", limit=5)
    assert any(r["subject"] == "Alpha" for r in kw), kw
    # ask() with a no-match query returns early (empty sources, NO model call -
    # offline-safe): proves the graceful-degrade shape without touching the network.
    no_match = mi.ask(conn, None, "zzz_no_such_term_qwxyz", ei.DEFAULT_OLLAMA,
                      "http://127.0.0.1:0/unused", "m", top_k=5)
    assert set(("answer", "sources", "model")) <= set(no_match.keys())
    assert no_match["sources"] == [] and no_match["model"] == ""


_test("meetings: file parser (2 generations, RLM, em-dash, speakers)", t_meetings_parser)
_test("meetings: index DB layer (upsert/hash, recent, empty-query, ask shape)",
      t_meetings_index_db)


def t_meetings_wiring():
    """Ask-your-meetings wiring in lia: config keys, the provider dropdown
    (local always; free/paid only when keyed; default from meetings_answer_provider,
    real base_urls), the menu item + hotkey + spawn method."""
    import inspect
    import lia as w
    App = w.LiaApp
    for k in ("meetings_ask_hotkey", "meetings_answer_provider", "meetings_top_k",
              "meetings_answer_model_local", "meetings_answer_model_gemini",
              "meetings_answer_model_groq", "meetings_answer_model_openai"):
        assert k in w.DEFAULT_CONFIG, k
    assert w.DEFAULT_CONFIG["meetings_answer_provider"] == "local"
    assert w.DEFAULT_CONFIG["meetings_ask_hotkey"] == "ctrl+alt+m"

    app = App.__new__(App)
    # local only (no cloud keys) -> single provider, default 0, Ollama url
    app.config = {"meetings_answer_provider": "local",
                  "meetings_answer_model_local": "gemma4:31b-it-qat"}
    provs, default = app._meetings_providers()
    assert len(provs) == 1 and provs[0]["provider"] == "local" and default == 0
    assert provs[0]["base_url"] == App._OLLAMA_CHAT_URL and provs[0]["key"] == "ollama"
    # all keys present -> 4 providers with the REAL endpoint constants
    app.config = {"meetings_answer_provider": "gemini",
                  "gemini_api_key": "AIza", "groq_api_key": "gsk", "openai_api_key": "sk",
                  "meetings_answer_model_openai": "gpt-5.6-terra"}
    provs, default = app._meetings_providers()
    byp = {p["provider"]: p for p in provs}
    assert set(byp) == {"local", "gemini", "groq", "openai"}, list(byp)
    assert byp["gemini"]["base_url"] == w.GEMINI_CHAT_URL and byp["gemini"]["key"] == "AIza"
    assert byp["groq"]["base_url"] == w.GroqLLMCleaner.CHAT_URL
    assert byp["openai"]["base_url"] == w.OpenAILLMCleaner.CHAT_URL
    assert byp["openai"]["model"] == "gpt-5.6-terra"
    assert "Terra" in byp["openai"]["label"]                 # paid label reflects the model
    assert provs[default]["provider"] == "gemini", "default must honor the pref"
    # a pref whose key is absent falls back to index 0 (local)
    app.config = {"meetings_answer_provider": "openai"}       # no openai key
    provs, default = app._meetings_providers()
    assert default == 0 and provs[0]["provider"] == "local"

    # method + hotkey + settings action wired
    for m in ("_open_meetings_ask", "_kick_meetings_indexer", "_meetings_providers"):
        assert hasattr(App, m), m
    assert "open_meetings_ask" in App._settings_action_map(App.__new__(App)), \
        "Ask-your-meetings not in the Settings actions"
    run_src = inspect.getsource(App.run)
    assert 'kb.add_hotkey(ask_hk' in run_src, "hotkey not registered"


_test("meetings: wiring (providers local/free/paid, settings action, hotkey)",
      t_meetings_wiring)


def t_meetings_phase2():
    """Phase 2: filter seam fix (date filter applies to retrieval), operator
    stripping, timestamp extraction, filter-only browse, and the multi-turn
    history param - all offline (empty vector store -> no Ollama call)."""
    import tempfile, os, datetime
    import meetings_index as mi
    import email_index as ei
    # helpers
    assert mi._first_ts("foo [12:30] bar") == "12:30"
    assert mi._first_ts("[1:02:03] x") == "1:02:03"
    assert mi._first_ts("no timestamp here") == ""
    assert mi._plain_query('waf after:2026-08-01 speaker:"Speaker A"') == "waf"

    d = tempfile.mkdtemp(prefix="meetp2_")
    conn = ei.connect(os.path.join(d, "m.db")); ei.init_schema(conn)
    vconn = ei.connect(os.path.join(d, "v.db")); ei.init_vectors(vconn)  # empty -> no embed

    def row(entry, title, body, dt):
        ts = int(dt.timestamp())
        return {"entry_id": entry, "store_id": "", "account": "meetings",
                "folder": "chunked", "sender_name": "", "sender_email": "",
                "to_recips": "", "cc_recips": "", "subject": title, "body": body,
                "received_ts": ts, "has_attach": 0, "unread": 0}
    mi._upsert(conn, row("jul.txt", "July", "landing zone review", datetime.datetime(2026, 7, 5)))
    mi._upsert(conn, row("aug.txt", "August", "landing zone decisions", datetime.datetime(2026, 8, 12)))
    conn.commit()

    # date filter now restricts retrieval (email hybrid ignored it on the semantic side;
    # here sem is empty so this proves the keyword+where + allowed-ids path)
    hits = mi._retrieve(conn, vconn, "landing after:2026-08-01", ei.DEFAULT_OLLAMA, limit=10)
    titles = [h["subject"] for h in hits]
    assert titles == ["August"], titles
    assert all("ts" in h for h in hits), "every hit must carry a ts field"
    # no filter -> both
    both = mi._retrieve(conn, vconn, "landing zone", ei.DEFAULT_OLLAMA, limit=10)
    assert set(h["subject"] for h in both) == {"July", "August"}, both
    # filter-only query (no free text) browses within the filter, newest-first
    fo = mi._retrieve(conn, vconn, "after:2026-08-01", ei.DEFAULT_OLLAMA, limit=10)
    assert [h["subject"] for h in fo] == ["August"], fo
    # ask() accepts history without error; no-match -> no model call
    r = mi.ask(conn, vconn, "zzz_no_match_qq", ei.DEFAULT_OLLAMA, "http://127.0.0.1:0/x",
               "m", top_k=5, history=[{"q": "prev", "a": "prev ans"}])
    assert r["sources"] == [] and r["model"] == ""


def t_meetings_autoindex_wiring():
    """Auto-index-on-save wiring: the bg kicker + config + both completion hooks."""
    import os
    import lia as w
    assert hasattr(w.LiaApp, "_kick_meetings_indexer_bg")
    assert w.DEFAULT_CONFIG.get("meetings_autoindex") is True
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "lia.py"), encoding="utf-8").read()
    # both meeting writers (chunked _write_output_file + diarized _run_diarize_job)
    # kick the incremental index on success.
    assert src.count("self.app._kick_meetings_indexer_bg()") == 2, \
        "expected the auto-index kick at BOTH meeting-completion sites"


_test("meetings: phase2 retrieval (filters both-sides, timestamps, follow-up)",
      t_meetings_phase2)
_test("meetings: auto-index on save (bg kicker + both hooks)", t_meetings_autoindex_wiring)


# ---- Action-item tracker --------------------------------------------------
def _meeting_with(tasks_block):
    return (
        "‏Meeting — Planning\n‏Duration: 30m\n‏\n"
        "‏" + "─" * 60 + "\n‏Summary\n‏" + "─" * 60 + "\n"
        "‏תקציר\n‏דיון\n‏\n" + tasks_block +
        "‏" + "═" * 60 + "\n‏Full transcript\n‏" + "═" * 60 + "\n"
        "‏[0:00]\n‏טקסט\n")


_TASKS_BLOCK = ("‏משימות\n"
                "‏- [ ] לבדוק את ה-API מול AWS\n"
                "‏- [ ] לתעד את ההחלטה\n‏\n")
_EMPTY_TASKS = "‏משימות\n‏\n"     # header, no bullets
_NO_TASKS = ""


def t_action_items_parser():
    """Parser + task_id + done-store, on synthetic fixtures matching the real
    format (RLM, plain 'משימות', '- [ ]', ═ boundary)."""
    import tempfile, os
    import action_items as ai
    d = tempfile.mkdtemp(prefix="aiparse_")
    p1 = os.path.join(d, "2026-08-13_10-00-00_x_meeting.txt")
    open(p1, "w", encoding="utf-8").write(_meeting_with(_TASKS_BLOCK))
    got = ai.parse_action_items(p1)
    assert got == ["לבדוק את ה-API מול AWS", "לתעד את ההחלטה"], got   # prefix stripped
    # empty section + no section -> []
    p2 = os.path.join(d, "2026-08-13_11-00-00_x_meeting.txt")
    open(p2, "w", encoding="utf-8").write(_meeting_with(_EMPTY_TASKS))
    assert ai.parse_action_items(p2) == []
    p3 = os.path.join(d, "2026-08-13_12-00-00_x_meeting.txt")
    open(p3, "w", encoding="utf-8").write(_meeting_with(_NO_TASKS))
    assert ai.parse_action_items(p3) == []
    # task_id: stable + text-sensitive
    a = ai._task_id("m.txt", "task one"); b = ai._task_id("m.txt", "task one")
    c = ai._task_id("m.txt", "task two")
    assert a == b and a != c and len(a) == 16

    # done-store roundtrip (isolated path)
    ai.STORE_PATH = os.path.join(d, "store.json")
    assert ai.load_store() == {}
    assert ai.set_done("tid1", True, ts=99) is True
    assert ai.load_store()["tid1"]["done"] is True
    assert ai.set_done("tid1", False) is True            # unset removes
    assert "tid1" not in ai.load_store()


def t_action_items_collect_and_api():
    """collect_items over a synthetic meetings dir + the ActionsApi surface
    (open/all filter, set_done merge, copy_open, path-safe open_meeting)."""
    import tempfile, os
    import action_items as ai
    import meetings_index as mi
    d = tempfile.mkdtemp(prefix="aicollect_")
    # one meeting WITH tasks, one WITHOUT -> only the first becomes a group
    open(os.path.join(d, "2026-08-12_09-00-00_a_meeting.txt"), "w", encoding="utf-8"
         ).write(_meeting_with(_TASKS_BLOCK))
    open(os.path.join(d, "2026-08-11_09-00-00_b_meeting.txt"), "w", encoding="utf-8"
         ).write(_meeting_with(_NO_TASKS))
    mi.MEETINGS_SRC_DIR = d
    ai.STORE_PATH = os.path.join(d, "store.json")

    groups = ai.collect_items()
    assert len(groups) == 1 and len(groups[0]["items"]) == 2, groups
    assert groups[0]["title"] == "Planning"

    api = ai.ActionsApi({})
    r = api.get_items(True)
    assert r["open"] == 2 and r["total"] == 2
    tid = groups[0]["items"][0]["task_id"]
    api.set_done(tid, True)
    r2 = api.get_items(True)
    assert r2["open"] == 1, "marking done should drop it from the open view"
    assert api.get_items(False)["total"] == 2, "total unchanged"
    co = api.copy_open()
    assert co.startswith("## Planning"), co
    assert "לתעד את ההחלטה" in co and "לבדוק את ה-API" not in co   # only the still-open one
    assert api.open_meeting("../../etc/passwd") is False           # path-escape blocked


def t_action_items_wiring():
    """Tracker wiring in lia: config, method, menu item, hotkey."""
    import inspect, os
    import lia as w
    App = w.LiaApp
    assert w.DEFAULT_CONFIG.get("action_items_hotkey") == "ctrl+alt+t"
    assert hasattr(App, "_open_action_items")
    assert "open_action_items" in App._settings_action_map(App.__new__(App)), \
        "Action items not in the Settings actions"
    run_src = inspect.getsource(App.run)
    assert "kb.add_hotkey(act_hk" in run_src, "hotkey not registered"


_test("actions: parser + task_id + done-store (synthetic real-format)", t_action_items_parser)
_test("actions: collect_items + ActionsApi (open/all, done, copy, path-safe)",
      t_action_items_collect_and_api)
_test("actions: wiring (config, method, menu, hotkey)", t_action_items_wiring)


# ---- Ask-your-meetings Phase 3 (synthesis / action-agg / prune) -----------
def t_meetings_intent_router():
    """The factual / synthesis / action classifier (offline regex). Action is
    checked first (so 'סכם את המשימות' -> action, not synthesis)."""
    import inspect
    import meetings_index as mi
    cases = {
        "מה הוחלט על ה-Landing Zone?": "factual",
        "מי אמר שצריך Netskope?": "factual",
        "כמה עלה ה-PoC?": "factual",
        "": "factual",
        "מה המשימות הפתוחות?": "action",
        "מה נשאר לי לעשות מהפגישות?": "action",
        "what are my open action items": "action",
        "סכם את המשימות הפתוחות של נתניאל": "action",   # action beats synthesis
        "סכם מה הוחלט על ה-firewall לאורך כל הפגישות": "synthesis",
        "summarize the AWS decisions across all meetings": "synthesis",
        "מה ההתקדמות בפרויקט?": "synthesis",
        "compare the two proposals": "synthesis",
    }
    for q, exp in cases.items():
        assert mi.classify_intent(q) == exp, (q, exp, mi.classify_intent(q))
    assert "route" in inspect.signature(mi.ask).parameters, "ask() lost the route kill-switch"


def t_meetings_synthesis_floor():
    """_synthesis_hits keeps the relevant head and drops the low-similarity tail,
    falls back to top-N when flooring leaves too few, and caps unscored hits."""
    import meetings_index as mi
    orig = mi.search

    def fake(scores):
        return lambda *a, **k: [{"id": i, "score": s, "subject": str(i),
                                 "received_ts": 0} for i, s in enumerate(scores)]
    try:
        # clean floor: top=1.0 -> floor 0.5 -> keep >=0.5 (6), drop 0.3/0.2
        mi.search = fake([1.0, 0.9, 0.8, 0.7, 0.6, 0.55, 0.3, 0.2])
        keep = mi._synthesis_hits(None, None, "q")
        assert [round(h["score"], 2) for h in keep] == [1.0, 0.9, 0.8, 0.7, 0.6, 0.55], keep
        # too few above floor (<min_keep) -> return all available
        mi.search = fake([1.0, 0.9, 0.1, 0.1])
        assert len(mi._synthesis_hits(None, None, "q")) == 4
        # unscored hits (keyword-only path) -> cap only
        mi.search = lambda *a, **k: [{"id": i, "subject": str(i)} for i in range(40)]
        assert len(mi._synthesis_hits(None, None, "q", cap=15)) == 15
    finally:
        mi.search = orig


def t_meetings_action_context():
    """_action_context builds numbered open-task blocks + UI-shaped sources from the
    action-item aggregation (synthetic meetings dir); done items drop out."""
    import tempfile, os
    import meetings_index as mi
    import action_items as ai
    saved_src, saved_store = mi.MEETINGS_SRC_DIR, ai.STORE_PATH
    try:
        d = tempfile.mkdtemp(prefix="meetact_")
        open(os.path.join(d, "2026-08-12_09-00-00_a_meeting.txt"), "w",
             encoding="utf-8").write(_meeting_with(_TASKS_BLOCK))
        mi.MEETINGS_SRC_DIR = d
        ai.STORE_PATH = os.path.join(d, "store.json")
        ctx, srcs = mi._action_context(open_only=True)
        assert len(srcs) == 1 and ctx.startswith("[1] Planning"), (ctx[:40], srcs)
        assert "- לבדוק את ה-API מול AWS" in ctx
        for k in ("entry_id", "subject", "received_ts", "ts", "sender_name", "folder"):
            assert k in srcs[0], k
        tid = ai.collect_items()[0]["items"][0]["task_id"]
        ai.set_done(tid, True)
        ctx2, _ = mi._action_context(open_only=True)
        assert "לבדוק את ה-API" not in ctx2 and "לתעד את ההחלטה" in ctx2
    finally:
        mi.MEETINGS_SRC_DIR, ai.STORE_PATH = saved_src, saved_store


def t_meetings_prune():
    """reindex() prunes meetings deleted from disk (messages + FTS trigger + vectors +
    watermark); the empty-listing guard refuses to wipe. Offline (no embed)."""
    import tempfile, os
    import meetings_index as mi
    import email_index as ei
    keys = ("MEET_INDEX_DIR", "MESSAGES_DB", "VECTORS_DB", "STATE_FILE",
            "STATUS_FILE", "HEARTBEAT_FILE", "MEETINGS_SRC_DIR")
    saved = {k: getattr(mi, k) for k in keys}
    try:
        tmp = tempfile.mkdtemp(prefix="meetprune_")
        src = os.path.join(tmp, "meetings"); idx = os.path.join(tmp, "idx")
        os.makedirs(src); os.makedirs(idx)
        mi.MEET_INDEX_DIR = idx
        mi.MESSAGES_DB = os.path.join(idx, "m.db")
        mi.VECTORS_DB = os.path.join(idx, "v.db")
        mi.STATE_FILE = os.path.join(idx, "s.json")
        mi.STATUS_FILE = os.path.join(idx, "st.json")
        mi.HEARTBEAT_FILE = os.path.join(idx, "hb.txt")
        mi.MEETINGS_SRC_DIR = src

        def meet(name, title, body):
            with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                f.write("‏Meeting — %s\n‏\n‏%s\n" % (title, body))

        meet("2026-08-01_10-00-00_a_meeting.txt", "A", "landing zone")
        meet("2026-08-02_11-00-00_b_meeting.txt", "B", "firewall")
        s, c, e, p = mi.reindex(full=True, do_embed=False)
        assert (s, p) == (2, 0), (s, p)
        conn = ei.connect(mi.MESSAGES_DB, readonly=True)
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM messages_fts "
                            "WHERE messages_fts MATCH 'firewall'").fetchone()[0] == 1
        conn.close()

        os.remove(os.path.join(src, "2026-08-02_11-00-00_b_meeting.txt"))
        s, c, e, p = mi.reindex(full=False, do_embed=False)
        assert p == 1, p
        conn = ei.connect(mi.MESSAGES_DB, readonly=True)
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM messages_fts "
                            "WHERE messages_fts MATCH 'firewall'").fetchone()[0] == 0, "FTS not pruned"
        conn.close()
        assert "2026-08-02_11-00-00_b_meeting.txt" not in mi._load_state().get("mtimes", {})

        empty = os.path.join(tmp, "empty"); os.makedirs(empty)
        mi.MEETINGS_SRC_DIR = empty
        s, c, e, p = mi.reindex(full=False, do_embed=False)
        assert p == 0
        conn = ei.connect(mi.MESSAGES_DB, readonly=True)
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1, "guard failed"
        conn.close()
    finally:
        for k, v in saved.items():
            setattr(mi, k, v)


def t_meetings_phase3_wiring():
    """Phase 3 wiring: config keys, the periodic-reindex watchdog + start, the
    intent-routing payload flag, meetings_search passing route + surfacing mode,
    and ask() returning a mode."""
    import os, tempfile
    import lia as w
    import meetings_index as mi
    import email_index as ei
    App = w.LiaApp
    assert w.DEFAULT_CONFIG.get("meetings_intent_routing") is True
    assert w.DEFAULT_CONFIG.get("meetings_reindex_interval_min") == 360
    assert hasattr(App, "_meetings_reindex_watchdog")
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert '"intent_routing": bool(self.config.get("meetings_intent_routing"' in src
    assert "self._meetings_reindex_watchdog" in src, "watchdog not started in run()"
    # the periodic watchdog uses self. (App method) so the 2-hook save-path count holds
    assert src.count("self.app._kick_meetings_indexer_bg()") == 2
    ms = open(os.path.join(here, "meetings_search.py"), encoding="utf-8").read()
    assert "route=self._route" in ms and '"mode": res.get("mode"' in ms and "modeBadge" in ms
    # ask() surfaces mode; no-match stays factual + offline-safe
    d = tempfile.mkdtemp(prefix="meetmode_")
    conn = ei.connect(os.path.join(d, "m.db")); ei.init_schema(conn)
    r = mi.ask(conn, None, "zzz_no_match_qq", ei.DEFAULT_OLLAMA,
               "http://127.0.0.1:0/x", "m", top_k=3)
    assert r.get("mode") == "factual" and r["sources"] == [] and r["model"] == ""


_test("meetings: phase3 intent router (factual/synthesis/action)", t_meetings_intent_router)
_test("meetings: phase3 synthesis relevance floor", t_meetings_synthesis_floor)
_test("meetings: phase3 action-item aggregation context", t_meetings_action_context)
_test("meetings: phase3 index prune (delete + empty-listing guard)", t_meetings_prune)
_test("meetings: phase3 wiring (config, watchdog, payload, mode)", t_meetings_phase3_wiring)


def t_webview_windows_defer_api():
    """Regression: pywebview windows must NOT read `window.pywebview.api` at inline-
    script PARSE time - it's undefined then, so it throws a TypeError that aborts the
    whole <script> (dead dropdown / buttons / Enter). Assign it inside the
    pywebviewready handler (like email_search / chat_window). This bug killed the
    ask + action-item windows until fixed 2026-08-15."""
    import os, re
    here = os.path.dirname(os.path.abspath(__file__))
    for fn in ("meetings_search.py", "action_items.py", "email_search.py",
               "chat_window.py", "compose_editor.py", "settings_window.py",
               "history_window.py", "summarize_window.py"):
        src = open(os.path.join(here, fn), encoding="utf-8").read()
        # a COLUMN-0 (non-indented, i.e. top-level) JS line touching the bridge = the bug
        bad = re.search(r"(?m)^\S[^\n]*window\.pywebview\.api", src)
        assert not bad, "%s: top-level window.pywebview.api access -> %r" % (fn, bad.group(0)[:60])
        # the bridge must be wired EITHER inline (own pywebviewready handler) OR
        # via the shared ui_kit boot (RK.ready + import ui_kit). ui_kit.JS_BASE's
        # own column-0 discipline is asserted by t_ui_kit_tokens.
        wired = ("window.pywebview.api" in src
                 or ("RK.ready(" in src and "import ui_kit" in src))
        assert wired, fn + ": never wires the api bridge (inline or via ui_kit)"


_test("webview: windows defer window.pywebview.api to pywebviewready (dead-window fix)",
      t_webview_windows_defer_api)


def t_webview_no_inline_handler_injection():
    """Security regression (2026-08-28 audit): a value concatenated into an
    inline on* handler lets a quote in a meeting title/id break out of the
    attribute into executable JS. Ids must ride in data-* attributes consumed
    by delegated listeners; and every window-local esc() must escape quotes
    (attribute context), like the canonical RK.esc."""
    import os, re
    here = os.path.dirname(os.path.abspath(__file__))
    files = ("meetings_search.py", "action_items.py", "email_search.py",
             "chat_window.py", "compose_editor.py", "settings_window.py",
             "history_window.py", "summarize_window.py", "ui_kit.py",
             "ui_kit_gallery.py")
    # e.g.  onclick="openMeeting(\''+esc(id)+'\')"   or   onclick="f('+i+')"
    # (?<!\w) so words merely CONTAINING "on..." (textContent) don't match
    sink = re.compile(r"""(?<!\w)on\w+\s*=\s*\\?["'][^"'\n]*['"]\s*\+""")
    for fn in files:
        src = open(os.path.join(here, fn), encoding="utf-8").read()
        m = sink.search(src)
        assert not m, "%s: interpolated inline handler -> %r" % (fn, m.group(0)[:60])
        for dm in re.finditer(r"function esc\(s\)\s*\{", src):
            body = src[dm.start():dm.start() + 400]
            assert ("&#39;" in body) or ("RK.esc" in body), (
                fn + ": esc() does not escape quotes")


_test("webview: no interpolated inline handlers + quote-escaping esc (injection guard)",
      t_webview_no_inline_handler_injection)


def t_no_internal_remnants():
    """Publication guard (2026-08-28 audit): the shipping sources carry no
    internal session markers and no hardcoded notetaker identity - the name
    comes from config `notetaker_names` via the «NT» placeholders. (The
    guarded strings are constructed so this test is not its own violation.)
    Legacy APP names inside the migration code are functional and allowed."""
    import glob as _glob
    import re as _re
    here = os.path.dirname(os.path.abspath(__file__))
    nt_latin = "Ju" + "lia"
    nt_he = "ג'" + "וליה"
    session_pat = _re.compile(r"\bS3[0-9]\b|[Ss]ession [0-9]")
    # a private-machine path (e.g. inside a generated lock file's header);
    # the settings demo uses the placeholder C:\Users\you - allowed
    path_pat = _re.compile(r"[A-Za-z]:[\\/]+Users[\\/](?!you\b)")
    # private-project-adjacency phrasing (pattern constructed so this test
    # file is not its own hit)
    sib_pat = _re.compile("sib" + "ling" + r"[^\n]{0,60}project", _re.I)
    files = sorted(_glob.glob(os.path.join(here, "*.py")))
    files += [os.path.join(here, "requirements.lock"),
              os.path.join(here, "requirements.txt"),
              os.path.join(here, "installer.iss")]
    files += [os.path.join(here, "..", p) for p in
              ("README.md", "CONTRIBUTING.md", "SECURITY.md",
               os.path.join("docs", "SELF_HOSTED_SERVER.md"))]
    assert len(files) > 20, "glob missed the module set"
    for fp in files:
        if not os.path.exists(fp):
            continue
        src = open(fp, encoding="utf-8").read()
        base = os.path.basename(fp)
        assert nt_latin not in src, base + ": hardcoded notetaker identity"
        assert nt_he not in src, base + ": hardcoded notetaker identity (he)"
        for pat, label in ((session_pat, "internal session marker"),
                           (path_pat, "private machine path"),
                           (sib_pat, "private-project phrasing")):
            m = pat.search(src)
            assert not m, "%s: %s: %r" % (base, label, m.group(0)[:50])


_test("publication: no internal remnants in the shipping sources",
      t_no_internal_remnants)


def t_least_privilege():
    """2026-08-28 audit: the exe is asInvoker, the installer defaults to a
    per-user install with an HKCU Run auto-start, and elevation is opt-in
    (run.bat / the installer's elevated task, gated on an elevated install).
    The app-side toggle picks the mechanism by its own elevation."""
    import lia as w
    here = os.path.dirname(os.path.abspath(__file__))
    bld = open(os.path.join(here, "build.py"), encoding="utf-8").read()
    assert "--uac-admin" not in bld, "the exe still requests elevation"
    iss = open(os.path.join(here, "installer.iss"), encoding="utf-8").read()
    assert "PrivilegesRequired=lowest" in iss
    assert "elevatedautostart" in iss and "IsAdminInstallMode" in iss
    assert r"Software\Microsoft\Windows\CurrentVersion\Run" in iss
    assert 'Tasks: autostart\n' not in iss.replace("Tasks: autostart and", "X"), \
        "the schtasks entry must be tied to elevatedautostart only"
    # app-side: both mechanisms wired, choice by elevation
    src = open(os.path.join(here, "lia.py"), encoding="utf-8").read()
    assert "_set_autostart_runkey" in src and "_autostart_runkey_exists" in src
    import inspect
    sas = inspect.getsource(w.set_auto_start)
    assert "is_user_admin()" in sas and "_set_autostart_runkey" in sas
    assert "_autostart_runkey_exists" in inspect.getsource(w.is_auto_start_enabled)
    # functional roundtrip on the real HKCU value - only when it doesn't exist
    if not w._autostart_runkey_exists():
        assert w._set_autostart_runkey(True)
        assert w._autostart_runkey_exists()
        assert w._set_autostart_runkey(False)
        assert not w._autostart_runkey_exists()


_test("publication: least-privilege build/installer/auto-start",
      t_least_privilege)


def t_ui_kit_tokens():
    """ui_kit is the shared design system. Assert its tokens, the page()
    assembler, and the JS bridge-defer discipline (never touch the bridge at
    inline-script parse time)."""
    import re
    import ui_kit as uk
    # every documented token must exist in the :root block
    tokens = ["--bg", "--card", "--card-2", "--line", "--line-2", "--ink",
              "--ink-2", "--muted", "--faint", "--accent", "--accent-h",
              "--accent-soft", "--on-accent", "--ok", "--warn", "--err", "--info",
              "--r-s", "--r-m", "--r-l", "--r-pill", "--font", "--mono",
              "--fs-base", "--shadow", "--ring", "--tr"]
    for t in tokens:
        assert t in uk.CSS_TOKENS, "ui_kit CSS_TOKENS missing " + t
    # a dark block is defined (reserved) but gated on data-theme
    assert '[data-theme="dark"]' in uk.CSS_TOKENS, "no reserved dark theme block"
    # component classes present in the base sheet
    for cls in [".sidebar", ".nav-item", ".page", ".section-title", ".field",
                ".btn", ".btn.primary", ".btn.ghost", ".btn.danger", ".check",
                ".radio", ".switch", ".chip", ".badge", ".list", ".row",
                ".status", ".spinner", ".toast", ".empty", ".masked", ".kbd"]:
        assert cls in uk.CSS_BASE, "ui_kit CSS_BASE missing " + cls
    # no external resources anywhere (CSP-safe / offline)
    blob = uk.CSS_TOKENS + uk.CSS_BASE + uk.JS_BASE
    assert "http://" not in blob and "https://" not in blob, "ui_kit references an external URL"
    # page() assembles a complete self-contained document
    html = uk.page("Test Title", "<div id='x'>body</div>")
    assert html.startswith("<!DOCTYPE html>"), "page() must start with the doctype"
    assert 'lang="en"' in html and 'dir="ltr"' in html, "page() chrome must be en/ltr"
    assert "<title>Test Title</title>" in html
    assert "pywebviewready" in html, "page() must wire the bridge on pywebviewready"
    assert "window.pywebview.api" in html, "page() must reference the bridge"
    assert "--accent" in html, "page() must inline the design tokens"
    # the bridge must NEVER be read at column 0 of the shared JS (parse-time = dead window)
    bad = re.search(r"(?m)^\S[^\n]*window\.pywebview\.api", uk.JS_BASE)
    assert not bad, "ui_kit.JS_BASE reads the bridge at parse time: %r" % (
        bad.group(0)[:60] if bad else "")
    assert "window.pywebview.api" in uk.JS_BASE, "ui_kit.JS_BASE never wires the bridge"


_test("ui_kit: design tokens, page() assembler, bridge-defer discipline",
      t_ui_kit_tokens)


def t_ui_kit_no_webview_import():
    """Importing ui_kit must NOT drag in the heavy `webview` package (the parent
    app imports ui_kit in-process; webview is only needed inside a child window,
    imported lazily in child_main). Checked in a FRESH interpreter."""
    import os
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    code = ("import sys; sys.path.insert(0, %r); import ui_kit; "
            "sys.exit(1 if 'webview' in sys.modules else 0)" % here)
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                       capture_output=True)
    assert r.returncode == 0, ("import ui_kit pulled in webview: %s"
                               % r.stderr.decode("utf-8", "replace")[:200])


_test("ui_kit: import does not load webview (lazy in child_main)",
      t_ui_kit_no_webview_import)


def t_settings_actions_coverage():
    """Every migrated tray surface has an allowlisted Settings action that
    resolves to a bound method (the N/N migration guard) - and dispatch never
    reaches an arbitrary attribute."""
    import lia as w
    App = w.LiaApp
    app = App.__new__(App)
    actions = App._settings_action_map(app)
    # the full migration set (old tray path -> Settings action).
    expected = [
        # General
        "capture_hotkey", "set_hotkey", "set_recording_mode", "set_paste_mode",
        "set_primary_language",
        "toggle_clipboard_auto_restore", "toggle_press_enter_after_paste",
        "toggle_silent_mode", "toggle_auto_start", "set_beep_device",
        # Audio
        "toggle_record_mic", "toggle_record_system", "toggle_mic_device",
        "set_meeting_mic_device", "toggle_loopback_device", "refresh",
        # Models
        "set_dictation_model", "set_meeting_model", "set_summary_model",
        "toggle_summary_local_tasks_pass", "set_summary_language",
        "set_file_model", "set_whisper_device",
        # AI Cleanup
        "set_cleanup_style", "set_cleanup_provider_model",
        # Keys & Server
        "apply_key", "clear_key", "apply_remote", "test_remote",
        "toggle_serve", "toggle_serve_autostart", "apply_serve", "serve_status",
        "set_transcription_role", "open_tailscale", "set_serve_model",
        # Meetings
        "toggle_auto_detect_meetings", "open_meetings_ask", "open_action_items",
        "open_meetings_folder", "edit_meeting_summary", "transcribe_file",
        "voice_ask_now", "set_voice_ask_output",
        "summarize_text_dialog", "open_live_transcript",
        # Vocabulary
        "save_vocabulary", "vocab_pending_list", "vocab_resolve",
        "vocab_learned_list", "vocab_remove_learned", "vocab_corrections_list",
        "vocab_corrections_scan", "vocab_add_correction", "vocab_remove_correction",
        "vocab_remove_corrections", "vocab_rebuild", "toggle_vocab_autolearn",
        # Snippets
        "snippets_get", "snippets_set",
        # Advanced
        "restart_app", "open_log", "open_config_dir", "quit_app",
    ]
    missing = [a for a in expected if a not in actions]
    assert not missing, "Settings actions missing: %s" % missing
    for name, entry in actions.items():
        assert isinstance(entry, tuple) and len(entry) == 2, name
        fn, slow = entry
        assert callable(fn), "not callable: " + name
        assert isinstance(slow, bool), "is_slow not bool: " + name
    # dispatch guards an unknown method (no getattr on arbitrary names).
    ok, msg, data = App._settings_dispatch(app, "definitely_not_a_method", [])
    assert ok is False and "Unknown action" in msg
    # the read-only getters (whose result must NOT trigger a state re-render that
    # would wipe the list they just loaded) are all real actions.
    for a in App._SETTINGS_READONLY_METHODS:
        assert a in actions, "read-only method not in allowlist: " + a
    for a in ("vocab_pending_list", "vocab_learned_list", "vocab_corrections_list",
              "vocab_corrections_scan", "snippets_get"):
        assert a in App._SETTINGS_READONLY_METHODS, "getter must be state-push-exempt: " + a


_test("settings: SETTINGS_ACTIONS covers every migrated tray surface (N/N)",
      t_settings_actions_coverage)


def t_meeting_mic_resolve():
    """The dedicated meeting mic resolves correctly: None follows the dictation
    mic; a present index is used; an unplugged index falls back to the
    dictation mic (never records silence from a stale handle)."""
    import lia as w

    class FakeApp:
        def __init__(self, cfg):
            self.config = cfg

    resolve = w.MeetingSession._resolve_meeting_mic
    orig = w.list_input_devices
    w.list_input_devices = lambda: [(1, "JOUNIVO"), (3, "Headset Mic")]
    try:
        # None -> same as dictation mic
        assert resolve(FakeApp({"input_device_index": 1})) == 1
        assert resolve(FakeApp({"input_device_index": None})) is None
        # dedicated present -> used (dictation mic untouched)
        assert resolve(FakeApp({"input_device_index": 1,
                                "meeting_input_device_index": 3})) == 3
        # dedicated == dictation -> no enumeration needed, same result
        assert resolve(FakeApp({"input_device_index": 1,
                                "meeting_input_device_index": 1})) == 1
        # dedicated UNPLUGGED -> falls back to the dictation mic
        assert resolve(FakeApp({"input_device_index": 1,
                                "meeting_input_device_index": 7})) == 1
        # enumeration failure -> falls back, never raises
        w.list_input_devices = lambda: (_ for _ in ()).throw(RuntimeError("dead"))
        assert resolve(FakeApp({"input_device_index": 1,
                                "meeting_input_device_index": 3})) == 1
    finally:
        w.list_input_devices = orig


_test("meeting: dedicated meeting mic resolve + unplug fallback",
      t_meeting_mic_resolve)


def t_vocab_corrections_analytics():
    """Corrections analytics + prune: count_corrections mirrors apply_corrections
    (same matcher), set_corpus_hits stamps every correction (0 for never-hit),
    and the app-side scan/bulk-remove methods exist and are wired slow+readonly."""
    import tempfile
    import vocab_learn as vl
    import lia as w
    # 1) count_corrections uses the SAME whole-word Hebrew-aware matcher as apply.
    pairs = [{"wrong": "בדרוק", "right": "Bedrock"},
             {"wrong": "Bedrook", "right": "Bedrock"},
             {"wrong": "נעולם", "right": "never"}]          # a garble that won't appear
    texts = ["דיברנו על בדרוק ועל בדרוק", "Bedrook again; בדרוקים should NOT match"]
    counts = vl.count_corrections(texts, pairs)
    assert counts[vl._norm("בדרוק")] == 2, counts          # "בדרוקים" excluded (boundary)
    assert counts[vl._norm("Bedrook")] == 1, counts
    assert counts[vl._norm("נעולם")] == 0, counts          # never-hit -> 0, not missing
    # equivalence with apply_corrections' own counts (nonzero ones)
    _, applied = vl.apply_corrections("\n".join(texts), pairs)
    assert {k: v for k, v in counts.items() if v} == applied, (counts, applied)
    assert vl.count_corrections("", pairs) == {} and vl.count_corrections(texts, []) == {}
    # 2) set_corpus_hits stamps corpus_hits on EVERY correction (0 when absent).
    path = os.path.join(tempfile.gettempdir(), "wt_corr_an_%d.json" % os.getpid())
    try:
        os.remove(path)
    except OSError:
        pass
    s = vl.VocabStore(path)
    s.add_corrections(pairs, source="test")
    s.set_corpus_hits({vl._norm("בדרוק"): 2, vl._norm("Bedrook"): 1})
    by = {c["wrong"].lower(): c for c in s.corrections()}
    assert by["בדרוק"]["corpus_hits"] == 2 and by["bedrook"]["corpus_hits"] == 1
    assert by["נעולם"]["corpus_hits"] == 0 and by["נעולם"].get("corpus_scanned")
    # persists
    assert vl.VocabStore(path).corrections()[0].get("corpus_scanned")
    # bulk remove
    assert s.remove_corrections(["נעולם", "בדרוק"]) == 2
    assert {c["wrong"] for c in s.corrections()} == {"Bedrook"}
    os.remove(path)
    # 3) app wiring: methods exist, scan is slow + read-only, bulk-remove present.
    App = w.LiaApp
    for m in ("_vocab_corrections_scan", "_vocab_remove_corrections"):
        assert hasattr(App, m), m
    actions = App._settings_action_map(App.__new__(App))
    assert actions["vocab_corrections_scan"][1] is True, "scan must be slow"
    assert actions["vocab_remove_corrections"][1] is False
    assert "vocab_corrections_scan" in App._SETTINGS_READONLY_METHODS
    # the Vocabulary page renders the new controls + scan/prune calls.
    sw = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                           "settings_window.py"), encoding="utf-8").read()
    for token in ("__scan_corr", "vocab_corrections_scan", "data-corr-unused",
                  "data-corr-remove", "vocab_remove_corrections", "corr-unused"):
        assert token in sw, "settings UI missing: " + token


_test("vocab: corrections analytics + archive scan + prune",
      t_vocab_corrections_analytics)


def t_corrections_autoharvest():
    """Continuous correction auto-harvest: transcript extraction, verified
    LLM pass (hallucinations dropped), idempotent run tracking, and wiring.
    The LLM call is monkeypatched — no live Ollama needed."""
    import tempfile
    import shutil
    import vocab_learn as vl
    import lia as w
    App = w.LiaApp
    # 1) transcript extraction: keep body after 'Transcript', drop RLM + delimiters.
    raw = ("\u200fMeeting\n\u200fDuration: 5:00\n\u200f" + "\u2500" * 10 + "\n"
           "\u200fSummary\n\u200f" + "\u2500" * 10 + "\n\u200fכותרת הדיון\n"
           "\u200fTranscript\n\u200f" + "\u2550" * 10 + "\n"
           "\u200f[0:00] Speaker A:\n\u200fדיברנו על בדרוק\n")
    body = w._extract_meeting_transcript(raw)
    assert "בדרוק" in body and "Summary" not in body and "כותרת" not in body
    assert "\u200f" not in body and "\u2550" not in body, repr(body)
    assert w._extract_meeting_transcript("") == ""
    # 2) harvest-from-text drops pairs whose `wrong` is NOT in the source (hallucination).
    canned = ('===CORRECTIONS=== [{"wrong":"בדרוק","right":"Bedrock"},'
              '{"wrong":"נעולם","right":"never"}]')   # נעולם absent from the text
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = lambda self, *a, **k: canned
    try:
        app = App.__new__(App)
        app.config = dict(w.DEFAULT_CONFIG)
        pairs = app._harvest_corrections_from_text("דיברנו על בדרוק שוב")
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig
    assert {(p["wrong"], p["right"]) for p in pairs} == {("בדרוק", "Bedrock")}
    # 3) run: adds source=auto-harvest, records the file, idempotent on re-run.
    tmpdir = tempfile.mkdtemp()
    open(os.path.join(tmpdir, "m1.txt"), "w", encoding="utf-8").write(
        "Transcript\n[0:00]\nדיברנו על בדרוק")
    spath = os.path.join(tempfile.gettempdir(), "wt_ah_%d.json" % os.getpid())
    try:
        os.remove(spath)
    except OSError:
        pass
    app = App.__new__(App)
    app.config = dict(w.DEFAULT_CONFIG)
    app.config["corrections_harvested_files"] = []
    app._vocab_store = vl.VocabStore(spath)
    app.is_recording = False
    app._is_meeting_active = lambda: False
    app._ollama_reachable = lambda timeout=2: True
    app._harvest_corrections_from_text = lambda t: [{"wrong": "בדרוק", "right": "Bedrock"}]
    old_dir, old_save = w.MEETINGS_DIR, w.save_config
    w.MEETINGS_DIR = tmpdir
    w.save_config = lambda cfg: None
    try:
        ok, msg = app._corrections_harvest_run(manual=True)
        assert ok and "1 meeting" in msg, msg
        corr = {c["wrong"]: c for c in app._vocab_store.corrections()}
        assert corr["בדרוק"]["source"] == "auto-harvest"
        assert "m1.txt" in app.config["corrections_harvested_files"]
        ok2, msg2 = app._corrections_harvest_run(manual=True)
        assert ok2 and "No new meetings" in msg2, msg2
    finally:
        w.MEETINGS_DIR, w.save_config = old_dir, old_save
        os.remove(spath)
        shutil.rmtree(tmpdir, ignore_errors=True)
    # 4) wiring: config defaults, methods, slow action, watchdog, UI tokens.
    for k, d in (("corrections_autoharvest", True),
                 ("corrections_harvest_interval_min", 720),
                 ("corrections_harvest_max_per_cycle", 4)):
        assert w.DEFAULT_CONFIG.get(k) == d, k
    for m in ("_harvest_corrections_from_text", "_corrections_harvest_run",
              "_corrections_harvest_watchdog", "_ollama_reachable",
              "_toggle_corrections_autoharvest"):
        assert hasattr(App, m), m
    actions = App._settings_action_map(App.__new__(App))
    assert actions["harvest_corrections_now"][1] is True, "harvest must be slow"
    assert "toggle_corrections_autoharvest" in actions
    sw = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                           "settings_window.py"), encoding="utf-8").read()
    for token in ("harvest_corrections_now", "toggle_corrections_autoharvest"):
        assert token in sw, "settings UI missing: " + token


_test("vocab: continuous correction auto-harvest (extract/verify/run/wiring)",
      t_corrections_autoharvest)


def t_voice_ask():
    """Voice Ask (hands-free ask-your-meetings): config defaults, methods, the
    dictation-loop gate, the in-process RAG answer path (mocked index), output
    routing (card/paste/both), the output setter, and Settings wiring.
    No mic, no LLM - the RAG and overlay are monkeypatched."""
    import lia as w
    import meetings_index as mi
    App = w.LiaApp
    # 1) config defaults
    for k, d in (("voice_ask_hotkey", "ctrl+alt+v"), ("voice_ask_output", "card"),
                 ("voice_ask_max_sec", 20), ("voice_ask_silence_stop_sec", 2.5)):
        assert w.DEFAULT_CONFIG.get(k) == d, k
    # 2) methods exist
    for m in ("_voice_ask_toggle", "_voice_ask_start", "_voice_ask_autostop",
              "_voice_ask_stop_and_answer", "_voice_ask_answer", "_voice_ask_present",
              "_set_voice_ask_output"):
        assert hasattr(App, m), m
    # 3) the dictation hotkey gate treats voice-ask as "mic busy" - source-level
    #    guard on the SAME line family as _compose_instr_active (like t_vocab_wiring).
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert "Hotkey ignored — Compose/Voice-ask recording is active" in src
    gate = src.index("Hotkey ignored — Compose/Voice-ask recording is active")
    assert "_voice_ask_active" in src[gate - 200:gate], "hotkey gate must check _voice_ask_active"
    # every compose-instr 'mic busy' guard also names voice-ask (the refresh_tray arbiter too)
    assert src.count("_voice_ask_active") >= 18, src.count("_voice_ask_active")
    assert "self.is_recording or self._compose_instr_active or self._voice_ask_active" in src, \
        "_refresh_tray must show the recording icon while voice-ask listens"
    # 4) answer path - no index
    app = App.__new__(App)
    app.config = dict(w.DEFAULT_CONFIG)
    old_open, old_ask = mi.open_read, mi.ask
    mi.open_read = lambda: (None, None)
    try:
        r = app._voice_ask_answer("x")
        assert "index" in r["answer"].lower(), r
    finally:
        mi.open_read = old_open
    # 4b) answer path - index present: passes the user's DEFAULT provider + question through

    class _C:
        def close(self):
            pass
    seen = {}

    def fake_ask(conn, vconn, question, embed_base_url, answer_base_url, answer_model,
                 answer_key="ollama", top_k=20, history=None, route=True):
        seen.update(dict(q=question, url=answer_base_url, model=answer_model,
                         key=answer_key, top_k=top_k, route=route))
        return {"answer": "תשובה", "sources": [{"subject": "פגישה", "received_ts": 0}],
                "model": answer_model, "mode": "factual"}
    mi.open_read = lambda: (_C(), _C())
    mi.ask = fake_ask
    app._meetings_providers = lambda: ([{"label": "L", "provider": "local",
                                         "base_url": "http://localhost:11434/v1/chat/completions",
                                         "key": "ollama", "model": "gemma4:31b-it-qat"}], 0)
    try:
        r = app._voice_ask_answer("מה סיכמנו?")
        assert r["answer"] == "תשובה" and r["mode"] == "factual", r
        assert seen["q"] == "מה סיכמנו?" and seen["model"] == "gemma4:31b-it-qat"
        assert seen["key"] == "ollama" and seen["top_k"] == 20 and seen["route"] is True
    finally:
        mi.open_read, mi.ask = old_open, old_ask
    # 5) present: card / paste / both routing
    calls = []

    class _Ov:
        def show_summary(self, title, text, on_open_file=None, on_save=None):
            calls.append(("card", title, text))
    app.overlay = _Ov()
    app._do_paste = lambda text, mode, press_enter=None: calls.append(("paste", text, mode))
    res = {"answer": "A1", "sources": [{"subject": "S", "received_ts": 0}], "mode": "factual"}
    for out, expect in (("card", {"card"}), ("paste", {"paste"}), ("both", {"card", "paste"}),
                        ("garbage", {"card"})):
        calls.clear()
        app.config["voice_ask_output"] = out
        app._voice_ask_present("שאלה", res)
        assert {c[0] for c in calls} == expect, (out, calls)
    card = [c for c in calls if c[0] == "card"]
    assert card and "A1" in card[0][2] and "S" in card[0][2] and "שאלה" in card[0][1]
    # 6) setter validates + persists (save_config patched)
    old_save = w.save_config
    w.save_config = lambda cfg: None
    try:
        ok, _ = app._set_voice_ask_output("paste")
        assert ok and app.config["voice_ask_output"] == "paste"
        ok2, _ = app._set_voice_ask_output("nope")
        assert ok2 is False and app.config["voice_ask_output"] == "paste"
    finally:
        w.save_config = old_save
    # 7) wiring: actions + Settings UI + state hotkeys
    actions = App._settings_action_map(App.__new__(App))
    for a in ("voice_ask_now", "set_voice_ask_output"):
        assert a in actions, a
    sw = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                           "settings_window.py"), encoding="utf-8").read()
    for token in ("voice_ask_now", "set_voice_ask_output", "hk.voice_ask"):
        assert token in sw, "settings UI missing: " + token
    assert '"voice_ask": c.get("voice_ask_hotkey"' in src, "hotkeys state must expose voice_ask"
    assert 'kb.add_hotkey(va_hk, self._voice_ask_toggle' in src, "hotkey registration missing"


_test("voice ask: hands-free ask-your-meetings (gate/answer/present/wiring)",
      t_voice_ask)


def t_local_tasks_pass():
    """LOCAL tasks pass (closes gemma4's long-meeting task gap): the two pure
    helpers, the summarize() hook (mocked Ollama - fires only when enabled, only
    for meeting summaries, keeps the summary on an empty/failed pass), the
    config flag + toggle + Settings wiring."""
    import lia as w
    App = w.LiaApp
    # 1) speaker-label owners are stripped; real names are kept
    S = w._strip_speaker_label_owners
    assert S("- [ ] x - אחראי: Speaker A") == "- [ ] x"
    assert S("- [ ] x - אחראי: Speaker A/B") == "- [ ] x"
    assert S("- [ ] x - אחראי: דובר 2") == "- [ ] x"
    assert S("- [ ] x - אחראי: איציק") == "- [ ] x - אחראי: איציק"
    assert S("- [ ] x - אחראי: Speaker A ודימה") == "- [ ] x - אחראי: Speaker A ודימה"
    assert S("not a task - אחראי: Speaker A") == "not a task - אחראי: Speaker A"
    # 2) merge replaces ONLY the tasks section; empty pass keeps the summary
    summ = "## כותרת\nX\n\n## משימות\n- [ ] old\n\n## הערות\nz"
    merged = w._merge_tasks_section(summ, "- [ ] n1\nprose\n- [ ] n2 - אחראי: נאור")
    assert "old" not in merged and "n1" in merged and "n2" in merged
    assert "## הערות\nz" in merged and merged.count("## משימות") == 1
    assert w._merge_tasks_section(summ, "nothing") == summ
    assert w._merge_tasks_section("## כותרת\nX", "- [ ] a").rstrip().endswith("## משימות\n- [ ] a")
    # 3) the hook inside summarize(): mocked Ollama, count calls
    calls = []

    def fake_once(self, url, system_prompt, content, num_ctx, think, read_to,
                  num_predict=None, options_extra=None):
        calls.append(system_prompt)
        if system_prompt == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT):
            return "- [ ] t1 - אחראי: Speaker A\n- [ ] t2 - אחראי: נאור"
        return "## כותרת\nX\n\n## משימות\n- [ ] old - אחראי: דובר 1"
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = fake_once
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        # enabled + meeting -> 2 calls, tasks replaced, labels stripped
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1", local_tasks_pass=True)
        assert len(calls) == 2 and calls[1] == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT), calls
        assert "old" not in out and "t1" in out and "t2 - אחראי: נאור" in out, out
        assert "Speaker A" not in out, out
        # disabled -> 1 call, single-pass kept (and its label owner stripped too)
        calls.clear()
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1", local_tasks_pass=False)
        assert len(calls) == 1 and "old" in out and "דובר 1" not in out, (calls, out)
        # enabled but NOT a meeting (general Summarize tool) -> no tasks pass
        calls.clear()
        c.summarize("טקסט", "SYS", meeting_meta=None, local_tasks_pass=True)
        assert len(calls) == 1, calls
        # tasks pass returns nothing -> summary kept intact
        calls.clear()
        w.OpenAILLMCleaner._ollama_summary_once = (
            lambda self, url, sp, content, n, t, r, num_predict=None, options_extra=None:
            "" if sp == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT) else "## משימות\n- [ ] keep")
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1", local_tasks_pass=True)
        assert "keep" in out, out
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig
    # 4) wiring
    assert w.DEFAULT_CONFIG.get("summary_local_tasks_pass") is False
    assert hasattr(App, "_toggle_summary_local_tasks_pass")
    actions = App._settings_action_map(App.__new__(App))
    assert "toggle_summary_local_tasks_pass" in actions
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert 'local_tasks_pass=bool(self.config.get("summary_local_tasks_pass", False))' in src
    sw = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                           "settings_window.py"), encoding="utf-8").read()
    assert "toggle_summary_local_tasks_pass" in sw


_test("summary: LOCAL tasks pass (strip labels / merge / hook / wiring)",
      t_local_tasks_pass)


def t_summary_prompt_sync():
    """Cross-repo prompt-sync pin (ported 2026-08-27; templated 2026-08-28):
    the prompt TEMPLATES are sha-pinned - a tuning change is a CONSCIOUS act
    in BOTH repos (retune, re-verify, update the pin here AND the upstream
    project's own sha gate). The templates hold notetaker-name placeholders
    («NT» family, config `notetaker_names`); rendering them with the upstream
    project's notetaker identity reproduces that repo's pinned bytes exactly
    (proven 2026-08-28: the pre-templating shas 4ffb66be0aeac76b /
    16fb8a74ecb3a6d8 / ca28d49ff8252fee / 5bd8eeb96151903c came back
    byte-identical), so the sync remains verifiable from the sibling side."""
    import hashlib
    import lia as w
    sha = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
    # NOTE (2026-08-28): the meeting prompt DELIBERATELY diverges from the
    # upstream project here - a person/org disambiguation example named a real
    # bank, genericized for the public repo (יהב -> אורי). A future re-sync
    # from upstream must re-apply that swap, not reintroduce the real name.
    assert sha(w._SUMMARY_PROMPT_MEETING) == "6c9b0fae63281d7b", (
        "meeting prompt TEMPLATE drifted: " + sha(w._SUMMARY_PROMPT_MEETING))
    assert sha(w._LOCAL_TASKS_PASS_PROMPT) == "9e3c0df8e27193e8", (
        "tasks-pass prompt TEMPLATE drifted: " + sha(w._LOCAL_TASKS_PASS_PROMPT))
    # The two rules the 2026-08-27 sync added must survive any future edit.
    assert "TONE: when describing" in w._SUMMARY_PROMPT_MEETING
    assert "OWNER NAME FORM" in w._LOCAL_TASKS_PASS_PROMPT
    assert "בקשה לעוזרת ה-AI" in w._LOCAL_TASKS_PASS_PROMPT
    # The EN mirror prompts are Lia-only but sha-pinned for the same reason.
    import lang_pack as LP
    assert sha(LP.SUMMARY_PROMPT_MEETING_EN) == "46192c0388c8c139", (
        "EN meeting prompt drifted: " + sha(LP.SUMMARY_PROMPT_MEETING_EN))
    assert sha(LP.LOCAL_TASKS_PASS_PROMPT_EN) == "10e7c5dd552f97e9", (
        "EN tasks-pass prompt drifted: " + sha(LP.LOCAL_TASKS_PASS_PROMPT_EN))
    assert sha(LP.CLOUD_PARITY_ADDENDUM_EN) == "717244a6da17ebc1", (
        "EN parity addendum drifted: " + sha(LP.CLOUD_PARITY_ADDENDUM_EN))
    # Notetaker templating: every placeholder resolves with the default
    # identity, in every prompt that carries the rule, in both languages.
    for tpl in (w._SUMMARY_PROMPT_MEETING, w._SUMMARY_PROMPT_MAP,
                w._LOCAL_TASKS_PASS_PROMPT, w._SUM3P_A, w._SUM3P_B,
                w._SUM3P_MERGE, LP.SUMMARY_PROMPT_MEETING_EN,
                LP.LOCAL_TASKS_PASS_PROMPT_EN, LP.SUMMARY_PROMPT_MAP_EN):
        rendered = LP.render_notetaker(tpl)
        assert "«NT" not in rendered, "unrendered notetaker placeholder"
    assert "Lia" in LP.render_notetaker(LP.SUMMARY_PROMPT_MEETING_EN)
    # A custom identity renders through (names[0]=display, names[1:]=aliases).
    r = LP.render_notetaker('«NT_UP» "«NT»" («NT_HE») e.g. («NT_1»)',
                            ["Nova", "נובה", "נובא"])
    assert r == 'NOVA "Nova" (נובה/נובא) e.g. (נובה)', r


_test("summary: prompt sync pin vs the private upstream project (sha-gated, he + en)",
      t_summary_prompt_sync)


def t_summary_dedup():
    """summary_dedup.py (verbatim the private upstream project's dedup module): the closed
    near-duplicate rule - number CONFLICT always distinct, number SUBSET merges
    (the revisited-topic enrichment), Hebrew-prefix-tolerant containment,
    prefer() keeps the number-superset / longer variant."""
    import summary_dedup as d
    # number conflict: each side carries numbers the other lacks -> DISTINCT
    assert not d.is_near_duplicate("להקים VLAN לרשת 10", "להקים VLAN לרשת 20")
    # number subset: the revisit only added a date -> merges
    assert d.is_near_duplicate("לתאם פגישה עם הספק בנושא הרישוי",
                               "לתאם פגישה עם הספק בנושא הרישוי עד 15/09")
    # containment, tolerant to one-letter Hebrew glue prefixes (הדוח/דוח)
    assert d.is_near_duplicate("לשלוח את הדוח ללקוח",
                               "לשלוח את דוח ללקוח ולוודא קבלה")
    # genuinely distinct topics stay distinct
    assert not d.is_near_duplicate("להקים שרת גיבוי חדש",
                                   "לבדוק את חוזה הרישוי מול פאלו")
    assert d.is_near_duplicate("אב", "אב") and not d.is_near_duplicate("", "x")
    # prefer: number-superset wins, then longer, first on a tie
    assert d.prefer("לתאם פגישה", "לתאם פגישה עד 15/09") == 1
    assert d.prefer("לתאם פגישה עד 15/09", "לתאם פגישה") == 0
    assert d.prefer("קצר", "ארוך יותר מזה") == 1
    assert d.prefer("אותו טקסט", "אותו טקסט") == 0


_test("summary: near-duplicate closed rule (summary_dedup)", t_summary_dedup)


def t_summary_backstops():
    """The deterministic backstop chain (ported): title backstop,
    tone softening (prose only), paren-owner normalization, the final
    near-duplicate task sweep, and their wiring inside summarize()."""
    import lia as w
    # 1) title backstop: topic-as-first-header demoted; known/canonical kept
    assert w._fix_title_header("## סיכום ארכיטקטורת הרשת\nגוף") == \
        "## כותרת הדיון\nסיכום ארכיטקטורת הרשת\nגוף"
    assert w._fix_title_header("## תקציר\nגוף") == "## תקציר\nגוף"
    s = "## כותרת הדיון\nX\n\n## תקציר\ny"
    assert w._fix_title_header(s) == s
    assert w._fix_title_header("") == ""
    # 2) tone: frame + word swaps on prose; task lines untouched
    assert w._soften_tone("בעקבות מחלוקת בין ליטל לגיא הוחלט") == \
        "לאחר דיון בין ליטל לגיא הוחלט"
    assert w._soften_tone("התגלעה מחלוקת סביב התקציב") == "התקיים דיון סביב התקציב"
    assert w._soften_tone("העימות סביב התקציב נמשך") == "הדיון סביב התקציב נמשך"
    assert w._soften_tone("- [ ] יישוב מחלוקת מול הספק") == "- [ ] יישוב מחלוקת מול הספק"
    # 3) paren owners: promoted when no dash-form owner, stripped otherwise
    assert w._normalize_paren_owners("- [ ] לשלוח דוח (אחראי: דנה)") == \
        "- [ ] לשלוח דוח - אחראי: דנה"
    assert w._normalize_paren_owners("- [ ] לשלוח דוח (אחראי: דנה) - אחראי: נאור") == \
        "- [ ] לשלוח דוח - אחראי: נאור"
    # 4) final task dedup: richer (owner/number-superset) survives; number
    #    conflicts keep both; other sections byte-identical
    s = ("## כותרת הדיון\nX\n\n## משימות\n"
         "- [ ] לתאם פגישה עם הספק בנושא הרישוי\n"
         "- [ ] לתאם פגישה עם הספק בנושא הרישוי עד 15/09 - אחראי: נאור\n"
         "- [ ] להקים VLAN לרשת 10\n"
         "- [ ] להקים VLAN לרשת 20\n\n## הערות\nz")
    out = w._dedupe_tasks_section(s)
    assert "עד 15/09 - אחראי: נאור" in out and out.count("לתאם פגישה") == 1, out
    assert out.count("VLAN") == 2 and "## הערות\nz" in out, out
    assert w._dedupe_tasks_section("no tasks here") == "no tasks here"
    # 5) wiring inside summarize(): meeting mode gets the chain (mocked Ollama)
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = (
        lambda self, url, sp, content, n, t, r, num_predict=None, options_extra=None:
        "## נושא חשוב מאוד בפגישה\nגוף\n\n## משימות\n- [ ] לשלוח דוח (אחראי: דנה)\n"
        "\nבעקבות מחלוקת בין הצדדים")
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1")
        assert out.startswith("## כותרת הדיון\nנושא חשוב מאוד בפגישה"), out
        assert "- [ ] לשלוח דוח - אחראי: דנה" in out, out
        assert "לאחר דיון בין הצדדים" in out, out
        # general (non-meeting) summaries: chain NOT applied
        out = c.summarize("טקסט", "SYS", meeting_meta=None)
        assert "## נושא חשוב מאוד בפגישה" in out and "מחלוקת" in out, out
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig


_test("summary: deterministic backstop chain (title/tone/owners/dedup)",
      t_summary_backstops)


def t_mr_overlap_chunker():
    """_split_for_summary overlap (upstream parity): 0 = byte-identical old
    behavior (join identity holds); >0 = each chunk after the first is prefixed
    with the previous chunk's tail lines, content+overlap within budget, and a
    boundary line appears WHOLE in the next window."""
    import lia as w
    S = w.GroqLLMCleaner._split_for_summary
    text = "\n".join("line %03d " % i + "א" * 40 for i in range(100))
    old = S(text, 1000)
    assert S(text, 1000, overlap_chars=0) == old
    assert "\n".join(old) == text          # join identity, nothing duplicated
    ov = S(text, 1000, overlap_chars=200)
    assert len(ov) > len(old)              # overlap comes OUT of the budget
    assert all(len(c) <= 1000 for c in ov)
    assert ov[0].split("\n")[0] == "line 000 " + "א" * 40   # first chunk unprefixed
    for a, b in zip(ov, ov[1:]):           # boundary line whole in the next window
        assert a.split("\n")[-1] in b
    # tiny input: single chunk, no prefixing
    assert S("קצר", 1000, overlap_chars=200) == ["קצר"]


_test("summary: mr window overlap chunker", t_mr_overlap_chunker)


def t_merge_task_windows():
    """_merge_task_windows: window order kept, exact dups dropped, fuzzy
    near-dups collapse to the RICHER variant (real owner beats none), number
    conflicts always keep both lines."""
    import lia as w
    p1 = "- [ ] לתאם פגישה עם הספק בנושא הרישוי\n- [ ] להקים VLAN לרשת 10"
    p2 = ("- [ ] לתאם פגישה עם הספק בנושא הרישוי עד 15/09 - אחראי: נאור\n"
          "- [ ] להקים VLAN לרשת 20\n"
          "- [ ] להקים VLAN לרשת 10")   # exact dup of a p1 line
    out = w._merge_task_windows([p1, p2], fuzzy=True)
    lines = out.splitlines()
    assert len(lines) == 3, out
    # richer variant replaced in place, at the FIRST occurrence position
    assert lines[0] == "- [ ] לתאם פגישה עם הספק בנושא הרישוי עד 15/09 - אחראי: נאור"
    assert lines[1] == "- [ ] להקים VLAN לרשת 10"
    assert lines[2] == "- [ ] להקים VLAN לרשת 20"
    # fuzzy OFF: only exact dedup - the rephrased task stays
    out = w._merge_task_windows([p1, p2], fuzzy=False)
    assert len(out.splitlines()) == 4, out
    # prose / empty windows contribute nothing
    assert w._merge_task_windows(["prose only", "", None]) == ""


_test("summary: windowed tasks merge (fuzzy dedup)", t_merge_task_windows)


def t_windowed_tasks_pass():
    """The tasks pass on a map-reduce-sized meeting runs per WINDOW (the old
    single-call path silently dropped the transcript START past num_ctx) and
    merges the checklists in code."""
    import lia as w
    calls = []

    def fake_once(self, url, system_prompt, content, num_ctx, think, read_to,
                  num_predict=None, options_extra=None):
        calls.append((system_prompt, len(content)))
        if system_prompt == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT):
            i = sum(1 for c in calls if c[0] == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT))
            return "- [ ] משימה מחלון %d" % i
        return "## כותרת הדיון\nX\n\n## משימות\n- [ ] old"
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = fake_once
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        big = "\n".join("שורה %d " % i + "א" * 60 for i in range(1600))  # ~110K chars
        out = c.summarize(big, "SYS", meeting_meta="Duration: 90",
                          local_tasks_pass=True, mr_overlap_tokens=1536,
                          mr_fuzzy_dedup=True)
        n_tasks_calls = sum(1 for s, _ in calls if s == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT))
        assert n_tasks_calls >= 2, calls            # windowed, not one truncated call
        # every window call fit the context window (the bug this fixes)
        cap_chars = int((w._SUMMARY_CTX_CAP - 4000) * w._SUMMARY_CPT)
        assert all(ln <= cap_chars for s, ln in calls
                   if s == w._render_nt(w._LOCAL_TASKS_PASS_PROMPT)), calls
        assert "משימה מחלון 1" in out and "משימה מחלון %d" % n_tasks_calls in out
        assert "old" not in out
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig


_test("summary: tasks pass windowed on map-reduce meetings", t_windowed_tasks_pass)


def t_mr_prefer_threshold():
    """summary_mr_prefer_tokens: above the threshold a fitting meeting is still
    run map-reduce (packed-window recall), the FORCED plan caps its chunks at
    the threshold, a plan that would not really split demotes to one-shot, and
    0 / general mode keep today's behavior."""
    import lia as w
    calls = []

    def fake_once(self, url, system_prompt, content, num_ctx, think, read_to,
                  num_predict=None, options_extra=None):
        calls.append((system_prompt, len(content)))
        return "## כותרת הדיון\nX\n\n## משימות\n- [ ] a"
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = fake_once
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        # ~35K chars: fits one window (35K/1.9+8192 < 32768) but est prompt
        # ~18.4K tokens >= 16000 -> forced map-reduce with threshold-capped chunks
        band = "\n".join("שורה %d " % i + "א" * 60 for i in range(520))
        assert int(len(band) / w._SUMMARY_CPT) + w._SUMMARY_RESERVE <= w._SUMMARY_CTX_CAP
        c.summarize(band, "SYS", meeting_meta="Duration: 45", mr_prefer_tokens=16000)
        maps = [ln for s, ln in calls if s == w._render_nt(w._SUMMARY_PROMPT_MAP)]
        assert len(maps) >= 2, calls   # really split (map calls), not one packed window
        cap = int((16000 - 2600) * w._SUMMARY_CPT) + 4096   # forced chunk + wrap slack
        assert all(ln <= cap for ln in maps), maps
        # threshold 0 -> one-shot (today's behavior)
        calls.clear()
        c.summarize(band, "SYS", meeting_meta="Duration: 45", mr_prefer_tokens=0)
        assert len(calls) == 1 and calls[0][0] == "SYS", calls
        # below the threshold -> one-shot
        calls.clear()
        small = band[:20000]
        c.summarize(small, "SYS", meeting_meta="Duration: 20", mr_prefer_tokens=16000)
        assert len(calls) == 1 and calls[0][0] == "SYS", calls
        # general mode (no meeting_meta) ignores the threshold
        calls.clear()
        c.summarize(band, "SYS", meeting_meta=None, mr_prefer_tokens=16000)
        assert len(calls) == 1 and calls[0][0] == "SYS", calls
        # one giant unsplittable line -> the forced plan demotes to one-shot
        calls.clear()
        giant = "א" * 35000
        c.summarize(giant, "SYS", meeting_meta="Duration: 45", mr_prefer_tokens=16000)
        assert len(calls) == 1 and calls[0][0] == "SYS", calls
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig
    assert w.DEFAULT_CONFIG.get("summary_mr_prefer_tokens") == 16000
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert 'mr_prefer_tokens=int(self.config.get("summary_mr_prefer_tokens", 16000))' in src


_test("summary: prefer-mr threshold (forced windowed above 16k)",
      t_mr_prefer_threshold)


def t_condense_guard_family():
    """The closed faithfulness guards a model rewrite must pass (the private upstream project
    port): length window, number survival, no new completed-action claim, no
    invented foreign tokens, status-word counts, known-name survival."""
    import lia as w
    G = w._condense_guard
    orig = "- הוחלט להקים VLAN לרשת 10 בעלות 450 שקל - אחראי: נאור\n- סטטוס רמת גן: מתקדם"
    known = w._condense_known_names(orig)
    assert "נאור" in known
    ok = "- הוחלט להקים VLAN לרשת 10 בעלות 450 שקל (נאור); רמת גן: מתקדם"
    assert G(orig, ok, known, 0.35, 1.02) == ok
    assert G(orig, "", known, 0.35, 1.02) is None            # empty
    assert G(orig, "קצר", known, 0.35, 1.02) is None          # below length window
    assert G(orig, ok.replace("450", ""), known, 0.35, 1.02) is None   # number lost
    assert G(orig, ok.replace("להקים", "הוקמו"), known, 0.35, 1.02) is None  # decision -> done
    assert G(orig, ok.replace("VLAN", "stylesheet"), known, 0.35, 1.02) is None  # invented Latin
    assert G(orig, ok.replace("מתקדם", ""), known, 0.35, 1.02) is None  # status word vanished
    assert G(orig, ok.replace("נאור", ""), known, 0.35, 1.02) is None   # known name lost
    # legit singular/plural Latin morph does not false-trip
    assert G("שני VLANs הוגדרו כאן אתמול בערב", "שני VLAN הוגדרו כאן אתמול", set(),
             0.35, 1.02) is not None


_test("summary: condense guard family (closed rules)", t_condense_guard_family)


def t_consolidate_pass():
    """_consolidate_pass: merges same-topic prose bullets ONLY when the guard
    family passes AND the bullet count strictly decreases; '## משימות' is never
    sent to the model; per-section fallback keeps the original."""
    import lia as w
    summ = ("## כותרת הדיון\nX\n\n## תקציר\n"
            "- דיון על שדרוג הרשת בסניף חיפה\n"
            "- הוחלט לדחות את שדרוג הרשת בסניף חיפה לרבעון הבא\n"
            "- עדכון על גיוס עובד חדש\n\n"
            "## משימות\n- [ ] לתאם פגישה - אחראי: נאור\n\n"
            "## דגשים מרכזיים\n- נקודה אחת בלבד\n")
    sent = []

    def call_merge(content, system):
        sent.append(content)
        assert system is w._CONSOLIDATE_PROMPT
        return ("- הוחלט לדחות את שדרוג הרשת בסניף חיפה לרבעון הבא\n"
                "- עדכון על גיוס עובד חדש")
    out = w._consolidate_pass(summ, call_merge)
    # only the 3-bullet תקציר was sent; משימות + the 1-bullet section were not
    assert len(sent) == 1 and "משימה" not in sent[0] and "לתאם פגישה" not in sent[0]
    assert out.count("שדרוג הרשת בסניף חיפה") == 1, out
    assert "- [ ] לתאם פגישה - אחראי: נאור" in out, out
    # same bullet count -> rejected (a rewrite, not a merge)
    out2 = w._consolidate_pass(summ, lambda c, s: c)
    assert out2 == summ
    # guard trip (number invented... use foreign token) -> section kept
    out3 = w._consolidate_pass(
        summ, lambda c, s: "- stylesheet חדש\n- שני")
    assert out3 == summ
    # wiring: summarize() runs it for meetings when enabled (mocked)
    calls = []

    def fake_once(self, url, system_prompt, content, num_ctx, think, read_to,
                  num_predict=None, options_extra=None):
        calls.append(system_prompt)
        if system_prompt is w._CONSOLIDATE_PROMPT:
            return ""   # empty -> guard keeps the section
        return summ
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = fake_once
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1",
                          consolidate_pass=True)
        assert any(s is w._CONSOLIDATE_PROMPT for s in calls), calls
        assert "שדרוג הרשת" in out
        calls.clear()
        c.summarize("טקסט", "SYS", meeting_meta="Duration: 1", consolidate_pass=False)
        assert not any(s is w._CONSOLIDATE_PROMPT for s in calls)
        calls.clear()
        c.summarize("טקסט", "SYS", meeting_meta=None, consolidate_pass=True)
        assert not any(s is w._CONSOLIDATE_PROMPT for s in calls)
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig
    assert w.DEFAULT_CONFIG.get("summary_consolidate_pass") is True


_test("summary: CONSOLIDATE pass (merge same-topic bullets, guarded)",
      t_consolidate_pass)


def t_task_done_pass():
    """TASK-DONE pass: explicit-evidence votes flip '- [ ]' to
    '- [x] ... - בוצע במהלך הפגישה' (never deletes), the bot-request line is
    immune, a runaway vote is ignored, and the pass is windowed on mr runs."""
    import lia as w
    # pure helpers
    summ = ("## כותרת הדיון\nX\n\n## משימות\n"
            "- [ ] לשלוח מכתב לאורן - אחראי: נאור\n"
            "- [ ] בקשה לעוזרת ה-AI (ליה): לתמלל\n"
            "- [ ] להקים שרת\n\n## הערות\nz")
    assert w._task_texts(summ) == ["לשלוח מכתב לאורן",
                                   "בקשה לעוזרת ה-AI (ליה): לתמלל", "להקים שרת"]
    assert w._parse_done_votes("1: בוצע\nאין\n7: בוצע\nblah", 3) == {1}
    out = w._apply_done_marks(summ, {1, 2})
    assert "- [x] לשלוח מכתב לאורן - אחראי: נאור - בוצע במהלך הפגישה" in out, out
    assert "- [ ] בקשה לעוזרת ה-AI" in out          # bot line immune
    assert "- [ ] להקים שרת" in out and "## הערות\nz" in out
    # summarize() wiring: vote applied; runaway ignored
    votes_reply = ["1: בוצע"]
    calls = []

    def fake_once(self, url, system_prompt, content, num_ctx, think, read_to,
                  num_predict=None, options_extra=None):
        calls.append(system_prompt)
        if system_prompt is w._TASK_DONE_PROMPT:
            assert "המשימות:" in content and "1. לשלוח מכתב לאורן" in content
            return "\n".join(votes_reply)
        return summ
    orig = w.OpenAILLMCleaner._ollama_summary_once
    w.OpenAILLMCleaner._ollama_summary_once = fake_once
    try:
        c = w.OpenAILLMCleaner(api_key="ollama", model="gemma4:31b-it-qat",
                               chat_url="http://localhost:11434/v1/chat/completions")
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1",
                          task_done_pass=True)
        assert "- [x] לשלוח מכתב לאורן" in out and "בוצע במהלך הפגישה" in out, out
        # runaway: 2 of 3 voted done -> ignored
        votes_reply[:] = ["1: בוצע", "3: בוצע"]
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 1",
                          task_done_pass=True)
        assert "- [x]" not in out, out
        # disabled / general mode -> no task-done call
        calls.clear()
        c.summarize("טקסט", "SYS", meeting_meta="Duration: 1", task_done_pass=False)
        c.summarize("טקסט", "SYS", meeting_meta=None, task_done_pass=True)
        assert not any(s is w._TASK_DONE_PROMPT for s in calls)
        # windowed on an mr-sized meeting: multiple task-done calls, any window's
        # vote flips
        votes_reply[:] = ["אין"]
        n_seen = []

        def fake_win(self, url, system_prompt, content, num_ctx, think, read_to,
                     num_predict=None, options_extra=None):
            if system_prompt is w._TASK_DONE_PROMPT:
                n_seen.append(1)
                return "1: בוצע" if len(n_seen) == 2 else "אין"
            if system_prompt == w._render_nt(w._SUMMARY_PROMPT_MAP):
                return "notes"
            return summ
        w.OpenAILLMCleaner._ollama_summary_once = fake_win
        big = "\n".join("שורה %d " % i + "א" * 60 for i in range(1600))
        out = c.summarize(big, "SYS", meeting_meta="Duration: 90",
                          task_done_pass=True, mr_overlap_tokens=1536)
        assert len(n_seen) >= 2, n_seen
        assert "- [x] לשלוח מכתב לאורן" in out, out
    finally:
        w.OpenAILLMCleaner._ollama_summary_once = orig
    assert w.DEFAULT_CONFIG.get("summary_task_done_pass") is True


_test("summary: TASK-DONE pass (evidence votes, [x] flip, runaway guard)",
      t_task_done_pass)


def t_cloud_parity():
    """Cloud PARITY: the compact addendum rides the CLOUD system prompt (meeting
    mode only; sha-pinned to the upstream project's byte-identical block) and the free
    deterministic backstop chain runs on the cloud output. No LLM passes on
    cloud; the local prompt path never reads it."""
    import hashlib
    import lia as w
    assert hashlib.sha256(
        w._SUMMARY_CLOUD_PARITY_ADDENDUM.encode("utf-8")).hexdigest()[:16] == \
        "4f97dd32d8417d8c"   # the SAME sha the upstream project pins
    sent = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content":
                "## נושא הפגישה ברבעון\nגוף\n\n## משימות\n"
                "- [ ] לשלוח דוח (אחראי: דנה)\n- [ ] לשלוח דוח (אחראי: דנה)\n"
                "\nבעקבות מחלוקת בין הצדדים"}}]}

    class _Sess:
        def post(self, url, headers=None, json=None, timeout=None):
            sent.append(json)
            return _Resp()
    orig = w.OpenAILLMCleaner._ensure_session
    w.OpenAILLMCleaner._ensure_session = lambda self: _Sess()
    try:
        c = w.OpenAILLMCleaner(api_key="sk-test", model="gpt-5.6-sol")
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 30",
                          cloud_parity=True)
        assert sent[0]["messages"][0]["content"].endswith(
            w._SUMMARY_CLOUD_PARITY_ADDENDUM)
        assert out.startswith("## כותרת הדיון\nנושא הפגישה ברבעון"), out
        assert out.count("- [ ] לשלוח דוח - אחראי: דנה") == 1, out   # dedup + paren
        assert "לאחר דיון בין הצדדים" in out, out                     # tone
        # parity off -> prompt untouched, output raw
        sent.clear()
        out = c.summarize("טקסט", "SYS", meeting_meta="Duration: 30",
                          cloud_parity=False)
        assert sent[0]["messages"][0]["content"] == "SYS"
        assert "## נושא הפגישה ברבעון" in out and "מחלוקת" in out
        # general mode -> no addendum even with parity on
        sent.clear()
        c.summarize("טקסט", "SYS", meeting_meta=None, cloud_parity=True)
        assert sent[0]["messages"][0]["content"] == "SYS"
    finally:
        w.OpenAILLMCleaner._ensure_session = orig
    assert w.DEFAULT_CONFIG.get("summary_cloud_parity") is True
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert 'cloud_parity=bool(self.config.get("summary_cloud_parity", True))' in src


_test("summary: cloud PARITY (addendum + free backstops on SOL)", t_cloud_parity)


def t_smart_chunk_boundaries():
    """Meetily-inspired boundary quality (2026-08-27): (1) smart_cut_tail cuts
    a drained meeting window at its quietest point near the end so the carried
    tail rides into the next chunk - lossless, never on short audio, no
    sub-min_carry tails; (2) RemoteTranscriber._split_at_silence overlaps by
    CHUNK_OVERLAP_S when a window has NO real silence (a doubled word beats a
    clipped one) and still cuts losslessly at real silence."""
    import numpy as np
    import lia as w
    sr = 16000
    rng = np.random.default_rng(7)
    # smart_cut_tail: quiet dip near the end -> cut inside the dip, lossless
    a = (rng.standard_normal(45 * sr) * 0.2).astype(np.float32)
    d0, d1 = int(43.2 * sr), int(43.5 * sr)
    a[d0:d1] *= 0.01
    head, tail = w.smart_cut_tail(a)
    assert tail is not None and d0 <= len(head) <= d1
    assert len(head) + len(tail) == len(a)
    # short audio untouched; tiny tail not carried
    h2, t2 = w.smart_cut_tail(a[:3 * sr])
    assert t2 is None and len(h2) == 3 * sr
    b = (rng.standard_normal(45 * sr) * 0.2).astype(np.float32)
    b[-int(0.05 * sr):] *= 0.001
    assert w.smart_cut_tail(b)[1] is None
    # continuous loud audio: any carry stays within the search window
    c = (np.sin(np.arange(45 * sr) * 0.3) * 0.3).astype(np.float32)
    t4 = w.smart_cut_tail(c)[1]
    assert t4 is None or 0.15 * sr <= len(t4) <= 2.5 * sr
    # remote split: no silence -> 1s overlap fallback; real silence -> lossless
    S = w.RemoteTranscriber._split_at_silence
    loud = (rng.standard_normal(25 * sr) * 0.3).astype(np.float32)
    ch = S(loud, sr)
    assert len(ch) == 2
    assert abs((len(ch[0]) + len(ch[1])) - (len(loud) + sr)) < 1600   # ~1s overlap
    quiet = loud.copy()
    q0 = int(15 * sr)
    quiet[q0:q0 + int(0.6 * sr)] *= 0.001
    ch2 = S(quiet, sr)
    assert abs(len(ch2[0]) - int(15.3 * sr)) < sr                     # cut at the silence
    assert sum(len(x) for x in ch2) == len(quiet)                     # no overlap needed
    # wiring: kill-switch default + rotation/finalise carry plumbing
    assert w.DEFAULT_CONFIG.get("meeting_chunk_smart_cut") is True
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert 'self.app.config.get("meeting_chunk_smart_cut", True)' in src
    assert src.count("self._with_carry(") >= 2      # rotation + finalise
    assert hasattr(w.MeetingSession, "_with_carry")


_test("audio: smart chunk boundaries (carry-the-tail + overlap fallback)",
      t_smart_chunk_boundaries)


def t_meeting_attendees():
    """Speaker naming 1a: attendee-name parsing (pure), the summary-metadata
    invited line, and the wiring (indexer CLI mode, async fetch at meeting
    start, Invited header in both transcript writers)."""
    import emailsearch_indexer as ex
    S = ex.split_attendee_names
    assert S("Dana Cohen; Yuval Bar", "dana cohen; Avi Levi <avi@x.com>") == \
        ["Dana Cohen", "Yuval Bar", "Avi Levi"]
    assert S("", None) == []
    assert S(" ; ;נעמה בר ;") == ["נעמה בר"]
    import lia as w
    s = w.MeetingSession.__new__(w.MeetingSession)
    s.source = "both"
    s.attendees = ["דנה", "Avi"]
    meta = s._meta_for_summary(65, 2)
    assert "מוזמנים ביומן" in meta and "דנה, Avi" in meta and "הקשר בלבד" in meta
    s.attendees = []
    assert "מוזמנים" not in s._meta_for_summary(65, 2)
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert '"--current-meeting", out_path' in src
    assert "self._fetch_meeting_attendees_async(session)" in src
    assert src.count('"Invited: " + "; ".join(self.attendees)') == 2  # both writers
    isrc = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                             "emailsearch_indexer.py"), encoding="utf-8").read()
    assert "--current-meeting" in isrc and "AllDayEvent" in isrc


_test("meeting: calendar attendees (parse / metadata / wiring)",
      t_meeting_attendees)


def t_self_speaker_detect():
    """Speaker naming 1b: the mic-vs-loopback correlation picks the LOCAL
    user's cluster only when clearly mic-dominant AND separated; a mic-only
    meeting (no loopback signal) never labels; wiring + config defaults."""
    import lia as w
    # 120s timeline: A talks 0-40s into the MIC, B talks 45-100s via LOOPBACK
    mic = [0.001] * 120
    loop = [0.001] * 120
    for i in range(0, 40):
        mic[i] = 0.20
        loop[i] = 0.01
    for i in range(45, 100):
        mic[i] = 0.02      # acoustic bleed into the mic
        loop[i] = 0.25
    utts = [{"speaker": "A", "start": 0, "end": 40000},
            {"speaker": "B", "start": 45000, "end": 100000}]
    lab, margin = w.pick_self_speaker(utts, mic, loop)
    assert lab == "A" and margin > 0.5, (lab, margin)
    # mic-only meeting: loopback silent -> no 'which side' signal -> None
    assert w.pick_self_speaker(utts, mic, [0.0] * 120)[0] is None
    # ambiguous: both clusters mic-dominant -> None
    assert w.pick_self_speaker(utts, [0.2] * 120, [0.01] * 120)[0] is None
    # too little speech -> None
    assert w.pick_self_speaker(
        [{"speaker": "A", "start": 0, "end": 1500}], mic, loop)[0] is None
    assert w.pick_self_speaker([], mic, loop)[0] is None
    # config + wiring
    assert w.DEFAULT_CONFIG.get("speaker_self_detect") is True
    assert w.DEFAULT_CONFIG.get("speaker_self_name") == ""
    app = w.LiaApp.__new__(w.LiaApp)
    app.config = {"speaker_self_name": "נאור"}
    assert app._self_speaker_name() == "נאור"
    app.config = {}
    assert app._self_speaker_name()   # windows display name / USERNAME fallback
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert "pick_self_speaker(" in src and "_note_channel_energy(mic_audio, loop_audio)" in src
    assert src.count("self._note_channel_energy(") == 2   # drain + close
    assert 'u.get("speaker_name")' in src                  # writer honors names


_test("meeting: self-speaker detection (mic vs loopback)", t_self_speaker_detect)


def t_speaker_rename_ui():
    """Speaker-naming UI: turn scanning (hint from the first turn, preview
    timestamp from the LONGEST turn, h:mm:ss support, named lines skipped),
    WAV segment extraction for the ▶ preview, audio resolution, and the
    dialog wiring (attendee chips + play button plumbed through)."""
    import struct
    import wave as _wave
    import lia as w
    content = ("Meeting — X\nInvited: דנה כהן; Avi Levi\n\n"
               "[0:05] Speaker A:\n‏שלום לכולם ברוכים הבאים לפגישה החשובה הזאת\n\n"
               "[0:40] Speaker B:\n‏תודה\n\n"
               "[2:15] Speaker B:\n‏אני רוצה להציג את הארכיטקטורה החדשה בפירוט\n\n"
               "[3:00] נאור:\n‏דובר שכבר שוים - לא אמור להופיע\n")
    turns = w.scan_speaker_turns(content)
    assert [t[0] for t in turns] == ["A", "B"], turns
    assert turns[0][2] == 5 and turns[1][2] == 135          # longest turn wins
    assert "שלום לכולם" in turns[0][1] and turns[1][1] == "תודה"
    assert w.scan_speaker_turns(
        "[1:02:10] Speaker Q:\nטקסט ארוך מספיק לבחירה")[0][2] == 3730
    assert w.scan_speaker_turns("no speakers here") == []
    # WAV segment extraction: 10s file whose sample VALUE encodes its second
    sr = 16000
    src = os.path.join(os.environ.get("TEMP", "."), "wt_test_seg.wav")
    out = os.path.join(os.environ.get("TEMP", "."), "wt_test_seg_out.wav")
    with _wave.open(src, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"".join(struct.pack("<h", 1000 * (i // sr))
                                for i in range(10 * sr)))
    assert w.extract_audio_segment(src, 5.0, 2.0, out)
    with _wave.open(out, "rb") as rf:
        assert rf.getnframes() == 2 * sr
        assert struct.unpack("<h", rf.readframes(1))[0] == 5000  # starts at 5s
    assert not w.extract_audio_segment(src, 11.0, 2.0, out)      # past the end
    # audio resolution: hint wins; nothing -> None
    R = w.LiaApp._resolve_meeting_audio
    assert R("x_meeting_diarized.txt", audio_hint=src) == src
    assert R("2020-01-01_00-00-00_x_meeting_diarized.txt") is None
    for p in (src, out):
        try:
            os.remove(p)
        except OSError:
            pass
    # wiring: dialog passes suggestions/on_play; card lambdas pass attendees
    srcf = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                             "lia.py"), encoding="utf-8").read()
    assert "suggestions=attendees, on_play=on_play" in srcf
    assert srcf.count("attendees=self.attendees, audio_hint=self._wav_path") == 2
    assert "def _create_speaker_rename_toplevel(self, speakers, on_save,\n" \
           "                                        suggestions=None, on_play=None)" in srcf


_test("meeting: speaker-rename UI (turns / preview segment / wiring)",
      t_speaker_rename_ui)


def t_speaker_profiles():
    """Voiceprint store (speaker naming, phase 3): learn folds a running
    centroid per name (casefold-merged), match is conservative (threshold AND
    margin), bad input never learns, the store persists, and the lia /
    diarize_local wiring is in place (talk-gated embeddings, learn on rename,
    self-learn, match before writing)."""
    import numpy as np
    import speaker_profiles as sp
    # isolate the store (suite APPDATA is already a temp dir; use a sub-salt)
    rng = np.random.default_rng(3)
    voice_a = rng.standard_normal(256)
    voice_b = rng.standard_normal(256)
    assert sp.learn("דנה", voice_a) == 1
    assert sp.learn("דנה", voice_a + rng.standard_normal(256) * 0.1) == 2  # merged
    assert sp.learn("יובל", voice_b) == 1
    assert sp.learn("", voice_a) == 0 and sp.learn("X", [float("nan")] * 256) == 0
    m = sp.match({"A": voice_a + rng.standard_normal(256) * 0.15,
                  "B": rng.standard_normal(256)})
    assert m.get("A") == "דנה" and "B" not in m, m
    # margin guard: two near-identical profiles -> ambiguous -> no match
    sp.learn("כמעט-דנה", voice_a + rng.standard_normal(256) * 0.05)
    m2 = sp.match({"A": voice_a})
    assert "A" not in m2 or m2["A"] in ("דנה", "כמעט-דנה")  # matched only if clear
    names = [n for n, _c, _u in sp.stats()]
    assert "דנה" in names and "יובל" in names
    # wiring
    import lia as w
    assert w.DEFAULT_CONFIG.get("speaker_profiles_enabled") is True
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert '_talk_s.get(l, 0.0) >= 10.0' in src              # weak-cluster gate
    assert "Voiceprint learned (rename)" in src              # rename-save learns
    assert "Voiceprint learned (self)" in src                # self cluster learns
    assert "speaker_profiles.match(cand)" in src             # auto-name matching
    assert 'return utts, (data.get("speaker_embeddings") or {})' in src
    dsrc = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                             "diarize_local.py"), encoding="utf-8").read()
    assert "return_embeddings=True" in dsrc
    assert '"speaker_embeddings": embeddings' in dsrc
    assert "all(x == x for x in row)" in dsrc                # all-finite gate


_test("meeting: voiceprint store (learn / match / wiring)", t_speaker_profiles)


def t_speaker_name_pass():
    """Phase 2 (LLM name assignment): the evidence builder (head + longest
    turns), the code-enforced vote parser (candidate membership, uniqueness,
    taken names, invented names blocked), and the pipeline wiring. The LLM
    itself was validated LIVE on gemma4 (scripted transcript: self-intro +
    addressed-and-answered both correctly assigned, no-evidence label left
    unnamed, 9.2s)."""
    import lia as w
    utts = [
        {"speaker": "A", "text": "שלום, אני דנה כהן ואציג את הסטטוס",
         "start": 0, "end": 9000},
        {"speaker": "B", "text": "תודה דנה. אבי, תעדכן על ההקמה?",
         "start": 9000, "end": 16000},
        {"speaker": "C", "text": "כן, ההקמה מתקדמת ונסיים עד סוף החודש",
         "start": 16000, "end": 24000},
    ]
    ev = w._speaker_name_evidence(utts, ["A", "B", "C"])
    assert "Speaker A: שלום, אני דנה כהן" in ev and len(ev.splitlines()) == 3
    # longest-turns supplement for a speaker outside the head budget
    long_utts = ([{"speaker": "A", "text": "א" * 500, "start": 0, "end": 5000}] * 20
                 + [{"speaker": "B", "text": "טקסט מזהה של הדובר האחרון",
                     "start": 900000, "end": 905000}])
    ev2 = w._speaker_name_evidence(long_utts, ["B"], head_chars=2000)
    assert "טקסט מזהה של הדובר האחרון" in ev2
    P = w._parse_speaker_name_votes
    votes = P("A: דנה כהן\nB: אין\nC: אבי לוי\nD: מישהו",
              {"A", "B", "C"}, ["דנה כהן", "אבי לוי", "נעמה בר"])
    assert votes == {"A": "דנה כהן", "C": "אבי לוי"}, votes
    assert P("A: דנה כהן\nB: דנה כהן", {"A", "B"}, ["דנה כהן"]) == {"A": "דנה כהן"}
    assert P("A: דנה כהן", {"A"}, ["דנה כהן"], taken=("דנה כהן",)) == {}
    assert P("A: מנכל חדש", {"A"}, ["דנה כהן"]) == {}
    assert P("Speaker A: דנה כהן", {"A"}, ["דנה כהן"]) == {"A": "דנה כהן"}
    # config + wiring
    assert w.DEFAULT_CONFIG.get("speaker_name_pass") is True
    assert w.DEFAULT_CONFIG.get("speaker_name_pass_model") == "gemma4:31b-it-qat"
    src = open(os.path.join(os.path.dirname(os.path.abspath(w.__file__)),
                            "lia.py"), encoding="utf-8").read()
    assert "self.app._llm_assign_speaker_names(" in src
    assert 'if nm and not u.get("speaker_name")' in src   # never overrides


_test("meeting: LLM speaker-name pass (evidence / votes / wiring)",
      t_speaker_name_pass)


def t_settings_state_shape():
    """_settings_state produces a JSON-serialisable dict, masks all six secrets
    (never leaks a raw key), and carries the model/cleanup tables + hotkeys."""
    import json as _json
    import lia as w
    App = w.LiaApp
    app = App.__new__(App)
    app.config = dict(w.DEFAULT_CONFIG)
    app.config["openai_api_key"] = "sk-secretsecretsecret1234"
    app.config["gemini_api_key"] = "AQ.secretsecret999"
    app.model_loaded = True
    app.is_recording = False
    app._ollama_cache = None
    # meeting/live/loopback probes must not need a running app
    app._active_meeting = None
    st = App._settings_state(app, devices=False, ollama=False)
    blob = _json.dumps(st, ensure_ascii=True)   # must be JSON-safe
    assert "sk-secretsecretsecret1234" not in blob, "raw OpenAI key leaked!"
    assert "AQ.secretsecret999" not in blob, "raw Gemini key leaked!"
    for k in ("openai_api_key", "groq_api_key", "gemini_api_key",
              "assemblyai_api_key", "hf_token", "remote_server_token"):
        assert k in st["secrets"] and k in st["has"], k
        assert k not in st["config"], "secret must not be in config blob: " + k
    assert st["has"]["openai_api_key"] is True and st["has"]["groq_api_key"] is False
    assert st["secrets"]["openai_api_key"].startswith("sk-s") and "…" in st["secrets"]["openai_api_key"]
    assert "secretsecret" not in st["secrets"]["openai_api_key"]  # only a short hint, not a large slice
    for grp in ("dictation", "meeting", "summary", "file", "device",
                "cleanup_styles", "cleanup_models"):
        assert grp in st["tables"], "state tables missing " + grp
    assert st["hotkeys"]["main"] and "config" in st["paths"]


_test("settings: _settings_state shape + secret masking (no raw key leaks)",
      t_settings_state_shape)


def t_bundle_secret_scrub():
    """WP2: one secret list covers config.json, the diagnostic bundle, and the
    settings-state payload. serve_token is a real credential (the serve Bearer)
    and must be on it; the bundle sanitizer must not leak any secret, must
    redact private network addresses, and must size-redact personal lists."""
    import re as _re
    import json as _json
    import lia as w
    # every secret-shaped default key is on the single list
    for k in w.DEFAULT_CONFIG:
        if _re.search(r"(_api_key|_token|_secret|password)$", k):
            assert k in w._SECRET_CONFIG_KEYS, "unlisted secret config key: " + k
    assert "serve_token" in w._SECRET_CONFIG_KEYS
    cfg = dict(w.DEFAULT_CONFIG)
    cfg.update({
        "openai_api_key": "sk-SENTINELKEY1234",
        "serve_token": "TOKSENTINEL9999",
        "remote_server_url": "ws://100.70.229.87:9090",
        "custom_vocabulary": "SECRETPROJECTNAME, another",
    })
    out = w._sanitize_config_for_bundle(cfg)
    blob = _json.dumps(out, ensure_ascii=False)
    for leak in ("sk-SENTINELKEY1234", "TOKSENTINEL9999", "100.70.229.87",
                 "SECRETPROJECTNAME"):
        assert leak not in blob, "bundle leaked %r" % leak
    assert out["openai_api_key"] == "set" and out["serve_token"] == "set"
    assert out["remote_server_url"].startswith("ws://<redacted-host>")
    assert out["custom_vocabulary"].startswith("<redacted:")
    # empty secret -> "" (not "set")
    assert w._sanitize_config_for_bundle({"groq_api_key": ""})["groq_api_key"] == ""


_test("security WP2: one secret list covers config + bundle + settings; serve_token masked",
      t_bundle_secret_scrub)


def t_settings_prewarm():
    """Settings pre-warm: build_window boots the window HIDDEN only when the
    payload carries prewarm; _reveal_window shows it + marks ready; _stdin_reader
    reveals on a focus/show message and tears down at EOF; the parent grew the
    spawn/prewarm helpers and REVEALS a live proc instead of respawning."""
    import types, inspect, sys as _sys
    import settings_window as sw

    class _Ev:
        def __iadd__(self, fn):
            return self

    class FakeWin:
        def __init__(self):
            self.events = types.SimpleNamespace(closing=_Ev())
            self.shown = 0
            self.restored = 0
            self.destroyed = 0
            self.pushed = []

        def show(self):
            self.shown += 1

        def restore(self):
            self.restored += 1

        def destroy(self):
            self.destroyed += 1

        def evaluate_js(self, s):
            self.pushed.append(s)

    class FakeWebview:
        def create_window(self, title, html=None, js_api=None, **kw):
            self.kw = kw
            self.win = FakeWin()
            return self.win

    # build_window spawns a stdin-reader thread; stub it so it can't touch the
    # real stdin during this part of the test.
    orig_reader = sw._stdin_reader
    sw._stdin_reader = lambda: None
    try:
        fw = FakeWebview()
        sw.build_window(fw, {"state": {}, "page": "general", "prewarm": True})
        assert fw.kw.get("hidden") is True, "prewarm must create the window hidden"
        fw2 = FakeWebview()
        sw.build_window(fw2, {"state": {}, "page": "general"})
        assert "hidden" not in fw2.kw, "normal open must not hide the window"
    finally:
        sw._stdin_reader = orig_reader

    # _reveal_window: un-minimize (restore) + show() + set _READY
    win = FakeWin()
    sw._WINDOW = win
    sw._READY = False
    sw._reveal_window()
    assert win.shown == 1 and win.restored == 1 and sw._READY is True, \
        "reveal must restore (un-minimize) AND show the window"

    # _stdin_reader reveals on a focus message, then tears down at EOF
    win2 = FakeWin()
    sw._WINDOW = win2
    sw._READY = False

    class FakeStdin:
        def __init__(self, lines):
            self._it = iter(lines)

        def readline(self):
            try:
                return next(self._it)
            except StopIteration:
                return ""

    orig_stdin = _sys.stdin
    _sys.stdin = FakeStdin(['{"t":"focus","page":"audio"}\n'])
    try:
        sw._stdin_reader()
    finally:
        _sys.stdin = orig_stdin
    assert win2.shown == 1 and win2.restored == 1, "a focus message must reveal (restore+show)"
    assert win2.destroyed == 1, "EOF must tear the window down"

    # parent wiring: helpers exist; payload carries the flag; a live proc is
    # revealed (focus) BEFORE any spawn in the open path.
    import lia as w
    App = w.LiaApp
    for m in ("_spawn_settings_proc", "_spawn_settings_proc_locked",
              "_prewarm_settings_window", "_open_settings_window",
              "_reveal_settings"):
        assert hasattr(App, m), "missing " + m
    spawn_src = inspect.getsource(App._spawn_settings_proc_locked)
    assert '"prewarm": prewarm' in spawn_src, "payload must carry the prewarm flag"
    open_src = inspect.getsource(App._open_settings_window)
    assert "_reveal_settings" in open_src and "_spawn_settings_proc" in open_src
    assert open_src.index("_reveal_settings") < open_src.index("_spawn_settings_proc"), \
        "a live proc must be revealed before falling through to spawn"
    # the reveal path shows + navigates via a focus message
    reveal_src = inspect.getsource(App._reveal_settings)
    assert '"t": "focus"' in reveal_src


_test("settings: pre-warm (hidden boot, reveal-on-focus, parent wiring)",
      t_settings_prewarm)


def t_history_window_wiring():
    """History moved to a pywebview window (history_window.py): _show_history
    spawns it (txt export kept as the fallback), HistoryApi formats entries
    newest-first, and the ui_kit prefs/geometry helper round-trips."""
    import inspect, os, tempfile, json as _json
    import lia as w
    App = w.LiaApp
    # dispatcher spawns the window; the old txt export survives as the fallback.
    src = inspect.getsource(App._show_history)
    assert "history_window.py" in src, "History must spawn the pywebview window"
    assert "_export_history_txt" in src, "txt fallback must remain"
    assert hasattr(App, "_export_history_txt")
    # HistoryApi.get_initial: newest-first, skips blanks, builds meta.
    import history_window as hw
    tmp = tempfile.mkdtemp()
    hw.HISTORY_FILE = os.path.join(tmp, "history.json")
    with open(hw.HISTORY_FILE, "w", encoding="utf-8") as f:
        _json.dump([
            {"timestamp": "2026-08-15T10:00:00", "text": "first", "duration": 2,
             "model": "ivrit", "source": "microphone", "task": "transcribe"},
            {"timestamp": "2026-08-15T11:00:00", "text": "  ", "duration": 1},   # blank -> skipped
            {"timestamp": "2026-08-15T12:00:00", "text": "שלום עולם", "duration": 3,
             "model": "groq", "source": "both", "task": "translate"},
        ], f, ensure_ascii=False)
    d = hw.HistoryApi().get_initial()
    assert d["count"] == 2, d           # the blank entry is dropped
    assert d["entries"][0]["text"] == "שלום עולם", d["entries"][0]  # newest first
    assert "translate" in d["entries"][0]["meta"]
    # ui_kit prefs + geometry round-trip + clamp
    import ui_kit as uk
    uk.UI_PREFS_FILE = os.path.join(tmp, "ui_prefs.json")
    uk.save_ui_pref("win_history", {"width": 900, "height": 700, "x": 40, "y": 20})
    assert uk.window_geometry("history") == {"width": 900, "height": 700, "x": 40, "y": 20}
    uk.save_ui_pref("win_bad", {"width": 5, "height": 5})   # absurd -> default
    assert uk.window_geometry("bad", {"d": 1}) == {"d": 1}


_test("history: pywebview window + txt fallback + ui_kit prefs/geometry",
      t_history_window_wiring)


def t_summarize_window():
    """Summarize Text/File is now a pywebview window (summarize_window.py) on
    ui_kit: SummarizeApi hands the typed/loaded text back via a result file; the
    parent spawns it (the Tk input stays as the frozen-build fallback) and runs
    the summary on close via _do_text_summary."""
    import os, json as _json, tempfile, inspect
    import summarize_window as sw
    for m in ("submit", "loadfile", "cancel", "mark_ready", "close_window"):
        assert hasattr(sw.SummarizeApi, m), "SummarizeApi missing " + m
    assert hasattr(sw, "build_window") and hasattr(sw, "main")
    src = inspect.getsource(sw)
    assert "uk.child_main" in src and "js_api=SummarizeApi()" in src
    assert "import ui_kit" in src and "RK.ready(" in src  # api-defer discipline
    # submit writes {"text": ...} to the result path (how the parent reads it)
    rp = os.path.join(tempfile.mkdtemp(), "res.json")
    sw._RESULT_PATH = rp
    sw.SummarizeApi().submit("  hello world  ")
    with open(rp, encoding="utf-8") as f:
        assert _json.load(f)["text"] == "  hello world  "
    # parent wiring
    import lia as w
    App = w.LiaApp
    assert hasattr(App, "_summarize_text_dialog") and hasattr(App, "_summarize_window_wait")
    assert hasattr(App, "_do_text_summary")
    assert hasattr(w.OverlayNotification, "show_summarize_input"), "Tk fallback gone"
    dsrc = inspect.getsource(App._summarize_text_dialog)
    assert "summarize_window.py" in dsrc, "must spawn the pywebview window"
    assert "show_summarize_input" in dsrc, "Tk fallback must remain"
    wsrc = inspect.getsource(App._summarize_window_wait)
    assert "_do_text_summary" in wsrc, "closing the window must run the summary"


_test("summarize: pywebview input window + result-file handoff + Tk fallback",
      t_summarize_window)


# ============================================================
# Startup at logon + tray identity (2026-09-01)
# ============================================================
section("Startup at logon + tray identity")


def t_startup_relaunch_plan():
    """The logon self-relaunch policy (pure): manual launches never retry;
    a logon launch retries with a growing delay, carries --autostart +
    --restarted (mutex handoff) + the attempt counter, and stops after
    _AUTOSTART_MAX_ATTEMPTS."""
    import lia as w
    assert w._autostart_relaunch_plan(["lia.py"], logon=False) is None
    plan = w._autostart_relaunch_plan(["lia.py", "--autostart"])
    assert plan is not None
    delay, argv = plan
    assert delay == 15 and os.path.isabs(argv[0])
    assert "--autostart" in argv and "--restarted" in argv
    assert argv[-2:] == ["--attempt", "1"]
    assert w._autostart_attempt(argv) == 1
    # An implicit logon launch (uptime heuristic) gets the flag added so the
    # later attempts are explicit.
    _d2, argv2 = w._autostart_relaunch_plan(["lia.py"], logon=True)
    assert "--autostart" in argv2 and argv2[-2:] == ["--attempt", "1"]
    # attempt 2 -> delay 45 + attempt 3; attempt 3 -> stop
    d3, a3 = w._autostart_relaunch_plan(["lia.py", "--autostart", "--attempt", "2"])
    assert d3 == 45 and a3[-2:] == ["--attempt", "3"] and a3.count("--attempt") == 1
    assert w._autostart_relaunch_plan(["lia.py", "--autostart", "--attempt", "3"]) is None
    assert w._autostart_attempt(["lia.py", "--attempt", "x"]) == 0
    # logon detection: explicit flag wins; otherwise the boot-uptime window
    assert w._is_logon_launch(["lia.py", "--autostart"], uptime=99999.0) is True
    assert w._is_logon_launch(["lia.py"], uptime=30.0) is True
    assert w._is_logon_launch(["lia.py"], uptime=3600.0) is False
    assert w._is_logon_launch(["lia.py"], uptime=-1.0) is False
    assert isinstance(w._uptime_seconds(), float)


_test("startup: logon relaunch policy (retries, flags, cap) + logon detection",
      t_startup_relaunch_plan)


def t_startup_trace_and_excepthook():
    """Breadcrumbs + the crash net: _startup_trace appends stage lines and
    never raises; the excepthook records a CRASH line without relaunching a
    manual launch, and is armed ONLY for a real launch (__main__), defined
    before the heavy imports - so a failure at module level before logging
    exists is no longer invisible (the 2026-09-01 "didn't start at logon"
    report) while an importing script/test never pollutes the breadcrumbs.
    The worker-thread counterpart (_thread_excepthook) logs a THREAD-CRASH."""
    import lia as w
    tmp = tempfile.mkdtemp()
    orig, orig_argv, orig_err = w._STARTUP_TRACE, sys.argv, sys.stderr
    w._STARTUP_TRACE = os.path.join(tmp, "sub", "startup_trace.log")
    try:
        w._startup_trace("launch", "x y")
        w._startup_trace("main")
        sys.argv = ["lia.py"]          # a manual launch: must not relaunch
        sys.stderr = io.StringIO()     # swallow the default traceback print
        try:
            raise ValueError("boom at import")
        except ValueError as e:
            w._startup_excepthook(type(e), e, e.__traceback__)
        with open(w._STARTUP_TRACE, encoding="utf-8") as f:
            txt = f.read()
        assert "launch x y" in txt and " main" in txt and "CRASH" in txt, txt
        assert "boom at import" in txt and "relaunch" not in txt, txt
        # Armed only for a REAL launch: importing lia.py - as this suite and
        # any diagnostic script do - must not install the crash nets.
        import threading as _th, types as _types
        assert sys.excepthook is not w._startup_excepthook
        assert _th.excepthook is not w._thread_excepthook
        # the worker-thread net records a THREAD-CRASH breadcrumb + traceback
        try:
            raise ValueError("thread boom")
        except ValueError as e:
            w._thread_excepthook(_types.SimpleNamespace(
                exc_type=type(e), exc_value=e, exc_traceback=e.__traceback__,
                thread=_types.SimpleNamespace(name="worker-7")))
        with open(w._STARTUP_TRACE, encoding="utf-8") as f:
            txt = f.read()
        assert "THREAD-CRASH worker-7" in txt and "thread boom" in txt, txt
    finally:
        w._STARTUP_TRACE, sys.argv, sys.stderr = orig, orig_argv, orig_err
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lia.py"),
               encoding="utf-8").read()
    gate = src.index('_ARM_CRASH_NET = (__name__ == "__main__")')
    assert gate < src.index("sys.excepthook = _startup_excepthook") < src.index("import numpy")
    assert src.index("threading.excepthook = _thread_excepthook") < src.index("import numpy")
    # the tray app marks its milestones and the logon launch waits for the shell
    assert '_startup_trace("main")' in src and '_startup_trace("tray-ready")' in src
    assert "_shell_tray_present()" in src.split('if __name__ == "__main__":')[1]


_test("startup: breadcrumb file + crash excepthook (before the heavy imports)",
      t_startup_trace_and_excepthook)


def t_hook_probe_watchdog_and_restart_notice():
    """The keyboard-hook liveness probe (2026-09-03, "Lia crashed after the
    reboot" = a dead WH_KEYBOARD_LL hook + the reactive self-restart): the
    probe helper's tri-state, _verify_keyboard_hook's decision matrix (skip
    while blocked, alive, transient stall, dead -> ONE restart with the probe
    reason, then rate-limited, disabled when the observer failed), the reason
    riding the relaunch argv, the notice the fresh instance shows for it, and
    the logon relaunch plan dropping a stale reason."""
    import types, threading as _th, subprocess as _sp
    import lia as w
    App = w.LiaApp
    # --- probe helper: None when nothing could be injected, True on echo,
    # False when the hook stays silent
    seen = _th.Event()
    assert w._probe_keyboard_hook(seen, wait_s=0.05, inject=lambda: False) is None
    assert w._probe_keyboard_hook(seen, wait_s=0.05,
                                  inject=lambda: (seen.set() or True)) is True
    assert w._probe_keyboard_hook(seen, wait_s=0.05, inject=lambda: True) is False
    # --- decision matrix on a stub app
    app = App.__new__(App)
    app._hook_probe_seen = _th.Event()
    app._hook_probe_enabled = True
    app.model_loaded = True
    app.is_recording = False
    app._is_meeting_active = lambda: False
    app._compose_active = app._compose_instr_active = False
    app._voice_ask_active = app._hotkey_capture_active = False
    restarts = []
    app._restart_app = lambda reason="manual": restarts.append(reason)
    orig_probe, orig_idle = w._probe_keyboard_hook, w.get_system_idle_seconds
    results = []
    try:
        w.get_system_idle_seconds = lambda: 5.0
        w._probe_keyboard_hook = lambda seen, wait_s=0.5, inject=None: results.pop(0)
        app.is_recording = True                      # blocked -> nothing probed
        results[:] = [False, False]
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is None
        assert results == [False, False]
        app.is_recording = False
        w.get_system_idle_seconds = lambda: 0.2      # user mid-typing
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is None
        w.get_system_idle_seconds = lambda: 600.0    # long idle: leave the timers alone
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is None
        w.get_system_idle_seconds = lambda: 5.0
        results[:] = [True]                          # alive
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is True and not restarts
        results[:] = [False, True]                   # transient stall -> alive
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is True and not restarts
        results[:] = [None]                          # could not inject -> unknown
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is None and not restarts
        results[:] = [False, False]                  # dead twice -> ONE restart
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is False
        assert restarts == ["dead-hotkey-hook-probe"], restarts
        results[:] = [False, False]                  # dead again <120s -> rate-limited
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is False
        assert restarts == ["dead-hotkey-hook-probe"], restarts
        app._hook_probe_enabled = False              # observer failed -> never probes
        results[:] = [False, False]
        assert App._verify_keyboard_hook(app, recheck_delay_s=0) is None
        assert results == [False, False]
    finally:
        w._probe_keyboard_hook, w.get_system_idle_seconds = orig_probe, orig_idle
    # --- the reason rides the relaunch argv ...
    launched = []

    class _FakePopen:
        def __init__(self, cmd, **kw):
            launched.append(list(cmd))
    app2 = App.__new__(App)
    app2.overlay = types.SimpleNamespace(show=lambda *a, **k: None)
    app2._quit = lambda: None
    orig_target, orig_popen, orig_sleep = w._get_startup_target, _sp.Popen, w.time.sleep
    try:
        w._get_startup_target = lambda: ("C:\\x\\Lia.exe", '"C:\\x\\lia.py"', "C:\\x", None)
        _sp.Popen = _FakePopen
        w.time.sleep = lambda s: None
        App._restart_app(app2, reason="dead-hotkey-hook-probe")
    finally:
        w._get_startup_target, _sp.Popen, w.time.sleep = orig_target, orig_popen, orig_sleep
    assert launched and launched[0][-2:] == [
        "--restarted", "--restart-reason=dead-hotkey-hook-probe"], launched
    # ... is read back by the fresh instance; only FELT reasons have a notice
    assert w._restart_reason_from_argv(
        ["lia.py", "--restarted", "--restart-reason=dead-hotkey-hook"]) == "dead-hotkey-hook"
    assert w._restart_reason_from_argv(["lia.py"]) == ""
    for r in ("dead-hotkey-hook", "dead-hotkey-hook-probe"):
        title, msg = w._RESTART_NOTICES[r]
        assert title and "hook" in msg.lower(), r
    assert "manual" not in w._RESTART_NOTICES
    # the fresh instance shows the notice once its icon is up (onboarding path)
    shown = []
    icon = types.SimpleNamespace(notify=lambda msg, title: shown.append((title, msg)))
    orig_argv = sys.argv
    try:
        sys.argv = ["lia.py", "--restarted", "--restart-reason=dead-hotkey-hook"]
        App._tray_first_run_onboarding(types.SimpleNamespace(config={
            "_first_run_welcome_shown": True, "_tray_icon_promoted": True}), icon)
    finally:
        sys.argv = orig_argv
    assert shown and shown[0][0] == "Lia restarted itself", shown
    # a logon relaunch chain must not carry a stale reason
    _d, argv2 = w._autostart_relaunch_plan(
        ["lia.py", "--restart-reason=dead-hotkey-hook", "--autostart"])
    assert not any(a.startswith("--restart-reason=") for a in argv2), argv2


_test("hotkey: OS-hook liveness probe + proactive restart + restart notice",
      t_hook_probe_watchdog_and_restart_notice)


def t_startup_target_autostart_flag():
    """Every logon mechanism launches with --autostart (the Run value, the
    elevated task and the .lnk fallback all build from _get_startup_target),
    and a stale Run value is recognised: same exe without the flag ->
    rewrite; a moved (missing) target -> rewrite; another install's live
    exe -> leave it alone; identical -> nothing."""
    import lia as w
    _t, args, _wd, _ico = w._get_startup_target()
    assert args.endswith("--autostart"), args
    cmd = w._autostart_cmdline()
    assert cmd.startswith('"') and cmd.endswith(" --autostart"), cmd
    exe = w._cmdline_exe(cmd)
    assert exe and os.path.isabs(exe) and '"' not in exe, exe
    assert w._cmdline_exe('bare.exe "x"') == "bare.exe"
    assert w._cmdline_exe("") == ""
    stale = w._autostart_runkey_stale
    assert stale(cmd, cmd) is False
    assert stale(cmd.upper(), cmd) is False               # case-insensitive
    assert stale('"%s" "old.py"' % exe, cmd, exists=lambda p: True) is True
    assert stale(r'"C:\gone\Lia.exe" "C:\gone\app\lia.py"', cmd,
                 exists=lambda p: False) is True
    assert stale(r'"C:\other\runtime\Lia.exe" "C:\other\app\lia.py" --autostart',
                 cmd, exists=lambda p: True) is False
    assert stale("", cmd, exists=lambda p: False) is True
    # the installer's Run value carries the flag too
    iss = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer.iss"),
               encoding="utf-8").read()
    assert 'lia.py"" --autostart"' in iss, "installer Run value lacks --autostart"
    # run() performs the refresh + legacy migration off the main thread
    import inspect
    assert "_autostart_maintenance" in inspect.getsource(w.LiaApp.run)
    assert "_refresh_autostart_runkey" in inspect.getsource(w.LiaApp._autostart_maintenance)


_test("startup: --autostart on every logon mechanism + stale Run-value policy",
      t_startup_target_autostart_flag)


def t_launcher_identity_portable_pythonw():
    """A locked-PC portable launch ('Lia (Work PC).bat' -> the code-signed
    runtime\\pythonw.exe) is a Lia-owned identity: _ensure_lia_launcher keeps
    it (no unsigned app\\Lia.exe copy that WDAC would block at logon), the
    startup target uses it, and the tray-promote gate accepts it. A shared
    interpreter is still rejected."""
    import inspect
    import lia as w
    root = tempfile.mkdtemp()
    rt = os.path.join(root, "runtime")
    app = os.path.join(root, "app")
    os.makedirs(rt)
    os.makedirs(app)
    pyw = os.path.join(rt, "pythonw.exe")
    open(pyw, "wb").close()
    open(os.path.join(app, "lia.py"), "w").close()
    assert w._is_portable_runtime_exe(pyw) is True
    assert w._lia_owned_executable(pyw) is True
    assert w._is_portable_runtime_exe(os.path.join(root, "pythonw.exe")) is False
    shared = os.path.join(tempfile.mkdtemp(), "pythonw.exe")
    assert w._lia_owned_executable(shared) is False
    assert w._lia_owned_executable(r"C:\x\runtime\Lia.exe") is True   # by name
    orig_exe, orig_argv = sys.executable, sys.argv
    try:
        sys.executable, sys.argv = pyw, [os.path.join(app, "lia.py")]
        assert w._ensure_lia_launcher() == pyw
        assert not os.path.exists(os.path.join(app, "Lia.exe")), \
            "must not mint an unsigned copy next to the signed runtime"
        t, args, wd, _i = w._get_startup_target()
        assert t == pyw and args.endswith("--autostart") and wd == app
    finally:
        sys.executable, sys.argv = orig_exe, orig_argv
    assert "_is_portable_runtime_exe" in inspect.getsource(w._ensure_lia_launcher)


_test("startup: signed portable pythonw is a Lia-owned launcher + tray identity",
      t_launcher_identity_portable_pythonw)


def t_autostart_relaunch_spawn():
    """The relaunch itself (sleep + Popen stubbed): a logon launch that dies
    re-spawns THIS executable with the script's absolute path, the explicit
    flags and the attempt counter, detached + windowless, after the planned
    delay - and leaves 'relaunch-scheduled' / 'relaunched' breadcrumbs. A
    manual launch spawns nothing."""
    import subprocess
    import lia as w
    tmp = tempfile.mkdtemp()
    orig = (w._STARTUP_TRACE, sys.argv, time.sleep, subprocess.Popen)
    w._STARTUP_TRACE = os.path.join(tmp, "startup_trace.log")
    slept, spawned = [], []

    class _P:
        def __init__(self, args, **kw):
            spawned.append((args, kw))
    try:
        time.sleep = lambda s: slept.append(s)
        subprocess.Popen = _P
        sys.argv = ["lia.py"]
        assert w._autostart_relaunch("manual") is False and not spawned
        sys.argv = ["lia.py", "--autostart", "--attempt", "1"]
        assert w._autostart_relaunch("uncaught RuntimeError") is True
        assert slept == [30], slept
        args, kw = spawned[0]
        assert args[0] == sys.executable and args[1] == os.path.abspath("lia.py")
        assert args[2:] == ["--autostart", "--restarted", "--attempt", "2"], args
        assert kw.get("close_fds") is True
        if os.name == "nt":
            assert kw.get("creationflags") == (0x00000008 | 0x08000000)
        with open(w._STARTUP_TRACE, encoding="utf-8") as f:
            txt = f.read()
        assert "relaunch-scheduled in 30s after: uncaught RuntimeError" in txt, txt
        assert "relaunched --autostart --restarted --attempt 2" in txt, txt
        # the serve child's logon Run value asks for the same retry explicitly
        import inspect
        assert "--serve --port %d --autostart" in inspect.getsource(
            w.LiaApp._create_serve_task)
    finally:
        w._STARTUP_TRACE, sys.argv, time.sleep, subprocess.Popen = orig


_test("startup: logon relaunch spawns the next attempt (detached, flagged, delayed)",
      t_autostart_relaunch_spawn)


# ============================================================
# Summary
# ============================================================
section("SUMMARY")
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
skipped = sum(1 for r in results if r[0] == SKIP)
total = len(results)
skip_note = f" ({skipped} skipped)" if skipped else ""
print(f"\n{passed}/{total} tests passed{skip_note}")
if failed:
    print(f"\n{failed} FAILED:")
    for status, name, err in results:
        if status == FAIL:
            print(f"  [{status}] {name}")
            print(f"    {err.splitlines()[0]}")
    sys.exit(1)
else:
    print("\n[ALL GREEN]" + (" (live-API tests skipped)" if skipped else ""))
