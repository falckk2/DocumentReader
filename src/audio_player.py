"""
Audio playback using Windows MCI (winmm.dll) via ctypes.

All mciSendStringW calls are routed through a single worker thread so that
COM apartment thread affinity is respected on Windows 10/11.
"""
import ctypes
import logging
import os
import queue
import threading
import time

log = logging.getLogger(__name__)

_winmm = ctypes.WinDLL("winmm")
_ALIAS = "DocumentReaderTrack"

# ---------------------------------------------------------------------------
# Single-threaded MCI dispatcher
# ---------------------------------------------------------------------------

_cmd_queue: queue.Queue = queue.Queue()


def _mci_worker() -> None:
    """Process all MCI commands from one thread (COM apartment safety)."""
    while True:
        item = _cmd_queue.get()
        if item is None:
            break
        cmd, result_q = item
        buf = ctypes.create_unicode_buffer(256)
        ret = _winmm.mciSendStringW(cmd, buf, 255, None)
        if ret != 0:
            log.debug("MCI command failed ret=%d cmd=%r reply=%r", ret, cmd, buf.value.strip())
        result_q.put((ret, buf.value.strip()))


_worker = threading.Thread(target=_mci_worker, daemon=True)
_worker.start()


def _mci(cmd: str) -> int:
    rq: queue.Queue = queue.Queue()
    _cmd_queue.put((cmd, rq))
    ret, _ = rq.get()
    return ret


def _mci_query(cmd: str) -> str:
    rq: queue.Queue = queue.Queue()
    _cmd_queue.put((cmd, rq))
    _, value = rq.get()
    return value


# ---------------------------------------------------------------------------
# AudioPlayer
# ---------------------------------------------------------------------------

class AudioPlayer:
    """Plays MP3 files using Windows MCI with pause/stop support."""

    def __init__(self):
        self._playing = False
        self._paused = False
        self._stop_event = threading.Event()
        self._on_done = None
        self._monitor_thread: threading.Thread | None = None
        self._open = False
        self._lock = threading.Lock()

    def play(self, filepath: str, on_done=None):
        """Load and play an MP3 file. Calls on_done() when it finishes naturally."""
        self.stop()
        self._stop_event.clear()
        self._on_done = on_done

        abs_path = os.path.abspath(filepath).replace("/", "\\")

        log.debug("AudioPlayer.play: opening %s", abs_path)
        _mci(f'close {_ALIAS}')
        ret = _mci(f'open "{abs_path}" type mpegvideo alias {_ALIAS}')
        if ret != 0:
            log.error("MCI open failed (ret=%d) for %s; firing on_done immediately", ret, abs_path)
            if on_done:
                on_done()
            return

        with self._lock:
            self._open = True

        _mci(f'set {_ALIAS} time format milliseconds')
        _mci(f'play {_ALIAS}')

        with self._lock:
            self._playing = True
            self._paused = False

        def _monitor():
            # Brief delay so MCI can parse the file and report length
            time.sleep(0.2)

            track_length = 0
            try:
                track_length = int(_mci_query(f'status {_ALIAS} length'))
            except Exception:
                log.warning("Could not read track length; position-based end detection disabled")
            log.debug("Monitor started: track_length=%dms", track_length)

            while not self._stop_event.is_set():
                time.sleep(0.1)
                with self._lock:
                    if not self._open:
                        break
                status = _mci_query(f'status {_ALIAS} mode')
                try:
                    pos = int(_mci_query(f'status {_ALIAS} position')) if track_length else 0
                except Exception:
                    pos = 0

                if status == "stopped":
                    break
                if track_length and pos >= track_length:
                    # Give the audio output buffer time to drain
                    for _ in range(5):
                        if self._stop_event.is_set():
                            break
                        time.sleep(0.05)
                    break

            with self._lock:
                self._playing = False
                self._paused = False
            fire = not self._stop_event.is_set() and self._on_done
            log.debug("Monitor exiting (stop_event=%s, will_fire_on_done=%s)",
                      self._stop_event.is_set(), bool(fire))
            if fire:
                self._on_done()

        self._monitor_thread = threading.Thread(target=_monitor, daemon=True)
        self._monitor_thread.start()

    def pause(self):
        with self._lock:
            if self._playing and not self._paused:
                _mci(f'pause {_ALIAS}')
                self._paused = True

    def resume(self):
        with self._lock:
            if self._playing and self._paused:
                _mci(f'resume {_ALIAS}')
                self._paused = False

    def stop(self):
        self._stop_event.set()
        with self._lock:
            if self._open:
                _mci(f'stop {_ALIAS}')
                _mci(f'close {_ALIAS}')
                self._open = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            if threading.current_thread() is self._monitor_thread:
                log.error("AudioPlayer.stop() called from monitor thread itself; skipping self-join "
                          "to avoid deadlock")
            else:
                joined = self._monitor_thread.join(timeout=2.0)  # noqa: F841 (join returns None)
                if self._monitor_thread.is_alive():
                    log.warning("Monitor thread did not exit within 2s join timeout")
        with self._lock:
            self._playing = False
            self._paused = False

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing and not self._paused

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused
