---
name: threading-boundaries
description: Thread boundaries and known thread-unsafe call sites in DocumentReader (Tk/customtkinter is single-threaded)
metadata:
  type: project
---

DocumentReader runs three+ thread contexts that interact dangerously:

1. **GUI thread** — customtkinter/Tk event loop. The ONLY thread allowed to touch widgets. customtkinter is not thread-safe.
2. **VoiceManager worker thread** — `VoiceManager.load()` runs `_load` on a daemon thread and calls `on_done(voices)` directly on it. App's `on_done` does non-Tk work then marshals via `self.after(0, ...)`.
3. **TTS synth daemon threads** — `_speak_online._run` and `_speak_offline._run` are fresh daemon threads per sentence.
4. **AudioPlayer monitor thread** — polls MCI; fires `on_done()` on itself at end of track.
5. **MCI dispatcher thread** — `_mci_worker` in audio_player.py; ALL `mciSendStringW` calls funnel through it via a queue (intentional, for COM apartment affinity). Do not bypass it.

**Known unsafe call sites:**
- `_on_sentence_done` (app.py) runs on the monitor/synth thread and calls `self.after(...)` and assigns `self._pending_after_id` — `after()` from a non-GUI thread is not guaranteed safe in Tk. `_stop()` reads/cancels `_pending_after_id` from the GUI thread concurrently. (ISSUE-003)
- `_stop_pyttsx3` calls `engine.stop()` from the GUI thread while `runAndWait()` blocks the synth thread — COM cross-thread hazard. (ISSUE-013)
- `AudioPlayer.stop()` joins `_monitor_thread`; if ever called from that thread it raises RuntimeError. (ISSUE-001)

- `event_generate` from a background thread is thread-safe but BLOCKING: tkinter marshals it to the main thread and the caller waits until the mainloop dispatches it. If the GUI thread is simultaneously blocked (e.g. `AudioPlayer.stop()` joining the monitor thread that is inside `event_generate`), you get a lock-step stall broken only by the join timeout — 2s GUI freeze (ISSUE-022).
- pyttsx3 worker (ISSUE-013 fix) is a single command-queue thread; while blocked in `runAndWait` it cannot process "stop" commands, so offline Stop/Pause cannot interrupt the current sentence (ISSUE-018).

**Why:** Tk's only documented thread-safe cross-thread call is `event_generate`. Everything else must marshal through it or `after` from the GUI thread only.

**How to apply:** When reviewing any callback whose name contains `_on_*_done` or any code inside a `threading.Thread(target=...)`, assume it is NOT on the GUI thread and flag any direct widget/`after`/shared-mutable access.
