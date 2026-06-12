---
name: recurring-patterns
description: Recurring classes of bugs found in DocumentReader, to check first in future investigations
metadata:
  type: project
---

Recurring bug classes in this codebase (check these first):

1. **Backend asymmetry (online vs offline TTS).** edge-tts (MCI/MP3) and pyttsx3 paths are not feature-equal. Pause/resume works for MCI but not pyttsx3 (ISSUE-006); speed maps differently (`+pct%` vs `int(200*speed)` wpm, ISSUE-012). Any new playback feature must be checked against BOTH backends.

2. **`_sentence_idx` off-by-one.** Index is post-incremented in `_read_next_sentence` BEFORE the sentence is actually spoken, but is also used as the saved/bookmark position. Pause/bookmark records the next sentence, skipping the interrupted one (ISSUE-007). Restored idx is also not bounds-checked against the page's sentence count (ISSUE-009).

3. **Temp-file / MCI alias lifecycle races.** `_tmp_files` (tts_engine) and the `DocumentReaderTrack` MCI alias are shared mutable resources touched by GUI thread + daemon threads with partial/no locking. `speak()` calls `stop()` which cleans temp files a still-running synth thread may need (ISSUE-002). AudioPlayer locks `_open` but not `_playing`/`_paused` (ISSUE-010).

4. **Silent exception swallowing.** Several `except Exception: return []` / `except: pass` blocks hid failures (voice loaders, tmp cleanup). Replaced the worst with `log.exception`. Watch for new bare excepts that mask root causes (ISSUE-014, ISSUE-015).

5. **Fragile text matching for highlight.** `_highlight_sentence` searches `sentence[:40]` from doc start every time, mis-highlighting duplicates (ISSUE-005). No sentence→offset map exists.

6. **Shared-Event set-then-clear cancellation.** `TTSEngine.speak()` sets then immediately clears the single shared `_stop_event`; in-flight synth threads that captured "the same object as a snapshot" get un-cancelled and resurrect (ISSUE-017, online play hijack + offline stale on_done). Pause has no engine-level flag at all, so in-flight synth plays during pause (ISSUE-019). Any cancellation check against `self._stop_event` is suspect — look for a per-utterance generation token instead.

7. **Fix duplicated at call sites misses a site.** The ISSUE-007 idx rewind was copied into `_pause` and `_stop` but missed `on_close` (ISSUE-020), and the ISSUE-009 clamp only bounds the upper end (negative idx passes, ISSUE-024). When validating a fix that is "apply X at every place Y happens", grep for ALL sites of Y (e.g. every `_save_bookmark()` caller).

8. **Unbounded blocking on the GUI thread.** `_mci()` waits forever on the dispatcher queue (ISSUE-026); `stop()` joins the monitor for 2s while the monitor can be blocked in a cross-thread Tk marshal (ISSUE-022); edge-tts synth has no network timeout (ISSUE-023). Any `.get()`/`.join()`/await without a timeout that the GUI thread can reach is a freeze candidate.

**Why:** The app glues several single-threaded/COM libraries (Tk, SAPI5, MCI) behind an async-ish sentence pump; the seams between them are where bugs cluster.

**How to apply:** Before deep-diving a new report, map it to one of these five buckets — most issues are a variant of one.
