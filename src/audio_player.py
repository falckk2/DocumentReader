"""
Audio playback using Windows MCI (winmm.dll) via ctypes.

All mciSendStringW calls are routed through a single worker thread so that
COM apartment thread affinity is respected on Windows 10/11.

End-of-track detection (ISSUE-011): the primary signal is the Win32
MM_MCINOTIFY message that `play {alias} notify` posts to a hidden
message-only window, eliminating the polling latency (0.2s warmup +
0.1s poll + 0.25s drain) that caused audible gaps between sentences.
A slow watchdog poll backs up the notify in case a message is ever lost,
and the original tight polling monitor remains as a full fallback when
the notify window cannot be created.
"""
import ctypes
import ctypes.wintypes as wintypes
import itertools
import logging
import os
import queue
import threading
import time

log = logging.getLogger(__name__)

_winmm = ctypes.WinDLL("winmm")
_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")
_ALIAS = "DocumentReaderTrack"

# ---------------------------------------------------------------------------
# Win32 plumbing for MM_MCINOTIFY (ISSUE-011)
# ---------------------------------------------------------------------------

MM_MCINOTIFY = 0x3B9
MCI_NOTIFY_SUCCESSFUL = 0x0001  # natural completion — the only code that fires on_done
MCI_NOTIFY_SUPERSEDED = 0x0002
MCI_NOTIFY_ABORTED = 0x0004
MCI_NOTIFY_FAILURE = 0x0008
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
HWND_MESSAGE = ctypes.c_void_p(-3)  # message-only window parent

LRESULT = ctypes.c_ssize_t  # pointer-sized signed int (correct on win64)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


try:
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                    wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
except Exception:  # pragma: no cover — only reachable with stubbed DLLs
    pass

_wndclass_counter = itertools.count()

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
            # ISSUE-011: an optional third element carries the HWND that
            # mciSendStringW should post MM_MCINOTIFY to (play ... notify).
            cmd, result_q, *extra = item
            hwnd_cb = extra[0] if extra else None
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
            callback = ctypes.c_void_p(hwnd_cb) if hwnd_cb else None
            ret = _winmm.mciSendStringW(cmd, buf, 255, callback)
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


def _mci_notify(cmd: str, hwnd: int) -> int:
    """Send an MCI command whose completion posts MM_MCINOTIFY to hwnd (ISSUE-011)."""
    rq: queue.Queue = queue.Queue()
    _cmd_queue.put((cmd, rq, hwnd))
    try:
        ret, _ = rq.get(timeout=5.0)
    except queue.Empty:
        log.error("MCI dispatcher did not respond within 5s for %r", cmd)
        return -1
    return ret


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
        # ISSUE-011 fix: hidden message-only window receiving MM_MCINOTIFY.
        # _notify_hwnd: None = not yet attempted, 0 = unavailable (polling
        # fallback), nonzero = the window's HWND.
        self._notify_hwnd: int | None = None
        self._notify_thread: threading.Thread | None = None
        self._notify_init_lock = threading.Lock()
        self._wndproc_ref = None  # strong ref so ctypes never GCs the callback
        self._notify_ctx: dict | None = None  # current playback's notify context
        self._notify_token = 0  # monotonic per-playback token (ISSUE-028 pattern)

    # ------------------------------------------------------------------
    # MM_MCINOTIFY window (ISSUE-011)
    # ------------------------------------------------------------------

    def _ensure_notify_window(self) -> int:
        """Return the hidden notify window's HWND, creating it on first use.

        Returns 0 when the window is unavailable (creation failed); play()
        then falls back to polling end-of-track detection. The result is
        cached either way — creation is attempted at most once per player.
        """
        with self._notify_init_lock:
            if self._notify_hwnd is not None:
                return self._notify_hwnd
            ready = threading.Event()
            result = {"hwnd": 0}
            self._notify_thread = threading.Thread(
                target=self._notify_window_main, args=(ready, result),
                daemon=True, name="mci-notify-window")
            self._notify_thread.start()
            if not ready.wait(timeout=5.0):
                log.warning("MCI notify window thread did not become ready within 5s")
            self._notify_hwnd = result["hwnd"]
            if not self._notify_hwnd:
                log.warning("MCI notify window unavailable; falling back to "
                            "polling end-of-track detection")
            return self._notify_hwnd

    def _notify_window_main(self, ready: threading.Event, result: dict) -> None:
        """Notify-thread main: create a message-only window and pump messages.

        A window must be created and pumped on the same thread, hence this
        dedicated daemon thread.
        """
        hinst = None
        cls_name = None
        try:
            wndproc = WNDPROC(self._wndproc)
            self._wndproc_ref = wndproc  # keep alive for the window's lifetime
            hinst = _kernel32.GetModuleHandleW(None)
            cls_name = f"DocumentReaderMCINotify{next(_wndclass_counter)}"
            wc = _WNDCLASSW()
            wc.lpfnWndProc = wndproc
            wc.lpszClassName = cls_name
            wc.hInstance = hinst
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError("RegisterClassW failed")
            hwnd = _user32.CreateWindowExW(
                0, cls_name, "DocumentReader MCI notify", 0,
                0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
            if not isinstance(hwnd, int) or not hwnd:
                raise OSError(f"CreateWindowExW returned {hwnd!r}")
            result["hwnd"] = hwnd
        except Exception:
            log.warning("Could not create MM_MCINOTIFY window; polling fallback "
                        "will be used", exc_info=True)
            result["hwnd"] = 0
            ready.set()
            return
        ready.set()
        log.debug("MCI notify window created (hwnd=0x%x)", hwnd)
        msg = wintypes.MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if not isinstance(ret, int) or ret <= 0:
                break  # 0 = WM_QUIT, -1 = error
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        try:
            _user32.UnregisterClassW(cls_name, hinst)
        except Exception:
            pass
        log.debug("MCI notify window thread exiting")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        """Thin ctypes WNDPROC; the real handling lives in _handle_mci_notify
        so tests can exercise it without a live window."""
        try:
            if msg == MM_MCINOTIFY:
                self._handle_mci_notify(wparam, lparam)
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("Error in MCI notify wndproc (msg=0x%x)", msg)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_mci_notify(self, wparam, lparam) -> None:
        """Handle MM_MCINOTIFY for the current playback.

        Runs on the notify-window thread. MCI_NOTIFY_SUCCESSFUL (natural
        completion) is the ONLY code that may fire on_done; SUPERSEDED /
        ABORTED / FAILURE arrive when stop() or a new play() interrupts the
        device and must be suppressed (matches the existing stop-suppression
        semantics). on_done itself is dispatched detached (ISSUE-022), never
        inline on this thread.
        """
        if wparam != MCI_NOTIFY_SUCCESSFUL:
            log.debug("MM_MCINOTIFY code=0x%x ignored (device=%s)", wparam, lparam)
            return
        with self._lock:
            ctx = self._notify_ctx
            if ctx is None or ctx["fired"] or ctx["stop_event"].is_set():
                return
            token = ctx["token"]
        # Stale-notify defense: a SUCCESSFUL posted for playback N can still
        # sit in the message queue when a stop()+play() pair installs
        # playback N+1 (MCI device ids are reused across close/open, so
        # lparam cannot disambiguate). If the device is actively playing,
        # this notify cannot belong to the current playback — drop it; the
        # current playback will post its own notify when it finishes.
        mode = _mci_query(f'status {_ALIAS} mode')
        if mode not in ("stopped", ""):
            log.debug("Stale MM_MCINOTIFY ignored (mode=%r, token=%d)", mode, token)
            return
        log.debug("MM_MCINOTIFY: track finished (token=%d)", token)
        self._complete_playback(token)

    def _complete_playback(self, token: int) -> bool:
        """Mark playback `token` naturally complete and fire its on_done.

        Token-guarded and idempotent, so a queued stale notify or the
        watchdog can never complete a newer playback or fire on_done twice
        (mirrors the ISSUE-028 per-playback design). Returns True when this
        call performed the completion.
        """
        with self._lock:
            ctx = self._notify_ctx
            if ctx is None or ctx["token"] != token or ctx["fired"]:
                return False
            if ctx["stop_event"].is_set():
                return False
            ctx["fired"] = True
            self._playing = False
            self._paused = False
            cb = ctx["on_done"]
        if cb:
            # ISSUE-022 pattern: never run on_done inline — it ends in
            # event_generate, which blocks until the GUI services it.
            threading.Thread(target=cb, daemon=True, name="on-done-dispatch").start()
        return True

    def _notify_watchdog(self, stop_event: threading.Event, token: int) -> None:
        """Safety net behind MM_MCINOTIFY: if the notify message is ever
        lost, detect the stopped device on a slow (2s) poll so the sentence
        pump cannot stall. Captures its own stop_event/token (ISSUE-028
        pattern) and exits promptly when stop() sets the event."""
        while not stop_event.is_set():
            if stop_event.wait(2.0):
                break
            with self._lock:
                ctx = self._notify_ctx
                if ctx is None or ctx["token"] != token or ctx["fired"]:
                    return
                if not self._open:
                    return
            mode = _mci_query(f'status {_ALIAS} mode')
            if stop_event.is_set():
                break
            if mode == "stopped":
                log.warning("Watchdog: device stopped but MM_MCINOTIFY was not "
                            "received; completing playback (token=%d)", token)
                self._complete_playback(token)
                return

    def close(self) -> None:
        """Release the player: stop playback and tear down the notify window."""
        self.stop()
        with self._notify_init_lock:
            hwnd = self._notify_hwnd
            thread = self._notify_thread
            self._notify_hwnd = 0  # any future play() uses the polling fallback
        if hwnd:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            if (thread and thread.is_alive()
                    and threading.current_thread() is not thread):
                thread.join(timeout=2.0)
                if thread.is_alive():
                    log.warning("MCI notify window thread did not exit within 2s")

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

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

        # ISSUE-011 fix: the primary end-of-track signal is MM_MCINOTIFY
        # posted by `play ... notify` to the hidden window — no polling
        # latency and no warmup/drain sleeps between sentences. SUCCESSFUL
        # means the device finished playing, so audio is never cut short.
        notify_active = False
        token = 0
        hwnd = self._ensure_notify_window()
        if hwnd:
            with self._lock:
                self._notify_token += 1
                token = self._notify_token
                self._notify_ctx = {
                    "token": token,
                    "stop_event": stop_event,
                    "on_done": on_done,
                    "fired": False,
                }
            ret = _mci_notify(f'play {_ALIAS} notify', hwnd)
            if ret == 0:
                notify_active = True
            else:
                log.warning("play ... notify failed (ret=%d); falling back to "
                            "polling end-of-track detection", ret)
                with self._lock:
                    self._notify_ctx = None
        if not notify_active:
            _mci(f'play {_ALIAS}')

        with self._lock:
            self._playing = True
            self._paused = False

        if notify_active:
            # Slow watchdog only — MM_MCINOTIFY does the real work. The
            # watchdog doubles as the joinable monitor thread so stop()'s
            # join semantics (ISSUE-001/022) are unchanged.
            self._monitor_thread = threading.Thread(
                target=self._notify_watchdog, args=(stop_event, token),
                daemon=True, name="mci-notify-watchdog")
            self._monitor_thread.start()
            return

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
            # ISSUE-011: drop the notify context so a queued MM_MCINOTIFY
            # (or the watchdog) can never fire this playback's on_done after
            # stop — MCI itself reports ABORTED/SUPERSEDED for the stop/close
            # below, which _handle_mci_notify also suppresses.
            self._notify_ctx = None
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
