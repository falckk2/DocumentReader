import json
import logging
import os
import tempfile
import threading
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb

import customtkinter as ctk

from src.pdf_reader import PDFReader
from src.tts_engine import TTSEngine
from src.voice_manager import VoiceManager

log = logging.getLogger(__name__)

_BOOKMARKS_FILE = os.path.join(os.path.expanduser("~"), ".documentreader_bookmarks.json")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class DocumentReaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("DocumentReader")
        self.geometry("1000x700")
        self.minsize(800, 550)

        self._pdf = PDFReader()
        self._tts = TTSEngine()
        self._voices = VoiceManager()

        self._current_page = 0
        self._sentences: list[str] = []
        self._sentence_idx = 0
        self._reading = False
        self._paused = False
        self._pending_after_id = None
        self._current_pdf_path: str | None = None

        # ISSUE-016 fix: debounce timer id for applying speed changes
        # mid-sentence.  Kept separate from _pending_after_id (which belongs
        # to the sentence pump's page-done scheduling) so the two can never
        # cancel each other's callbacks.
        self._speed_debounce_id = None

        # ISSUE-036 fix: serialize bookmark read-modify-write cycles
        # so rapid pause-then-stop (or concurrent saves) never lose data.
        self._bookmark_lock = threading.Lock()

        # ISSUE-003 fix: used by _on_sentence_done (background thread) to
        # marshal "sentence done" notifications safely to the GUI thread via
        # event_generate, which is the only thread-safe Tk call from non-GUI
        # threads.
        self._sentence_done_event = "<<SentenceDone>>"

        # ISSUE-037 fix: same event_generate pattern for the VoiceManager
        # background thread's load-complete callback. _voices_load_result
        # is written on the background thread and read once on the GUI
        # thread by the bound handler, so it never needs a lock: the event
        # is only ever generated after the value is fully assigned, and
        # event_generate(when="tail") queues the handler after that write
        # happens-before it on the same thread.
        self._voices_loaded_event = "<<VoicesLoaded>>"
        self._voices_load_result = None

        # ISSUE-038 fix: Play must stay disabled until voice discovery has
        # actually finished, even if a PDF is already open.
        self._voices_ready = False

        # ISSUE-005 fix: track the text-widget index where the last highlight
        # ended so the next search starts there, avoiding mis-highlighting
        # repeated phrases that appear earlier on the page.
        self._highlight_search_start = "1.0"

        self._build_ui()
        # ISSUE-003 fix: bind the virtual event on the GUI thread after the
        # window exists so event_generate from background threads is safe.
        self.bind(self._sentence_done_event, self._on_sentence_done_event)
        # ISSUE-037 fix: same reasoning for the voice-load-complete event.
        self.bind(self._voices_loaded_event, self._on_voices_loaded_event)
        self._load_voices()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── Top bar ────────────────────────────────────────────────────
        top = ctk.CTkFrame(self, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(top, text="Open PDF", width=110, command=self._open_pdf).grid(
            row=0, column=0, padx=12, pady=8
        )

        self._title_label = ctk.CTkLabel(
            top, text="No document loaded", font=ctk.CTkFont(size=14, weight="bold")
        )
        self._title_label.grid(row=0, column=1, padx=8, pady=8, sticky="w")

        self._status_label = ctk.CTkLabel(top, text="", text_color="gray")
        self._status_label.grid(row=0, column=2, padx=12, pady=8, sticky="e")

        # ── Main content area ───────────────────────────────────────────
        content = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        # Text display
        text_frame = ctk.CTkFrame(content)
        text_frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 4))
        text_frame.grid_columnconfigure(0, weight=1)
        text_frame.grid_rowconfigure(0, weight=1)

        self._text_box = tk.Text(
            text_frame,
            wrap="word",
            font=("Segoe UI", 13),
            bg="#1e1e2e",
            fg="#cdd6f4",
            insertbackground="#cdd6f4",
            selectbackground="#45475a",
            relief="flat",
            padx=16,
            pady=12,
            state="disabled",
        )
        self._text_box.tag_configure(
            "highlight", background="#313244", foreground="#89b4fa", font=("Segoe UI", 13, "bold")
        )
        self._text_box.grid(row=0, column=0, sticky="nsew")

        scrollbar = ctk.CTkScrollbar(text_frame, command=self._text_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._text_box.configure(yscrollcommand=scrollbar.set)

        # Page navigation
        nav = ctk.CTkFrame(content, fg_color="transparent")
        nav.grid(row=1, column=0, pady=(0, 4))

        self._prev_btn = ctk.CTkButton(nav, text="◀", width=40, command=self._prev_page, state="disabled")
        self._prev_btn.grid(row=0, column=0, padx=4)

        self._page_label = ctk.CTkLabel(nav, text="—", width=120)
        self._page_label.grid(row=0, column=1, padx=8)

        self._next_btn = ctk.CTkButton(nav, text="▶", width=40, command=self._next_page, state="disabled")
        self._next_btn.grid(row=0, column=2, padx=4)

        # ── Bottom control bar ──────────────────────────────────────────
        bottom = ctk.CTkFrame(self, corner_radius=0)
        bottom.grid(row=2, column=0, sticky="ew", padx=0, pady=0)
        bottom.grid_columnconfigure(1, weight=1)

        # Voice selector
        voice_row = ctk.CTkFrame(bottom, fg_color="transparent")
        voice_row.grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")

        ctk.CTkLabel(voice_row, text="Voice:").grid(row=0, column=0, padx=(0, 6))
        self._voice_var = tk.StringVar(value="Loading voices…")
        self._voice_menu = ctk.CTkOptionMenu(
            voice_row,
            variable=self._voice_var,
            values=["Loading voices…"],
            width=260,
            command=self._on_voice_change,
        )
        self._voice_menu.grid(row=0, column=1)

        # Speed slider
        speed_row = ctk.CTkFrame(bottom, fg_color="transparent")
        speed_row.grid(row=0, column=1, padx=12, pady=(8, 2))

        ctk.CTkLabel(speed_row, text="Speed:").grid(row=0, column=0, padx=(0, 6))
        self._speed_var = tk.DoubleVar(value=1.0)
        self._speed_slider = ctk.CTkSlider(
            speed_row, from_=0.5, to=2.0, variable=self._speed_var,
            width=160, command=self._on_speed_change,
        )
        self._speed_slider.grid(row=0, column=1)
        self._speed_display = ctk.CTkLabel(speed_row, text="1.0x", width=44)
        self._speed_display.grid(row=0, column=2, padx=(6, 0))

        # Playback buttons
        btn_row = ctk.CTkFrame(bottom, fg_color="transparent")
        btn_row.grid(row=1, column=0, columnspan=2, pady=(4, 10))

        self._play_btn = ctk.CTkButton(
            btn_row, text="▶  Play", width=110, command=self._play, state="disabled"
        )
        self._play_btn.grid(row=0, column=0, padx=8)

        self._pause_btn = ctk.CTkButton(
            btn_row, text="⏸  Pause", width=110, command=self._pause, state="disabled",
            fg_color="#45475a", hover_color="#585b70",
        )
        self._pause_btn.grid(row=0, column=1, padx=8)

        self._stop_btn = ctk.CTkButton(
            btn_row, text="⏹  Stop", width=110, command=self._stop, state="disabled",
            fg_color="#f38ba8", hover_color="#eba0ac", text_color="#1e1e2e",
        )
        self._stop_btn.grid(row=0, column=2, padx=8)

        self._auto_advance_var = tk.BooleanVar(value=False)
        self._auto_advance_cb = ctk.CTkCheckBox(
            btn_row, text="Auto-advance pages", variable=self._auto_advance_var,
        )
        self._auto_advance_cb.grid(row=0, column=3, padx=24)

    # ------------------------------------------------------------------
    # Voice loading
    # ------------------------------------------------------------------

    def _load_voices(self):
        self._set_status("Loading voices…")

        def on_done(voices):
            # NOTE: this callback runs on the VoiceManager background thread.
            # ISSUE-014 fix: wrap in try/except so failures here do not
            # silently leave the UI stuck on "Loading voices…" forever.
            # ISSUE-037 fix: this used to call self.after() directly from
            # this background thread, which is not thread-safe for Tk/
            # customtkinter. Instead, stash the outcome on an instance
            # attribute and marshal to the GUI thread with event_generate
            # (the only thread-safe Tk call from a non-GUI thread) — the
            # same pattern used by _on_sentence_done for ISSUE-003. The
            # actual widget updates happen in _on_voices_loaded_event on
            # the GUI thread.
            try:
                log.debug("Voice load callback fired with %d voices", len(voices))
                if not voices:
                    log.warning("No voices were discovered (offline + online both empty)")
                    self._voices_load_result = {"status": "No voices found"}
                else:
                    display = [str(v) for v in voices]
                    default = self._voices.get_default_voice()
                    default_str = str(default) if default else display[0]
                    self._voices_load_result = {
                        "display": display,
                        "default_str": default_str,
                        "status": f"{len(voices)} voices loaded",
                    }
            except Exception:
                log.exception("Unexpected error in voice-load callback; UI may be stuck")
                self._voices_load_result = {"status": "Error loading voices"}
            try:
                self.event_generate(self._voices_loaded_event, when="tail")
            except Exception:
                # Window may be closing; ignore
                pass

        self._voices.load(on_done=on_done)

    def _on_voices_loaded_event(self, _event=None):
        # Runs on the GUI thread in response to <<VoicesLoaded>>.
        result = self._voices_load_result
        if not result:
            return
        self._set_status(result.get("status", ""))
        if "display" in result:
            self._voice_menu.configure(values=result["display"])
            self._voice_var.set(result["default_str"])
            # ISSUE-038 fix: voices are now actually usable — enable Play,
            # but only if a PDF is already open (mirrors the gating in
            # _open_pdf for the reverse ordering).
            self._voices_ready = True
            if self._pdf.is_open:
                self._play_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # PDF handling
    # ------------------------------------------------------------------

    def _open_pdf(self):
        path = fd.askopenfilename(
            title="Open PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        self._stop()
        log.info("Opening PDF: %s", path)
        try:
            count = self._pdf.open(path)
        except Exception as e:
            log.exception("Failed to open PDF: %s", path)
            mb.showerror("Error", f"Could not open PDF:\n{e}")
            return

        log.info("PDF opened with %d page(s)", count)
        self._current_pdf_path = path
        self._current_page = 0
        self._title_label.configure(text=os.path.basename(path))
        self._set_status(f"{count} page(s)")
        self._update_page_display()
        self._update_nav_buttons()
        # ISSUE-038 fix: only enable Play once voice discovery has actually
        # finished. If a PDF opens before _load_voices' async callback
        # fires, the dropdown still holds the "Loading voices…" placeholder,
        # which is not a resolvable Voice — pressing Play would silently
        # fail with "No voice selected." and stop. _on_voices_loaded_event
        # enables Play (if a PDF is open) once voices actually arrive.
        if self._voices_ready:
            self._play_btn.configure(state="normal")
        self._restore_bookmark(path)

    def _update_page_display(self):
        # ISSUE-039 fix: extract page text once and derive sentences from
        # it, instead of get_all_text() and get_sentences() each separately
        # re-running PyMuPDF extraction for the same page.
        text, self._sentences = self._pdf.get_text_and_sentences(self._current_page)
        self._sentence_idx = 0
        # ISSUE-005 fix: reset search start for highlight tracking on new page.
        self._highlight_search_start = "1.0"

        self._text_box.configure(state="normal")
        self._text_box.delete("1.0", "end")
        self._text_box.insert("end", text if text else "(No text found on this page)")
        self._text_box.configure(state="disabled")

        total = self._pdf.page_count
        log.debug("Page display updated: page %d/%d, %d sentence(s), text_len=%d",
                  self._current_page + 1, total, len(self._sentences), len(text))
        self._page_label.configure(text=f"Page {self._current_page + 1} of {total}")

    def _update_nav_buttons(self):
        total = self._pdf.page_count
        self._prev_btn.configure(state="normal" if self._current_page > 0 else "disabled")
        self._next_btn.configure(state="normal" if self._current_page < total - 1 else "disabled")

    def _prev_page(self):
        if self._current_page > 0:
            self._stop()
            self._current_page -= 1
            self._update_page_display()
            self._update_nav_buttons()

    def _next_page(self):
        if self._current_page < self._pdf.page_count - 1:
            self._stop()
            self._current_page += 1
            self._update_page_display()
            self._update_nav_buttons()

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def _play(self):
        if not self._pdf.is_open:
            log.debug("_play ignored: no PDF open")
            return
        if self._paused:
            log.info("Resuming playback at sentence_idx=%d", self._sentence_idx)
            # ISSUE-006 fix: for online voices, resume the paused MCI player.
            # For offline voices there is no true pause (pyttsx3 was stopped),
            # so we re-read starting from _sentence_idx (which was rewound in
            # _pause to point at the interrupted sentence).
            self._tts.resume()
            self._paused = False
            self._play_btn.configure(state="disabled")
            self._pause_btn.configure(state="normal")
            self._stop_btn.configure(state="normal")
            # For offline, _player.is_paused is False after stop, so resume()
            # is a no-op; we fall through to _read_next_sentence to restart.
            if not self._tts.is_playing:
                self._reading = True
                self._read_next_sentence()
            else:
                # ISSUE-031 fix: the player actually resumed the paused MCI
                # track, so the interrupted sentence will finish from where it
                # left off.  The ISSUE-007 rewind in _pause has already served
                # its purpose (the pause-time bookmark write); re-advance the
                # index past the resumed sentence so the track's natural
                # on_done continues with the NEXT sentence instead of
                # re-reading the one the user just heard finish.
                if self._sentence_idx < len(self._sentences):
                    self._sentence_idx += 1
            return

        if not self._sentences:
            self._set_status("No text to read on this page.")
            return

        log.info("Starting playback: page %d, %d sentence(s) from idx=%d",
                 self._current_page + 1, len(self._sentences), self._sentence_idx)
        self._reading = True
        self._paused = False
        self._play_btn.configure(state="disabled")
        self._pause_btn.configure(state="normal")
        self._stop_btn.configure(state="normal")
        self._read_next_sentence()

    def _pause(self):
        if self._reading and not self._paused:
            # ISSUE-007 fix: _sentence_idx was already incremented before the
            # current sentence was sent to the TTS engine.  Rewind by one so
            # the bookmark and the offline-resume path both point at the
            # sentence that was actually interrupted.
            if self._sentence_idx > 0:
                self._sentence_idx -= 1
            log.info("Pausing playback at sentence_idx=%d (rewound)", self._sentence_idx)
            self._tts.pause()
            self._paused = True
            self._save_bookmark()
            self._play_btn.configure(state="normal", text="▶  Resume")
            self._pause_btn.configure(state="disabled")

    def _stop(self, completed: bool = False):
        log.info("Stop requested (reading=%s, paused=%s, idx=%d, completed=%s)",
                 self._reading, self._paused, self._sentence_idx, completed)
        # ISSUE-025 fix: on natural completion there is no interrupted
        # sentence — _on_page_done has already cleared or advanced the
        # bookmark — so skip the ISSUE-007 rewind-and-save entirely.
        if (self._reading or self._paused) and not completed:
            # ISSUE-007 fix: if actively reading (not paused), _sentence_idx
            # was post-incremented before speech; rewind to the interrupted
            # sentence so the bookmark restores correctly.
            if self._reading and not self._paused and self._sentence_idx > 0:
                self._sentence_idx -= 1
            self._save_bookmark()
        self._reading = False
        self._paused = False
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
            self._pending_after_id = None
        # ISSUE-016 fix: a debounced speed-change restart must never fire
        # after Stop (its guard would catch it anyway, but cancel eagerly).
        if self._speed_debounce_id is not None:
            self.after_cancel(self._speed_debounce_id)
            self._speed_debounce_id = None
        self._tts.stop()
        self._sentence_idx = 0
        self._clear_highlight()
        # ISSUE-038 fix: only re-enable Play if voices are actually usable —
        # _stop() runs at the start of _open_pdf() while a *previous* PDF
        # may still be open, so gating on self._pdf.is_open alone could
        # re-enable Play during the async voice-load window.
        self._play_btn.configure(
            state="normal" if (self._pdf.is_open and self._voices_ready) else "disabled",
            text="▶  Play",
        )
        self._pause_btn.configure(state="disabled")
        self._stop_btn.configure(state="disabled")

    def _read_next_sentence(self):
        if not self._reading or self._paused:
            log.debug("_read_next_sentence skipped (reading=%s, paused=%s)", self._reading, self._paused)
            return
        if self._sentence_idx >= len(self._sentences):
            # Page finished
            log.info("Page %d finished (idx=%d >= %d sentences)",
                     self._current_page + 1, self._sentence_idx, len(self._sentences))
            self._pending_after_id = self.after(0, self._on_page_done)
            return

        sentence = self._sentences[self._sentence_idx]
        self._highlight_sentence(sentence)

        display = self._voice_var.get()
        voice = self._voices.find_by_display(display)
        if voice is None:
            log.warning("No voice resolved for dropdown value %r; stopping", display)
            self._set_status("No voice selected.")
            self._stop()
            return

        speed = self._speed_var.get()
        log.debug("Speaking sentence idx=%d (len=%d) voice=%s source=%s speed=%.2f",
                  self._sentence_idx, len(sentence), voice.id, voice.source, speed)
        self._sentence_idx += 1
        self._tts.speak(sentence, voice, speed, on_done=self._on_sentence_done)

    def _on_sentence_done(self):
        # NOTE: invoked from a TTS/audio background thread, not the GUI thread.
        # ISSUE-003 fix: use event_generate (the only thread-safe Tk call from
        # a non-GUI thread) to marshal to the GUI thread.  Do NOT schedule
        # callbacks with after() here — it is not thread-safe.
        log.debug("Sentence done callback (reading=%s, paused=%s, next_idx=%d)",
                  self._reading, self._paused, self._sentence_idx)
        if self._reading and not self._paused:
            try:
                self.event_generate(self._sentence_done_event, when="tail")
            except Exception:
                # Window may be closing; ignore
                pass

    def _on_sentence_done_event(self, _event=None):
        # Runs on the GUI thread in response to <<SentenceDone>>.
        if self._reading and not self._paused:
            self._read_next_sentence()

    def _on_page_done(self):
        if self._auto_advance_var.get() and self._current_page < self._pdf.page_count - 1:
            log.info("Auto-advancing from page %d to %d", self._current_page + 1, self._current_page + 2)
            # ISSUE-004 fix: clear the previous page's highlight before loading
            # the next page so there is no stale highlight during the transition.
            self._clear_highlight()
            self._current_page += 1
            self._update_page_display()   # resets _sentence_idx = 0 and rebuilds _sentences
            self._update_nav_buttons()
            # _update_page_display already set _sentence_idx = 0; call
            # _read_next_sentence directly (we are already on the GUI thread).
            self._read_next_sentence()
        else:
            if self._current_page >= self._pdf.page_count - 1:
                self._set_status("Finished reading document.")
                # ISSUE-025 fix: the document was fully read — clear the
                # bookmark so the next open does not offer to resume at (and
                # re-read) the already-finished last sentence.
                self._clear_bookmark()
            else:
                self._set_status("Page done.")
                # ISSUE-025 fix: the page was fully read — bookmark the start
                # of the NEXT page instead of the already-read last sentence.
                self._save_bookmark(page=self._current_page + 1, sentence_idx=0)
            self._stop(completed=True)

    # ------------------------------------------------------------------
    # Highlighting
    # ------------------------------------------------------------------

    def _highlight_sentence(self, sentence: str):
        # ISSUE-005 fix: search forward from where the last highlight ended
        # so repeated phrases on the same page are highlighted in order.
        self._text_box.configure(state="normal")
        self._text_box.tag_remove("highlight", "1.0", "end")
        # ISSUE-032 fix: use the full sentence as the search key instead
        # of truncating to 40 chars, which caused false matches on sentences
        # sharing a common prefix longer than 40 characters.
        search_key = sentence[:200] if len(sentence) > 200 else sentence
        pos = self._text_box.search(
            search_key, self._highlight_search_start, stopindex="end", nocase=False
        )
        if not pos:
            # Wrap around (e.g. sentence_idx was rewound after pause/resume)
            pos = self._text_box.search(search_key, "1.0", stopindex="end", nocase=False)
        if pos:
            end = f"{pos}+{len(sentence)}c"
            self._text_box.tag_add("highlight", pos, end)
            self._text_box.see(pos)
            # Advance the search start past this occurrence for the next call
            self._highlight_search_start = end
        self._text_box.configure(state="disabled")

    def _clear_highlight(self):
        self._text_box.configure(state="normal")
        self._text_box.tag_remove("highlight", "1.0", "end")
        self._text_box.configure(state="disabled")
        # ISSUE-005 fix: reset search position when highlight is cleared.
        self._highlight_search_start = "1.0"

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_voice_change(self, _value):
        pass  # Voice is read at speak time

    def _on_speed_change(self, value):
        self._speed_display.configure(text=f"{value:.1f}x")
        # ISSUE-016 fix: speed changes apply IMMEDIATELY during playback by
        # re-speaking the current sentence at the new speed (user decision
        # 2026-06-12; voice changes stay deferred to the next sentence by
        # design).  The slider fires this callback continuously while
        # dragging, so debounce: every tick cancels the previous timer and
        # only the final settled value triggers a single restart.
        if self._speed_debounce_id is not None:
            self.after_cancel(self._speed_debounce_id)
            self._speed_debounce_id = None
        if self._reading and not self._paused:
            self._speed_debounce_id = self.after(300, self._apply_speed_change)

    def _apply_speed_change(self):
        # ISSUE-016 fix: debounced restart of the current sentence at the new
        # speed.  Runs on the GUI thread (after-callback).  Re-check state at
        # FIRE time, not schedule time — the user may have paused or stopped
        # during the debounce window.  While paused no restart is needed
        # (resume / the next sentence read the slider naturally), and _stop
        # cancels this timer anyway (belt and braces).
        self._speed_debounce_id = None
        if not self._reading or self._paused:
            log.debug("Speed-change restart skipped (reading=%s, paused=%s)",
                      self._reading, self._paused)
            return
        if self._sentence_idx <= 0 or not self._sentences:
            return
        # ISSUE-007: _read_next_sentence post-increments, so the in-flight
        # sentence is _sentences[_sentence_idx - 1]; rewind one and re-speak
        # it.  speak() internally stops the current utterance for both
        # backends (generation bump ISSUE-017 suppresses the stale on_done;
        # the started-word interrupt flag ISSUE-018 halts offline pyttsx3).
        # If the sentence's natural done event is already queued, both
        # callbacks serialize on the GUI thread: done-then-restart re-speaks
        # the sentence that just started at the new speed (correct), while
        # restart-then-stale-done merely cuts the repeated audio short and
        # continues with the following sentence — no skip, no double rewind.
        self._sentence_idx -= 1
        log.info("Speed changed to %.2fx mid-sentence; restarting sentence idx=%d",
                 self._speed_var.get(), self._sentence_idx)
        self._read_next_sentence()

    def _set_status(self, msg: str):
        self._status_label.configure(text=msg)

    # ------------------------------------------------------------------
    # Bookmark persistence
    # ------------------------------------------------------------------

    def _load_bookmarks(self) -> dict:
        # ISSUE-024 fix: the bookmarks file is untrusted external data — catch
        # all I/O and decoding errors (not just FileNotFoundError/JSON errors)
        # and require a dict root before handing the data to callers.
        try:
            with open(_BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            log.debug("No usable bookmarks file (%s): %s", _BOOKMARKS_FILE, e)
            return {}
        if not isinstance(data, dict):
            log.warning("Bookmarks file root is %s, expected dict; ignoring",
                        type(data).__name__)
            return {}
        return data

    def _save_bookmark(self, page: int | None = None, sentence_idx: int | None = None):
        """Persist current (or explicitly given) page + sentence index for the open PDF."""
        if not self._current_pdf_path:
            return
        # ISSUE-036 fix: serialize the read-modify-write cycle.
        with self._bookmark_lock:
            bookmarks = self._load_bookmarks()
            bookmarks[self._current_pdf_path] = {
                "page": self._current_page if page is None else page,
                "sentence_idx": self._sentence_idx if sentence_idx is None else sentence_idx,
            }
            log.debug("Saving bookmark for %s: %r",
                      self._current_pdf_path, bookmarks[self._current_pdf_path])
            self._write_bookmarks(bookmarks)

    def _clear_bookmark(self):
        """ISSUE-025 fix: remove the bookmark for the open PDF (document fully read)."""
        if not self._current_pdf_path:
            return
        # ISSUE-036 fix: serialize the read-modify-write cycle.
        with self._bookmark_lock:
            bookmarks = self._load_bookmarks()
            if self._current_pdf_path in bookmarks:
                del bookmarks[self._current_pdf_path]
                log.debug("Cleared bookmark for %s", self._current_pdf_path)
                self._write_bookmarks(bookmarks)

    def _write_bookmarks(self, bookmarks: dict):
        # ISSUE-036 fix: atomic write via temp file + os.replace so a crash
        # mid-write cannot leave a truncated/empty bookmarks file.
        try:
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(_BOOKMARKS_FILE) or ".",
                                             prefix=".bookmarks-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(bookmarks, f, indent=2)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, _BOOKMARKS_FILE)
        except OSError as e:
            log.error("Failed to write bookmarks file %s: %s", _BOOKMARKS_FILE, e)

    def _restore_bookmark(self, path: str) -> bool:
        """Jump to saved position for *path*. Returns True if a bookmark existed."""
        bookmarks = self._load_bookmarks()
        bm = bookmarks.get(path)
        # ISSUE-024 fix: the entry itself must be a dict.
        if not isinstance(bm, dict) or not bm:
            return False
        page = bm.get("page", 0)
        sentence_idx = bm.get("sentence_idx", 0)
        # ISSUE-024 fix: persisted values are untrusted — require ints so a
        # corrupted/hand-edited file cannot raise TypeError in a GUI callback,
        # and clamp negatives (a negative sentence_idx would otherwise index
        # sentences from the END of the page via Python negative indexing).
        if not isinstance(page, int) or not isinstance(sentence_idx, int):
            log.warning("Bookmark for %s has non-int page/sentence_idx (%r); ignoring",
                        path, bm)
            return False
        page = max(0, page)
        log.info("Bookmark found for %s: page=%d sentence_idx=%d (doc has %d pages)",
                 path, page, sentence_idx, self._pdf.page_count)
        if page >= self._pdf.page_count:
            log.warning("Bookmark page %d out of range; ignoring", page)
            return False
        # Skip the prompt if the bookmark is at the very beginning
        if page == 0 and sentence_idx == 0:
            return True
        resume = mb.askyesno(
            "Resume Reading",
            f"A bookmark was found for this document.\n\n"
            f"Resume from page {page + 1}, sentence {sentence_idx + 1}?",
        )
        if resume:
            self._current_page = page
            # Only reload page text if jumping to a different page
            if page != 0:
                # ISSUE-039 fix: single extraction shared for text + sentences.
                text, self._sentences = self._pdf.get_text_and_sentences(self._current_page)
                self._text_box.configure(state="normal")
                self._text_box.delete("1.0", "end")
                self._text_box.insert("end", text if text else "(No text found on this page)")
                self._text_box.configure(state="disabled")
                self._page_label.configure(text=f"Page {page + 1} of {self._pdf.page_count}")
                self._update_nav_buttons()
            # ISSUE-009 fix: clamp sentence_idx to valid range so a stale
            # bookmark (e.g. PDF was re-saved with fewer sentences) does not
            # silently skip playback by starting past the last sentence.
            # ISSUE-024 fix: clamp the lower bound too (negative values).
            max_idx = max(0, len(self._sentences) - 1)
            clamped = max(0, min(sentence_idx, max_idx))
            if clamped != sentence_idx:
                log.warning("Bookmark sentence_idx=%d clamped to %d (page has %d sentences)",
                            sentence_idx, clamped, len(self._sentences))
            self._sentence_idx = clamped
        return True

    def on_close(self):
        log.info("Window closing; tearing down "
                 "(reading=%s, paused=%s, idx=%d)",
                 self._reading, self._paused, self._sentence_idx)
        # ISSUE-030 fix: only save when reading is actually in progress.
        # An idle close has nothing new to record: after a manual Stop the
        # rewound position was already saved (an unconditional save here
        # clobbered it with sentence 0), and after natural completion
        # _on_page_done cleared/advanced the bookmark (an unconditional
        # save resurrected the cleared entry as a stale resume prompt).
        # _stop(completed=True) leaves _reading and _paused False, so both
        # idle cases are covered by this single gate.
        if self._reading or self._paused:
            # ISSUE-020 fix: apply the same ISSUE-007 rewind as _pause/_stop —
            # when closing mid-sentence, _sentence_idx points at the NEXT
            # sentence, so rewind by one to bookmark the interrupted one.
            if self._reading and not self._paused and self._sentence_idx > 0:
                self._sentence_idx -= 1
            self._save_bookmark()
        # ISSUE-035 fix: close the player (tears down the MCI notify window)
        # after stopping playback.
        self._tts.stop()
        self._tts.close()
        self._pdf.close()
        self.destroy()
