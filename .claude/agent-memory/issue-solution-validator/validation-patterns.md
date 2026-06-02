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

## Validation Methodology That Works Well
1. Run `python -m unittest tests/test_issue_validations.py -v 2>&1` for full output
2. For ERRORs: check if they are test infrastructure bugs (KeyError on sys.modules) vs real failures
3. For FAILs: check if the assertion is too prescriptive (requires exact implementation choice) vs genuinely wrong behavior
4. Supplement with direct Python inspection: `python -c "from src.X import Y; import inspect; print(inspect.getsource(Y.method))"` 
5. For logic correctness (clamping, arithmetic), write explicit equivalence test cases in the analysis

## Status Values
- `VALIDATED ✅` — fix confirmed correct, issue resolved
- `PARTIAL ⚠️` — known remaining gap documented, partial fix is correct
- `OPEN` — fix is wrong or incomplete, needs another fix attempt
