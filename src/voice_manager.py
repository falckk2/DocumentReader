import asyncio
import logging
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Voice:
    id: str           # Unique identifier used by the TTS engine
    name: str         # Display name shown in UI
    locale: str       # e.g. "en-US"
    gender: str       # "Male" / "Female"
    source: str       # "online" or "offline"

    def __str__(self) -> str:
        tag = "Online" if self.source == "online" else "Offline"
        return f"[{tag}] {self.name} ({self.locale})"


class VoiceManager:
    def __init__(self):
        self._voices: list[Voice] = []
        self._loaded = False

    def load(self, on_done=None):
        """Load voices in a background thread. Calls on_done(voices) when complete."""
        def _load():
            voices = []
            offline = self._load_offline_voices()
            online = self._load_online_voices()
            log.info("Voice load: %d offline, %d online", len(offline), len(online))
            voices.extend(offline)
            voices.extend(online)
            self._voices = voices
            self._loaded = True
            if on_done:
                on_done(voices)

        t = threading.Thread(target=_load, daemon=True)
        t.start()

    def _load_offline_voices(self) -> list[Voice]:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            raw = engine.getProperty("voices")
            engine.stop()
            result = []
            for v in raw:
                locale = ""
                if v.languages:
                    raw_lang = v.languages[0]
                    if isinstance(raw_lang, bytes):
                        raw_lang = raw_lang.decode("utf-8", errors="ignore")
                    locale = raw_lang[:5] if len(raw_lang) >= 5 else raw_lang
                gender = "Female" if "female" in (v.gender or "").lower() else "Male"
                result.append(Voice(
                    id=v.id,
                    name=v.name,
                    locale=locale or "en",
                    gender=gender,
                    source="offline",
                ))
            return result
        except Exception:
            log.exception("Failed to load offline (pyttsx3) voices")
            return []

    def _load_online_voices(self) -> list[Voice]:
        try:
            import edge_tts

            async def _fetch():
                return await edge_tts.list_voices()

            raw = asyncio.run(_fetch())
            result = []
            for v in raw:
                result.append(Voice(
                    id=v["ShortName"],
                    name=v["FriendlyName"],
                    locale=v["Locale"],
                    gender=v["Gender"],
                    source="online",
                ))
            return result
        except Exception:
            log.exception("Failed to load online (edge-tts) voices (network or API error?)")
            return []

    @property
    def voices(self) -> list[Voice]:
        return self._voices

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def get_default_voice(self) -> Voice | None:
        """Return the first online English voice, or first offline if none."""
        online_en = [v for v in self._voices if v.source == "online" and "en-US" in v.locale]
        if online_en:
            return online_en[0]
        online = [v for v in self._voices if v.source == "online"]
        if online:
            return online[0]
        offline = [v for v in self._voices if v.source == "offline"]
        return offline[0] if offline else None

    def find_by_display(self, display_name: str) -> Voice | None:
        for v in self._voices:
            if str(v) == display_name:
                return v
        return None
