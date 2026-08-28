# Contributing to Lia

Thanks for looking. Lia is a single-developer project that got big; issues and
pull requests are welcome, and so is "this broke for me" with a log excerpt.

## Dev setup

```bash
git clone https://github.com/Danaor/lia.git
cd lia/lia
pip install -r requirements.lock
python lia.py
```

`requirements.lock` is the pinned, hash-verified set (generated with
`pip-compile --generate-hashes --allow-unsafe` and audited with `pip-audit`) -
use it for reproducible installs and for building release artifacts.
`requirements.txt` states the human-readable intent (minimum versions) and is
fine for a quick dev setup. When you bump a dependency, regenerate the lock
from a directory OUTSIDE `lia/` (the folder's `build.py` shadows the `build`
package pip-tools imports) and re-run `pip-audit -r requirements.lock
--disable-pip`.

The app runs as a tray icon, non-elevated by default (least privilege): the
global hotkey and paste work in normal apps without admin rights. The one
thing a non-elevated Lia cannot do is dictate INTO elevated windows (Task
Manager, regedit, admin consoles) - Windows blocks input across integrity
levels. If you need that, launch through `run.bat` (which elevates, or runs
the registered scheduled task without a UAC prompt) - and note that this runs
the ENTIRE app with highest privileges for the session, not just the paste.
A plain `python lia.py` is the normal dev loop.

Always pass `-X utf8` when you run anything that prints Hebrew, or Windows
will crash the process on a cp1252 encode.

## Running the tests

```bash
cd lia
set LIA_SKIP_LIVE=1
python -X utf8 run_tests.py
```

- `LIA_SKIP_LIVE=1` skips the tests that call paid cloud APIs. Without it,
  those run against whatever keys are in your config.
- Point `APPDATA` at a fresh temporary directory for each run. The suite
  writes real store files (vocabulary, voiceprints, indexes), and a reused
  directory makes later runs fail on leftover state.

Expected: 126 passed, 13 skipped.

## Layout

Everything lives in `lia/`:

| File | What it is |
|---|---|
| `lia.py` | The app: tray, hotkeys, recording, transcription backends, meetings, summaries. Monolithic by design. |
| `lang_pack.py` | Bilingual marker table, language detection, and the English mirror prompts. |
| `ui_kit.py` | The shared pywebview design system (CSS tokens, components, subprocess plumbing). |
| `settings_window.py` | The Settings window (a 9-page pywebview app, its own subprocess). |
| `meetings_index.py` / `meetings_search.py` | Local RAG over your meetings. |
| `action_items.py`, `chat_window.py`, `history_window.py`, `summarize_window.py`, `compose_editor.py` | The other windows, each its own subprocess. |
| `email_index.py`, `email_search.py`, `emailsearch_indexer.py` | The experimental Outlook email index. |
| `diarize_local.py`, `speaker_profiles.py` | Local diarization and cross-meeting voiceprints. |
| `vocab_learn.py`, `summary_dedup.py` | Vocabulary learning and summary cleanup. |
| `generate_logo.py` | Derives the brand assets (`lia_logo.png`, `lia.ico`) from the `lia.png` artwork. |
| `run_tests.py` | The test suite. |

## Things worth knowing before you change code

- **Prompts are pinned.** The Hebrew and English summary prompts are covered by
  SHA assertions in the suite. If you intend to change one, update the pin in
  the same commit - the failure is the guard working.
- **Generation is language-selected, parsing is language-union.** Every writer
  picks one language from config; every parser matches Hebrew *and* English
  markers, always. That is what lets old and new meeting files share one
  archive, index, and task tracker.
- **UI doctrine: English chrome, `dir="auto"` content.** Window shells are
  English and left-to-right; anything the user or a model produced gets
  `dir="auto"` so Hebrew still renders right-to-left inside it.
- **pywebview windows: never touch `window.pywebview.api` at parse time.** It
  is undefined then, and the throw kills the whole inline script, leaving a
  window that looks alive but does nothing. Assign it inside the
  `pywebviewready` handler.
- `vocab_learn.py` and `summary_dedup.py` are kept byte-synced with another
  project. Prefer additive changes there.

## Pull requests

Small and focused beats large and sweeping. Please run the suite before
opening one, and say in the description what you verified by actually running
(the app is full of Windows-specific paths that no unit test reaches).
