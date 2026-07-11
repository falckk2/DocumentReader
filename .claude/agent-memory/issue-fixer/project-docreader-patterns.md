---
name: project-docreader-patterns
description: Recurring bug patterns, frequently-implicated files, and fix strategies specific to the DocumentReader project
metadata:
  type: project
---

## Frequently Implicated Files

- `src/audio_player.py` — MCI state bugs, thread-safety of `_playing`/`_paused` flags, monitor-thread deadlock
- `src/tts_engine.py` — temp file races, pyttsx3 COM threading, speed mapping consistency, utterance cancellation
- `src/app.py` — thread-safety of Tk callbacks, bookmark off-by-one, highlight tracking, offline resume logic

## Recurring Patterns

**Thread-safety in Tk callbacks:** Background TTS/audio threads must NEVER call `self.after()`. Use `self.event_generate("<<VirtualEvent>>", when="tail")` bound to a handler on the GUI thread. This was the fix for ISSUE-003.

**Never join a thread that marshals to the GUI:** `event_generate` from a background thread BLOCKS until the GUI mainloop services it. If the GUI thread `join()`s that thread (AudioPlayer.stop joining the monitor), you get a lock-step freeze for the join timeout. Fix (ISSUE-022): fire `on_done` on a detached "on-done-dispatch" daemon thread so the joined thread exits immediately.

**Cancellation via generation token, never set-then-clear a shared Event:** `speak()` doing `stop_event.set()` then `clear()` resurrects in-flight utterances that check the event after the clear (ISSUE-017). The validated pattern: `_generation` int + `_gen_lock` in TTSEngine; `stop()`/`pause()` bump it; each utterance captures `gen` at start and checks `gen == self._generation` before playing (online) / in a gated on_done closure (offline). Closures gate on_done without changing the pyttsx3 queue tuple format (a test asserts on it). Same pattern applied per-playback in AudioPlayer (ISSUE-028): each `play()` creates a fresh `threading.Event` and the monitor captures its own event + on_done, never the shared slots.

**Gate generation checks at the HANDOFF, never at COMPLETION:** a suggested fix can be wrong even when written by the validator — ISSUE-027's primary suggestion (re-check `gen == self._generation` inside `_done_and_cleanup` before on_done) would have killed online pause/resume: `pause()` bumps the generation (ISSUE-019), resume continues the MCI audio of that gen-stale utterance, and its natural on_done is what keeps the sentence pump alive. Completion callbacks of gen-stale-but-resumed playbacks MUST fire. The correct closure of the TOCTOU is holding `_gen_lock` across check+`_player.play()`; that in turn required `play()`'s MCI-open-failure on_done to fire on a detached thread (an inline callback would `event_generate` toward a GUI thread blocked on `_gen_lock` in `stop()` — deadlock). Test `test_done_and_cleanup_does_not_recheck_generation` pins this; before changing online completion gating, trace the pause→resume→natural-on_done path.

**pyttsx3 COM threading:** pyttsx3 (SAPI5) engine must live on a single dedicated thread (ISSUE-013). To interrupt mid-`runAndWait`, connect a `started-word` callback that checks a `threading.Event` and calls `engine.stop()` — the callback runs ON the worker thread inside runAndWait, so it is COM-safe (ISSUE-018).

**Post-increment index bias:** `_sentence_idx` is incremented before speech fires. Every bookmark-save site (`_pause`, `_stop`, `on_close`) must rewind by 1 when actively reading — ISSUE-007 fixed two sites, ISSUE-020 caught the missed third. Exception: natural completion has no interrupted sentence; `_stop(completed=True)` skips the rewind and `_on_page_done` clears/advances the bookmark instead (ISSUE-025).

**Persisted external data is untrusted:** the bookmarks JSON needed dict-root check, int type checks, and both-end clamping (ISSUE-009 only clamped the upper end; ISSUE-024 added the rest). Negative indices silently read from the page END via Python negative indexing — no exception, just bizarre behavior.

**Open-then-swap for fallible resource replacement:** PDFReader.open closed the old doc before `fitz.open(path)` could fail, leaving a closed-doc reference (ISSUE-021). Open into a local, validate, then swap.

**Unbounded waits:** every cross-thread `Queue.get()` whose caller can be the GUI thread needs a timeout + error fallback, and the worker must always answer the caller even on failure (ISSUE-026). Network ops on the sentence-advance critical path need `asyncio.wait_for` (ISSUE-023) — liveness depends on on_done always eventually firing.

**Why:** All these stem from the async/multi-thread architecture: GUI thread, MCI monitor thread, edge-tts daemon thread, pyttsx3-worker thread, on-done-dispatch threads, and VoiceManager background thread all interact.

**How to apply:** When adding any new callback or inter-thread communication path, verify which thread it runs on before touching shared state or Tk widgets; gate any deferred callback on the current generation; never let the GUI thread wait unboundedly on another thread.

**VoiceManager background thread has the same after()-thread-safety trap as the MCI/TTS threads:** ISSUE-032 found `_load_voices`'s `on_done` (invoked from `VoiceManager.load`'s daemon thread) calling `self.after(...)` three times — the exact ISSUE-003 violation, just in a different subsystem. Fixed with the identical event_generate pattern: background thread stashes a result dict on an instance attr, then `event_generate("<<VoicesLoaded>>", when="tail")`; a GUI-thread handler bound in `__init__` does the actual widget mutation. No lock needed since the attr is fully written before the event fires and `on_done` is only invoked once per `load()` call.

**Readiness gates need to be checked from BOTH directions:** ISSUE-033 (Play enabled before async voice load finishes) required gating in two places, not one — `_open_pdf` checks `self._voices_ready` before enabling Play (handles PDF-opens-after-voices-ready), and the voices-loaded GUI handler checks `self._pdf.is_open` before enabling Play (handles PDF-opens-before-voices-ready). Missed a third spot on the first pass: `_stop()` unconditionally re-enables Play based on `self._pdf.is_open` alone, but `_stop()` runs at the top of `_open_pdf` while a *previous* document may still be open — opening a second PDF during the voice-load window would have re-enabled Play prematurely through that path. Any new boolean readiness flag gating a button must be threaded through every place that button's state is set, not just the "happy path" setter.

## Settled design decisions (do not re-litigate)

- ISSUE-016 (user decision 2026-06-12): SPEED slider changes apply IMMEDIATELY during playback via a 300ms-debounced restart of the current sentence (`_apply_speed_change` in app.py, debounce id `_speed_debounce_id`, cancelled in `_stop`). VOICE changes stay deferred to the next sentence — that is intentional design, and `test_voice_change_handler_is_pass` pins it.

## Test suite gotchas

- `tests/test_issue_validations.py` uses source-inspection assertions: substrings in comments can trip `assertNotIn` checks (e.g. a comment containing `self.after(` failed a test). Word comments carefully in fixed code.
- Many app tests build `DocumentReaderApp.__new__(...)` and hand-set only the fields the method under test touches. Adding a NEW instance field to app.py that an existing method (e.g. `_stop`) reads will break those helpers with a confusing `RecursionError` (uninitialized CTk `__getattr__` recurses on missing attributes), not AttributeError. After adding any instance field, grep the test file for `_make_app` helpers exercising the methods that read it and set the field there (hit this with `_speed_debounce_id` + TestIssue031's `_stop` test).
- The old 2 test ERRORs (`KeyError: 'src.tts_engine'` in TestIssue002) were fixed by the validator on 2026-06-12 (explicit import in setUp); as of ISSUE-016's fix (2026-06-12) the suite baseline is fully green at 149 tests.
- pytest is NOT installed (Python 3.14); run the suite with `python -m unittest tests.test_issue_validations`.
- `test_queue_command_format` / ISSUE-013 tests pin the pyttsx3 queue tuple `("speak", text, voice_id, rate_wpm, on_done)` — extend behavior via closures over on_done, not by changing the tuple.
- Added `_voices_ready` (ISSUE-033) to the growing list of instance fields `_stop()` reads: `tests/test_issue_validations.py`'s `TestIssue016ImmediateSpeedApply.test_stop_cancels_pending_speed_debounce` and `TestIssue031OnlineResumeReadvance._make_app` both build the app via `__new__` and call the real `_stop()` — both needed `app._voices_ready = True` added. Grep the test file for every `_make_app`/`__new__` fixture that calls a real (non-mocked) `_stop`/`_open_pdf`/`_play` before adding a field those methods read.

## Known follow-up (not yet filed as an issue)

Online pause mid-audio + resume may re-read the just-finished sentence: `_pause` rewinds `_sentence_idx` (ISSUE-007), online resume continues MCI audio, and the natural `on_done` then reads the rewound index again. Out of scope for the 2026-06-11 batch (ISSUE-017–026); worth flagging to bug-detective.
