import json
import os
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb

import customtkinter as ctk

from src.pdf_reader import PDFReader
from src.tts_engine import TTSEngine
from src.voice_manager import VoiceManager

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

        self._build_ui()
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
            if not voices:
                self.after(0, lambda: self._set_status("No voices found"))
                return
            display = [str(v) for v in voices]
            default = self._voices.get_default_voice()
            default_str = str(default) if default else display[0]

            def update():
                self._voice_menu.configure(values=display)
                self._voice_var.set(default_str)
                self._set_status(f"{len(voices)} voices loaded")

            self.after(0, update)

        self._voices.load(on_done=on_done)

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
        try:
            count = self._pdf.open(path)
        except Exception as e:
            mb.showerror("Error", f"Could not open PDF:\n{e}")
            return

        self._current_pdf_path = path
        self._current_page = 0
        self._title_label.configure(text=path.split("/")[-1].split("\\")[-1])
        self._set_status(f"{count} page(s)")
        self._update_page_display()
        self._update_nav_buttons()
        self._play_btn.configure(state="normal")
        self._restore_bookmark(path)

    def _update_page_display(self):
        text = self._pdf.get_all_text(self._current_page)
        self._sentences = self._pdf.get_sentences(self._current_page)
        self._sentence_idx = 0

        self._text_box.configure(state="normal")
        self._text_box.delete("1.0", "end")
        self._text_box.insert("end", text if text else "(No text found on this page)")
        self._text_box.configure(state="disabled")

        total = self._pdf.page_count
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
            return
        if self._paused:
            self._tts.resume()
            self._paused = False
            self._play_btn.configure(state="disabled")
            self._pause_btn.configure(state="normal")
            self._stop_btn.configure(state="normal")
            return

        if not self._sentences:
            self._set_status("No text to read on this page.")
            return

        self._reading = True
        self._paused = False
        self._play_btn.configure(state="disabled")
        self._pause_btn.configure(state="normal")
        self._stop_btn.configure(state="normal")
        self._read_next_sentence()

    def _pause(self):
        if self._reading and not self._paused:
            self._tts.pause()
            self._paused = True
            self._save_bookmark()
            self._play_btn.configure(state="normal", text="▶  Resume")
            self._pause_btn.configure(state="disabled")

    def _stop(self):
        if self._reading or self._paused:
            self._save_bookmark()
        self._reading = False
        self._paused = False
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
            self._pending_after_id = None
        self._tts.stop()
        self._sentence_idx = 0
        self._clear_highlight()
        self._play_btn.configure(state="normal" if self._pdf.is_open else "disabled", text="▶  Play")
        self._pause_btn.configure(state="disabled")
        self._stop_btn.configure(state="disabled")

    def _read_next_sentence(self):
        if not self._reading or self._paused:
            return
        if self._sentence_idx >= len(self._sentences):
            # Page finished
            self._pending_after_id = self.after(0, self._on_page_done)
            return

        sentence = self._sentences[self._sentence_idx]
        self._highlight_sentence(sentence)

        voice = self._voices.find_by_display(self._voice_var.get())
        if voice is None:
            self._set_status("No voice selected.")
            self._stop()
            return

        speed = self._speed_var.get()
        self._sentence_idx += 1
        self._tts.speak(sentence, voice, speed, on_done=self._on_sentence_done)

    def _on_sentence_done(self):
        if self._reading and not self._paused:
            self._pending_after_id = self.after(0, self._read_next_sentence)

    def _on_page_done(self):
        if self._auto_advance_var.get() and self._current_page < self._pdf.page_count - 1:
            # Auto-advance to next page
            self._current_page += 1
            self._update_page_display()
            self._update_nav_buttons()
            self._sentence_idx = 0
            self._read_next_sentence()
        else:
            if self._current_page >= self._pdf.page_count - 1:
                self._set_status("Finished reading document.")
            else:
                self._set_status("Page done.")
            self._stop()

    # ------------------------------------------------------------------
    # Highlighting
    # ------------------------------------------------------------------

    def _highlight_sentence(self, sentence: str):
        self._text_box.configure(state="normal")
        self._text_box.tag_remove("highlight", "1.0", "end")
        start = "1.0"
        while True:
            pos = self._text_box.search(sentence[:40], start, stopindex="end", nocase=False)
            if not pos:
                break
            end = f"{pos}+{len(sentence)}c"
            self._text_box.tag_add("highlight", pos, end)
            self._text_box.see(pos)
            break
        self._text_box.configure(state="disabled")

    def _clear_highlight(self):
        self._text_box.configure(state="normal")
        self._text_box.tag_remove("highlight", "1.0", "end")
        self._text_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_voice_change(self, _value):
        pass  # Voice is read at speak time

    def _on_speed_change(self, value):
        self._speed_display.configure(text=f"{value:.1f}x")

    def _set_status(self, msg: str):
        self._status_label.configure(text=msg)

    # ------------------------------------------------------------------
    # Bookmark persistence
    # ------------------------------------------------------------------

    def _load_bookmarks(self) -> dict:
        try:
            with open(_BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_bookmark(self):
        """Persist current page + sentence index for the open PDF."""
        if not self._current_pdf_path:
            return
        bookmarks = self._load_bookmarks()
        bookmarks[self._current_pdf_path] = {
            "page": self._current_page,
            "sentence_idx": self._sentence_idx,
        }
        try:
            with open(_BOOKMARKS_FILE, "w", encoding="utf-8") as f:
                json.dump(bookmarks, f, indent=2)
        except OSError:
            pass

    def _restore_bookmark(self, path: str) -> bool:
        """Jump to saved position for *path*. Returns True if a bookmark existed."""
        bookmarks = self._load_bookmarks()
        bm = bookmarks.get(path)
        if not bm:
            return False
        page = bm.get("page", 0)
        sentence_idx = bm.get("sentence_idx", 0)
        if page >= self._pdf.page_count:
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
                self._sentences = self._pdf.get_sentences(self._current_page)
                text = self._pdf.get_all_text(self._current_page)
                self._text_box.configure(state="normal")
                self._text_box.delete("1.0", "end")
                self._text_box.insert("end", text if text else "(No text found on this page)")
                self._text_box.configure(state="disabled")
                self._page_label.configure(text=f"Page {page + 1} of {self._pdf.page_count}")
                self._update_nav_buttons()
            self._sentence_idx = sentence_idx
        return True

    def on_close(self):
        self._save_bookmark()
        self._tts.stop()
        self._pdf.close()
        self.destroy()
