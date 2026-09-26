# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(Security tab -> "Report a vulnerability"). You should get a first response
within a few days. Please do not open public issues for security reports.

Only the latest release is supported with fixes.

## What Lia stores, and where

Everything lives under `%APPDATA%\Lia` on your machine unless noted below.

| Data | Where | Kept for |
|---|---|---|
| Meeting audio (WAV) | `%APPDATA%\Lia\meetings` | ~30 days (configurable) |
| Meeting audio (Opus archive) | `%APPDATA%\Lia\meetings` | ~2 years (configurable) |
| Transcripts, summaries, an older summary kept when you re-summarize (`*.bak.html`) | `%APPDATA%\Lia\meetings`, `...\summaries` | until you delete them (opt-in auto-retention: `meeting_transcript_retention_days`) |
| Dictation history | `%APPDATA%\Lia\history.json` | 2 weeks by default (Settings > General) |
| Email / meeting search indexes, voiceprints, vocabulary | `%APPDATA%\Lia\...` | until you delete them |
| Dictation clips for tuning (`debug_clips`, `foreign_script_clips`) | `%APPDATA%\Lia\...` | only when you turn on "Keep dictation clips" (last 100 / 20) |
| Report-a-problem bundles | `%APPDATA%\Lia\diagnostics` | the newest 5 |
| Logs (`lia.log`, `serve.log`, `settings_window.log`, `startup_trace.log`) | `%APPDATA%\Lia` | rotating, a few MB each |
| GPU-sharing state (only if you turn on "Share the GPU with another local app") | `C:\ProgramData\GpuLease` (private to your account) | while in use |

- **Delete everything**: Settings > Advanced > "Delete all my data" wipes the
  folder and quits. The uninstaller also offers deletion (default: keep).
- **Log privacy**: the log records only the SIZE of your content - dictations,
  questions, meeting titles, calendar subjects, attendee and speaker names -
  and meeting file names appear without their title. Full text is logged only
  if you opt in with `log_transcripts: true`.
- **Report a problem**: the bundle (for a public GitHub issue) contains the
  log, your settings with every key masked and personal values redacted, and
  basic system info. It additionally scrubs meeting titles in file names, your
  Windows account name in paths, e-mail and IP addresses. It is created
  locally and never sent anywhere - read it before you attach it.
- **Data at rest**: API keys and tokens are DPAPI-encrypted (below), but the
  transcripts, meeting audio, dictation history, and the email/meeting search
  indexes are stored unencrypted under `%APPDATA%\Lia`. On a shared or corporate
  machine, rely on full-disk encryption (BitLocker) to protect them; anyone with
  file access to your profile can read them.

## API keys

API keys and tokens are encrypted at rest with Windows DPAPI (per-user scope),
in `config.json` and in the short-lived files used to hand keys to Lia's own
child windows. This is fail-closed: if protection is unavailable, saving the
key is refused rather than ever writing it in plaintext. Honest scope: DPAPI
protects against other users on the machine and offline file theft. It does
not protect against malware already running under your own account - nothing
user-mode can. The Settings window never receives a key (it shows a masked
hint), and a key rejected by its provider is reported by a fingerprint only.

## Privilege model

The packaged app runs **non-elevated (asInvoker)** and installs per-user with
no UAC prompt. Hotkeys, recording, and paste work in normal applications
without admin rights. The one capability that requires elevation - dictating
into elevated windows (Task Manager, admin consoles) - is an explicit opt-in:
the installer's "elevated auto-start" task or `run.bat` when running from
source. Be clear about what that opt-in means: Windows has no way to elevate
just the paste, so in elevated mode the **entire Lia process runs with highest
privileges for the whole session**, not only the moment of dictation.

Guards for the elevated mode:
- The elevated auto-start task is offered and created only for an install
  under Program Files (only administrators can change those files). The check
  uses the Program Files location from the system registry and the folder's
  real permissions, not environment variables. A task found pointing at a
  folder other accounts can modify is removed at startup.
- An elevated Lia never runs the update installer with its own token (it is
  started at normal integrity), never looks up helper tools on `PATH`, and
  never adds a per-user folder to the DLL search path.

## Network posture

- **Local by default**: local Whisper / Parakeet models, and a local Ollama
  (`ollama_base_url`, loopback only - Lia refuses any other address) for Ask
  your meetings, Email search, Chat and Voice Ask. Cloud backends
  (Groq / OpenAI / Gemini) run only if you add a key and select them.
- **Busy GPU**: dictation stays local when the GPU is busy. Sending it to a
  cloud service at such times is an opt-in (Settings > Models > "When the GPU
  is busy"), and a meeting summary likewise.
- **Update check**: Lia asks `api.github.com` for the latest release at launch
  and every 12 hours (the request carries Lia's version, nothing else). Turn it
  off in Settings > Advanced > About. An update is installed only after you
  click Update (see Supply chain for how it is verified).
- **Key check**: at startup Lia makes one authenticated request to each cloud
  provider you configured, to tell you early when a key was rejected.
- Self-hosted server mode (client side): plaintext `ws://` is accepted only
  toward private addresses (loopback, RFC1918, the 100.64.0.0/10 mesh-VPN range,
  `.local` / `.ts.net`). A `ws://` URL to a public host is refused unless you
  explicitly set `remote_allow_insecure_ws: true`; use `wss://` through a tunnel
  instead (see `docs/SELF_HOSTED_SERVER.md`). The saved access token is sent
  only to the server it was saved for, and your private vocabulary is sent to
  a server only if you opt in (`remote_send_vocabulary`). Transcribed text from
  a self-hosted server is pasted into the focused window; a compromised server
  could return crafted text, so point the client only at a server you control.
  (Control characters are stripped before paste.)
- Hosting a server (host side): the built-in transcription server binds to your
  Tailscale address (or loopback when Tailscale is off), never to every
  interface implicitly, and always has an access token (one is created when you
  turn the server on; a typed token needs 20+ characters). Connections are
  checked before the WebSocket upgrade: the token, the `Host` header (a
  DNS-rebinding web page is refused), the `Origin`, and per-server / per-address
  connection limits. Never port-forward the server to the public internet;
  reach it over Tailscale (or a `wss://` reverse proxy).

## Content from meetings and e-mail

Meeting speech and e-mail are untrusted input. Every window escapes it before
display and runs under a Content-Security-Policy that blocks all network access
(no remote script, style, image, font or connection). Saved summary HTML files
carry a no-script, no-network policy too. Model output never triggers actions:
corrections the models propose from meetings wait in Settings > Vocabulary as
suggestions until you accept them, and a correction is always inserted as
plain text.

## Supply chain

- `lia/requirements.lock` pins the full dependency tree with sha256 hashes and
  is kept `pip-audit`-clean; release builds install from it in a clean
  environment and embed a hash-pinned CPython.
- Local model downloads are pinned to reviewed Hugging Face commits
  (`MODEL_REVISIONS` in `lia/lia.py`), including the Parakeet model (Lia
  fetches the pinned snapshot itself) and the optional pyannote pipeline.
- **Signed updates**: every release's `SHA256SUMS.txt` is signed with the Lia
  release key (Ed25519, `SHA256SUMS.txt.sig`). The in-app updater installs a
  release only when the signature verifies against the public key built into
  Lia, the signed list names the installer's hash, GitHub's recorded digest
  agrees, and the download comes from this repository's release for that
  version. The installer is then held open and re-hashed until it starts.
- Release executables are not Authenticode-signed yet; expect Windows
  SmartScreen to warn on first run. You can check a download by hand against
  `SHA256SUMS.txt`.
