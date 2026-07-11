# ReadAloud v2 — Build Blueprint

> **Audience:** an AI agent (or developer) building the successor to DocumentReader.
> **How to use this file:** read it top to bottom before writing any code. It contains
> the product goals, the stack decision (already made — do not relitigate it), the
> architecture, everything the prototype taught us, and the build plan. When this
> document says "v1", it means the prototype in `../DocumentReader` (this repo is a
> sibling of the new project: the new app lives one directory up from DocumentReader,
> e.g. `../readaloud/`, and refers back here as `../DocumentReader/`).

---

## 1. Product vision

A document reader that reads PDFs (later: EPUB, DOCX, web articles) aloud with
natural voices, sentence-level highlighting, and effortless resume — on **phones,
tablets, and desktops**, possibly web later. It must be **monetizable**: free tier
with on-device voices, paid subscription for premium neural voices and cross-device
sync.

v1 (`../DocumentReader`) proved the product loop end to end on Windows desktop:
open PDF → sentences highlighted as they're read → pause/resume/skip/speed →
position always remembered. v2 makes it cross-platform and commercial. v1 is a
**behavioral specification and lessons archive**, not a codebase to port — none of
its UI/audio/TTS layers are cross-platform.

## 2. Stack decision (final)

**Flutter (Dart), single codebase.** Targets in priority order: Android + iOS
(phones/tablets) → Windows + macOS desktop → web (last, optional).

Why Flutter over the alternatives considered:
- **Background audio is the make-or-break feature** — reading must continue with
  the screen locked. `just_audio` + `audio_service` solve this properly on iOS and
  Android. Web-wrapper approaches (Capacitor/PWA) fight iOS on exactly this.
- First-class mobile + genuinely usable desktop from one codebase.
- Mature ecosystem for every subsystem we need (see §5 package table).
- Runner-up was web-first React + Capacitor + Tauri; choose that only if the web
  channel ever becomes primary. Python (v1's language) has no credible commercial
  mobile path — that's why v2 is a rewrite, not a port.

**Repo layout:** new Flutter project in a sibling directory of `DocumentReader`
(suggested name `readaloud`). Keep the domain layer in pure Dart with no Flutter
imports so it is unit-testable and portable (see §5).

## 3. What to read in the v1 repo

| Path (relative to the new project) | What it gives you |
|---|---|
| `../DocumentReader/issues.md` | **The crown jewel.** 39 discovered→fixed→validated issues with root causes, fix rationale, and validation notes. This is the v2 test specification. |
| `../DocumentReader/tests/test_issue_validations.py` | 271 tests pinning v1 behavior — mine for edge cases and expected semantics. |
| `../DocumentReader/src/app.py` | The playback/bookmark/UI state machine — read the comments; every `ISSUE-NNN` comment marks a hard-won rule. |
| `../DocumentReader/src/tts_engine.py` | Generation-token cancellation pattern (§7.2) — port this design. |
| `../DocumentReader/src/pdf_reader.py` | Sentence-splitting rules and PDF edge cases. |
| `../DocumentReader/V2_BLUEPRINT.md` | This file. |

## 4. Feature inventory

### 4.1 v1 parity (all proven; must exist in v2)
- Open PDF; per-page text extraction; graceful handling of encrypted PDFs and
  per-page extraction failures (ISSUE-015, 021).
- Sentence-by-sentence reading with **synchronized highlight** of the current
  sentence (occurrence-aware — see ISSUE-005/032).
- Play / Pause / Resume / Stop with exact-position semantics (§7.3).
- Sentence skip back/ahead (works while reading AND while paused).
- Page navigation; optional auto-advance to next page (with bookmark
  clear/advance on completion — ISSUE-025).
- Speed 0.5x–5.0x, applied **immediately** mid-sentence via debounced restart
  (ISSUE-016), voice changes deferred to next sentence (deliberate UX decision).
- Voice catalog: on-device voices + cloud neural voices, loaded async with the
  Play control gated until voices are usable (ISSUE-038).
- **Bookmarks/resume**: every document's `(page, sentence index, last_opened)`
  persisted; auto-resume on open without prompting; autosave every ~15s while
  reading; save on pause/stop/close with the rewind rules of §7.3.
- Recent-documents list (sorted by `last_opened`, missing files filtered out).
- Keyboard shortcuts (desktop): Space play/pause, ←/→ sentence skip, PgUp/PgDn
  page nav. Map to media-key / lock-screen / notification controls on mobile via
  `audio_service`.
- Reading progress indicator ("sentence i of n").

### 4.2 New in v2 (product features)
- Library screen: imported documents with covers, progress %, sort by recency.
- Background playback with lock-screen / notification transport controls (mobile).
- EPUB support (second format, after PDF is solid).
- Cross-device sync of library + positions (premium, needs backend — defer to M4).
- Sleep timer; per-document speed memory. (Cheap, high perceived value.)

### 4.3 Explicit non-goals for v2.0
- No annotation/editing of documents.
- No OCR of scanned PDFs (detect "no text layer" and tell the user; OCR is a
  possible later premium feature).
- No social features.

## 5. Architecture

Three layers; dependency arrows point downward only.

```
┌────────────────────────────────────────────────┐
│ UI (Flutter widgets, per-platform adaptivity)  │
├────────────────────────────────────────────────┤
│ Application services (playback controller,     │
│ library manager, voice catalog, purchases)     │
├────────────────────────────────────────────────┤
│ Domain (PURE DART: document model, sentence    │
│ splitter, reading-position state machine,      │
│ bookmark store interface)  ← unit tests here   │
└────────────────────────────────────────────────┘
```

- The **domain layer must have zero Flutter imports**. The reading state machine
  (§7.3) is the heart of the app and must be testable without widgets or audio.
- TTS and audio sit behind interfaces: `TtsSynthesizer` (text→audio or direct
  utterance) and `AudioOutput`. On-device and cloud implementations are swappable;
  the state machine never knows which is active. v1's biggest bug source was
  leaking backend differences (pyttsx3 vs edge-tts) into app logic — see §7.

### Package choices (vet current versions at build time)
| Concern | Package | Note |
|---|---|---|
| PDF text extraction + render | `pdfrx` or Syncfusion PDF | Needs per-page **text with positions** for highlighting; verify text-position API before committing. |
| On-device TTS | `flutter_tts` | iOS AVSpeech / Android TextToSpeech / desktop voices. Free tier. |
| Cloud TTS | Azure Speech REST (official) | See §6 — do NOT use the edge-tts trick. |
| Audio playback | `just_audio` | Plays synthesized audio (cloud path). |
| Background audio + media controls | `audio_service` | Wraps the playback controller; lock-screen controls. |
| Local persistence | `drift` (SQLite) | Replaces v1's JSON file; §7.4 rules still apply. |
| State management | `riverpod` (or `bloc`) | Pick one, stay consistent. |
| Purchases | `purchases_flutter` (RevenueCat) | Subscriptions on both stores + trials. |
| Backend (M4, sync) | Supabase | Auth + Postgres + storage; cheap to start. |

## 6. TTS strategy and the legal landmine

- **v1 uses `edge-tts`, an unofficial client for Microsoft Edge's read-aloud
  endpoint. It must NOT ship in a commercial product** — ToS risk and the endpoint
  can break at any time. v2's cloud voices must use the official **Azure Speech**
  API (same neural voices, paid per character) or a competitor (Google, Amazon
  Polly, ElevenLabs).
- Economics drive the tiering: on-device voices cost nothing per use → free tier.
  Cloud neural TTS costs per character → premium subscription covers it. Cache
  synthesized audio per (document, sentence, voice, speed) to cut repeat cost —
  v1 synthesized per sentence per play with no cache.
- On-device TTS APIs speak directly (no audio file), cloud TTS returns audio you
  play yourself. **This asymmetry shaped half of v1's bugs** (pause/resume
  semantics differ — §7.3). Design the `TtsSynthesizer` interface around
  *utterances with completion callbacks and interruption support*, and document
  per-implementation capabilities (`canPauseMidUtterance`, etc.) so the state
  machine handles both honestly instead of pretending they're identical.

## 7. Lessons learned — design rules for v2

Everything below was paid for with a real bug in v1 (issue numbers refer to
`../DocumentReader/issues.md`). These are requirements, not suggestions.

### 7.1 Concurrency and thread boundaries
- **All UI mutation happens on the UI thread, period.** v1 hit the same violation
  twice (ISSUE-003, 037) because the rule wasn't enforced structurally. In Dart
  this is easier (single isolate for UI), but completion callbacks from audio/TTS
  plugins can arrive on platform threads — marshal through streams/futures into
  the main isolate before touching state that widgets read.
- **Async completion callbacks must never throw silently** (ISSUE-014, 034):
  every callback path either completes the operation or surfaces an error state
  the UI shows. A load that fails must still *complete* ("error" result), never
  hang a spinner forever.
- **Never block the UI waiting on a worker** (ISSUE-001, 022, 026, 029): no
  joins/waits with the UI held. Workers that can die need supervision — a dead
  audio backend must fail requests fast, not hang them.
- **Protect shared mutable state** (ISSUE-010, 036): in v2 prefer confining state
  to the state machine rather than locking, but any cross-isolate/plugin state
  needs explicit ownership.

### 7.2 Cancellation: generation tokens (port this pattern)
v1's single most important pattern (ISSUE-017, 019, 027, 028): every utterance
captures a **monotonically increasing generation number** at start. `stop()`,
`pause()`, and any newer `speak()` bump the counter, permanently invalidating all
in-flight work. Completion callbacks check "is my generation still current?"
before advancing the reading loop. Do **not** use shared boolean/event flags that
get cleared for the next utterance — a cancelled utterance can be "resurrected"
by the clear (ISSUE-017), or complete in the window between check and playback
handoff (ISSUE-027 — the check and the handoff must be atomic).

### 7.3 The reading state machine (exact semantics, hardest-won knowledge)
The reading loop pointer (`sentenceIdx`) is **post-incremented** when a sentence
is dispatched, so "the sentence being read" = `idx - 1`. Every rule below exists
because getting one of them wrong shipped a real bug:

- **Pause** rewinds the pointer to the interrupted sentence and saves the
  bookmark there (ISSUE-007).
- **Resume** splits by capability: if the audio backend actually paused mid-audio
  (cloud path), resume the audio AND re-advance the pointer past the resumed
  sentence so its natural completion continues with the *next* one (ISSUE-031,
  clamped at page end); if the backend cannot pause (on-device path), re-speak
  the rewound sentence from its start (ISSUE-006). Pause during in-flight
  synthesis must cancel the synthesis result (generation bump), or audio starts
  playing while the app shows "paused" (ISSUE-019).
- **Stop** while actively reading rewinds one before saving; while paused it
  saves the already-rewound index without rewinding again (no double rewind).
- **Close** mid-reading applies the same rewind (ISSUE-020); close while idle
  saves **nothing** — an unconditional save clobbers the Stop-saved position and
  resurrects bookmarks cleared on completion (ISSUE-030).
- **Completion**: finishing a page bookmarks the start of the NEXT page;
  finishing the document CLEARS the bookmark — otherwise reopening offers to
  resume at the already-read last sentence (ISSUE-025).
- **Mid-sentence changes**: speed changes restart the current sentence at the new
  speed, debounced ~300ms because sliders fire continuously (ISSUE-016). The
  restart rewinds one and re-dispatches; the stale utterance's completion is
  suppressed by its generation token. Both orderings of (restart, stale
  completion event) must be benign — v1 validated both.
- **Sentence skip** (v2 feature, built on the same rules): while reading, target
  = `idx - 1 + delta`, clamp at 0, refuse past page end; while paused, move the
  already-rewound pointer and **drop any paused audio** so resume re-reads from
  the new position instead of resuming stale audio.
- **Readiness gating** (ISSUE-038): the Play affordance stays disabled until the
  voice catalog is actually usable, in BOTH orderings (document-then-voices and
  voices-then-document), and every code path that re-enables Play must check
  readiness (v1 missed the Stop path).

### 7.4 Persistence rules
- **Validate everything read from disk** (ISSUE-009, 024): type-check, clamp
  indices at both ends (a negative index silently indexes from the end in some
  languages), treat any malformed entry as "no bookmark". Never let corrupt
  persisted data throw inside a UI callback.
- **Writes are atomic and serialized** (ISSUE-036): temp-file + atomic rename (or
  SQLite transactions, which drift gives for free), and read-modify-write cycles
  serialized so concurrent save paths (autosave timer vs pause vs close) can't
  lose data.
- **Merge, don't clobber**: position saves and metadata stamps (`last_opened`)
  update the same record from different paths — writes must merge fields.
- Autosave every ~15s while actively reading (not while paused — pause already
  saved a better position).

### 7.5 Document processing
- Sentence splitting: split on `[.!?]` + whitespace AND at end-of-text (the last
  sentence on a page has no trailing whitespace — ISSUE-033). Extract each page's
  text **once** and derive display text + sentences from the single extraction
  (ISSUE-039). Expect malformed pages (per-page try/catch, ISSUE-015) and
  encrypted documents (clear user-facing error, ISSUE-015/021 — a failed open
  must not destroy the currently-open document).
- Highlighting: locate the current sentence by **occurrence tracking** (search
  forward from where the last highlight ended, reset on page change), never
  "first match of a prefix" — repeated phrases and shared 40-char prefixes both
  produced wrong highlights (ISSUE-005, 032). In v2, prefer highlighting by
  **character offsets** computed at split time instead of text search — the
  splitter should emit `(text, startOffset, endOffset)` per sentence, which
  eliminates this bug class entirely.
- Speed→rate mapping is clamped per backend at the edges (ISSUE-012): Azure rate
  −50%..+400% for 0.5x..5x; on-device rate parameter per platform docs.
- Use event-driven end-of-utterance callbacks, never position polling
  (ISSUE-011 — polling adds audible latency between sentences).

### 7.6 Resource lifecycle
- Synthesized-audio temp files/cache entries are owned by their utterance and
  cleaned up only after playback fully stops (ISSUE-002); full teardown of the
  audio backend on app exit (ISSUE-035).
- Timers/subscriptions that reschedule themselves must reschedule even when an
  iteration fails (v1's autosave uses try/finally).

### 7.7 Process lessons (how to work)
- **The issues.md workflow was the single best process decision in v1.** Keep it:
  a running `issues.md` where discovery, fix, and validation are separate
  sections with explicit status transitions (OPEN → FIXED → VALIDATED), and
  validation is done by a *separate* pass that writes tests before flipping the
  status. 39/39 issues got real regression tests this way.
- Write behavior tests against the domain layer as bugs are found; v1 ended with
  271 tests and they repeatedly caught regressions during later fixes.
- Source-string assertions in tests must strip comments first (three separate
  false positives in v1 from assertions matching explanatory comments).
- If multiple agents/contributors work in parallel: **fetch/rebase before
  numbering new issues** (a numbering collision happened in v1), and validate any
  merged work that arrived without tests.

## 8. UX and visual guidance

What v1 got right (keep): dark theme with a calm palette, document text dominates
the window, bold accent-colored highlight on the current sentence (very easy to
follow), grouped transport controls, compact status line ("Reading sentence 3 of
7"), auto-resume with a status note instead of a modal prompt.

Known v1 visual gaps (fix from day one in v2):
1. Empty state was a blank void — v2's first screen is the Library, and its empty
   state needs a clear "Add your first document" call to action.
2. Disabled controls must visually recede (v1's disabled Stop stayed bright pink).
3. Use a real icon set, not emoji/unicode glyphs subject to font fallback.
4. Constrain reading-text line length on wide screens (~70ch max) for readability.
5. Modal prompts are a last resort; prefer status text + undoable actions.

Mobile-specific: transport controls belong on the lock screen/notification
(`audio_service`); in-app, thumb-reachable bottom bar with Play/Pause, skip
sentence, speed. Tablets/desktop get the keyboard shortcuts from §4.1.

## 9. Monetization plan

- **Free tier**: on-device voices, full reader features, limited library size
  (e.g. 3 documents) — the reader must be genuinely useful free, or reviews sink it.
- **Premium subscription** (monthly/annual via RevenueCat, both stores + trial):
  - Neural cloud voices (the audible "wow" difference; covers the per-character cost).
  - Unlimited library.
  - Cross-device sync of library + positions (M4).
  - Later candidates: OCR for scanned PDFs, higher speed limits, voice cloning.
- Desktop/web can share the subscription via account login (Supabase auth) once
  the backend exists; until then desktop is a free companion (acquisition
  channel), not a revenue blocker.
- Instrument from day one: track activation (first document read aloud), D7
  retention, paywall views → trial starts → conversions.

## 10. Build plan (milestones)

**M0 — Scaffold (small):** Flutter project in the sibling directory; CI running
`flutter test` + `flutter analyze`; layer skeleton from §5; `issues.md` +
`V2_NOTES.md` conventions from §7.7 in place.

**M1 — Domain core (the real foundation):** pure-Dart document model, sentence
splitter (with offsets), reading state machine, bookmark store — built
test-first by porting §7.3/§7.4/§7.5 and the applicable cases from
`../DocumentReader/issues.md` into unit tests. **Definition of done: every
lesson in §7 that applies to the domain layer has a named test.**

**M2 — Single-platform MVP (Android first):** PDF import + render, on-device
TTS, sentence highlighting by offsets, transport UI, bookmarks/auto-resume/
autosave, background playback with notification controls. Ship to closed testing.

**M3 — Premium voices + paywall:** Azure Speech integration behind the
`TtsSynthesizer` interface, per-sentence audio cache, RevenueCat subscription,
free/premium gating. iOS build + TestFlight (background audio entitlements).

**M4 — Sync + desktop:** Supabase auth + library/position sync (positions are
last-write-wins per document with timestamps); Windows/macOS builds with
keyboard shortcuts; EPUB support.

**M5 — Web (optional, only if demanded):** evaluate Flutter web vs a separate
thin web reader; do not let it block mobile.

Do the milestones in order; each one leaves a shippable increment.

## 11. First actions for the building agent

1. Read this file fully; skim `../DocumentReader/issues.md` end to end.
2. Create the Flutter project in the sibling directory (`flutter create readaloud`
   or agreed name), commit the scaffold.
3. Start M1: write the reading-state-machine tests from §7.3 BEFORE the
   implementation — they are the spec.
4. Keep your own `issues.md` from day one using the v1 conventions.
5. When a v1 behavior is ambiguous, the answer is in `../DocumentReader/src/app.py`'s
   ISSUE-comments or the validation notes in `../DocumentReader/issues.md` — check
   there before guessing.
