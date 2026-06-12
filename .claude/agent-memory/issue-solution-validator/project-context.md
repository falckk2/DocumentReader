---
name: project-context
description: DocumentReader project stack, key files, test conventions, and known infrastructure facts
metadata:
  type: project
---

## Stack
- Python 3.14, Windows 11
- GUI: `customtkinter` (cannot be imported headlessly — avoid importing `src.app` in tests without stubs; note: importing `src.app` for `inspect.getsource` works fine in this environment, just never instantiate the app)
- PDF: `PyMuPDF` (fitz) — patchable with `unittest.mock.patch("fitz.open")`
- Online TTS: `edge-tts` (async)
- Offline TTS: `pyttsx3` (Windows SAPI5 COM)
- Audio: Windows MCI via `ctypes.WinDLL("winmm")` — must be stubbed in tests with `patch("ctypes.WinDLL")`

## Test Infrastructure
- Test runner: `python -m unittest` (pytest not installed)
- Test file: `tests/test_issue_validations.py` (138 tests as of 2026-06-12 night; all green; ~6s runtime — the ISSUE-028 stale-monitor test alone takes ~4.5s by design, two deliberate 2s join timeouts)
- Tests mix code inspection (`inspect.getsource`) with behavioral tests built on `Class.__new__` + stubbed attributes — no live GUI, MCI, or TTS
- `ctypes.WinDLL` is patched at module level in the test file before `src.audio_player` is imported
- Behavioral AudioPlayer tests: patch module-level `ap._mci = MagicMock(return_value=0)` and `ap._mci_query = MagicMock(return_value="stopped")`, then `player.play(...)` runs the real monitor loop which exits quickly on "stopped"
- Behavioral TTSEngine tests: `_make_tts_engine()` helper in the test file builds via `TTSEngine.__new__` with `_player=MagicMock()`, real locks/events/queue, and no worker thread — then `_speak_online`/`_speak_offline` can be exercised directly (override `_edge_synthesize` with an async stub gated on a `threading.Event`)
- Behavioral app-method tests: `DocumentReaderApp.__new__(DocumentReaderApp)` + set only the attributes the method touches; patch `app_mod.mb.askyesno` and `_load_bookmarks`/`_BOOKMARKS_FILE` as needed

## Former Test Infrastructure Defect — FIXED 2026-06-12
The two `KeyError: 'src.tts_engine'` errors in `TestIssue002TmpFileLock` were fixed by adding a `setUp` that does `import src.tts_engine  # noqa: F401`. Suite has been 0-failure/0-error since.

## Key Files
- `main.py` — entry point, logging setup; wires `app.protocol("WM_DELETE_WINDOW", app.on_close)` (line ~37)
- `src/app.py` — `DocumentReaderApp` GUI class
- `src/pdf_reader.py` — `PDFReader` class
- `src/voice_manager.py` — `VoiceManager`, `Voice` dataclass
- `src/tts_engine.py` — `TTSEngine` (generation-token cancellation `_generation`/`_gen_lock`; pyttsx3 worker; edge-tts with 30s `asyncio.wait_for`)
- `src/audio_player.py` — `AudioPlayer` MCI wrapper; module-level `_mci_worker` dispatcher thread with 5s caller timeouts; monitor fires on_done on a detached "on-done-dispatch" thread

## Issues File
- `issues.md` in project root — tracks all issues found by `bug-detective`, fixed by `issue-fixer`, validated by this agent
- Sort rule (header line): OPEN → NEEDS_REVIEW → FIXED → PARTIAL → VALIDATED; entries are separated by lines that are exactly `---`, so the file can be safely re-sorted by splitting on `\n---\n` (verified: no section body contains a bare `---` line)
- As of 2026-06-12 (night): 0 OPEN, 1 NEEDS_REVIEW (016), 1 PARTIAL (011), 29 VALIDATED (001-010, 012-015, 017-031). Next free ID: ISSUE-032.
