---
name: logging-convention
description: How diagnostic logging is configured and used in DocumentReader
metadata:
  type: project
---

Logging convention established for DocumentReader:

- Root logging configured in `main.py` via `_setup_logging()`, called BEFORE importing `src.app` so import-time errors are captured.
- Outputs to stderr AND `~/documentreader.log`. Level via env var `DOCREADER_LOGLEVEL` (default DEBUG).
- Format includes `%(threadName)s` — deliberately, because most bugs here are threading-related; thread name in logs is how you tell GUI-thread calls from monitor/synth-thread calls apart.
- Each module uses `log = logging.getLogger(__name__)` at module top.
- Levels: DEBUG = per-sentence/per-MCI state, INFO = significant events (open PDF, play/stop, page advance, voice counts), WARNING = recoverable anomalies (no voices, out-of-range bookmark, length read failed), ERROR/`log.exception` = failures (replaced the old bare `print()` calls in tts_engine).

**Why:** The user (issues are documentation-only; do not fix logic) needs runtime visibility into threading/lifecycle bugs that are invisible in static reads.

**How to apply:** When adding new diagnostics, follow this module-logger + threadName-in-format pattern. Never use `print`. Never add logging that changes control flow or has side effects. Remove stale logs when the related issue closes.
