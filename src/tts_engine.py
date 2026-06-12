import asyncio
import logging
import os
import queue
import tempfile
import threading

from src.audio_player import AudioPlayer
from src.voice_manager import Voice

log = logging.getLogger(__name__)


class TTSEngine:
    """
    Unified TTS interface.
    - Online voices (source='online'): uses edge-tts -> MP3 -> AudioPlayer
    - Offline voices (source='offline'): uses pyttsx3 on a persistent dedicated thread
    """

    def __init__(self):
        self._player = AudioPlayer()

        # ISSUE-017 fix: per-utterance generation token. Each utterance
        # captures self._generation when it starts; stop() and pause() bump
        # the counter, permanently invalidating every in-flight utterance.
        # Unlike the old shared set-then-cleared _stop_event, a cancelled
        # utterance can never be resurrected by a later speak().
        self._gen_lock = threading.Lock()
        self._generation = 0

        # ISSUE-002 fix: protect _tmp_files with a lock so GUI and synth threads
        # do not race on the shared list.
        self._tmp_lock = threading.Lock()
        self._tmp_files: list[str] = []

        # ISSUE-018 fix: interrupt flag observed by the pyttsx3 'started-word'
        # callback, which runs on the worker thread inside runAndWait — so
        # offline speech can be stopped mid-sentence without a cross-thread
        # COM call (preserving the ISSUE-013 single-thread constraint).
        self._pyttsx3_interrupt = threading.Event()

        # ISSUE-013 fix: pyttsx3 runs on a single long-lived dedicated thread
        # driven by a command queue, avoiding repeated COM init/teardown and
        # cross-thread stop() on runAndWait().
        self._pyttsx3_queue: queue.Queue = queue.Queue()
        self._pyttsx3_thread = threading.Thread(
            target=self._pyttsx3_worker, daemon=True, name="pyttsx3-worker"
        )
        self._pyttsx3_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str, voice: Voice, speed: float = 1.0, on_done=None):
        """Speak text with the given voice at the given speed multiplier."""
        # ISSUE-017 fix: stop() bumps _generation, cancelling any in-flight
        # utterance for good. The new utterance then captures the fresh
        # generation in _speak_online/_speak_offline.
        self.stop()
        log.debug("speak() called: gen=%d source=%s voice=%s speed=%.2f text_len=%d",
                  self._generation, voice.source, voice.id, speed, len(text))
        if voice.source == "online":
            self._speak_online(text, voice, speed, on_done)
        else:
            self._speak_offline(text, voice, speed, on_done)

    def pause(self):
        # ISSUE-019 fix: bump the generation so an in-flight online synthesis
        # discards its result instead of starting playback while the app is
        # paused (the app resumes by re-speaking the rewound sentence).
        self._bump_generation()
        # ISSUE-018 fix: interrupt an in-flight offline sentence at the next
        # word boundary. pyttsx3 has no true pause; the app layer re-speaks
        # the rewound sentence on resume.
        self._pyttsx3_interrupt.set()
        if self._player.is_playing:
            self._player.pause()

    def resume(self):
        if self._player.is_paused:
            self._player.resume()
        # Offline / in-flight-synth resume is handled by the app layer, which
        # re-calls speak() for the rewound sentence when nothing is playing.

    def stop(self):
        log.debug("TTSEngine.stop() called (gen -> %d)", self._generation + 1)
        # ISSUE-017 fix: invalidate every in-flight utterance permanently.
        self._bump_generation()
        # ISSUE-018 fix: the 'started-word' callback sees this flag from
        # inside runAndWait and stops the engine at the next word boundary,
        # so offline Stop takes effect mid-sentence instead of after it.
        self._pyttsx3_interrupt.set()
        self._player.stop()
        # Also ask the worker to stop the engine (processed on the worker
        # thread itself, preserving the ISSUE-013 constraint).
        self._pyttsx3_queue.put(("stop", None, None, None, None))
        # ISSUE-002 fix: clean up temp files only after the player is fully
        # stopped (player.stop() has already set stop_event and joined the
        # monitor), so no synth/play thread still holds a reference.
        self._cleanup_tmp()

    def _bump_generation(self) -> int:
        """ISSUE-017 fix: invalidate all in-flight utterances."""
        with self._gen_lock:
            self._generation += 1
            return self._generation

    @property
    def is_playing(self) -> bool:
        return self._player.is_playing

    @property
    def is_paused(self) -> bool:
        return self._player.is_paused

    # ------------------------------------------------------------------
    # Online (edge-tts)
    # ------------------------------------------------------------------

    def _speak_online(self, text: str, voice: Voice, speed: float, on_done):
        # ISSUE-017 fix: capture this utterance's generation token. stop(),
        # pause(), and any newer speak() bump self._generation, so the check
        # after synthesis permanently cancels this run — there is no shared
        # event that a later speak() could clear to resurrect it.
        gen = self._generation

        def _run():
            tmp = self._make_tmp_mp3()
            try:
                rate_str = self._speed_to_edge_rate(speed)
                log.debug("edge-tts synth start: gen=%d voice=%s rate=%s -> %s",
                          gen, voice.id, rate_str, tmp)
                asyncio.run(self._edge_synthesize(text, voice.id, rate_str, tmp))
                if gen == self._generation:
                    log.debug("edge-tts synth done, handing to player: gen=%d %s", gen, tmp)
                    # Wrap on_done to clean up this specific tmp file after playback.
                    def _done_and_cleanup():
                        log.debug("Playback finished for utterance gen=%d", gen)
                        self._delete_tmp(tmp)
                        if on_done:
                            on_done()
                    self._player.play(tmp, on_done=_done_and_cleanup)
                else:
                    # Cancelled by stop()/pause()/a newer speak() while
                    # synthesizing: discard without playing or firing on_done.
                    log.debug("edge-tts synth done but utterance gen=%d is stale "
                              "(current gen=%d); discarding %s",
                              gen, self._generation, tmp)
                    self._delete_tmp(tmp)
            except Exception as e:
                log.exception("Online TTS (edge-tts) failed: %s", e)
                self._delete_tmp(tmp)
                # ISSUE-017 fix: only keep the sentence pump alive if this
                # utterance is still current; a stale failure must not advance.
                if on_done and gen == self._generation:
                    on_done()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @staticmethod
    async def _edge_synthesize(text: str, voice_id: str, rate: str, out_path: str):
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_id, rate=rate)
        # ISSUE-023 fix: bound the network operation so a TCP-level stall
        # (Wi-Fi drop, proxy black-hole) cannot hang the synth thread — and
        # with it the sentence pump — forever. A TimeoutError propagates to
        # _run's except block, which logs, deletes the tmp file, and fires
        # on_done so reading recovers automatically.
        await asyncio.wait_for(communicate.save(out_path), timeout=30)

    @staticmethod
    def _speed_to_edge_rate(speed: float) -> str:
        # ISSUE-012 fix: round (not truncate), and clamp to edge-tts safe range
        # of -50% to +100%.
        pct = round((speed - 1.0) * 100)
        pct = max(-50, min(100, pct))
        return f"{pct:+d}%"

    # ------------------------------------------------------------------
    # Offline (pyttsx3) — dedicated worker thread
    # ------------------------------------------------------------------

    def _pyttsx3_worker(self):
        """
        ISSUE-013 fix: single long-lived thread owns the pyttsx3 engine.
        Commands: ("speak", text, voice_id, rate_wpm, on_done)
                  ("stop",  None, None,     None,     None)
        """
        engine = None
        pending_stop = False

        while True:
            try:
                cmd = self._pyttsx3_queue.get()
            except Exception:
                break

            action = cmd[0]

            if action == "stop":
                log.debug("pyttsx3 worker processing stop command (engine=%s)",
                          "live" if engine else "none")
                pending_stop = True
                if engine:
                    try:
                        engine.stop()
                    except Exception:
                        pass
                continue

            if action != "speak":
                continue

            _, text, voice_id, rate_wpm, on_done = cmd
            pending_stop = False
            # ISSUE-018 fix: a fresh utterance starts un-interrupted. stop()
            # and pause() set the flag; the 'started-word' callback below then
            # stops the engine from inside runAndWait at the next word
            # boundary, so Stop/Pause interrupt offline speech mid-sentence.
            self._pyttsx3_interrupt.clear()

            try:
                import pyttsx3
                if engine is None:
                    engine = pyttsx3.init()

                    def _on_word(name, location, length, _eng=engine):
                        # ISSUE-018 fix: runs on this worker thread inside
                        # runAndWait, so calling stop() here is COM-safe
                        # (no cross-thread call — ISSUE-013 preserved).
                        if self._pyttsx3_interrupt.is_set():
                            try:
                                _eng.stop()
                            except Exception:
                                pass

                    engine.connect("started-word", _on_word)
                    log.debug("pyttsx3 engine initialized on worker thread")
                engine.setProperty("voice", voice_id)
                engine.setProperty("rate", rate_wpm)
                engine.say(text)
                log.debug("pyttsx3 runAndWait start (voice=%s)", voice_id)
                engine.runAndWait()
                log.debug("pyttsx3 runAndWait returned (interrupted=%s)",
                          self._pyttsx3_interrupt.is_set())
            except Exception as e:
                log.exception("Offline TTS (pyttsx3) failed: %s", e)
                # Re-initialize engine on next use after a failure
                engine = None

            if not pending_stop and not self._pyttsx3_interrupt.is_set() and on_done:
                # on_done is generation-gated by _speak_offline (ISSUE-017),
                # so even a late completion racing a new speak() cannot fire
                # a stale on_done and double-advance the reader.
                on_done()

    def _speak_offline(self, text: str, voice: Voice, speed: float, on_done):
        # ISSUE-012 fix: consistent speed mapping (200 wpm base, rounded).
        rate_wpm = max(80, min(500, round(200 * speed)))
        # ISSUE-017 fix: gate on_done on this utterance's generation so a
        # sentence that completes after stop()/pause()/a newer speak() cannot
        # fire a stale on_done (which caused double sentence advances).
        gen = self._generation

        def _gated_on_done():
            if gen == self._generation and on_done:
                on_done()
            elif gen != self._generation:
                log.debug("Suppressed stale offline on_done (gen=%d, current gen=%d)",
                          gen, self._generation)

        self._pyttsx3_queue.put(("speak", text, voice.id, rate_wpm, _gated_on_done))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_tmp_mp3(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with self._tmp_lock:
            self._tmp_files.append(path)
        return path

    def _delete_tmp(self, path: str):
        """Remove a single temp file (called after its playback completes)."""
        with self._tmp_lock:
            try:
                self._tmp_files.remove(path)
            except ValueError:
                pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _cleanup_tmp(self):
        """Remove all tracked temp files (called on stop after player is idle)."""
        with self._tmp_lock:
            paths = list(self._tmp_files)
            self._tmp_files.clear()
        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
