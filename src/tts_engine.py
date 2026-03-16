import asyncio
import os
import tempfile
import threading

from src.audio_player import AudioPlayer
from src.voice_manager import Voice


class TTSEngine:
    """
    Unified TTS interface.
    - Online voices (source='online'): uses edge-tts → MP3 → AudioPlayer
    - Offline voices (source='offline'): uses pyttsx3 directly
    """

    def __init__(self):
        self._player = AudioPlayer()
        self._pyttsx3_engine = None
        self._pyttsx3_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._tmp_files: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str, voice: Voice, speed: float = 1.0, on_done=None):
        """Speak text with the given voice at the given speed multiplier."""
        self.stop()
        self._stop_event.clear()
        if voice.source == "online":
            self._speak_online(text, voice, speed, on_done)
        else:
            self._speak_offline(text, voice, speed, on_done)

    def pause(self):
        if self._player.is_playing:
            self._player.pause()
        elif self._pyttsx3_engine:
            # pyttsx3 doesn't support true pause; we stop instead
            self._stop_pyttsx3()

    def resume(self):
        if self._player.is_paused:
            self._player.resume()

    def stop(self):
        self._stop_event.set()
        self._player.stop()
        self._stop_pyttsx3()
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
        def _run():
            try:
                tmp = self._make_tmp_mp3()
                rate_str = self._speed_to_edge_rate(speed)
                asyncio.run(self._edge_synthesize(text, voice.id, rate_str, tmp))
                if not self._stop_event.is_set():
                    self._player.play(tmp, on_done=on_done)
            except Exception as e:
                print(f"[TTS online error] {e}")
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
        # edge-tts rate is like "+20%" or "-10%"
        pct = int((speed - 1.0) * 100)
        return f"{pct:+d}%"

    # ------------------------------------------------------------------
    # Offline (pyttsx3)
    # ------------------------------------------------------------------

    def _speak_offline(self, text: str, voice: Voice, speed: float, on_done):
        def _run():
            try:
                import pyttsx3
                engine = pyttsx3.init()
                self._pyttsx3_engine = engine
                engine.setProperty("voice", voice.id)
                # pyttsx3 default rate is ~200 wpm
                engine.setProperty("rate", int(200 * speed))
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                print(f"[TTS offline error] {e}")
            finally:
                self._pyttsx3_engine = None
                if not self._stop_event.is_set() and on_done:
                    on_done()

        self._pyttsx3_thread = threading.Thread(target=_run, daemon=True)
        self._pyttsx3_thread.start()

    def _stop_pyttsx3(self):
        engine = self._pyttsx3_engine
        if engine:
            try:
                engine.stop()
            except Exception:
                pass
            self._pyttsx3_engine = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_tmp_mp3(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        self._tmp_files.append(path)
        return path

    def _cleanup_tmp(self):
        for path in list(self._tmp_files):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        self._tmp_files.clear()
