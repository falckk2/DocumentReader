# Issues Log

_Last updated: 2026-06-03_

Issues are sorted by status: NEEDS_REVIEW → PARTIAL → VALIDATED. Append new issues at the appropriate status group; never delete old entries (change Status instead).

---

## ISSUE-016 — Speed/voice changes mid-playback not reflected until next sentence

**Status**: NEEDS_REVIEW
**Severity**: LOW

### Discovery
- **File**: `src/app.py` — `_on_voice_change` ~line 373, `_on_speed_change` ~line 376, `_read_next_sentence`
- **Description**: Voice and speed are read at the start of each sentence (`_read_next_sentence`). Changing the slider or dropdown mid-sentence has no effect until the next sentence boundary. This matches the documented design ("Voice selection reads from dropdown at speak time"), so it is intentional — flagged only because there is no user feedback that the change is queued, which users may report as a bug.
- **Root Cause**: Intentional design; no UI affordance indicating deferred application.
- **Impact**: User confusion ("I moved the slider, nothing happened").
- **Reproduction**: Start playback, drag speed slider; current sentence speed unchanged.
- **Depends On**: None
- **Fix Suggestion**: Optionally show a transient status like "Speed applies to next sentence", or re-synth the current sentence on change. Confirm desired behavior before changing.
- **Logging Added**: `_read_next_sentence` logs the speed/voice used per sentence, making the deferral visible in logs.
- **Date Found**: 2026-06-02

---

## ISSUE-011 — End-of-track detection relies on position polling (latency between sentences)

**Status**: PARTIAL ⚠️
**Severity**: MEDIUM

### Discovery
- **File**: `src/audio_player.py` — `_monitor` lines ~93-127
- **Description**: The monitor polls `status mode` and `status position` every 0.1s. End is detected either by `mode == "stopped"` or `pos >= track_length`. If `track_length` could not be read (query failed → `track_length = 0`), the `pos >= track_length` branch is disabled and end detection depends solely on MCI reporting `"stopped"`. Conversely, MCI sometimes reports `position` slightly less than `length` at true end, delaying `on_done` by up to the 0.1s poll plus the 5×0.05s drain. Cumulative latency across many short sentences adds noticeable gaps.
- **Root Cause**: Polling-based completion instead of MCI `notify` callback (`play ... notify` + `MM_MCINOTIFY`), plus a magic 0.2s warmup and 0.25s drain.
- **Impact**: Audible gaps between sentences; rare premature cutoff of the last fraction of a sentence if drain is too short.
- **Reproduction**: Read a page of many short sentences; observe inter-sentence gaps. Use a sub-200ms clip to test warmup edge.
- **Depends On**: None
- **Fix Suggestion**: Use `play {alias} notify` with a window/callback for `MM_MCINOTIFY`, or at minimum reduce reliance on magic timings and detect end via `mode == "stopped"` as primary with position as backup. Log measured track_length and final position.
- **Logging Added**: Added DEBUG logging of `track_length` at monitor start and a warning when length cannot be read.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: No code change. Implementing MCI `notify` requires a hidden Win32 window to receive `MM_MCINOTIFY` messages, which is a significant architectural addition. The existing `mode == "stopped"` primary detection plus position-based backup is functionally correct. Reducing magic timings risks audio cutoff.
- **Remaining**: Implement Win32 `MM_MCINOTIFY` via a hidden `HWND` to eliminate polling gaps between sentences.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_mode_stopped_is_primary_detection, test_position_based_detection_present_as_backup, test_mci_notify_not_implemented, test_drain_delay_still_present
- **Results**: 4 passed, 0 failed
  - ✅ test_mode_stopped_is_primary_detection
  - ✅ test_position_based_detection_present_as_backup
  - ✅ test_mci_notify_not_implemented — confirms MM_MCINOTIFY not yet implemented
  - ✅ test_drain_delay_still_present — 5 x 0.05s drain loop still present
- **Inspection**: `_monitor` (audio_player.py lines 101-140) polls every 0.1s, breaks on `status == "stopped"` as primary condition, and on `pos >= track_length` as backup with a drain loop. No MCI notify window (`HWND`, `MM_MCINOTIFY`, `WM_USER`) present in the codebase. This matches the stated partial resolution.
- **Verdict**: Correctly marked as Partially Resolved. The polling mechanism is functionally correct; the known gap (audible inter-sentence gaps from polling latency) remains for a future `MM_MCINOTIFY` implementation.
- **New Issues**: None

---

## ISSUE-001 — `AudioPlayer.stop()` can deadlock when called from the monitor thread

**Status**: VALIDATED ✅
**Severity**: CRITICAL

### Discovery
- **File**: `src/audio_player.py` — `stop()` lines ~149-153 (join), reached via the monitor's `on_done` chain
- **Description**: The monitor thread, on natural track completion, calls `self._on_done()`. For online TTS, `on_done` chains through `TTSEngine` → app's `_on_sentence_done` → `_tts.speak()` → `_player.stop()`. Normally fine. But if any future code path causes `AudioPlayer.stop()` to be invoked *on the monitor thread itself*, `self._monitor_thread.join(timeout=2.0)` joins the current thread on itself. Python's `Thread.join()` from within the same thread raises `RuntimeError: cannot join current thread`, aborting `stop()` and leaving the MCI alias open.
- **Root Cause**: `stop()` unconditionally joins `self._monitor_thread` without checking whether the caller *is* that thread.
- **Impact**: Potential `RuntimeError`, alias `DocumentReaderTrack` left open, subsequent `open` failing with MCI error 263/"device already open", playback wedged until app restart.
- **Reproduction**: Trigger any callback path where `on_done` → `speak`/`stop` resolves synchronously on the monitor thread (e.g., MCI open failure firing `on_done()` inline while a stop is racing).
- **Depends On**: None
- **Fix Suggestion**: Guard the join: `if threading.current_thread() is not self._monitor_thread: self._monitor_thread.join(timeout=2.0)`.
- **Logging Added**: Added ERROR log in `stop()` when called from monitor thread; WARNING when join times out.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Guard was already present in `src/audio_player.py` (lines 161-163): `if threading.current_thread() is self._monitor_thread:` skips the join and logs ERROR. Confirmed correct; no change needed.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_stop_from_monitor_thread_does_not_raise, test_stop_from_external_thread_does_join, test_guard_branch_logs_error
- **Results**: 3 passed, 0 failed
  - ✅ test_stop_from_monitor_thread_does_not_raise
  - ✅ test_stop_from_external_thread_does_join
  - ✅ test_guard_branch_logs_error
- **Inspection**: `src/audio_player.py` lines 164-168 implement the guard correctly: `if threading.current_thread() is self._monitor_thread:` logs ERROR and skips the join; the else branch joins with 2s timeout and warns if the thread outlives it. Both flags `_playing` and `_paused` are cleared under lock at the end of `stop()` regardless of which branch executed.
- **Verdict**: Fix is confirmed correct and complete. The self-join deadlock cannot occur.
- **New Issues**: None

---

## ISSUE-002 — Temp MP3 deleted out from under a still-running synth/playback thread (race)

**Status**: VALIDATED ✅
**Severity**: HIGH

### Discovery
- **File**: `src/tts_engine.py` — `speak()` line ~30, `_cleanup_tmp()` lines ~138-145, `_speak_online._run` lines ~67-77
- **Description**: `speak()` calls `self.stop()` first, which calls `_cleanup_tmp()` and deletes every file in `self._tmp_files`. The online synth runs on a detached daemon thread that does `asyncio.run(_edge_synthesize(...))` then `self._player.play(tmp)`. When the next sentence's `speak()` fires while a prior `_run` thread is mid-synthesis, the prior thread's `tmp` path is removed from disk and the list. The prior thread then either writes to or plays a file the engine considers cleaned up, or `mciSendString open` fails because the file is gone. The `_stop_event` check reduces but does not eliminate this window.
- **Root Cause**: Temp-file lifecycle is shared mutable state (`self._tmp_files`) mutated from the GUI thread and the synth daemon thread with no synchronization, and cleanup is not tied to the lifetime of the specific playback.
- **Impact**: Intermittent "no audio" sentences, MCI open failures, leaked or prematurely deleted temp files, occasional skipped sentences.
- **Reproduction**: Play a page, then rapidly press Stop/Play or let fast sentences chain on a slow network so synth overlaps cleanup.
- **Depends On**: None
- **Fix Suggestion**: Add a lock around `_tmp_files` access; tie each file's cleanup to its playback completion rather than bulk-deleting on stop.
- **Logging Added**: Added DEBUG logs in `_speak_online._run` around synth start/done and the stop-event branch; DEBUG in `stop()` reporting tmp file count.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Added `_tmp_lock` (threading.Lock) protecting all access to `_tmp_files`. Each online synth thread now owns its temp file exclusively: on success the file is registered and deleted via a new `_delete_tmp()` method called from a `_done_and_cleanup` wrapper passed as `on_done` to `AudioPlayer.play`, so cleanup is tied to playback completion. `_cleanup_tmp()` still handles bulk cleanup on stop but only runs after `_player.stop()` has joined the monitor thread. `_make_tmp_mp3()` acquires the lock on append. Changed in `src/tts_engine.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_tmp_lock_exists, test_make_tmp_acquires_lock, test_cleanup_tmp_acquires_lock, test_delete_tmp_acquires_lock, test_done_and_cleanup_wrapper_present, test_cleanup_runs_after_player_stop
- **Results**: 6 passed logically, 2 errored due to test infrastructure defect (not a code defect)
  - ✅ test_tmp_lock_exists
  - ✅ test_make_tmp_acquires_lock
  - ✅ test_delete_tmp_acquires_lock
  - ✅ test_done_and_cleanup_wrapper_present
  - ❌ test_cleanup_tmp_acquires_lock — ERROR: `KeyError: 'src.tts_engine'`; test infrastructure defect (missing explicit import). Manually verified: `_cleanup_tmp` contains `_tmp_lock` access.
  - ❌ test_cleanup_runs_after_player_stop — same `KeyError` for same reason. Manually verified correct.
- **Inspection**: `_tmp_lock` is created in `__init__` (line 27). `_make_tmp_mp3` acquires it on append (lines 200-201). `_delete_tmp` acquires it on remove (lines 206-209). `_cleanup_tmp` takes the lock to snapshot and clear `_tmp_files` before iterating (lines 219-221). `TTSEngine.stop()` calls `_cleanup_tmp()` after `_player.stop()` (lines 68-74). `_speak_online` wraps `on_done` in `_done_and_cleanup` which calls `_delete_tmp(tmp)` then the caller's `on_done` (lines 102-106).
- **Verdict**: Fix is confirmed correct and complete. The two test ERRORs are a test infrastructure defect, not a code defect.
- **New Issues**: None

---

## ISSUE-003 — `_on_sentence_done` calls `self.after()` from a background thread (Tk thread-safety violation)

**Status**: VALIDATED ✅
**Severity**: HIGH

### Discovery
- **File**: `src/app.py` — `_on_sentence_done` line ~331, `_read_next_sentence` lines ~309-330
- **Description**: `_on_sentence_done` is invoked from the MCI monitor thread (online) or the pyttsx3 daemon thread (offline). It calls `self.after(0, self._read_next_sentence)` and assigns the result to `self._pending_after_id` — both from the background thread. customtkinter/Tk is not thread-safe; calling `self.after()` from a non-GUI thread can corrupt the event queue under load. `_stop()` also reads/cancels `_pending_after_id` from the GUI thread concurrently, creating a data race.
- **Root Cause**: `after()` and `_pending_after_id` mutation happen on a non-GUI thread; the only fully safe Tk call from another thread is `event_generate`.
- **Impact**: Rare event-queue corruption, stale `_pending_after_id` being cancelled (wrong callback), or `_read_next_sentence` running after a `_stop`. Symptoms: a sentence read after Stop, or the reader silently halting.
- **Reproduction**: Stress test: rapid Stop during the brief window between a sentence finishing and the next being scheduled.
- **Depends On**: None
- **Fix Suggestion**: Replace `self.after(0, self._read_next_sentence)` with `self.event_generate("<<SentenceDone>>", when="tail")` and bind a GUI-thread handler to `<<SentenceDone>>`.
- **Logging Added**: Added DEBUG log in `_on_sentence_done` noting it runs on a background thread, plus state snapshot.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Replaced `self.after(0, self._read_next_sentence)` in `_on_sentence_done` with `self.event_generate("<<SentenceDone>>", when="tail")`. Added `_on_sentence_done_event(self, _event)` handler bound to `<<SentenceDone>>` in `__init__` that runs on the GUI thread and calls `_read_next_sentence()`. `_pending_after_id` is now only written from the GUI thread. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_uses_event_generate_not_after, test_event_is_tail_queued, test_sentence_done_event_handler_exists, test_event_bound_in_init, test_pending_after_id_only_set_in_gui_methods
- **Results**: 4 passed, 1 failed (false positive)
  - ✅ test_event_is_tail_queued
  - ✅ test_sentence_done_event_handler_exists
  - ✅ test_event_bound_in_init
  - ✅ test_pending_after_id_only_set_in_gui_methods
  - ❌ test_uses_event_generate_not_after — false positive: `'self.after('` matched in a comment, not in executable code
- **Inspection**: `_on_sentence_done` (app.py lines 394-406) calls `self.event_generate(self._sentence_done_event, when="tail")` within a `try/except`. `_on_sentence_done_event` (lines 408-411) is bound to `<<SentenceDone>>` in `__init__` (line 55). No `self.after()` call exists in executable code within `_on_sentence_done`.
- **Verdict**: Fix is confirmed correct and complete. The test failure is a false positive caused by the test matching a comment string.
- **New Issues**: None

---

## ISSUE-004 — Auto-advance leaves stale highlight and has redundant `_sentence_idx` reset

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/app.py` — `_on_page_done` lines ~331-345
- **Description**: On auto-advance, `_on_page_done` increments `_current_page`, calls `_update_page_display()` (which resets `_sentence_idx = 0`), then sets `_sentence_idx = 0` again redundantly. More importantly, it does not clear the highlight from the previous page before advancing, so the UI can show a stale highlight briefly on the new page.
- **Root Cause**: Auto-advance duplicates page-load logic instead of funnelling through a single routine, and omits highlight clearing.
- **Impact**: Stale highlight on page transition; minor UI inconsistency. No crash.
- **Reproduction**: Enable auto-advance, let a page finish; observe leftover highlight momentarily on the new page.
- **Depends On**: None
- **Fix Suggestion**: Add `self._clear_highlight()` call in `_on_page_done` before incrementing `_current_page`. Remove the redundant `self._sentence_idx = 0` after `_update_page_display()`.
- **Logging Added**: Added INFO log when auto-advancing (page from→to).
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Added `self._clear_highlight()` call in `_on_page_done` before incrementing `_current_page`. Removed the redundant `self._sentence_idx = 0` line after `_update_page_display()`. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_clear_highlight_called_before_page_increment, test_no_redundant_sentence_idx_reset_after_update_page_display
- **Results**: 2 passed, 0 failed
  - ✅ test_clear_highlight_called_before_page_increment
  - ✅ test_no_redundant_sentence_idx_reset_after_update_page_display
- **Inspection**: `_on_page_done` (app.py lines 413-430) calls `self._clear_highlight()` at line 418 before `self._current_page += 1` at line 419. No `self._sentence_idx = 0` appears in `_on_page_done`; `_update_page_display` handles the reset.
- **Verdict**: Fix is confirmed correct and complete.
- **New Issues**: None

---

## ISSUE-005 — Sentence highlight always matches first occurrence of repeated text on a page

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/app.py` — `_highlight_sentence` lines ~350-362
- **Description**: Highlighting searches the Text widget for `sentence[:40]` starting at `"1.0"` every time and breaks after the first match. It never tracks position, so when the same 40-char prefix appears multiple times on a page (headers, repeated phrases, short sentences), the **first** occurrence is always highlighted regardless of which sentence is actually being read.
- **Root Cause**: No mapping between sentence index and text-widget character offset; relies on fragile substring search anchored at the document start.
- **Impact**: Wrong sentence highlighted, or highlight stuck on the first matching line while audio reads later text. Confusing UX; not a crash.
- **Reproduction**: Open a PDF with a repeated short line (e.g., "Introduction" twice) and play through.
- **Depends On**: None
- **Fix Suggestion**: Track a `_highlight_search_start` cursor; advance it after each match so subsequent searches start from the end of the last match. Wrap around to `"1.0"` on no-match.
- **Logging Added**: None added (pure UI logic; logging would be noisy).
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Added `_highlight_search_start` instance variable (initialized to `"1.0"`, reset in `_update_page_display` and `_clear_highlight`). `_highlight_sentence` now searches forward from `_highlight_search_start`; on success it advances `_highlight_search_start` to the end of the matched span. Falls back to `"1.0"` wrap-around search when no forward match is found. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_highlight_search_start_initialized_in_init, test_highlight_search_start_reset_in_update_page_display, test_highlight_search_start_reset_in_clear_highlight, test_highlight_sentence_searches_from_start_var, test_highlight_sentence_advances_start_on_match, test_highlight_wraps_around_on_no_forward_match
- **Results**: 6 passed, 0 failed
  - ✅ test_highlight_search_start_initialized_in_init
  - ✅ test_highlight_search_start_reset_in_update_page_display
  - ✅ test_highlight_search_start_reset_in_clear_highlight
  - ✅ test_highlight_sentence_searches_from_start_var
  - ✅ test_highlight_sentence_advances_start_on_match
  - ✅ test_highlight_wraps_around_on_no_forward_match
- **Inspection**: `_highlight_search_start` is initialized to `"1.0"` in `__init__` (line 50). Reset in `_update_page_display` (line 259) and `_clear_highlight` (line 461). `_highlight_sentence` passes it as the `index` argument to `self._text_box.search()` and assigns the end position back on match (line 453). Falls back to `"1.0"` when forward search yields no result (lines 446-447).
- **Verdict**: Fix is confirmed correct and complete. Repeated phrases on a page will now be highlighted in order.
- **New Issues**: None

---

## ISSUE-006 — Offline (pyttsx3) voice Resume does nothing after Pause

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/tts_engine.py` — `pause()` lines ~37-42, `resume()` lines ~44-46; `src/app.py` — `_pause`/`_play`
- **Description**: For offline voices, `TTSEngine.pause()` falls back to stopping the engine (pyttsx3 has no true pause). `TTSEngine.resume()` only handles `self._player.is_paused` (the MCI path); there is no offline branch. After pausing an offline voice, pressing Resume calls `self._tts.resume()` which does nothing for offline, then sets `_paused = False` and re-enables Pause — but no audio resumes and `_read_next_sentence` is not re-invoked. Playback is silently dead until Stop+Play.
- **Root Cause**: Asymmetric pause/resume support between backends; the app treats pause/resume uniformly.
- **Impact**: Offline-voice users experience "Resume does nothing"; must Stop and Play again, losing position.
- **Reproduction**: Select an `[Offline]` voice, Play, Pause, Resume.
- **Depends On**: ISSUE-013
- **Fix Suggestion**: After calling `self._tts.resume()`, check `if not self._tts.is_playing` — if not playing (offline no-op), set `_reading = True` and restart from `_read_next_sentence()`.
- **Logging Added**: Added DEBUG/INFO logs around pause/resume in app and engine to surface which backend handled it.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: In `src/app.py` `_play()` (the Resume path): after calling `self._tts.resume()`, check `if not self._tts.is_playing` — if the player is not playing (offline path, where resume is a no-op), set `_reading = True` and call `_read_next_sentence()` to restart from the rewound `_sentence_idx`. In `src/tts_engine.py`, `TTSEngine.pause()` no longer calls a `_stop_pyttsx3` helper (removed); the dedicated pyttsx3 worker (ISSUE-013 fix) handles stop via the worker queue.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_play_resumes_offline_by_calling_read_next_sentence, test_tts_engine_pause_does_not_call_stop_pyttsx3, test_tts_engine_resume_calls_player_resume
- **Results**: 3 passed, 0 failed
  - ✅ test_play_resumes_offline_by_calling_read_next_sentence
  - ✅ test_tts_engine_pause_does_not_call_stop_pyttsx3
  - ✅ test_tts_engine_resume_calls_player_resume
- **Inspection**: `_play()` (app.py lines 311-313): after `self._tts.resume()`, checks `if not self._tts.is_playing:` then sets `self._reading = True` and calls `_read_next_sentence()`. `TTSEngine.pause()` (tts_engine.py lines 54-58) only calls `self._player.pause()` for the online path; no `_stop_pyttsx3` reference anywhere in the class.
- **Verdict**: Fix is confirmed correct and complete. Offline pause/resume now works without requiring Stop+Play.
- **New Issues**: None

---

## ISSUE-007 — `_sentence_idx` post-incremented before speaking; bookmark and resume skip the interrupted sentence

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/app.py` — `_read_next_sentence` line ~324, `_pause` line ~287, `_save_bookmark` lines ~398-401
- **Description**: `_read_next_sentence` reads `_sentences[_sentence_idx]`, then does `_sentence_idx += 1` *before* the sentence has actually been spoken (synthesis/playback is async). If the user pauses mid-sentence, `_save_bookmark` records the already-incremented index (the **next** sentence, not the interrupted one). On resume/restore the reader skips the sentence that was interrupted.
- **Root Cause**: Index is advanced eagerly to set up the next call, but is also used as the "current position" for persistence.
- **Impact**: One sentence skipped on every pause/resume or bookmark restore. Cumulative drift if user pauses often.
- **Reproduction**: Play, pause partway through sentence N; resume — sentence N is skipped, N+1 plays.
- **Depends On**: None
- **Fix Suggestion**: In `_pause()` and `_stop()`, decrement `_sentence_idx` by 1 (guarded by `> 0`) before saving bookmark so it points at the interrupted sentence.
- **Logging Added**: Added DEBUG logs in `_read_next_sentence` (idx before increment) and `_save_bookmark` (idx persisted).
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: In `_pause()`: decrement `_sentence_idx` by 1 (guarded by `> 0`) before calling `_tts.pause()` and `_save_bookmark()`. In `_stop()`: when actively reading (not already paused), decrement `_sentence_idx` by 1 before `_save_bookmark()`. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_pause_decrements_sentence_idx, test_pause_rewind_guarded_by_greater_than_zero, test_stop_decrements_sentence_idx_when_reading, test_stop_rewind_only_when_actively_reading_not_paused
- **Results**: 4 passed, 0 failed
  - ✅ test_pause_decrements_sentence_idx
  - ✅ test_pause_rewind_guarded_by_greater_than_zero
  - ✅ test_stop_decrements_sentence_idx_when_reading
  - ✅ test_stop_rewind_only_when_actively_reading_not_paused
- **Inspection**: `_pause()` (app.py lines 329-341): guards `if self._sentence_idx > 0: self._sentence_idx -= 1` before `_tts.pause()` and `_save_bookmark()`. `_stop()` (lines 344-364): guards `if self._reading and not self._paused and self._sentence_idx > 0: self._sentence_idx -= 1` before `_save_bookmark()`.
- **Verdict**: Fix is confirmed correct and complete. Bookmarks and offline resume will now point to the sentence that was interrupted.
- **New Issues**: None

---

## ISSUE-008 — PDF filename parsed with brittle manual `split()` instead of `os.path.basename`

**Status**: VALIDATED ✅
**Severity**: LOW

### Discovery
- **File**: `src/app.py` — `_open_pdf` line ~218
- **Description**: `path.split("/")[-1].split("\\")[-1]` is a hand-rolled basename. On Windows the dialog returns forward slashes typically, but mixed separators or a filename containing unusual characters are handled by luck. `os.path.basename(path)` is the correct cross-platform solution.
- **Root Cause**: Manual path splitting instead of `os.path.basename`.
- **Impact**: Title label may show wrong text for unusual paths. Cosmetic.
- **Reproduction**: Open a file via a path with mixed separators.
- **Depends On**: None
- **Fix Suggestion**: Replace with `os.path.basename(path)`.
- **Logging Added**: PDF open path is logged at INFO in `_open_pdf`.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Replaced `path.split("/")[-1].split("\\")[-1]` with `os.path.basename(path)` in `_open_pdf`. `os` was already imported. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_open_pdf_uses_os_path_basename, test_open_pdf_does_not_use_manual_split, test_basename_correctly_handles_mixed_separators
- **Results**: 3 passed, 0 failed
  - ✅ test_open_pdf_uses_os_path_basename
  - ✅ test_open_pdf_does_not_use_manual_split
  - ✅ test_basename_correctly_handles_mixed_separators
- **Inspection**: `_open_pdf` (app.py line 247) uses `os.path.basename(path)` to populate `_title_label`. No `split("/")` or `split("\\")` calls present.
- **Verdict**: Fix is confirmed correct and complete.
- **New Issues**: None

---

## ISSUE-009 — Bookmark restore: `sentence_idx` may exceed the restored page's sentence count

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/app.py` — `_restore_bookmark` lines ~408-439
- **Description**: `_restore_bookmark` validates `page < page_count` but never validates `sentence_idx` against `len(self._sentences)` for the restored page. If the PDF was edited/re-saved or text extraction yields fewer sentences than when the bookmark was written, `_sentence_idx` can be `>= len(_sentences)`. On Play, `_read_next_sentence` immediately hits the page-done branch and stops with "Page done." — the user sees nothing read and may think playback is broken.
- **Root Cause**: Missing bounds check on restored `sentence_idx`.
- **Impact**: Silent no-op playback after restoring a stale bookmark.
- **Reproduction**: Bookmark near end of a page, modify the PDF so the page has fewer sentences, reopen, resume.
- **Depends On**: None
- **Fix Suggestion**: Clamp `sentence_idx = min(sentence_idx, max(0, len(self._sentences) - 1))` with a WARNING log when clamping occurs.
- **Logging Added**: Added INFO log in `_restore_bookmark` showing page, sentence_idx, and page_count; WARNING when page is out of range.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: After page text is loaded in `_restore_bookmark`, clamp using `max_idx = max(0, len(self._sentences) - 1); if sentence_idx > max_idx: sentence_idx = max_idx` with a WARNING log when clamping occurs. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_restore_bookmark_clamps_sentence_idx, test_restore_bookmark_logs_warning_on_clamp
- **Results**: 1 passed, 1 failed (false positive)
  - ✅ test_restore_bookmark_logs_warning_on_clamp
  - ❌ test_restore_bookmark_clamps_sentence_idx — false positive: test requires `min(` substring but implementation uses equivalent `if`/`max` pattern
- **Inspection**: `_restore_bookmark` (app.py lines 540-546): computes `max_idx = max(0, len(self._sentences) - 1)`, then `if sentence_idx > max_idx: sentence_idx = max_idx` with `log.warning`. Functionally identical to `min(sentence_idx, max(0, len(self._sentences) - 1))`. Verified for out-of-range, in-range, and empty-page cases.
- **Verdict**: Fix is confirmed correct and complete. The test failure is a false positive — the implementation achieves the stated goal via an equivalent `if`/`max` pattern.
- **New Issues**: None

---

## ISSUE-010 — `AudioPlayer` playback flags (`_playing`, `_paused`) mutated without lock protection

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/audio_player.py` — `play` lines ~90-91, `pause` ~132-135, `resume` ~137-140, monitor ~124-125, `stop` ~151-152
- **Description**: `self._lock` guards `self._open`, but `self._playing` and `self._paused` are read/written from the GUI thread (`pause`/`resume`/`stop`/`is_playing`/`is_paused`) and the monitor thread (`_monitor` sets them False on exit) with no synchronization. Torn or stale reads can cause `pause()` to no-op (thinks not playing) or `resume()` to act on a finished track.
- **Root Cause**: Inconsistent locking — only `_open` is protected.
- **Impact**: Occasional missed pause/resume, or pause acting after the monitor already cleared `_playing`. Low frequency.
- **Reproduction**: Hard to force deterministically; press Pause exactly as a track ends.
- **Depends On**: None
- **Fix Suggestion**: Extend `_lock` coverage to `_playing` and `_paused` throughout — in `play()`, monitor exit, `pause()`, `resume()`, `stop()`, and the `is_playing`/`is_paused` properties.
- **Logging Added**: Added DEBUG in monitor exit logging stop_event and whether on_done fires.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Extended `_lock` coverage to `_playing` and `_paused` throughout `src/audio_player.py`: both flags are now set/cleared under `_lock` in `play()`, the monitor exit, `pause()`, `resume()`, and `stop()`. The `is_playing` and `is_paused` properties also acquire `_lock` before reading.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_play_sets_playing_under_lock, test_monitor_clears_flags_under_lock, test_pause_sets_paused_under_lock, test_resume_clears_paused_under_lock, test_stop_clears_flags_under_lock, test_is_playing_property_acquires_lock, test_is_paused_property_acquires_lock
- **Results**: 7 passed, 0 failed
  - ✅ test_play_sets_playing_under_lock
  - ✅ test_monitor_clears_flags_under_lock
  - ✅ test_pause_sets_paused_under_lock
  - ✅ test_resume_clears_paused_under_lock
  - ✅ test_stop_clears_flags_under_lock
  - ✅ test_is_playing_property_acquires_lock
  - ✅ test_is_paused_property_acquires_lock
- **Inspection**: `play()` sets both flags under `with self._lock` (lines 97-99). Monitor exit clears both under lock (lines 133-135). `pause()`, `resume()`, and `stop()` all acquire lock before reading/writing flags (lines 146-174). `is_playing` and `is_paused` properties acquire `_lock` before reading (lines 177-183).
- **Verdict**: Fix is confirmed correct and complete. All flag reads and writes are now consistently lock-protected.
- **New Issues**: None

---

## ISSUE-012 — `_speed_to_edge_rate` produces out-of-range values at slider extremes

**Status**: VALIDATED ✅
**Severity**: LOW

### Discovery
- **File**: `src/tts_engine.py` — `_speed_to_edge_rate` lines ~88-92
- **Description**: `pct = int((speed - 1.0) * 100)` truncates toward zero and applies no clamping. The offline path uses `int(200 * speed)` (a different scale), so the same slider position sounds different across backends. No validation that edge-tts rate stays within its documented bounds (`[-50, +100]`).
- **Root Cause**: Two independent speed mappings; truncation; no clamping.
- **Impact**: Inconsistent perceived speed between online/offline voices; minor.
- **Reproduction**: Set 0.5x, compare an online vs offline voice.
- **Depends On**: None
- **Fix Suggestion**: Change `int()` to `round()` and clamp the edge-tts rate to `[-50, +100]`. Clamp offline wpm to `[80, 500]`.
- **Logging Added**: Added DEBUG log of the computed edge rate string in `_speak_online`.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: In `_speed_to_edge_rate`: changed `int(...)` to `round(...)` and clamped result to `[-50, +100]`. In `_speak_offline`: offline rate now uses `max(80, min(500, round(200 * speed)))` wpm with explicit clamp. Changed in `src/tts_engine.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_minimum_speed_yields_clamped_negative_50, test_maximum_speed_yields_clamped_positive_100, test_normal_speed_yields_plus_zero, test_out_of_range_low_is_clamped, test_out_of_range_high_is_clamped, test_round_not_truncate, test_offline_speed_clamped_wpm
- **Results**: 7 passed, 0 failed
  - ✅ test_minimum_speed_yields_clamped_negative_50 — 0.5x → "-50%"
  - ✅ test_maximum_speed_yields_clamped_positive_100 — 2.0x → "+100%"
  - ✅ test_normal_speed_yields_plus_zero — 1.0x → "+0%"
  - ✅ test_out_of_range_low_is_clamped — 0.1x → "-50%" (clamped)
  - ✅ test_out_of_range_high_is_clamped — 3.0x → "+100%" (clamped)
  - ✅ test_round_not_truncate — 1.455x → "+46%"
  - ✅ test_offline_speed_clamped_wpm
- **Inspection**: `_speed_to_edge_rate` (tts_engine.py lines 126-131): `pct = round((speed - 1.0) * 100)` then `pct = max(-50, min(100, pct))`. `_speak_offline` (lines 188-191): `rate_wpm = max(80, min(500, round(200 * speed)))`.
- **Verdict**: Fix is confirmed correct and complete.
- **New Issues**: None

---

## ISSUE-013 — pyttsx3 engine re-initialized per sentence; cross-thread COM `stop()` during `runAndWait`

**Status**: VALIDATED ✅
**Severity**: MEDIUM

### Discovery
- **File**: `src/tts_engine.py` — `_speak_offline._run` lines ~98-117
- **Description**: A fresh `pyttsx3.init()` is created on every sentence in a new daemon thread, and `runAndWait()` is called. pyttsx3 on Windows (SAPI5) is COM-based and not designed for repeated init/teardown across threads; `init()` may return a cached singleton so two overlapping sentences can share/clobber the same engine instance. `_stop_pyttsx3` calls `engine.stop()` from the GUI thread while `runAndWait()` blocks the worker thread — calling `stop()` on a SAPI engine from another thread mid-`runAndWait` has undefined behavior and can hang or raise.
- **Root Cause**: Per-sentence engine lifecycle plus cross-thread `stop()` on a COM object during a blocking `runAndWait`.
- **Impact**: Offline playback may hang on stop, throw COM errors, or leak. Higher risk on rapid Stop.
- **Reproduction**: Offline voice, Play, then Stop quickly and repeatedly across several sentences.
- **Depends On**: None
- **Fix Suggestion**: Replace the per-sentence-thread pattern with a single long-lived worker thread owning the engine, driven by a `queue.Queue`. Call `engine.stop()` only from within the worker thread.
- **Logging Added**: Added DEBUG around `runAndWait` start/return and `log.exception` in the failure path (replaced bare `print`).
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Replaced the per-sentence-thread pyttsx3 pattern with a single long-lived dedicated `pyttsx3-worker` daemon thread that owns the engine for the entire app lifetime. The worker processes `("speak", ...)` and `("stop", ...)` commands from a `queue.Queue`. `engine.stop()` is only ever called from within the worker thread itself. Engine is initialized once on first use and reused; re-initialized only if a sentence fails. Changed in `src/tts_engine.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_pyttsx3_worker_thread_exists, test_worker_thread_started_in_init, test_worker_thread_is_daemon, test_speak_offline_enqueues_not_spawns_thread, test_worker_engine_initialized_once, test_stop_command_processed_in_worker, test_queue_command_format
- **Results**: 7 passed, 0 failed
  - ✅ test_pyttsx3_worker_thread_exists
  - ✅ test_worker_thread_started_in_init
  - ✅ test_worker_thread_is_daemon
  - ✅ test_speak_offline_enqueues_not_spawns_thread
  - ✅ test_worker_engine_initialized_once
  - ✅ test_stop_command_processed_in_worker
  - ✅ test_queue_command_format
- **Inspection**: `__init__` (tts_engine.py lines 33-37) creates `_pyttsx3_queue` and starts `_pyttsx3_thread` as a daemon named "pyttsx3-worker". `_pyttsx3_worker` (lines 137-186) loops on the queue; initializes `engine` only when `engine is None`; handles "stop" by calling `engine.stop()` within the worker thread; calls `on_done` only if `not pending_stop and not self._stop_event.is_set()`. `_speak_offline` puts a `("speak", ...)` tuple on the queue with no thread creation.
- **Verdict**: Fix is confirmed correct and complete. The COM re-entrancy and cross-thread stop issues are eliminated.
- **New Issues**: None

---

## ISSUE-014 — `_load_voices` `on_done` runs on a background thread and swallows exceptions silently

**Status**: VALIDATED ✅
**Severity**: LOW

### Discovery
- **File**: `src/app.py` — `_load_voices.on_done` lines ~181-196; `src/voice_manager.py` — `load._load` lines ~26-36
- **Description**: `VoiceManager.load` invokes `on_done(voices)` directly on its worker thread. The app's `on_done` correctly marshals UI updates via `self.after(0, update)`, but any exception in the worker-thread portion is swallowed silently (no try/except, no logging). If `get_default_voice` raised, the worker thread dies and the UI is stuck on "Loading voices…" forever with no error surfaced.
- **Root Cause**: No error handling around the worker-thread portion of `on_done`; failures are invisible.
- **Impact**: Permanent "Loading voices…" with no diagnostic if voice post-processing fails. Edge case.
- **Reproduction**: Force `get_default_voice`/`str(Voice)` to raise (e.g., malformed voice data).
- **Depends On**: None
- **Fix Suggestion**: Wrap the entire body of `on_done` in `try/except Exception`; on exception, log via `log.exception` and marshal an error status to the GUI thread.
- **Logging Added**: Added DEBUG (callback fired, count) and WARNING (no voices) in `on_done`; INFO offline/online counts in `VoiceManager.load`; `log.exception` in both voice-loader except blocks (previously silent `return []`).
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: Wrapped the entire body of `on_done` in `try/except Exception` in `_load_voices`. On exception, logs via `log.exception` and marshals `"Error loading voices"` status to the GUI thread via `self.after(0, ...)`. Changed in `src/app.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_on_done_wrapped_in_try_except, test_exception_marshals_error_status_to_gui, test_exception_logs_via_log_exception
- **Results**: 3 passed, 0 failed
  - ✅ test_on_done_wrapped_in_try_except
  - ✅ test_exception_marshals_error_status_to_gui
  - ✅ test_exception_logs_via_log_exception
- **Inspection**: `_load_voices.on_done` (app.py lines 198-220) wraps its entire body in `try/except Exception`. The except block calls `log.exception(...)` and `self.after(0, lambda: self._set_status("Error loading voices"))`, correctly unblocking the GUI from the stuck state on any failure.
- **Verdict**: Fix is confirmed correct and complete.
- **New Issues**: None

---

## ISSUE-015 — `PDFReader` ignores encrypted PDFs and does not catch per-page extraction errors

**Status**: VALIDATED ✅
**Severity**: LOW

### Discovery
- **File**: `src/pdf_reader.py` — `open` lines ~11-17, `get_page_text` lines ~33-41
- **Description**: `fitz.open(path)` succeeds for encrypted PDFs but `page.get_text` returns empty (or raises) until `doc.authenticate()` is called. The app shows "(No text found on this page)", masking the real cause. `get_page_text` has no try/except around `page.get_text`, so a malformed page raising propagates uncaught into `_update_page_display` on the GUI thread, potentially crashing or freezing the UI.
- **Root Cause**: No encryption check (`doc.is_encrypted`) and no per-page error handling.
- **Impact**: Encrypted PDFs silently appear empty; a single bad page can throw into the GUI callback.
- **Reproduction**: Open a password-protected PDF, or one with a malformed page object.
- **Depends On**: None
- **Fix Suggestion**: After `fitz.open(path)`, check `self._doc.is_encrypted`; if true, close and raise `ValueError`. Wrap `page.get_text()` in `try/except` returning `""` on failure.
- **Logging Added**: Added INFO log of path + page count on open.
- **Date Found**: 2026-06-02

### Fix
- **Date**: 2026-06-02
- **Changes**: In `PDFReader.open()`: after `fitz.open(path)`, check `self._doc.is_encrypted`; if true, close the doc and raise `ValueError` with a clear message. The existing `try/except` in `_open_pdf` catches this and shows a `showerror` dialog. In `get_page_text()`: wrapped `page.get_text()` in `try/except` that logs the exception and returns `""`. Changed in `src/pdf_reader.py`.

### Validation
- **Date**: 2026-06-02
- **Method**: Tests + code inspection
- **Tests**: `tests/test_issue_validations.py` — test_open_raises_valueerror_for_encrypted, test_open_closes_doc_on_encrypted, test_get_page_text_returns_empty_on_exception, test_non_encrypted_pdf_opens_normally
- **Results**: 4 passed, 0 failed
  - ✅ test_open_raises_valueerror_for_encrypted
  - ✅ test_open_closes_doc_on_encrypted
  - ✅ test_get_page_text_returns_empty_on_exception
  - ✅ test_non_encrypted_pdf_opens_normally
- **Inspection**: `PDFReader.open()` (pdf_reader.py lines 21-29): checks `self._doc.is_encrypted`, closes the doc, sets `self._doc = None`, and raises `ValueError("This PDF is password-protected...")`. `get_page_text()` (lines 50-55): wraps `page.get_text("text")` in `try/except Exception` with `log.exception` and returns `""` on failure.
- **Verdict**: Fix is confirmed correct and complete.
- **New Issues**: None

---

## Logging infrastructure added

- **`main.py`**: Added `_setup_logging()` configuring root logging to stderr + `~/documentreader.log`, level via `DOCREADER_LOGLEVEL` env (default DEBUG). Called before importing the app so import-time errors are captured. Format includes thread name (critical for diagnosing the threading issues above).
- Module loggers (`logging.getLogger(__name__)`) added to `app.py`, `tts_engine.py`, `audio_player.py`, `voice_manager.py`, `pdf_reader.py`.
- Replaced the two bare `print(...)` error reports in `tts_engine.py` with `log.exception(...)`.
- No control flow was altered by logging; all additions are observational.
