---
name: project-docreader-patterns
description: Recurring bug patterns, frequently-implicated files, and fix strategies specific to the DocumentReader project
metadata:
  type: project
---

## Frequently Implicated Files

- `src/audio_player.py` — MCI state bugs, thread-safety of `_playing`/`_paused` flags, monitor-thread deadlock
- `src/tts_engine.py` — temp file races, pyttsx3 COM threading, speed mapping consistency
- `src/app.py` — thread-safety of Tk callbacks, bookmark off-by-one, highlight tracking, offline resume logic

## Recurring Patterns

**Thread-safety in Tk callbacks:** Background TTS/audio threads must NEVER call `self.after()`. Use `self.event_generate("<<VirtualEvent>>", when="tail")` bound to a handler on the GUI thread. This was the fix for ISSUE-003.

**MCI alias guard:** `AudioPlayer.stop()` must check `threading.current_thread() is self._monitor_thread` before joining to avoid RuntimeError. Already in place.

**pyttsx3 COM threading:** pyttsx3 (SAPI5) engine must live on a single dedicated thread. Never call `engine.stop()` from a different thread during `runAndWait()`. Use a `queue.Queue` command pattern — fixed in ISSUE-013.

**Post-increment index bias:** `_sentence_idx` is incremented before speech fires. Bookmark/pause must rewind by 1 to capture the currently-speaking sentence, not the next one — fixed in ISSUE-007.

**Temp file lifecycle (online TTS):** Each temp MP3 is owned by its specific synthesis run. Cleanup must happen via a per-file `_delete_tmp()` called in the `on_done` wrapper, not bulk `_cleanup_tmp()` which runs during stop — fixed in ISSUE-002.

**Why:** All these stem from the async/multi-thread architecture: GUI thread, MCI monitor thread, edge-tts daemon thread, pyttsx3-worker thread, and VoiceManager background thread all interact.

**How to apply:** When adding any new callback or inter-thread communication path, verify which thread it runs on before touching shared state or Tk widgets.
