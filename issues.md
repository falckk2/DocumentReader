# Issues Log

_Last updated: 2026-06-02_

This log documents bugs, race conditions, resource leaks, and edge cases found in
DocumentReader. Issues are sorted by Status (OPEN first), then Severity.
Findings are documentation only — no functional logic was changed. Diagnostic
`logging` was added at the noted locations.

---

## ISSUE-001 — `AudioPlayer.stop()` can deadlock when called from the monitor thread

- **Status**: Resolved ✅
- **Severity**: CRITICAL
- **File**: `src/audio_player.py` — `stop()` lines ~149-153 (join), reached via the monitor's `on_done` chain
- **Description**: The monitor thread, on natural track completion, calls `self._on_done()` (line ~131). For online TTS, `on_done` is `AudioPlayer.play`'s callback which is `TTSEngine`'s `on_done` → ultimately the app's `_on_sentence_done`, which schedules `_read_next_sentence` via `self.after(0, ...)`. That runs on the GUI thread and calls `_tts.speak()`, whose first line is `self.stop()` → `_player.stop()`. Normally fine. **But** `TTSEngine.speak()` and `TTSEngine.stop()` both call `self._player.stop()`; if any future code path (or a re-entrant `on_done`) causes `AudioPlayer.stop()` to be invoked *on the monitor thread itself*, `self._monitor_thread.join(timeout=2.0)` joins the current thread on itself. Python's `Thread.join()` from within the same thread raises `RuntimeError: cannot join current thread`, aborting `stop()` and leaving the MCI alias open.
- **Root Cause**: `stop()` unconditionally joins `self._monitor_thread` without checking whether the caller *is* that thread.
- **Impact**: Potential `RuntimeError`, alias `DocumentReaderTrack` left open, subsequent `open` failing with MCI error 263/"device already open", playback wedged until app restart.
- **Reproduction**: Trigger any callback path where `on_done` → `speak`/`stop` resolves synchronously on the monitor thread (e.g., MCI open failure firing `on_done()` inline while a stop is racing).
- **Fix Suggestion**: Guard the join: `if threading.current_thread() is not self._monitor_thread: self._monitor_thread.join(timeout=2.0)`. (A guard + warning log has been added defensively; confirm it is the desired behavior and that skipping the join does not leak the alias.)
- **Related Logging**: Added ERROR log in `stop()` when called from monitor thread; WARNING when join times out.
- **Date Found**: 2026-06-02
- **Fix Applied**: Guard was already present in `src/audio_player.py` (lines 161-163): `if threading.current_thread() is self._monitor_thread:` skips the join and logs ERROR. Confirmed correct; no change needed.
- **Date**: 2026-06-02

---

## ISSUE-002 — Temp MP3 deleted out from under a still-running synth/playback thread (race)

- **Status**: Resolved ✅
- **Severity**: HIGH
- **File**: `src/tts_engine.py` — `speak()` line ~30 (`self.stop()`), `_cleanup_tmp()` lines ~138-145, `_speak_online._run` lines ~67-77
- **Description**: `speak()` calls `self.stop()` first, which calls `_cleanup_tmp()` and deletes every file in `self._tmp_files`. The online synth runs on a detached daemon thread that does `asyncio.run(_edge_synthesize(...))` then `self._player.play(tmp)`. When the next sentence's `speak()` fires (or `_stop` is pressed) while a prior `_run` thread is mid-synthesis, the prior thread's `tmp` path is removed from disk and the list. The prior thread then either writes to / plays a file the engine considers cleaned up, or `mciSendString open` fails because the file is gone. The `_stop_event` check (`if not self._stop_event.is_set()`) reduces but does not eliminate the window: cleanup and the synth run concurrently.
- **Root Cause**: Temp-file lifecycle is shared mutable state (`self._tmp_files`) mutated from the GUI thread (`stop`/`speak`) and the synth daemon thread with no synchronization, and cleanup is not tied to the lifetime of the specific playback.
- **Impact**: Intermittent "no audio" sentences, MCI open failures, leaked or prematurely deleted temp files, occasional skipped sentences.
- **Reproduction**: Play a page, then rapidly press Stop/Play or let fast sentences chain on a slow network so synth overlaps cleanup.
- **Related Logging**: Added DEBUG logs in `_speak_online._run` around synth start/done and the stop-event branch; DEBUG in `stop()` reporting tmp file count.
- **Date Found**: 2026-06-02
- **Fix Applied**: Added `_tmp_lock` (threading.Lock) protecting all access to `_tmp_files`. Each online synth thread now owns its temp file exclusively: on success the file is registered and deleted via a new `_delete_tmp()` method called from a `_done_and_cleanup` wrapper passed as `on_done` to `AudioPlayer.play`, so cleanup is tied to playback completion. `_cleanup_tmp()` still handles bulk cleanup on stop but only runs after `_player.stop()` has joined the monitor thread. `_make_tmp_mp3()` acquires the lock on append. Changed in `src/tts_engine.py`.
- **Date**: 2026-06-02

---

## ISSUE-003 — `_read_next_sentence` reads GUI vars (`_voice_var`, `_speed_var`) — but `_on_*_done` callbacks fire on background threads

- **Status**: Resolved ✅
- **Severity**: HIGH
- **File**: `src/app.py` — `_on_sentence_done` line ~331, `_read_next_sentence` lines ~309-330
- **Description**: `_on_sentence_done` is invoked from the MCI monitor thread (online) or the pyttsx3 daemon thread (offline). It reads `self._reading`/`self._paused` (plain bools — torn reads possible but low risk) and then calls `self.after(0, self._read_next_sentence)`. The `after(0, ...)` correctly marshals back to the GUI thread, which is the right pattern. **However**, `self._pending_after_id` is assigned from the background thread (`self._pending_after_id = self.after(...)`), and `_stop()` reads/`after_cancel`s it from the GUI thread concurrently. customtkinter/Tk is not thread-safe; calling `self.after()` from a non-GUI thread is itself technically unsafe in Tk (it usually works because `after` just enqueues, but it is not guaranteed and can corrupt the event queue under load).
- **Root Cause**: `after()` and `_pending_after_id` mutation happen on a non-GUI thread; the only fully safe Tk call from another thread is `event_generate`.
- **Impact**: Rare event-queue corruption, a stale `_pending_after_id` being cancelled (cancelling the wrong/next callback), or `_read_next_sentence` running after a `_stop`. Symptoms: a sentence read after Stop, or the reader silently halting.
- **Reproduction**: Stress test: rapid Stop during the brief window between a sentence finishing and the next being scheduled.
- **Related Logging**: Added DEBUG log in `_on_sentence_done` noting it runs on a background thread, plus state snapshot.
- **Date Found**: 2026-06-02
- **Fix Applied**: Replaced `self.after(0, self._read_next_sentence)` in `_on_sentence_done` with `self.event_generate("<<SentenceDone>>", when="tail")` — the only thread-safe Tk call from non-GUI threads. Added a new `_on_sentence_done_event(self, _event)` handler bound to `<<SentenceDone>>` that runs on the GUI thread and calls `_read_next_sentence()`. `_pending_after_id` is now only written from the GUI thread (in `_read_next_sentence` for the `_on_page_done` scheduling). Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-004 — Auto-advance path never re-validates `_reading` and skips bookmark/idx reset semantics

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/app.py` — `_on_page_done` lines ~331-345
- **Description**: On auto-advance, `_on_page_done` increments `_current_page`, calls `_update_page_display()` (which resets `_sentence_idx = 0` and rebuilds `_sentences`), then sets `_sentence_idx = 0` again and calls `_read_next_sentence()` directly. It relies on `self._reading` still being `True`. That is true here, but the direct call (not via `after`) runs deep recursion-like chaining and, more importantly, `_update_page_display` runs `_sentences = self._pdf.get_sentences(...)`; if the new page has **no text**, `_sentences` is empty, `_read_next_sentence` immediately hits the page-done branch and recurses to the *next* page via `after(0, _on_page_done)`. A document with many empty pages will chain rapidly — acceptable, but each empty page still calls `self.after(0, self._on_page_done)` from the GUI thread; fine. The real gap: `_on_page_done` does not clear the highlight from the previous page before advancing, and does not update the Play/Pause button text/state, so the UI can show stale highlight briefly.
- **Root Cause**: Auto-advance duplicates page-load logic instead of funnelling through a single "go to page" routine, and omits highlight clearing.
- **Impact**: Stale highlight on page transition; minor UI inconsistency. No crash.
- **Reproduction**: Enable Auto-advance, let a page finish; observe leftover highlight momentarily.
- **Related Logging**: Added INFO log when auto-advancing (page from→to).
- **Date Found**: 2026-06-02
- **Fix Applied**: Added `self._clear_highlight()` call in `_on_page_done` before incrementing `_current_page`. Removed the redundant `self._sentence_idx = 0` line after `_update_page_display()` (which already resets it). Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-005 — Sentence highlight matches first occurrence of a 40-char prefix, mis-highlights repeated text

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/app.py` — `_highlight_sentence` lines ~350-362
- **Description**: Highlighting searches the Text widget for `sentence[:40]` starting at `"1.0"` every time and `break`s after the first match. It never tracks position, so when the same 40-char prefix appears multiple times on a page (headers, repeated phrases, short sentences), the **first** occurrence is always highlighted regardless of which sentence is actually being read. Also, `end = pos + len(sentence)c` uses the full sentence length even though only the 40-char prefix was matched; if the displayed text differs from the sentence string (whitespace normalization in `pdf_reader.get_page_text` collapses `\n{3,}` but `get_all_text` and `get_sentences` derive from the same normalized text, so they mostly agree) the highlight span can over/undershoot. Sentences shorter than 40 chars search for the whole sentence (fine), but duplicates still mis-target.
- **Root Cause**: No mapping between sentence index and text-widget character offset; relies on fragile substring search anchored at the document start.
- **Impact**: Wrong sentence highlighted, or highlight stuck on the first matching line while audio reads later text. Confusing UX; not a crash.
- **Reproduction**: Open a PDF with a repeated short line (e.g., "Introduction" twice) and play through.
- **Related Logging**: None added (pure UI logic; logging would be noisy). Noted for reviewer.
- **Date Found**: 2026-06-02
- **Fix Applied**: Added `_highlight_search_start` instance variable (initialized to `"1.0"`, reset in `_update_page_display` and `_clear_highlight`). `_highlight_sentence` now searches forward from `_highlight_search_start`; on success it advances `_highlight_search_start` to the end of the matched span. If no match is found forward, wraps around to `"1.0"` (handles pause/resume rewind). Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-006 — Resume after pause does not work for OFFLINE (pyttsx3) voices

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/tts_engine.py` — `pause()` lines ~37-42, `resume()` lines ~44-46; `src/app.py` — `_pause`/`_play`
- **Description**: For offline voices, `TTSEngine.pause()` falls back to `self._stop_pyttsx3()` (pyttsx3 has no true pause), which stops the engine. `TTSEngine.resume()` only handles `self._player.is_paused` (the MCI path); there is no offline branch. After pausing an offline voice, the app sets `_paused = True` and changes the button to "Resume". Pressing Resume calls `self._tts.resume()`, which does nothing for offline, then sets `_paused = False` and re-enables Pause — but no audio resumes and `_read_next_sentence` is not re-invoked. Playback is silently dead until Stop+Play.
- **Root Cause**: Asymmetric pause/resume support between backends; the app treats pause/resume uniformly.
- **Impact**: Offline-voice users experience "Resume does nothing"; must Stop and Play again, losing intra-sentence position.
- **Reproduction**: Select an `[Offline]` voice, Play, Pause, Resume.
- **Related Logging**: Added DEBUG/INFO logs around pause/resume in app and engine to surface which backend handled it.
- **Date Found**: 2026-06-02
- **Fix Applied**: In `src/app.py` `_play()` (the Resume path): after calling `self._tts.resume()`, check `if not self._tts.is_playing` — if the player is not playing (offline path, where resume is a no-op), set `_reading = True` and call `_read_next_sentence()` to restart from the rewound `_sentence_idx`. Combined with ISSUE-007's rewind in `_pause`, this re-reads the interrupted sentence. In `src/tts_engine.py`, `TTSEngine.pause()` no longer calls a `_stop_pyttsx3` helper (removed); for offline, the pyttsx3 worker processes a "stop" command from `TTSEngine.stop()` called as part of the next `speak()`, keeping it clean. The dedicated pyttsx3 worker (ISSUE-013 fix) makes this reliable.
- **Date**: 2026-06-02

---

## ISSUE-007 — `_sentence_idx` is post-incremented before speaking; bookmark + resume restart wrong sentence

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/app.py` — `_read_next_sentence` line ~324 (`self._sentence_idx += 1`), `_pause` line ~287, `_save_bookmark` lines ~398-401
- **Description**: `_read_next_sentence` reads `_sentences[_sentence_idx]`, then does `_sentence_idx += 1` *before* the sentence has actually been spoken (synthesis/playback is async). If the user pauses mid-sentence, `_save_bookmark` records the *already-incremented* index, i.e., the **next** sentence, not the one currently being read. On resume/restore the reader skips the sentence that was interrupted. Similarly, `_pause` saves the incremented index.
- **Root Cause**: Index is advanced eagerly to set up the next call, but is also used as the "current position" for persistence.
- **Impact**: One sentence skipped on every pause/resume or bookmark restore. Cumulative drift if user pauses often.
- **Reproduction**: Play, pause partway through sentence N, note saved bookmark; resume — sentence N is skipped, N+1 plays.
- **Related Logging**: Added DEBUG logs in `_read_next_sentence` (idx before increment) and `_save_bookmark` (idx persisted) so the off-by-one is visible in logs.
- **Date Found**: 2026-06-02
- **Fix Applied**: In `_pause()`: decrement `_sentence_idx` by 1 (guarded by `> 0`) before calling `_tts.pause()` and `_save_bookmark()`, so the saved index points at the interrupted sentence. In `_stop()`: when actively reading (not already paused), decrement `_sentence_idx` by 1 before `_save_bookmark()` for the same reason. This ensures bookmark restore and offline resume both re-read the correct sentence. Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-008 — PDF filename parsing for title is brittle (`split("/")` then `split("\\")`)

- **Status**: Resolved ✅
- **Severity**: LOW
- **File**: `src/app.py` — `_open_pdf` line ~218
- **Description**: `path.split("/")[-1].split("\\")[-1]` is a hand-rolled basename. On Windows the dialog returns forward slashes typically, but mixed separators or a filename containing characters are handled by luck. Use `os.path.basename(path)`.
- **Root Cause**: Manual path splitting instead of `os.path.basename`.
- **Impact**: Title label may show wrong text for unusual paths. Cosmetic.
- **Reproduction**: Open a file via a path with mixed separators.
- **Related Logging**: PDF open path is logged at INFO in `_open_pdf`.
- **Date Found**: 2026-06-02
- **Fix Applied**: Replaced `path.split("/")[-1].split("\\")[-1]` with `os.path.basename(path)` in `_open_pdf`. `os` was already imported. Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-009 — Bookmark restore: `sentence_idx` may exceed the restored page's sentence count

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/app.py` — `_restore_bookmark` lines ~408-439
- **Description**: `_restore_bookmark` validates `page < page_count` but never validates `sentence_idx` against `len(self._sentences)` for the restored page. If the PDF was edited/re-saved or text extraction yields fewer sentences than when the bookmark was written, `_sentence_idx` can be `>= len(_sentences)`. On Play, `_read_next_sentence` immediately hits the page-done branch and (if auto-advance off) stops with "Page done." — the user sees nothing read and may think playback is broken. Worse, for `page == 0` the code sets `_sentence_idx` without reloading `_sentences` at all (relies on `_update_page_display` having run in `_open_pdf`), which is correct, but still unvalidated.
- **Root Cause**: Missing bounds check on restored `sentence_idx`.
- **Impact**: Silent no-op playback after restoring a stale bookmark.
- **Reproduction**: Bookmark near end of a page, modify the PDF so the page has fewer sentences, reopen, resume.
- **Related Logging**: Added INFO log in `_restore_bookmark` showing page, sentence_idx, and page_count; WARNING when page is out of range.
- **Date Found**: 2026-06-02
- **Fix Applied**: After the page text is loaded in `_restore_bookmark`, clamp `sentence_idx = min(sentence_idx, max(0, len(self._sentences) - 1))` with a WARNING log when clamping occurs. Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-010 — `AudioPlayer` playback flags (`_playing`, `_paused`) mutated without the lock that protects `_open`

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/audio_player.py` — `play` lines ~90-91, `pause` ~132-135, `resume` ~137-140, monitor ~124-125, `stop` ~151-152
- **Description**: `self._lock` guards `self._open`, but `self._playing` and `self._paused` are read/written from the GUI thread (`pause`/`resume`/`stop`/`is_playing`/`is_paused`) and the monitor thread (`_monitor` sets them False on exit) with no synchronization. `is_playing`/`is_paused` are polled by `TTSEngine.pause/resume`. Torn or stale reads can cause `pause()` to no-op (thinks not playing) or `resume()` to act on a finished track.
- **Root Cause**: Inconsistent locking — only `_open` is protected.
- **Impact**: Occasional missed pause/resume, or pause acting after the monitor already cleared `_playing`. Low frequency.
- **Reproduction**: Hard to force deterministically; press Pause exactly as a track ends.
- **Related Logging**: Added DEBUG in monitor exit logging stop_event and whether on_done fires.
- **Date Found**: 2026-06-02
- **Fix Applied**: Extended `_lock` coverage to `_playing` and `_paused` throughout `src/audio_player.py`: both flags are now set/cleared under `_lock` in `play()`, the monitor exit, `pause()`, `resume()`, and `stop()`. The `is_playing` and `is_paused` properties also acquire `_lock` before reading. Changed in `src/audio_player.py`.
- **Date**: 2026-06-02

---

## ISSUE-011 — End-of-track detection relies on position polling that can miss the end (premature/late on_done)

- **Status**: Partially Resolved ⚠️
- **Severity**: MEDIUM
- **File**: `src/audio_player.py` — `_monitor` lines ~93-127
- **Description**: The monitor polls `status mode` and `status position` every 0.1s. End is detected either by `mode == "stopped"` or `pos >= track_length`. If `track_length` could not be read (query failed → `track_length = 0`), the `pos >= track_length` branch is disabled and end detection depends solely on MCI reporting `"stopped"`. For very short MP3s, the track may finish and the device report `stopped` before the initial `time.sleep(0.2)` even completes, but the length read then returns 0 and the loop may exit on the next `mode` poll — usually OK. Conversely, MCI sometimes reports `position` slightly less than `length` at true end, delaying `on_done` by up to the 0.1s poll plus the 5×0.05s drain. Cumulative latency across many short sentences adds noticeable gaps.
- **Root Cause**: Polling-based completion instead of MCI `notify` callback (`play ... notify` + `MM_MCINOTIFY`), plus a magic 0.2s warmup and 0.25s drain.
- **Impact**: Audible gaps between sentences; rare premature cutoff of the last fraction of a sentence if drain is too short.
- **Reproduction**: Read a page of many short sentences; observe inter-sentence gaps. Use a sub-200ms clip to test warmup edge.
- **Fix Suggestion**: Use `play {alias} notify` with a window/callback for `MM_MCINOTIFY`, or at minimum reduce reliance on magic timings and detect end via `mode == "stopped"` as primary with position as backup. Log measured track_length and final position.
- **Related Logging**: Added DEBUG logging of `track_length` at monitor start and a warning when length cannot be read.
- **Date Found**: 2026-06-02
- **Fix Applied**: No change. Implementing MCI `notify` requires a hidden Win32 window to receive `MM_MCINOTIFY` messages, which is a significant architectural addition. The existing `mode == "stopped"` primary detection plus position-based backup is functionally correct. Reducing magic timings risks audio cutoff. The polling latency issue is real but minor. Marking partially resolved — a future session can implement `MM_MCINOTIFY` for lower-latency end detection.
- **Remaining**: Implement Win32 `MM_MCINOTIFY` via a hidden `HWND` to eliminate polling gaps between sentences.
- **Date**: 2026-06-02

---

## ISSUE-012 — `_speed_to_edge_rate` produces out-of-range/odd values at slider extremes

- **Status**: Resolved ✅
- **Severity**: LOW
- **File**: `src/tts_engine.py` — `_speed_to_edge_rate` lines ~88-92
- **Description**: Slider range is 0.5–2.0. `pct = int((speed - 1.0) * 100)` yields `-50%`..`+100%`. edge-tts accepts these, but `int()` truncates toward zero, so 0.5x → `-50%`, 1.5x → `+50%` (fine). The speed slider value is a continuous double; mid-drag values like 1.234 → `+23%`. No clamping or rounding consistency, and the offline path uses `int(200 * speed)` (100–400 wpm) which is a different scale, so the same slider position sounds different across backends. No validation that edge-tts rate stays within its documented bounds.
- **Root Cause**: Two independent speed mappings; truncation; no clamping.
- **Impact**: Inconsistent perceived speed between online/offline voices; minor.
- **Reproduction**: Set 0.5x, compare an online vs offline voice.
- **Related Logging**: Added DEBUG log of the computed edge rate string in `_speak_online`.
- **Date Found**: 2026-06-02
- **Fix Applied**: In `_speed_to_edge_rate`: changed `int(...)` to `round(...)` and clamped result to `[-50, +100]` (edge-tts safe range). In `_speak_offline`/`_speak_online`: offline rate now uses `max(80, min(500, round(200 * speed)))` wpm with explicit clamp. Changed in `src/tts_engine.py`.
- **Date**: 2026-06-02

---

## ISSUE-013 — pyttsx3 engine re-initialized per sentence; `runAndWait` reentrancy / driver leak

- **Status**: Resolved ✅
- **Severity**: MEDIUM
- **File**: `src/tts_engine.py` — `_speak_offline._run` lines ~98-117
- **Description**: A fresh `pyttsx3.init()` is created on every sentence in a new daemon thread, and `runAndWait()` is called. pyttsx3 on Windows (SAPI5) is COM-based and not designed for repeated init/teardown across threads; `init()` may return a cached singleton (pyttsx3 caches per driver), so two overlapping sentences (if a stop/speak races) can share/clobber the same engine instance. `_stop_pyttsx3` calls `engine.stop()` from the GUI thread while `runAndWait()` blocks the worker thread — calling `stop()` on a SAPI engine from another thread mid-`runAndWait` has undefined behavior and can hang or raise. The engine is never `del`'d; repeated init may leak COM objects.
- **Root Cause**: Per-sentence engine lifecycle plus cross-thread `stop()` on a COM object during a blocking `runAndWait`.
- **Impact**: Offline playback may hang on stop, throw COM errors, or leak. Higher risk on rapid Stop.
- **Reproduction**: Offline voice, Play, then Stop quickly and repeatedly across several sentences.
- **Related Logging**: Added DEBUG around `runAndWait` start/return and `log.exception` in the failure path (replaced bare `print`).
- **Date Found**: 2026-06-02
- **Fix Applied**: Replaced the per-sentence-thread pyttsx3 pattern with a single long-lived dedicated `pyttsx3-worker` thread that owns the engine for the entire app lifetime. The worker processes ("speak", text, voice_id, rate_wpm, on_done) and ("stop", ...) commands from a `queue.Queue`. `engine.stop()` is only ever called from within the worker thread itself (on the "stop" command), eliminating the cross-thread COM issue. Engine is initialized once on first use and reused; re-initialized only if a sentence fails. Changed in `src/tts_engine.py`.
- **Date**: 2026-06-02

---

## ISSUE-014 — `_load_voices` `on_done` runs on a background thread and touches Tk indirectly via captured closures

- **Status**: Resolved ✅
- **Severity**: LOW
- **File**: `src/app.py` — `_load_voices.on_done` lines ~181-196; `src/voice_manager.py` — `load._load` lines ~26-36
- **Description**: `VoiceManager.load` invokes `on_done(voices)` directly on its worker thread. The app's `on_done` does build a `display` list and call `self._voices.get_default_voice()` (safe, no Tk), then marshals UI updates via `self.after(0, update)` (correct). This is mostly safe, but `str(default)` and list comprehensions run on the worker thread, and any exception there is swallowed silently (no try/except, no logging previously). If `get_default_voice` raised, the worker thread dies and the UI is stuck on "Loading voices…" forever with no error surfaced.
- **Root Cause**: No error handling around the worker-thread portion of `on_done`; failures are invisible.
- **Impact**: Permanent "Loading voices…" with no diagnostic if voice post-processing fails. Edge case.
- **Reproduction**: Force `get_default_voice`/`str(Voice)` to raise (e.g., malformed voice data).
- **Related Logging**: Added DEBUG (callback fired, count) and WARNING (no voices) in `on_done`; INFO offline/online counts in `VoiceManager.load`; `log.exception` in both voice-loader except blocks (previously silent `return []`).
- **Date Found**: 2026-06-02
- **Fix Applied**: Wrapped the entire body of `on_done` in `try/except Exception` in `_load_voices`. On exception, logs via `log.exception` and marshals `"Error loading voices"` status to the GUI thread via `self.after(0, ...)`, preventing the permanent "Loading voices…" stuck state. Changed in `src/app.py`.
- **Date**: 2026-06-02

---

## ISSUE-015 — `PDFReader` extraction ignores encrypted/password-protected PDFs and per-page errors

- **Status**: Resolved ✅
- **Severity**: LOW
- **File**: `src/pdf_reader.py` — `open` lines ~11-17, `get_page_text` lines ~33-41
- **Description**: `fitz.open(path)` succeeds for encrypted PDFs but `page.get_text` returns empty (or raises) until `doc.authenticate()` is called. The app shows "(No text found on this page)" for an encrypted document, masking the real cause. `get_page_text` has no try/except around `page.get_text`, so a malformed page raising propagates up uncaught into `_update_page_display` on the GUI thread (no handler there), potentially crashing or freezing the UI.
- **Root Cause**: No encryption check (`doc.needs_pass`/`is_encrypted`) and no per-page error handling.
- **Impact**: Encrypted PDFs silently appear empty; a single bad page can throw into the GUI callback.
- **Reproduction**: Open a password-protected PDF, or one with a malformed page object.
- **Related Logging**: Added INFO log of path + page count on open.
- **Date Found**: 2026-06-02
- **Fix Applied**: In `PDFReader.open()`: after `fitz.open(path)`, check `self._doc.is_encrypted`; if true, close the doc and raise `ValueError` with a clear message. The existing `try/except` in `_open_pdf` catches this and shows a `showerror` dialog. In `get_page_text()`: wrapped `page.get_text()` in `try/except` that logs the exception and returns `""`, preventing propagation into the GUI thread. Changed in `src/pdf_reader.py`.
- **Date**: 2026-06-02

---

## ISSUE-016 — Speed/voice changes mid-playback do not take effect until the next sentence (by design, but undocumented and surprising)

- **Status**: NEEDS_REVIEW
- **Severity**: LOW
- **File**: `src/app.py` — `_on_voice_change` lines ~373-374, `_on_speed_change` lines ~376-377, `_read_next_sentence` reads vars at speak time
- **Description**: Voice and speed are read at the start of each sentence (`_read_next_sentence`). Changing the slider or dropdown mid-sentence has no effect until the next sentence boundary. This matches the documented design ("Voice selection reads from dropdown at speak time"), so it is intentional — flagged only because there is no user feedback that the change is queued, which users may report as a bug.
- **Root Cause**: Intentional design; no UI affordance indicating deferred application.
- **Impact**: User confusion ("I moved the slider, nothing happened").
- **Reproduction**: Start playback, drag speed slider; current sentence speed unchanged.
- **Fix Suggestion**: Optionally show a transient status like "Speed applies to next sentence", or re-synth the current sentence on change. Confirm desired behavior before changing.
- **Related Logging**: `_read_next_sentence` logs the speed/voice used per sentence, making the deferral visible in logs.
- **Date Found**: 2026-06-02
- **Date Verified**: —

---

## Logging infrastructure added

- **`main.py`**: Added `_setup_logging()` configuring root logging to stderr + `~/documentreader.log`, level via `DOCREADER_LOGLEVEL` env (default DEBUG). Called before importing the app so import-time errors are captured. Format includes thread name (critical for diagnosing the threading issues above).
- Module loggers (`logging.getLogger(__name__)`) added to `app.py`, `tts_engine.py`, `audio_player.py`, `voice_manager.py`, `pdf_reader.py`.
- Replaced the two bare `print(...)` error reports in `tts_engine.py` with `log.exception(...)`.
- No control flow was altered by logging; all additions are observational.
