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
        # ISSUE-026 fix: guard every command individually so an unexpected
        # exception can neither kill the dispatcher (wedging every future
        # caller) nor orphan the current caller blocked on its result queue.
        try:
            cmd, result_q = item
            # ISSUE-029 fix: a malformed item that unpacks into two elements
            # but carries a non-Queue would otherwise reach the execute
            # block, where BOTH result_q.put calls raise — the second one
            # uncaught, killing the dispatcher. Reject it here instead.
            if not isinstance(result_q, queue.Queue):
                raise TypeError(f"result queue is {type(result_q).__name__}, expected queue.Queue")
        except Exception:
            log.exception("Malformed MCI command item ignored: %r", item)
            continue
        try:
            buf = ctypes.create_unicode_buffer(256)
            ret = _winmm.mciSendStringW(cmd, buf, 255, None)
            if ret != 0:
                log.debug("MCI command failed ret=%d cmd=%r reply=%r", ret, cmd, buf.value.strip())
            result_q.put((ret, buf.value.strip()))
        except Exception:
            log.exception("MCI dispatcher error executing %r", cmd)
            # ISSUE-029 fix: the recovery path must itself be guarded — if
            # answering the caller fails too, log and keep the dispatcher
            # alive rather than letting the exception kill the thread.
            try:
                result_q.put((-1, ""))
            except Exception:
                log.exception("MCI dispatcher could not deliver failure result for %r", cmd)


_worker = threading.Thread(target=_mci_worker, daemon=True)
_worker.start()


def _mci(cmd: str) -> int:
    rq: queue.Queue = queue.Queue()
    _cmd_queue.put((cmd, rq))
    # ISSUE-026 fix: never block the caller (often the GUI thread) forever.
    try:
        ret, _ = rq.get(timeout=5.0)
    except queue.Empty:
        log.error("MCI dispatcher did not respond within 5s for %r", cmd)
        return -1
    return ret


def _mci_query(cmd: str) -> str:
    rq: queue.Queue = queue.Queue()
    _cmd_queue.put((cmd, rq))
    # ISSUE-026 fix: never block the caller (often the GUI thread) forever.
    try:
        _, value = rq.get(timeout=5.0)
    except queue.Empty:
        log.error("MCI dispatcher did not respond within 5s for %r", cmd)
        return ""
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
        self._monitor_thread: threading.Thread | None = None
        self._open = False
        self._lock = threading.Lock()

    def play(self, filepath: str, on_done=None):
        """Load and play an MP3 file. Calls on_done() when it finishes naturally."""
        self.stop()
        # ISSUE-028 fix: per-playback cancellation event (mirrors the
        # ISSUE-017 generation-token design). stop() sets the CURRENT
        # event; each play() creates a fresh one instead of clearing a
        # shared event, so a stale monitor that outlived the 2s join
        # timeout still sees its own permanently-set event and its own
        # captured on_done — it can never observe (or advance) the new
        # playback.
        stop_event = threading.Event()
        self._stop_event = stop_event

        abs_path = os.path.abspath(filepath).replace("/", "\\")

        log.debug("AudioPlayer.play: opening %s", abs_path)
        _mci(f'close {_ALIAS}')
        ret = _mci(f'open "{abs_path}" type mpegvideo alias {_ALIAS}')
        if ret != 0:
            log.error("MCI open failed (ret=%d) for %s; dispatching on_done", ret, abs_path)
            if on_done:
                # ISSUE-027 fix: never invoke the caller's callback inline.
                # The calling synth thread may hold TTSEngine._gen_lock
                # across this play() call; an inline on_done would marshal
                # to the GUI thread (event_generate) while a GUI-side stop()
                # blocks on that same lock — a deadlock. Dispatch detached,
                # matching the ISSUE-022 monitor dispatch.
                threading.Thread(target=on_done, daemon=True, name="on-done-dispatch").start()
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

            # ISSUE-028 fix: the monitor observes only its own playback's
            # stop_event and on_done (captured from the enclosing play()
            # call), never the shared instance slots a newer play() would
            # have reassigned.
            while not stop_event.is_set():
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
                        if stop_event.is_set():
                            break
                        time.sleep(0.05)
                    break

            with self._lock:
                # ISSUE-028 fix: only clear the shared flags when this
                # monitor's playback is still current. If stop_event is set,
                # stop() already cleared them — and a newer play() may have
                # set them again for ITS playback, which a stale monitor
                # must not clobber.
                if not stop_event.is_set():
                    self._playing = False
                    self._paused = False
            cb = on_done if not stop_event.is_set() else None
            log.debug("Monitor exiting (stop_event=%s, will_fire_on_done=%s)",
                      stop_event.is_set(), bool(cb))
            if cb:
                # ISSUE-022 fix: fire on_done from a detached dispatcher thread.
                # The on_done chain ends in app.event_generate(), which blocks
                # until the GUI mainloop services the marshalled call. Firing
                # it on this monitor thread let a concurrent stop() on the GUI
                # thread join() this thread while it was waiting on that same
                # GUI thread — a lock-step cycle broken only by the 2s join
                # timeout (a hard GUI freeze). The dispatcher thread lets the
                # monitor exit immediately, so the join returns promptly.
                threading.Thread(target=cb, daemon=True, name="on-done-dispatch").start()

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
                # ISSUE-022 fix: on_done now fires on a detached dispatcher
                # thread, so the monitor can no longer be blocked inside
                # event_generate while we join it — this join returns promptly.
                self._monitor_thread.join(timeout=2.0)
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
