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

**Why:** The app glues several single-threaded/COM libraries (Tk, SAPI5, MCI) behind an async-ish sentence pump; the seams between them are where bugs cluster.

**How to apply:** Before deep-diving a new report, map it to one of these five buckets — most issues are a variant of one.
