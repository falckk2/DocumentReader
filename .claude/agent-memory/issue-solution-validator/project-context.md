---
name: project-context
description: DocumentReader project stack, key files, test conventions, and known infrastructure facts
metadata:
  type: project
---

## Stack
- Python 3.14, Windows 11
- GUI: `customtkinter` (cannot be imported headlessly — avoid importing `src.app` in tests without stubs)
- PDF: `PyMuPDF` (fitz) — patchable with `unittest.mock.patch("fitz.open")`
- Online TTS: `edge-tts` (async)
- Offline TTS: `pyttsx3` (Windows SAPI5 COM)
- Audio: Windows MCI via `ctypes.WinDLL("winmm")` — must be stubbed in tests with `patch("ctypes.WinDLL")`

## Test Infrastructure
- Test runner: `python -m unittest` (pytest not installed)
- Test file: `tests/test_issue_validations.py` (70 tests as of 2026-06-02)
- Tests use code inspection (`inspect.getsource`) as the primary mechanism — no live GUI, MCI, or TTS
- `ctypes.WinDLL` is patched at module level in the test file before `src.audio_player` is imported
- `src.tts_engine` is NOT imported at test module level — tests that use `sys.modules["src.tts_engine"]` must either do an explicit `import src.tts_engine` or call a setUp that does (see ISSUE-002 note)

## Known Test Infrastructure Defect (2026-06-02)
Tests `test_cleanup_tmp_acquires_lock` and `test_cleanup_runs_after_player_stop` (ISSUE-002 class) access `sys.modules["src.tts_engine"]` without an explicit import, and run alphabetically before `test_delete_tmp_acquires_lock` which does the first explicit import. This causes `KeyError: 'src.tts_engine'` on both tests. The fix is confirmed correct by manual inspection — these are test bugs, not code bugs.

**Why:** The test class `TestIssue002TmpFileLock` has no `setUp` that imports `src.tts_engine`. The `_make_engine()` helper uses `patch("src.tts_engine.TTSEngine._pyttsx3_worker")` which populates `sys.modules`, but `_make_engine()` is not called from `setUp()`.

## Key Files
- `main.py` — entry point, logging setup
- `src/app.py` — `DocumentReaderApp` GUI class
- `src/pdf_reader.py` — `PDFReader` class
- `src/voice_manager.py` — `VoiceManager`, `Voice` dataclass
- `src/tts_engine.py` — `TTSEngine` (routes to edge-tts or pyttsx3 worker)
- `src/audio_player.py` — `AudioPlayer` MCI wrapper; module-level `_mci_worker` thread

## Issues File
- `issues.md` in project root — tracks all issues found by `bug-detective`, fixed by `issue-fixer`, validated by this agent
