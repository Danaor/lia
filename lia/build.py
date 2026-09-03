"""Build Lia into a standalone .exe using PyInstaller.

KEYLESS LOCAL build. Bundles the local faster-whisper (ivrit.ai Hebrew)
transcription stack so the app works out of the box with NO API keys:
press-to-talk Hebrew dictation + plain local meeting transcripts. The
~1.5GB Hebrew model is NOT bundled — it downloads once from HuggingFace on
the first dictation (no key needed). Pick "Bundle in the installer" in the
packaging flow only if you need a fully offline installer.

Deliberately EXCLUDED (kept small + they don't work in a frozen onefile):
  - torch / pyannote — LOCAL SPEAKER DIARIZATION runs as a separate Python
    subprocess (diarize_local.py) via find_python_interpreter(), which returns
    None in a frozen exe. So diarization is unavailable in the packaged build;
    GPU-less colleagues default to plain local transcripts anyway. (Run from
    source with run.bat to keep diarization + the editor / email / chat windows,
    which are all subprocess-based.)
  - transformers / openvino — unused by the faster-whisper path.

INCLUDED: ctranslate2 + faster_whisper + onnxruntime (VAD) + av (mp4 decode)
+ tokenizers + huggingface_hub (first-run model download) + the cloud/audio/UI
deps. ctranslate2 falls back to CPU when no CUDA GPU is present.

Rebuild with `python build.py`.
"""
import PyInstaller.__main__
import os

HERE = os.path.dirname(os.path.abspath(__file__))

VERSION = "1.3.1"
_v = tuple(int(x) for x in VERSION.split(".")) + (0,)


def _write_version_file():
    """Windows version resource, so the exe's Properties tab (and SmartScreen's
    publisher line) show a real name and version instead of blanks. Written
    next to the spec and passed to PyInstaller as --version-file."""
    path = os.path.join(HERE, "version_info.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "VSVersionInfo(\n"
            "  ffi=FixedFileInfo(filevers=%(v)r, prodvers=%(v)r, mask=0x3f,\n"
            "                    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,\n"
            "                    date=(0, 0)),\n"
            "  kids=[\n"
            "    StringFileInfo([StringTable('040904B0', [\n"
            "      StringStruct('CompanyName', 'Naor Daniel'),\n"
            "      StringStruct('FileDescription', 'Lia - Local Inference Assistant'),\n"
            "      StringStruct('FileVersion', '%(s)s'),\n"
            "      StringStruct('InternalName', 'Lia'),\n"
            "      StringStruct('LegalCopyright', 'Copyright (c) 2026 Naor Daniel. MIT licensed.'),\n"
            "      StringStruct('OriginalFilename', 'Lia.exe'),\n"
            "      StringStruct('ProductName', 'Lia'),\n"
            "      StringStruct('ProductVersion', '%(s)s')])]),\n"
            "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
            "  ]\n"
            ")\n" % {"v": _v, "s": VERSION})
    return path

# Heavy stacks we deliberately leave OUT (see module docstring).
EXCLUDES = [
    "torch",
    "transformers",
    "pyannote",
    "pyannote.audio",
    "speechbrain",
    "pytorch_lightning",
    "lightning",
    "asteroid_filterbanks",
    "openvino",       # OpenVINOTranscriber path (hidden from UI)
    "openvino_genai",
]

# Native / data-heavy packages PyInstaller must collect fully (DLLs + data).
COLLECT_ALL = [
    "ctranslate2",
    "faster_whisper",
    "av",
]

args = [
    os.path.join(HERE, "lia.py"),
    "--onefile",
    "--noconsole",
    "--name", "Lia",
    "--icon", os.path.join(HERE, "lia.ico"),
    "--version-file", _write_version_file(),
    # Ship the brand icon next to the exe (used for app windows / dialogs).
    "--add-data", os.path.join(HERE, "lia.ico") + os.pathsep + ".",
    # Ship the tray status-icon PNGs (loaded by _create_icon at runtime).
    "--add-data", os.path.join(HERE, "tray_icons") + os.pathsep + "tray_icons",
    # Cloud + audio + UI + local-stack deps PyInstaller's static analysis can miss.
    "--hidden-import", "pyaudio",
    "--hidden-import", "pyaudiowpatch",
    "--hidden-import", "keyboard",
    "--hidden-import", "pyperclip",
    "--hidden-import", "pyautogui",
    "--hidden-import", "pystray",
    "--hidden-import", "PIL",
    "--hidden-import", "PIL._tkinter_finder",
    "--hidden-import", "websocket",   # Real-Time mode (OpenAI WebSocket)
    "--hidden-import", "numpy",
    "--hidden-import", "requests",
    "--hidden-import", "onnxruntime",       # faster-whisper VAD
    "--hidden-import", "tokenizers",
    "--hidden-import", "huggingface_hub",   # first-run model download
    # LEAST PRIVILEGE (2026-08-28 audit): the exe runs asInvoker. Global
    # hotkeys and paste work in normal apps without elevation; only dictation
    # INTO elevated windows (Task Manager, admin consoles) needs the opt-in
    # elevated mode (run.bat / the installer's elevated autostart task).
    "--clean",
    "--distpath", os.path.join(HERE, "dist"),
    "--workpath", os.path.join(HERE, "build"),
    "--specpath", HERE,
]
for _m in EXCLUDES:
    args += ["--exclude-module", _m]
for _c in COLLECT_ALL:
    args += ["--collect-all", _c]

PyInstaller.__main__.run(args)

print("\nBuild complete (keyless local)! EXE: Lia/dist/Lia.exe")
