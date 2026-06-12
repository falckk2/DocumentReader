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

**Cancellation via generation token, never set-then-clear a shared Event:** `speak()` doing `stop_event.set()` then `clear()` resurrects in-flight utterances that check the event after the clear (ISSUE-017). The validated pattern: `_generation` int + `_gen_lock` in TTSEngine; `stop()`/`pause()` bump it; each utterance captures `gen` at start and checks `gen == self._generation` before playing (online) / in a gated on_done closure (offline). Closures gate on_done without changing the pyttsx3 queue tuple format (a test asserts on it).

**pyttsx3 COM threading:** pyttsx3 (SAPI5) engine must live on a single dedicated thread (ISSUE-013). To interrupt mid-`runAndWait`, connect a `started-word` callback that checks a `threading.Event` and calls `engine.stop()` — the callback runs ON the worker thread inside runAndWait, so it is COM-safe (ISSUE-018).

**Post-increment index bias:** `_sentence_idx` is incremented before speech fires. Every bookmark-save site (`_pause`, `_stop`, `on_close`) must rewind by 1 when actively reading — ISSUE-007 fixed two sites, ISSUE-020 caught the missed third. Exception: natural completion has no interrupted sentence; `_stop(completed=True)` skips the rewind and `_on_page_done` clears/advances the bookmark instead (ISSUE-025).

**Persisted external data is untrusted:** the bookmarks JSON needed dict-root check, int type checks, and both-end clamping (ISSUE-009 only clamped the upper end; ISSUE-024 added the rest). Negative indices silently read from the page END via Python negative indexing — no exception, just bizarre behavior.

**Open-then-swap for fallible resource replacement:** PDFReader.open closed the old doc before `fitz.open(path)` could fail, leaving a closed-doc reference (ISSUE-021). Open into a local, validate, then swap.

**Unbounded waits:** every cross-thread `Queue.get()` whose caller can be the GUI thread needs a timeout + error fallback, and the worker must always answer the caller even on failure (ISSUE-026). Network ops on the sentence-advance critical path need `asyncio.wait_for` (ISSUE-023) — liveness depends on on_done always eventually firing.

**Why:** All these stem from the async/multi-thread architecture: GUI thread, MCI monitor thread, edge-tts daemon thread, pyttsx3-worker thread, on-done-dispatch threads, and VoiceManager background thread all interact.

**How to apply:** When adding any new callback or inter-thread communication path, verify which thread it runs on before touching shared state or Tk widgets; gate any deferred callback on the current generation; never let the GUI thread wait unboundedly on another thread.

## Test suite gotchas

- `tests/test_issue_validations.py` uses source-inspection assertions: substrings in comments can trip `assertNotIn` checks (e.g. a comment containing `self.after(` failed a test). Word comments carefully in fixed code.
- 2 pre-existing test ERRORs (`KeyError: 'src.tts_engine'` in TestIssue002) are a test-infra defect (missing import before `sys.modules[...]` access) owned by the validator — not code defects; do not count them as regressions.
- `test_queue_command_format` / ISSUE-013 tests pin the pyttsx3 queue tuple `("speak", text, voice_id, rate_wpm, on_done)` — extend behavior via closures over on_done, not by changing the tuple.

## Known follow-up (not yet filed as an issue)

Online pause mid-audio + resume may re-read the just-finished sentence: `_pause` rewinds `_sentence_idx` (ISSUE-007), online resume continues MCI audio, and the natural `on_done` then reads the rewound index again. Out of scope for the 2026-06-11 batch (ISSUE-017–026); worth flagging to bug-detective.
