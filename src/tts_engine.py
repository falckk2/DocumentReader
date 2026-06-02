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
        self._stop_event = threading.Event()

        # ISSUE-002 fix: protect _tmp_files with a lock so GUI and synth threads
        # do not race on the shared list.
        self._tmp_lock = threading.Lock()
        self._tmp_files: list[str] = []

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
        log.debug("speak() called: source=%s voice=%s speed=%.2f text_len=%d",
                  voice.source, voice.id, speed, len(text))
        self.stop()
        self._stop_event.clear()
        if voice.source == "online":
            self._speak_online(text, voice, speed, on_done)
        else:
            self._speak_offline(text, voice, speed, on_done)

    def pause(self):
        if self._player.is_playing:
            self._player.pause()
        # Offline (pyttsx3) does not support true pause; the app layer
        # handles offline pause by stopping and then re-queuing the sentence.

    def resume(self):
        if self._player.is_paused:
            self._player.resume()
        # Offline resume is handled by the app layer (re-calls speak()).

    def stop(self):
        log.debug("TTSEngine.stop() called")
        self._stop_event.set()
        self._player.stop()
        # Signal pyttsx3 worker to abort any in-flight sentence
        self._pyttsx3_queue.put(("stop", None, None, None, None))
        # ISSUE-002 fix: clean up temp files only after the player is fully
        # stopped (player.stop() has already set stop_event and joined the
        # monitor), so no synth/play thread still holds a reference.
        self._cleanup_tmp()

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
        # Capture a snapshot of the stop_event and tmp ownership token so
        # this specific synthesis run can be invalidated independently.
        stop_event = self._stop_event  # shared Event; already cleared above

        def _run():
            tmp = self._make_tmp_mp3()
            try:
                rate_str = self._speed_to_edge_rate(speed)
                log.debug("edge-tts synth start: voice=%s rate=%s -> %s", voice.id, rate_str, tmp)
                asyncio.run(self._edge_synthesize(text, voice.id, rate_str, tmp))
                if not stop_event.is_set():
                    log.debug("edge-tts synth done, handing to player: %s", tmp)
                    # Wrap on_done to clean up this specific tmp file after playback.
                    def _done_and_cleanup():
                        self._delete_tmp(tmp)
                        if on_done:
                            on_done()
                    self._player.play(tmp, on_done=_done_and_cleanup)
                else:
                    log.debug("edge-tts synth done but stop_event set; not playing %s", tmp)
                    self._delete_tmp(tmp)
            except Exception as e:
                log.exception("Online TTS (edge-tts) failed: %s", e)
                self._delete_tmp(tmp)
                if on_done:
                    on_done()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @staticmethod
    async def _edge_synthesize(text: str, voice_id: str, rate: str, out_path: str):
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_id, rate=rate)
        await communicate.save(out_path)

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

            try:
                import pyttsx3
                if engine is None:
                    engine = pyttsx3.init()
                    log.debug("pyttsx3 engine initialized on worker thread")
                engine.setProperty("voice", voice_id)
                engine.setProperty("rate", rate_wpm)
                engine.say(text)
                log.debug("pyttsx3 runAndWait start (voice=%s)", voice_id)
                engine.runAndWait()
                log.debug("pyttsx3 runAndWait returned")
            except Exception as e:
                log.exception("Offline TTS (pyttsx3) failed: %s", e)
                # Re-initialize engine on next use after a failure
                engine = None

            if not pending_stop and not self._stop_event.is_set() and on_done:
                on_done()

    def _speak_offline(self, text: str, voice: Voice, speed: float, on_done):
        # ISSUE-012 fix: consistent speed mapping (200 wpm base, rounded).
        rate_wpm = max(80, min(500, round(200 * speed)))
        self._pyttsx3_queue.put(("speak", text, voice.id, rate_wpm, on_done))

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
