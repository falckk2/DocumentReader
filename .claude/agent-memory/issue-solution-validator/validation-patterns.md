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

### Recurring pattern: fix-explanation comments quote the OLD API/call they replaced
Seen 3x now (ISSUE-003, ISSUE-032, ISSUE-034). This codebase's fix comments explain the change by naming what was removed, e.g. `# this used to call self.after() directly` or `# instead of get_all_text() and get_sentences() each separately`. A raw `assertNotIn("self.after(", src)` / `assertNotIn("get_all_text(", src)` over `inspect.getsource()` output will false-positive on these comments. **Always write a `_strip_comments()` helper** (split each line on `#`, drop lines that are pure comments) and assert against the comment-stripped code, not the raw source, whenever the assertion is "old pattern X does NOT appear anymore." Assertions that check something DOES appear (`assertIn`) are safe to run against raw source since matching in a comment is not a real failure to catch.

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

## GUI-thread serialization reasoning (ISSUE-016, validated 2026-06-12)
- Tk after-timer callbacks vs queued `<<...>>` virtual events on the GUI thread have NO guaranteed relative order (Tcl checks timers first when both are due) — validate BOTH orderings explicitly. Best test pattern: `__new__`-built app with the REAL `_read_next_sentence` (mock only `_tts`/`_voices`), then call the two handlers in each order and assert the spoken-sentence sequence + final index.
- A "stale done" queued in Tk's event queue is beyond generation-bump suppression (the bump gates on_done BEFORE event_generate); app-level effects of an already-queued done must be argued benign, not assumed suppressed.
- Debounced-restart pattern (`after(300)` + cancel-on-tick + fire-time state re-check + cancel in `_stop`) is the validated shape for "apply control change to in-flight sentence". The fire-time guard set is `_reading / not _paused / idx>0 / sentences truthy`; `_stop` gives triple protection (cancel + flag + idx=0). Distinct after-id fields can't collide — Tk after ids are unique per scheduled callback.

## MCI Notify / Win32 Window Testing Patterns (ISSUE-011, validated 2026-06-12)
- Under tests `ctypes.WinDLL` is patched to a `MagicMock`, so all Win32 calls are mocks. `_ensure_notify_window` always returns 0 (fallback) — expected by design, not a regression.
- To test the notify path without a real window: `player._ensure_notify_window = MagicMock(return_value=0x9999)` and stub `ap._mci_notify = MagicMock(return_value=0)`. This drives the `hwnd != 0` branch in `play()`, installs `_notify_ctx`, and starts the watchdog thread.
- `_handle_mci_notify` and `_complete_playback` are directly testable without any window: manually install `_notify_ctx` and call the methods; no Win32 plumbing needed.
- `inspect.getsource()` fails on module-level functions that are patched to `MagicMock` at import time. Workaround: read the source file with `open(...)` and search the raw text.
- The `_wndclass_counter = itertools.count()` is module-global; calling `next(_wndclass_counter)` in a test advances it permanently — safe for the tests but be aware when asserting exact counter values.
- Watchdog behavior is testable by replacing `player._complete_playback` with a callable mock and controlling `_mci_query` return values; the watchdog exits when the mock sets `stop_event`.

## Notify Token Safety Analysis (ISSUE-011)
- Token races (notify + watchdog both fire simultaneously, stop + notify race, new play + queued old notify) are all safe because `_complete_playback` re-checks `ctx["token"] != token` under `_lock`. This is the ISSUE-028 per-playback pattern applied to the notify path.
- The only cross-lock gap: `_handle_mci_notify` reads token under lock, releases, queries MCI mode (up to 5s), then calls `_complete_playback(token)`. During that window stop+play could have cycled multiple times. Safe: `_complete_playback` re-checks under lock.
- `stop()` clears `_notify_ctx = None` under `_lock` as the FIRST action (before MCI stop/close). This means any concurrent `_handle_mci_notify` acquiring `_lock` after will see `ctx is None` and return. Any that acquired `_lock` BEFORE will have its token mismatch caught by `_complete_playback` after the new play() installs a higher token.

## Status Values
- `VALIDATED ✅` — fix confirmed correct, issue resolved
- `PARTIAL ⚠️` — known remaining gap documented, partial fix is correct
- `OPEN` — fix is wrong or incomplete, needs another fix attempt

## Validating an Externally-Authored Round of Fixes (2026-07-11 session, ISSUE-032..036)
- A different contributor's fixes may ship with their own provisional `### Validation` section (code-inspection-only, no test suite run) plus a self-aware `> 🔍 Agent Note` flagging that independent validation is still needed. Treat that provisional section as historical record — do not edit or delete it. Append a second, clearly-labeled `### Validation (independent — issue-solution-validator, DATE)` section below it with real tests.
- Systematically diff every claim in the `### Fix` prose against `inspect.getsource()` of the actual changed method, not just "does the bug still repro." Three real gaps found this way in one round (see project-context.md for specifics: a hard-cap described as a rare fallback, a claimed short-fragment filter that doesn't exist, "class-level" lock that's actually instance-level). None of the three invalidated the fix — the reported bug was genuinely closed in all three — but all three would have gone unnoticed with a "does it work" pass alone. Write a test that pins the CODE'S actual behavior (not the prose's claimed behavior); that's what future-proofs the discrepancy note.
- A `**Status**: OPEN` header does not mean the fix is missing — read the `### Fix` section first. One issue in this round had a complete, correct, in-code fix but an un-updated OPEN status field (author oversight). Promote straight to VALIDATED with a short attribution note pointing at the commit, rather than treating it as needing a fresh fix.
- When two independent contributor rounds mint colliding issue numbers (e.g. both use 032-034 for different bugs), expect the merge to have renumbered one round and left an explanatory blockquote at the top of each renumbered Discovery section (`> Note: originally logged as ISSUE-0XX; renumbered to ISSUE-0YY...`). Don't "fix" the numbering — it's already resolved; just validate under the current numbers.
- Relocating a validated entry into the VALIDATED group when it's a multi-KB block with nested `###` subsections: safest to (1) insert the fully-rewritten new block (old content + new Validation section) at the target seam via one Edit with generous unique context on both sides, then (2) delete the old block separately. If the old block is large/awkward to match as one Edit `old_string` (e.g. its start got orphaned mid-file after an Edit collision), fall back to a small Python script using a unique marker string inserted via Edit, then `content[:content.index(marker)]` truncation via Bash/PowerShell — faster and less error-prone than hand-crafting a giant old_string for Edit.
- Lock-serialization test for a *file-write* critical section (not just an in-memory one): replace the target's `_load_bookmarks`-equivalent method with a plain function that sets an "entered" `Event`, blocks on a "release" `Event`, then returns `{}`; start caller 1 on a thread, wait for "entered", start caller 2 on a second thread, assert caller 2's completion `Event` is NOT set within ~0.3s, then release and assert both complete. Same shape as the AudioPlayer generation-bump lock test but applied to `threading.Lock()`-guarded file I/O.
- Atomic-write test for `tempfile.mkstemp` + `os.replace` patterns: patch the serialization call itself (e.g. `patch.object(app_mod.json, "dump", side_effect=OSError(...))`, not a generic `Exception`, since realistic disk-full/permission failures surface as `OSError` and that's usually the only exception type the code's outer handler actually catches) and assert (a) the original destination file is byte-for-byte unchanged, and (b) no `prefix*.tmp`-pattern file is left in the directory (`os.listdir` filter). This proves both "no corruption" and "no leak" in one test.
