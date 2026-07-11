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
- Test file: `tests/test_issue_validations.py` (245 tests as of 2026-07-11; all green; ~9-9.5s runtime — the ISSUE-028 stale-monitor test alone takes ~4.5s by design, two deliberate 2s join timeouts)
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
- As of 2026-07-11 (latest): 0 OPEN, 0 NEEDS_REVIEW, 0 FIXED, 0 PARTIAL, 39 VALIDATED (001-039, gaps at old numbers absorbed by renumbering — see below). Next free ID: ISSUE-040.
- Within the VALIDATED group, newly validated entries are APPENDED at the end (025, 031, 016, 011-second-pass, 037, 038, 039, 032, 033, 034, 035, 036 are out of numeric order by convention), so don't "fix" the ordering. When a FIXED/OPEN entry appears anywhere in the file (top OR bottom — seen both), that is correct per the sort rule only until validated; this agent must relocate each entry into the VALIDATED group (append after the last VALIDATED entry) as part of validating it (cut from wherever it sits, paste at the end of the VALIDATED block, change Status, add a Validation section) — do not leave it in place just because appending is more work.
- **Merged-branch renumbering**: on 2026-07-11 a concurrent contributor round (commit ccf7b68, "Engineer_Mack") had independently numbered its own issues 032-036 while this agent's own prior round had already used 032-034 for different bugs. The merge resolved the collision by renumbering the earlier round's issues to 037/038/039 (with an explicit "> Note: originally logged as ISSUE-0XX; renumbered..." blockquote at the top of each Discovery section) and kept Engineer_Mack's 032-036 as-is. If issue numbers ever look duplicated or out of story order, check for this kind of renumbering note before assuming an error.
- **Externally-authored fixes (no test suite access)**: Engineer_Mack's round fixed 032-036 via code inspection only (`### Validation` sections say "Method: Code inspection", each followed by a `> 🔍 Agent Note` explicitly flagging that independent test-suite validation had not been done, host lacked `ctypes.WinDLL`). When validating this kind of entry, ADD a second `### Validation (independent — issue-solution-validator, DATE)` section below the existing one rather than editing/removing the original — same non-destructive principle as re-validating a PARTIAL issue, just for a different reason (a different author's provisional self-check vs. a prior agent run of this same role).
- **Fix-note wording vs. actual code — a recurring but usually benign gap**: ISSUE-032's Fix said "full sentence" but code hard-caps at `sentence[:200]` for every sentence over 200 chars (not just "very long" ones) — cosmetic wording gap, fix still closes the reported bug for realistic sentence lengths. ISSUE-033's Fix claimed "a filter for very short fragments (< 2 chars)" was added for abbreviation false-splits — this was NOT implemented at all (`_split_sentences` only filters empty strings); pre-existing abbreviation-split behavior, not a regression, doesn't block VALIDATED. ISSUE-036's Fix said "class-level `_bookmark_lock`" but it's actually an instance attribute set in `__init__` — functionally equivalent for a single-instance GUI app. Pattern: always diff the Fix section's prose against `inspect.getsource()` output line-by-line, not just check the bug is closed — write a test that pins the ACTUAL behavior (not the described behavior) so future drift is caught, and document the discrepancy honestly in the Validation section without downgrading the verdict unless the gap actually leaves the original bug open.
- **Status field can lag the actual diff**: ISSUE-036 shipped with `**Status**: OPEN` even though its `### Fix` section fully described (and the code fully contained) a working fix — an authoring oversight in the source commit, not a sign of an incomplete fix. Don't trust the Status field alone; always read the Fix section and diff it against the code before deciding whether an "OPEN" issue is actually unfixed.
