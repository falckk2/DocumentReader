---
name: validation-patterns
description: Recurring patterns, common false positives, and test writing notes from validation sessions
metadata:
  type: project
---

## Common Fix Patterns in This Codebase (2026-06-02 session)
- Threading fixes use `threading.Lock` and `threading.Event` — always check flag reads are also under the lock, not just writes
- Thread-safe Tk calls: `event_generate(..., when="tail")` is the only safe call from non-GUI threads; `self.after()` is not safe from background threads
- pyttsx3 COM safety: must stay on one thread — fixed with a dedicated `pyttsx3-worker` queue/thread pattern
- MCI audio: all `mciSendStringW` calls routed through a single `_mci_worker` thread for COM apartment safety
- Temp file races: solved with a lock + per-file cleanup tied to playback completion (`_done_and_cleanup` wrapper)

## False Positives Encountered

### ISSUE-003: `self.after(` in comment
Test `test_uses_event_generate_not_after` fails because it does a substring search of the full method source (including comments). The string `self.after(` appears only in a comment: `# self.after() here — after() is not thread-safe.`. Fix is correct; test assertion is too broad.

### ISSUE-009: `min()` vs `if/max` equivalence
Test `test_restore_bookmark_clamps_sentence_idx` checks for `'min('` in source. The fix uses `max_idx = max(0, len(self._sentences) - 1); if sentence_idx > max_idx: sentence_idx = max_idx` which is functionally identical to `min(sentence_idx, max(0, ...))`. Test assertion is overly prescriptive about implementation style.

## Test Design Guidelines Learned
- When testing "method X does NOT call Y", check that Y is not in the *executable* code, not just the source string (comments can cause false positives)
- When testing "implementation uses function F", prefer checking for behavior/equivalence rather than requiring a specific function name
- For `sys.modules["module.name"]` access, always ensure the module is explicitly imported in the test or in `setUp()` — alphabetical test ordering can bite you
- `inspect.getsource()` on a nested function (like `_monitor` inside `play()`) is found by getting source of the enclosing method — works because the monitor is a closure defined inline
- Lock-serialization tests: to prove "bump cannot land mid-handoff", block inside the handed-off call (fake `play()` waiting on an Event) and assert a concurrent `_bump_generation()` does NOT complete within 0.5s, then release and assert it does. Deterministic, no sleeps-as-synchronization.
- Stale-monitor tests: stall the monitor inside a fake `_mci_query` keyed on `threading.current_thread()` identity so only the OLD monitor blocks; deliberately let the 2s join time out (costs ~2s per join — budget ~4.5s for the full scenario). The monitor calls module-global `_mci_query` at call time, so reassigning `ap._mci_query` works.

## Validation Methodology That Works Well
1. Run `python -m unittest tests.test_issue_validations -v` for full output (pytest NOT installed)
2. For ERRORs: check if they are test infrastructure bugs (KeyError on sys.modules) vs real failures
3. For FAILs: check if the assertion is too prescriptive (requires exact implementation choice) vs genuinely wrong behavior
4. Supplement with direct Python inspection: `python -c "from src.X import Y; import inspect; print(inspect.getsource(Y.method))"`
5. For logic correctness (clamping, arithmetic), write explicit equivalence test cases in the analysis

## Recurring Defect Families (2026-06-12 sessions)
- **Set-then-cleared shared Event** = resurrection bug. Eliminated from BOTH TTSEngine (gen token, ISSUE-017) and AudioPlayer (fresh per-playback Event + closure-captured on_done, ISSUE-028). Pattern is now extinct in the codebase; if new playback/cancel code appears, grep for `.clear()` on shared events.
- **Check-then-act residuals**: closed in `_speak_online` by holding `_gen_lock` across check AND `_player.play()` (ISSUE-027). Key enabler: `play()` must never fire on_done inline (failure path dispatches detached), or holding the lock would deadlock against a GUI-side `stop()`.
- **The rewound `_sentence_idx` is a trap**: `_pause` rewinds it (ISSUE-007) assuming the sentence will be RE-spoken on resume. True for offline/mid-synth/bookmarks; FALSE for online mid-audio resume — closed by ISSUE-031 (validated): `_play`'s resume else-branch re-advances the index when `_tts.is_playing` is True after `resume()`. The branch selector is `is_playing` itself, and `_gen_lock` guarantees only two pause-vs-synth orderings (discard-before-play → False → re-read; bump-after-play → True → re-advance), so the mid-synth case can never take the re-advance path. When validating anything touching pause/resume, write out the index value at each step of all three flows (mid-audio, mid-synth, offline).
- **`on_close` is the chronic miss site for bookmark semantics**: ISSUE-020, ISSUE-030. Now gated on `_reading or _paused`; that gate is correct because `_stop(completed=True)` runs synchronously on the GUI thread before `on_close` can.
- **Recovery paths need guarding too**: ISSUE-026's `except` handler's own `result_q.put` could raise (fixed in ISSUE-029 with isinstance guard + guarded recovery put). When a fix adds an error handler, check whether the handler itself can throw.
- **Detached on-done-dispatch threads are not retroactively cancellable**: once the monitor (or play()'s failure path) hands on_done to a dispatch thread, a Stop+Play landing in that sub-ms window could advance the new read. Bounded only by app-level `_reading` re-check. Consciously accepted residual of the ISSUE-022 design — noted in ISSUE-027 validation, NOT filed (no enlargeable window, humanly unreachable). Don't re-file it.

## Validating Multi-Issue Fix Batches (what worked 2026-06-12)
- Trace *interactions* between fixes, not just each fix in isolation. Writing out the full event sequence for each user flow (pause→resume, stop→play, finish→close→reopen) is what surfaced ISSUE-030 and ISSUE-031.
- When a fixer REJECTS part of a fix suggestion with a stated rationale, verify the rationale by tracing the path it claims to protect. The ISSUE-027 rejection (no gen re-check in `_done_and_cleanup`) is correct: pause() bumps the gen, and the resumed track's natural on_done is the only thing keeping the sentence pump alive — a re-check would halt reading after every online pause/resume. The trace also exposed ISSUE-031 (orthogonal duplicate-read).
- `play()` is only ever called from a synth thread holding `TTSEngine._gen_lock`, and `TTSEngine.stop()` bumps under that lock before `player.stop()` — so play/stop cannot interleave mid-registration. Use this when reasoning about AudioPlayer races reachable in practice.
- pyttsx3 callbacks (`started-word`) are invoked on the thread running `runAndWait` — an in-worker `engine.stop()` from the callback preserves the single-owner COM constraint (ISSUE-013) by construction.
- Python ≥3.11: `asyncio.TimeoutError is TimeoutError` (builtin, an Exception subclass), so `except Exception` catches `asyncio.wait_for` timeouts.
- Lock-free reads of an int token in CPython are fine (GIL-atomic) as long as all writes are serialized under a lock — don't fail a fix for not locking reads.
- Re-sorting issues.md: split on `\n---\n`, map sections by `## ISSUE-(\d+)`, reassemble in explicit desired order, assert same section count AND same sorted-line multiset before writing. Safe and fast.
- Re-validating a previously-PARTIAL issue: append a second `### Validation (re-validation after ISSUE-NNN)` section; never edit the original Validation.

## Status Values
- `VALIDATED ✅` — fix confirmed correct, issue resolved
- `PARTIAL ⚠️` — known remaining gap documented, partial fix is correct
- `OPEN` — fix is wrong or incomplete, needs another fix attempt
