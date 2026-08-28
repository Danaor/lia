# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(Security tab -> "Report a vulnerability"). You should get a first response
within a few days. Please do not open public issues for security reports.

Only the latest release is supported with fixes.

## What Lia stores, and where

Everything lives under `%APPDATA%\Lia` on your machine. Nothing is uploaded
anywhere unless you configure a cloud backend or API key.

| Data | Where | Kept for |
|---|---|---|
| Meeting audio (WAV) | `%APPDATA%\Lia\meetings` | ~30 days (configurable) |
| Meeting audio (Opus archive) | `%APPDATA%\Lia\meetings` | ~2 years (configurable) |
| Transcripts + summaries | `%APPDATA%\Lia\meetings`, `...\summaries` | until you delete them (opt-in auto-retention: `meeting_transcript_retention_days`) |
| Dictation history | `%APPDATA%\Lia\history.json` | last 1000 entries |
| Email/meeting search indexes, voiceprints, vocabulary | `%APPDATA%\Lia\...` | until you delete them |
| Log | `%APPDATA%\Lia\lia.log` | rotating, ~8 MB max total |

- **Delete everything**: Settings -> Advanced -> "Delete all my data" wipes the
  folder and quits (running from source; the packaged exe has no Settings
  window - there, use the uninstaller's delete prompt or delete
  `%APPDATA%\Lia` yourself). The uninstaller offers deletion (default: keep).
- **Log privacy**: the log records only the SIZE of user content (dictations,
  questions, detected meeting titles). Full text is logged only if you opt in
  with `log_transcripts: true`.

## API keys

API keys and tokens are encrypted at rest with Windows DPAPI (per-user scope),
in `config.json` and in the short-lived files used to hand keys to Lia's own
child windows. This is fail-closed: if protection is unavailable, saving the
key is refused (and cloud features that would write one to disk are disabled)
rather than ever writing it in plaintext. Honest scope: DPAPI protects against
other users on the machine and offline file theft. It does not protect against
malware already running under your own account - nothing user-mode can.

## Privilege model

The packaged app runs **non-elevated (asInvoker)** and installs per-user with
no UAC prompt. Hotkeys, recording, and paste work in normal applications
without admin rights. The one capability that requires elevation - dictating
into elevated windows (Task Manager, admin consoles) - is an explicit opt-in:
the installer's "elevated auto-start" task (offered only on an elevated
install) or `run.bat` when running from source. Be clear about what that
opt-in means: Windows has no way to elevate just the paste, so in elevated
mode the **entire Lia process runs with highest privileges for the whole
session**, not only the moment of dictation.

## Network posture

- Fully local by default: local Whisper/Parakeet models, local Ollama for
  summaries and RAG. Cloud backends (Groq / OpenAI / Gemini / AssemblyAI) run
  only if you add a key and select them.
- Self-hosted server mode: plaintext `ws://` is accepted only toward private
  addresses (loopback, RFC1918, the 100.64.0.0/10 mesh-VPN range, `.local` /
  `.ts.net`). A `ws://` URL to a public host is refused unless you explicitly
  set `remote_allow_insecure_ws: true`; use `wss://` through a tunnel instead
  (see `docs/SELF_HOSTED_SERVER.md`).

## Supply chain

- `lia/requirements.lock` pins the full dependency tree with sha256 hashes and
  is kept `pip-audit`-clean; release builds install from it in a clean
  environment.
- Local model downloads are pinned to reviewed Hugging Face commits
  (`MODEL_REVISIONS` in `lia/lia.py`). Known gap: the Parakeet English model
  is fetched by `onnx-asr`, which exposes no revision parameter - it pulls
  fixed filenames from `istupakov/parakeet-tdt-0.6b-v2-onnx`.
- The self-hosted server guide pins the WhisperLive Docker image by digest.
- Release artifacts are currently unsigned; each release lists SHA256
  checksums. Expect Windows SmartScreen to warn on first run.
