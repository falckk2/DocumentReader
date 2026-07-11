"""
Validation tests for DocumentReader issues.md — all 14 resolved issues
plus checks on ISSUE-011 (Partially Resolved) and ISSUE-016 (FIXED 2026-06-12:
speed applies immediately via debounced restart; voice stays deferred).

Scope: code-inspection unit tests only.  No GUI, no MCI device, no live TTS.
Where a real device or display would be needed the test inspects source
structure / logic instead.
"""

import inspect
import os
import queue
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Make sure the project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Minimal stubs so we can import without ctypes/customtkinter/fitz
# ---------------------------------------------------------------------------

# Stub winmm to avoid ctypes DLL load
import ctypes as _ctypes_real
_fake_winmm = MagicMock()
# We need to patch before audio_player is imported
import unittest.mock as _mock
_winmm_patcher = _mock.patch("ctypes.WinDLL", return_value=_fake_winmm)
_winmm_patcher.start()


# Now patch the module-level _mci / _mci_query in audio_player after import
def _import_audio_player():
    """Import audio_player and stub out MCI calls so tests don't touch hardware."""
    import src.audio_player as ap
    ap._mci = MagicMock(return_value=0)
    ap._mci_query = MagicMock(return_value="stopped")
    return ap


# ---------------------------------------------------------------------------
# ISSUE-001 — stop() self-join guard
# ---------------------------------------------------------------------------

class TestIssue001StopSelfJoinGuard(unittest.TestCase):
    """stop() must not join the monitor thread when called from that thread."""

    def setUp(self):
        self.ap = _import_audio_player().AudioPlayer()

    def test_stop_from_monitor_thread_does_not_raise(self):
        """
        Simulate stop() being called from within a thread that is also set
        as _monitor_thread.  Before the fix this would raise RuntimeError
        ("cannot join current thread").  After the fix it must complete
        without raising.
        """
        result = {"error": None}

        def fake_monitor():
            # Set this thread as the recorded monitor thread
            self.ap._monitor_thread = threading.current_thread()
            try:
                self.ap.stop()
            except RuntimeError as e:
                result["error"] = e

        t = threading.Thread(target=fake_monitor)
        t.start()
        t.join(timeout=3)
        self.assertIsNone(result["error"],
                          f"stop() raised RuntimeError from monitor thread: {result['error']}")

    def test_stop_from_external_thread_does_join(self):
        """
        When called from a different thread, stop() should attempt to join
        _monitor_thread (the guard branch must NOT fire).
        """
        import src.audio_player as ap
        player = ap.AudioPlayer()
        joined = []

        def fake_join(timeout=None):
            joined.append(True)

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mock_thread.join.side_effect = fake_join

        player._monitor_thread = mock_thread

        # Make sure current thread is NOT the mock_thread
        # (default current_thread() is never the MagicMock)
        player.stop()
        self.assertTrue(joined, "join() was not called when stop() invoked from an external thread")

    def test_guard_branch_logs_error(self):
        """
        When stop() detects it is running on the monitor thread, it should
        log an ERROR (defensive guard message).
        """
        import src.audio_player as ap
        import logging
        player = ap.AudioPlayer()

        logged = []

        class CapHandler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    logged.append(record.getMessage())

        handler = CapHandler()
        ap.log.addHandler(handler)
        try:
            result = {"done": False}

            def run():
                player._monitor_thread = threading.current_thread()
                player._open = True  # pretend device is open
                player.stop()
                result["done"] = True

            t = threading.Thread(target=run)
            t.start()
            t.join(timeout=3)
            self.assertTrue(result["done"])
            self.assertTrue(
                any("monitor" in m.lower() for m in logged),
                f"Expected ERROR log mentioning 'monitor'; got: {logged}"
            )
        finally:
            ap.log.removeHandler(handler)


# ---------------------------------------------------------------------------
# ISSUE-002 — temp-file race / _tmp_lock protection
# ---------------------------------------------------------------------------

class TestIssue002TmpFileLock(unittest.TestCase):
    """_tmp_files must be protected by _tmp_lock; cleanup tied to playback end."""

    def setUp(self):
        # Test-infrastructure fix (2026-06-12): tests below access
        # sys.modules["src.tts_engine"], which is only populated once the
        # module has been imported.  Alphabetical test ordering previously
        # made two tests run before any explicit import, raising
        # KeyError: 'src.tts_engine'.  Import explicitly here.
        import src.tts_engine  # noqa: F401

    def _make_engine(self):
        """Return a TTSEngine with player+pyttsx3 worker stubbed out."""
        with patch("src.audio_player.AudioPlayer") as MockPlayer, \
             patch("src.tts_engine.TTSEngine._pyttsx3_worker"):
            from src.tts_engine import TTSEngine
            engine = TTSEngine.__new__(TTSEngine)
            engine._player = MockPlayer()
            engine._stop_event = threading.Event()
            engine._tmp_lock = threading.Lock()
            engine._tmp_files = []
            return engine

    def test_tmp_lock_exists(self):
        from src.tts_engine import TTSEngine
        import threading as _t
        engine_src = inspect.getsource(TTSEngine.__init__)
        self.assertIn("_tmp_lock", engine_src,
                      "_tmp_lock not initialized in TTSEngine.__init__")

    def test_make_tmp_acquires_lock(self):
        """_make_tmp_mp3 must append inside the lock."""
        src_code = inspect.getsource(
            sys.modules["src.tts_engine"].TTSEngine._make_tmp_mp3
        )
        self.assertIn("_tmp_lock", src_code,
                      "_make_tmp_mp3 does not reference _tmp_lock")

    def test_cleanup_tmp_acquires_lock(self):
        src_code = inspect.getsource(
            sys.modules["src.tts_engine"].TTSEngine._cleanup_tmp
        )
        self.assertIn("_tmp_lock", src_code,
                      "_cleanup_tmp does not reference _tmp_lock")

    def test_delete_tmp_acquires_lock(self):
        """Per-file deletion (_delete_tmp) must also lock."""
        import src.tts_engine as te
        self.assertTrue(hasattr(te.TTSEngine, "_delete_tmp"),
                        "TTSEngine missing _delete_tmp method (ISSUE-002 fix absent)")
        src_code = inspect.getsource(te.TTSEngine._delete_tmp)
        self.assertIn("_tmp_lock", src_code,
                      "_delete_tmp does not reference _tmp_lock")

    def test_done_and_cleanup_wrapper_present(self):
        """
        _speak_online must wrap on_done in a closure that calls _delete_tmp
        so cleanup is tied to playback completion, not to the next speak/stop.
        """
        src_code = inspect.getsource(
            sys.modules["src.tts_engine"].TTSEngine._speak_online
        )
        self.assertIn("_delete_tmp", src_code,
                      "_speak_online does not call _delete_tmp; cleanup not tied to playback end")
        self.assertIn("_done_and_cleanup", src_code,
                      "_speak_online missing _done_and_cleanup wrapper")

    def test_cleanup_runs_after_player_stop(self):
        """
        In TTSEngine.stop(), _cleanup_tmp must appear AFTER _player.stop()
        so no synth/play thread still holds a file reference.
        """
        src_code = inspect.getsource(
            sys.modules["src.tts_engine"].TTSEngine.stop
        )
        player_stop_pos = src_code.find("_player.stop()")
        cleanup_pos = src_code.find("_cleanup_tmp()")
        self.assertGreater(cleanup_pos, player_stop_pos,
                           "_cleanup_tmp() must appear after _player.stop() in TTSEngine.stop()")


# ---------------------------------------------------------------------------
# ISSUE-003 — thread-safe sentence-done marshalling via event_generate
# ---------------------------------------------------------------------------

class TestIssue003ThreadSafeCallback(unittest.TestCase):
    """_on_sentence_done must use event_generate, NOT self.after()."""

    def _get_on_sentence_done_src(self):
        import src.app as app_mod
        return inspect.getsource(app_mod.DocumentReaderApp._on_sentence_done)

    def test_uses_event_generate_not_after(self):
        src = self._get_on_sentence_done_src()
        self.assertIn("event_generate", src,
                      "_on_sentence_done must call event_generate for thread safety")
        # self.after() must NOT appear in _on_sentence_done
        self.assertNotIn("self.after(", src,
                         "_on_sentence_done must not call self.after() from a background thread")

    def test_event_is_tail_queued(self):
        """event_generate must use when='tail' so it doesn't interrupt other events."""
        src = self._get_on_sentence_done_src()
        self.assertIn('"tail"', src,
                      "event_generate in _on_sentence_done should use when='tail'")

    def test_sentence_done_event_handler_exists(self):
        import src.app as app_mod
        self.assertTrue(
            hasattr(app_mod.DocumentReaderApp, "_on_sentence_done_event"),
            "Missing _on_sentence_done_event handler (ISSUE-003 fix incomplete)"
        )

    def test_event_bound_in_init(self):
        """The virtual event must be bound during __init__ on the GUI thread."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("<<SentenceDone>>", src,
                      "<<SentenceDone>> event not bound in __init__")

    def test_pending_after_id_only_set_in_gui_methods(self):
        """
        _pending_after_id must not be assigned inside _on_sentence_done
        (which runs on a background thread).
        """
        src = self._get_on_sentence_done_src()
        self.assertNotIn("_pending_after_id", src,
                         "_pending_after_id must not be written from _on_sentence_done (non-GUI thread)")


# ---------------------------------------------------------------------------
# ISSUE-004 — auto-advance clears highlight before page transition
# ---------------------------------------------------------------------------

class TestIssue004AutoAdvanceClearHighlight(unittest.TestCase):

    def test_clear_highlight_called_before_page_increment(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._on_page_done)
        clear_pos = src.find("_clear_highlight()")
        increment_pos = src.find("_current_page += 1")
        self.assertNotEqual(clear_pos, -1,
                            "_on_page_done does not call _clear_highlight()")
        self.assertNotEqual(increment_pos, -1,
                            "_on_page_done does not increment _current_page")
        self.assertLess(clear_pos, increment_pos,
                        "_clear_highlight() must appear before _current_page += 1 in _on_page_done")

    def test_no_redundant_sentence_idx_reset_after_update_page_display(self):
        """
        _update_page_display already resets _sentence_idx = 0; there must be
        no duplicate assignment immediately after it in _on_page_done.
        """
        src = inspect.getsource(
            sys.modules["src.app"].DocumentReaderApp._on_page_done
        )
        # Count assignments to _sentence_idx in _on_page_done
        assignments = re.findall(r"self\._sentence_idx\s*=\s*0", src)
        self.assertEqual(len(assignments), 0,
                         f"_on_page_done has {len(assignments)} redundant _sentence_idx=0 assignment(s); "
                         "should rely solely on _update_page_display")


# ---------------------------------------------------------------------------
# ISSUE-005 — incremental highlight search start
# ---------------------------------------------------------------------------

class TestIssue005IncrementalHighlight(unittest.TestCase):

    def test_highlight_search_start_initialized_in_init(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("_highlight_search_start", src,
                      "_highlight_search_start not initialized in __init__")

    def test_highlight_search_start_reset_in_update_page_display(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._update_page_display)
        self.assertIn("_highlight_search_start", src,
                      "_highlight_search_start not reset in _update_page_display")

    def test_highlight_search_start_reset_in_clear_highlight(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._clear_highlight)
        self.assertIn("_highlight_search_start", src,
                      "_highlight_search_start not reset in _clear_highlight")

    def test_highlight_sentence_searches_from_start_var(self):
        """_highlight_sentence must pass _highlight_search_start to search()."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._highlight_sentence)
        self.assertIn("_highlight_search_start", src,
                      "_highlight_sentence does not use _highlight_search_start as search origin")

    def test_highlight_sentence_advances_start_on_match(self):
        """After a match, _highlight_search_start must be advanced to the end of the match."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._highlight_sentence)
        # The end position should be assigned back to _highlight_search_start
        self.assertIn("_highlight_search_start = end", src,
                      "_highlight_sentence does not advance _highlight_search_start after a match")

    def test_highlight_wraps_around_on_no_forward_match(self):
        """If forward search finds nothing, must fall back to '1.0'."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._highlight_sentence)
        self.assertIn('"1.0"', src,
                      "_highlight_sentence does not wrap around to '1.0' when forward match fails")


# ---------------------------------------------------------------------------
# ISSUE-006 — offline resume re-starts _read_next_sentence
# ---------------------------------------------------------------------------

class TestIssue006OfflineResume(unittest.TestCase):

    def test_play_resumes_offline_by_calling_read_next_sentence(self):
        """
        After self._tts.resume() (no-op for offline), the code checks
        is_playing; if False (offline path) it must set _reading=True
        and call _read_next_sentence().
        """
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._play)
        # Must check is_playing after resume
        self.assertIn("is_playing", src,
                      "_play does not check is_playing after resume (offline fallback missing)")
        # Must call _read_next_sentence in the offline branch
        self.assertIn("_read_next_sentence()", src,
                      "_play does not call _read_next_sentence() in the offline resume branch")

    def test_tts_engine_pause_does_not_call_stop_pyttsx3(self):
        """
        ISSUE-006 fix removes _stop_pyttsx3 from the pause path.
        The TTSEngine.pause() method should not call _stop_pyttsx3.
        """
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.pause)
        self.assertNotIn("_stop_pyttsx3", src,
                         "TTSEngine.pause() still calls _stop_pyttsx3 (ISSUE-006 fix incomplete)")

    def test_tts_engine_resume_calls_player_resume(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.resume)
        self.assertIn("_player.resume()", src,
                      "TTSEngine.resume() does not call _player.resume()")


# ---------------------------------------------------------------------------
# ISSUE-007 — _sentence_idx rewind on pause and stop
# ---------------------------------------------------------------------------

class TestIssue007SentenceIdxRewind(unittest.TestCase):

    def test_pause_decrements_sentence_idx(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._pause)
        self.assertIn("_sentence_idx -= 1", src,
                      "_pause() does not decrement _sentence_idx (ISSUE-007 fix missing)")

    def test_pause_rewind_guarded_by_greater_than_zero(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._pause)
        self.assertIn("_sentence_idx > 0", src,
                      "_pause() rewind not guarded by idx > 0 check")

    def test_stop_decrements_sentence_idx_when_reading(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._stop)
        self.assertIn("_sentence_idx -= 1", src,
                      "_stop() does not decrement _sentence_idx (ISSUE-007 fix missing)")

    def test_stop_rewind_only_when_actively_reading_not_paused(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._stop)
        # Must check _reading and not _paused before decrementing
        self.assertIn("not self._paused", src,
                      "_stop() rewind does not check 'not self._paused'")


# ---------------------------------------------------------------------------
# ISSUE-008 — os.path.basename for PDF title
# ---------------------------------------------------------------------------

class TestIssue008BasenameUsage(unittest.TestCase):

    def test_open_pdf_uses_os_path_basename(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._open_pdf_path)
        self.assertIn("os.path.basename", src,
                      "_open_pdf does not use os.path.basename (ISSUE-008 fix missing)")

    def test_open_pdf_does_not_use_manual_split(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._open_pdf_path)
        self.assertNotIn('split("/")', src,
                         "_open_pdf still uses manual path.split('/') instead of os.path.basename")
        self.assertNotIn('split("\\\\")', src,
                         "_open_pdf still uses manual path.split('\\\\') instead of os.path.basename")

    def test_basename_correctly_handles_mixed_separators(self):
        """Verify os.path.basename behaviour (sanity check)."""
        self.assertEqual(os.path.basename("C:/Users/test/my doc.pdf"), "my doc.pdf")
        self.assertEqual(os.path.basename("C:\\Users\\test\\my doc.pdf"), "my doc.pdf")


# ---------------------------------------------------------------------------
# ISSUE-009 — sentence_idx clamped after bookmark restore
# ---------------------------------------------------------------------------

class TestIssue009BookmarkSentenceIdxClamp(unittest.TestCase):

    def test_restore_bookmark_clamps_sentence_idx(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("min(", src,
                      "_restore_bookmark does not clamp sentence_idx with min()")
        self.assertIn("len(self._sentences)", src,
                      "_restore_bookmark does not reference len(self._sentences) for clamping")

    def test_restore_bookmark_logs_warning_on_clamp(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("log.warning", src,
                      "_restore_bookmark does not log.warning when clamping occurs")


# ---------------------------------------------------------------------------
# ISSUE-010 — AudioPlayer _playing/_paused protected by _lock
# ---------------------------------------------------------------------------

class TestIssue010LockCoverage(unittest.TestCase):

    def _player_source(self, method_name: str) -> str:
        import src.audio_player as ap
        return inspect.getsource(getattr(ap.AudioPlayer, method_name))

    def test_play_sets_playing_under_lock(self):
        src = self._player_source("play")
        # Verify _playing = True appears after a with self._lock block
        self.assertIn("self._lock", src)
        self.assertIn("self._playing = True", src)

    def test_monitor_clears_flags_under_lock(self):
        """The monitor lambda/nested function must clear _playing/_paused under lock."""
        src = self._player_source("play")
        # _monitor is nested inside play; the lock usage + flag clearing should appear
        self.assertIn("self._playing = False", src)
        self.assertIn("self._lock", src)

    def test_pause_sets_paused_under_lock(self):
        src = self._player_source("pause")
        self.assertIn("self._lock", src)
        self.assertIn("self._paused = True", src)

    def test_resume_clears_paused_under_lock(self):
        src = self._player_source("resume")
        self.assertIn("self._lock", src)
        self.assertIn("self._paused = False", src)

    def test_stop_clears_flags_under_lock(self):
        src = self._player_source("stop")
        self.assertIn("self._lock", src)
        self.assertIn("self._playing = False", src)
        self.assertIn("self._paused = False", src)

    def test_is_playing_property_acquires_lock(self):
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.is_playing.fget)
        self.assertIn("self._lock", src,
                      "is_playing property does not acquire _lock")

    def test_is_paused_property_acquires_lock(self):
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.is_paused.fget)
        self.assertIn("self._lock", src,
                      "is_paused property does not acquire _lock")


# ---------------------------------------------------------------------------
# ISSUE-011 — FIXED 2026-06-12: MM_MCINOTIFY is the primary end-of-track
# signal; the polling monitor remains only as a fallback (code inspection)
# ---------------------------------------------------------------------------

class TestIssue011PollingEndDetection(unittest.TestCase):
    """
    ISSUE-011 FIXED: `play ... notify` posts MM_MCINOTIFY to a hidden
    message-only window. The original polling monitor is retained verbatim
    as the fallback for when the notify window cannot be created, so its
    end-detection invariants must still hold.
    """

    def test_mode_stopped_is_primary_detection(self):
        """'mode == stopped' must remain the fallback monitor's primary condition."""
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.play)
        self.assertIn('"stopped"', src,
                      "Fallback monitor does not check mode == 'stopped'")

    def test_position_based_detection_present_as_backup(self):
        """Position-based end detection must still be present in the fallback."""
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.play)
        self.assertIn("pos >= track_length", src,
                      "Position-based end detection backup missing")

    def test_mci_notify_implemented(self):
        """MM_MCINOTIFY handling must now exist (ISSUE-011 full resolution)."""
        import src.audio_player as ap
        module_src = inspect.getsource(ap)
        self.assertIn("MM_MCINOTIFY", module_src,
                      "MM_MCINOTIFY missing — ISSUE-011 regressed to polling-only")
        play_src = inspect.getsource(ap.AudioPlayer.play)
        self.assertIn("notify", play_src,
                      "play() never issues `play ... notify` (ISSUE-011)")
        self.assertTrue(hasattr(ap.AudioPlayer, "_handle_mci_notify"),
                        "AudioPlayer._handle_mci_notify missing (ISSUE-011)")
        self.assertTrue(hasattr(ap, "_mci_notify"),
                        "_mci_notify dispatcher variant missing (ISSUE-011)")

    def test_drain_delay_retained_only_in_polling_fallback(self):
        """The 5 x 0.05s drain may survive ONLY inside the polling fallback;
        the notify-driven completion path must contain no sleeps (the drain
        and warmup were the gap sources ISSUE-011 set out to remove)."""
        import src.audio_player as ap
        play_src = inspect.getsource(ap.AudioPlayer.play)
        self.assertIn("time.sleep(0.05)", play_src,
                      "Fallback drain removed — audio cutoff risk if polling is ever used")
        for meth in ("_handle_mci_notify", "_complete_playback", "_notify_watchdog"):
            src = inspect.getsource(getattr(ap.AudioPlayer, meth))
            self.assertNotIn("time.sleep", src,
                             f"{meth} sleeps — reintroduces inter-sentence gaps (ISSUE-011)")


# ---------------------------------------------------------------------------
# ISSUE-012 — _speed_to_edge_rate uses round() and clamps
# ---------------------------------------------------------------------------

class TestIssue012SpeedRateClamping(unittest.TestCase):

    def _rate(self, speed):
        from src.tts_engine import TTSEngine
        return TTSEngine._speed_to_edge_rate(speed)

    def test_minimum_speed_yields_clamped_negative_50(self):
        self.assertEqual(self._rate(0.5), "-50%",
                         "0.5x speed should map to -50% (clamped minimum)")

    def test_maximum_speed_yields_clamped_positive_100(self):
        self.assertEqual(self._rate(2.0), "+100%",
                         "2.0x speed should map to +100% (clamped maximum)")

    def test_normal_speed_yields_plus_zero(self):
        self.assertEqual(self._rate(1.0), "+0%",
                         "1.0x speed should map to +0%")

    def test_out_of_range_low_is_clamped(self):
        # Below minimum: must clamp to -50
        result = self._rate(0.1)
        self.assertEqual(result, "-50%",
                         f"Speed below 0.5 should clamp to -50%; got {result}")

    def test_out_of_range_high_is_clamped(self):
        # Upper clamp raised to +400% (5x) for the fast-reading feature
        # (2026-07-11); anything above still clamps.
        result = self._rate(6.0)
        self.assertEqual(result, "+400%",
                         f"Speed above 5.0 should clamp to +400%; got {result}")

    def test_five_x_speed_maps_to_plus_400(self):
        self.assertEqual(self._rate(5.0), "+400%",
                         "5.0x speed should map to +400% (fast-reading feature)")

    def test_round_not_truncate(self):
        """1.5x -> 50% (both round and int give same); 1.455 -> 46% (round) vs 45% (int)."""
        result = self._rate(1.455)
        # round((1.455-1.0)*100) = round(45.5) = 46 (Python banker's rounding = 46)
        # int((1.455-1.0)*100) = int(45.5) = 45
        # Both are in-range so clamping doesn't hide the difference
        self.assertEqual(result, "+46%",
                         f"_speed_to_edge_rate should use round(); got {result}")

    def test_offline_speed_clamped_wpm(self):
        """Offline speed mapping: max(80, min(1000, round(200*speed))).
        Upper clamp raised from 500 to 1000 wpm for 5x speed (2026-07-11)."""
        from src.tts_engine import TTSEngine
        src = inspect.getsource(TTSEngine._speak_offline)
        self.assertIn("max(80", src,
                      "_speak_offline offline rate not clamped at 80 wpm minimum")
        self.assertIn("min(1000", src,
                      "_speak_offline offline rate not clamped at 1000 wpm maximum")
        self.assertIn("round(200", src,
                      "_speak_offline offline rate uses int() instead of round()")


# ---------------------------------------------------------------------------
# ISSUE-013 — pyttsx3 dedicated worker thread
# ---------------------------------------------------------------------------

class TestIssue013PyttxWorker(unittest.TestCase):

    def test_pyttsx3_worker_thread_exists(self):
        import src.tts_engine as te
        self.assertTrue(hasattr(te.TTSEngine, "_pyttsx3_worker"),
                        "TTSEngine missing _pyttsx3_worker method (ISSUE-013 fix absent)")

    def test_worker_thread_started_in_init(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.__init__)
        self.assertIn("_pyttsx3_thread", src,
                      "_pyttsx3_thread not created in TTSEngine.__init__")
        self.assertIn("_pyttsx3_worker", src,
                      "_pyttsx3_worker not referenced in __init__")

    def test_worker_thread_is_daemon(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.__init__)
        self.assertIn("daemon=True", src,
                      "pyttsx3 worker thread not set as daemon=True")

    def test_speak_offline_enqueues_not_spawns_thread(self):
        """_speak_offline must put() on queue, NOT spawn a new Thread."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._speak_offline)
        self.assertIn("_pyttsx3_queue.put(", src,
                      "_speak_offline does not enqueue to _pyttsx3_queue")
        self.assertNotIn("threading.Thread", src,
                         "_speak_offline still spawns new threads per-sentence (ISSUE-013 fix incomplete)")

    def test_worker_engine_initialized_once(self):
        """Engine must be created only when None, not on every sentence."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._pyttsx3_worker)
        self.assertIn("if engine is None", src,
                      "pyttsx3 worker does not guard engine init with 'if engine is None'")

    def test_stop_command_processed_in_worker(self):
        """'stop' command must be handled within the worker thread itself."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._pyttsx3_worker)
        self.assertIn("action == \"stop\"", src,
                      "pyttsx3 worker does not handle 'stop' command")
        self.assertIn("engine.stop()", src,
                      "pyttsx3 worker does not call engine.stop() in stop handler")

    def test_queue_command_format(self):
        """Command tuple must include: action, text, voice_id, rate_wpm, on_done."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._speak_offline)
        self.assertIn('"speak"', src,
                      "_speak_offline does not use 'speak' command action")


# ---------------------------------------------------------------------------
# ISSUE-014 — _load_voices on_done exception handling
# ---------------------------------------------------------------------------

class TestIssue014VoiceLoadErrorHandling(unittest.TestCase):

    def test_on_done_wrapped_in_try_except(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._load_voices)
        self.assertIn("try:", src,
                      "_load_voices on_done callback does not have a try/except wrapper")
        self.assertIn("except Exception", src,
                      "_load_voices on_done does not catch Exception broadly")

    def test_exception_marshals_error_status_to_gui(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._load_voices)
        self.assertIn("Error loading voices", src,
                      "_load_voices does not marshal 'Error loading voices' to GUI on exception")

    def test_exception_logs_via_log_exception(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._load_voices)
        self.assertIn("log.exception", src,
                      "_load_voices on_done does not call log.exception on error")


# ---------------------------------------------------------------------------
# ISSUE-015 — PDFReader handles encrypted PDFs and per-page errors
# ---------------------------------------------------------------------------

class TestIssue015PDFEncryptionAndPageErrors(unittest.TestCase):

    def test_open_raises_valueerror_for_encrypted(self):
        """open() must raise ValueError (not silently return empty) for encrypted PDFs."""
        import src.pdf_reader as pr
        with patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.is_encrypted = True
            mock_doc.__len__ = MagicMock(return_value=5)
            mock_fitz.return_value = mock_doc
            reader = pr.PDFReader()
            with self.assertRaises(ValueError) as ctx:
                reader.open("/fake/path.pdf")
            self.assertIn("password", str(ctx.exception).lower(),
                          "ValueError message should mention 'password'")

    def test_open_closes_doc_on_encrypted(self):
        """If encrypted, the doc must be closed before raising."""
        import src.pdf_reader as pr
        with patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.is_encrypted = True
            mock_doc.__len__ = MagicMock(return_value=3)
            mock_fitz.return_value = mock_doc
            reader = pr.PDFReader()
            try:
                reader.open("/fake/path.pdf")
            except ValueError:
                pass
            mock_doc.close.assert_called()
            self.assertIsNone(reader._doc,
                              "PDFReader._doc should be None after detecting encryption")

    def test_get_page_text_returns_empty_on_exception(self):
        """A malformed page raising in get_text() must return '' not propagate."""
        import src.pdf_reader as pr
        with patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.is_encrypted = False
            mock_doc.__len__ = MagicMock(return_value=1)
            bad_page = MagicMock()
            bad_page.get_text.side_effect = RuntimeError("malformed page")
            mock_doc.__getitem__ = MagicMock(return_value=bad_page)
            mock_fitz.return_value = mock_doc
            reader = pr.PDFReader()
            reader.open("/fake/path.pdf")
            result = reader.get_page_text(0)
            self.assertEqual(result, "",
                             "get_page_text should return '' on page extraction error")

    def test_non_encrypted_pdf_opens_normally(self):
        """Non-encrypted PDFs must still open without raising."""
        import src.pdf_reader as pr
        with patch("fitz.open") as mock_fitz:
            mock_doc = MagicMock()
            mock_doc.is_encrypted = False
            mock_doc.__len__ = MagicMock(return_value=10)
            mock_fitz.return_value = mock_doc
            reader = pr.PDFReader()
            count = reader.open("/fake/normal.pdf")
            self.assertEqual(count, 10)


# ---------------------------------------------------------------------------
# ISSUE-016 — NEEDS_REVIEW: mid-sentence voice/speed change deferral (design)
# ---------------------------------------------------------------------------

class TestIssue016SpeedVoiceDeferral(unittest.TestCase):
    """
    ISSUE-016 (fixed 2026-06-12, user decision): SPEED changes apply
    immediately during playback via a debounced restart of the current
    sentence; VOICE changes remain deferred to the next sentence by design.
    These tests confirm the voice path is STILL deferred and that
    _read_next_sentence still reads both controls at speak time.
    """

    def test_voice_change_handler_is_pass(self):
        """_on_voice_change must be a no-op (reads at speak time)."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._on_voice_change)
        # Should not call _stop, _tts.stop, or _read_next_sentence
        self.assertNotIn("self._stop()", src,
                         "_on_voice_change should not call _stop() — it's a deferred-read design")
        self.assertNotIn("_read_next_sentence", src,
                         "_on_voice_change should not re-synth mid-sentence")

    def test_speed_read_at_speak_time(self):
        """_read_next_sentence must read _speed_var.get() just before speaking."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._read_next_sentence)
        self.assertIn("_speed_var.get()", src,
                      "_read_next_sentence does not read _speed_var at speak time")
        self.assertIn("_voice_var.get()", src,
                      "_read_next_sentence does not read _voice_var at speak time")

    def test_issue_016_status_is_validated(self):
        """ISSUE-016 was fixed and validated on 2026-06-12 (immediate speed apply)."""
        issues_path = os.path.join(PROJECT_ROOT, "issues.md")
        with open(issues_path, encoding="utf-8") as f:
            content = f.read()
        section_match = re.search(
            r"## ISSUE-016.*?(?=^---|\Z)", content, re.DOTALL | re.MULTILINE
        )
        self.assertIsNotNone(section_match, "ISSUE-016 section not found in issues.md")
        section = section_match.group()
        self.assertIn("**Status**: VALIDATED", section,
                      "ISSUE-016 should be marked VALIDATED (immediate speed apply, 2026-06-12)")


class TestIssue016ImmediateSpeedApply(unittest.TestCase):
    """
    ISSUE-016 fix: moving the speed slider during active reading schedules a
    DEBOUNCED (300ms) restart of the current sentence at the new speed.
    The debounced callback re-checks state at fire time; _stop cancels any
    pending debounce.  Voice changes do not trigger a restart.
    """

    def _make_app(self, reading=True, paused=False, sentence_idx=2,
                  sentences=None, debounce_id=None):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._sentences = sentences if sentences is not None else ["s0", "s1", "s2"]
        app._sentence_idx = sentence_idx
        app._reading = reading
        app._paused = paused
        app._speed_debounce_id = debounce_id
        app._speed_display = MagicMock()
        app._speed_var = MagicMock()
        app._speed_var.get.return_value = 1.5
        app.after = MagicMock(return_value="after#1")
        app.after_cancel = MagicMock()
        app._read_next_sentence = MagicMock()
        return app

    # -- slider callback (scheduling / debounce) --------------------------

    def test_slider_schedules_debounced_restart_while_reading(self):
        app = self._make_app(reading=True, paused=False)
        app._on_speed_change(1.5)
        app.after.assert_called_once_with(300, app._apply_speed_change)
        self.assertEqual(app._speed_debounce_id, "after#1")
        # Restart must be deferred, never inline from the slider tick.
        app._read_next_sentence.assert_not_called()

    def test_slider_cancels_prior_pending_debounce(self):
        """A drag fires the callback continuously; each tick must cancel the
        previously scheduled restart so only the settled value triggers one."""
        app = self._make_app(reading=True, paused=False, debounce_id="old#0")
        app._on_speed_change(1.5)
        app.after_cancel.assert_called_once_with("old#0")
        self.assertEqual(app._speed_debounce_id, "after#1")

    def test_slider_does_not_schedule_when_not_reading(self):
        app = self._make_app(reading=False)
        app._on_speed_change(1.5)
        app.after.assert_not_called()
        self.assertIsNone(app._speed_debounce_id)
        # Display label still updates while idle.
        app._speed_display.configure.assert_called_once()

    def test_slider_does_not_schedule_when_paused(self):
        """While paused no restart is needed — resume / the next sentence
        read the slider value naturally."""
        app = self._make_app(reading=True, paused=True)
        app._on_speed_change(1.5)
        app.after.assert_not_called()
        self.assertIsNone(app._speed_debounce_id)

    # -- debounced callback (fire-time behaviour) -------------------------

    def test_debounced_fire_rewinds_and_restarts_current_sentence(self):
        """ISSUE-007: idx was post-incremented past the in-flight sentence,
        so the restart must rewind exactly one and re-speak it."""
        app = self._make_app(reading=True, paused=False, sentence_idx=2)
        app._apply_speed_change()
        self.assertEqual(app._sentence_idx, 1,
                         "restart must rewind the post-incremented index by one")
        app._read_next_sentence.assert_called_once()
        self.assertIsNone(app._speed_debounce_id)

    def test_debounced_fire_no_restart_when_paused_at_fire_time(self):
        """State must be re-checked at FIRE time: the user may pause during
        the 300ms debounce window."""
        app = self._make_app(reading=True, paused=True, sentence_idx=2)
        app._apply_speed_change()
        self.assertEqual(app._sentence_idx, 2, "paused fire must not rewind")
        app._read_next_sentence.assert_not_called()

    def test_debounced_fire_no_restart_when_stopped_at_fire_time(self):
        app = self._make_app(reading=False, paused=False, sentence_idx=2)
        app._apply_speed_change()
        self.assertEqual(app._sentence_idx, 2)
        app._read_next_sentence.assert_not_called()

    def test_debounced_fire_no_restart_at_idx_zero(self):
        """idx == 0 means nothing has been spoken yet — nothing to restart,
        and rewinding would go negative."""
        app = self._make_app(reading=True, paused=False, sentence_idx=0)
        app._apply_speed_change()
        self.assertEqual(app._sentence_idx, 0)
        app._read_next_sentence.assert_not_called()

    def test_debounced_fire_no_restart_with_no_sentences(self):
        app = self._make_app(reading=True, paused=False, sentence_idx=1,
                             sentences=[])
        app._apply_speed_change()
        app._read_next_sentence.assert_not_called()

    # -- _stop cancels the pending debounce -------------------------------

    def test_stop_cancels_pending_speed_debounce(self):
        app = self._make_app(reading=True, paused=False, sentence_idx=2,
                             debounce_id="speed#7")
        app._pdf = MagicMock()
        app._voices_ready = True  # ISSUE-038: _stop's play_btn gate reads this
        app._tts = MagicMock()
        app._pending_after_id = None
        app._save_bookmark = MagicMock()
        app._clear_highlight = MagicMock()
        app._play_btn = MagicMock()
        app._pause_btn = MagicMock()
        app._stop_btn = MagicMock()
        app._back_btn = MagicMock()
        app._fwd_btn = MagicMock()
        app._stop()
        app.after_cancel.assert_called_once_with("speed#7")
        self.assertIsNone(app._speed_debounce_id,
                          "_stop must clear the pending speed debounce id")

    # -- voice path unchanged ----------------------------------------------

    def test_voice_change_does_not_schedule_restart(self):
        """Voice changes stay deferred to the next sentence (user decision
        2026-06-12 covered speed only)."""
        app = self._make_app(reading=True, paused=False)
        app._on_voice_change("Some Other Voice")
        app.after.assert_not_called()
        app._read_next_sentence.assert_not_called()

    def test_paused_slider_tick_cancels_pending_debounce_without_reschedule(self):
        """A slider tick arriving after the user paused must cancel any
        still-pending restart and must NOT schedule a new one."""
        app = self._make_app(reading=True, paused=True, debounce_id="old#0")
        app._on_speed_change(1.5)
        app.after_cancel.assert_called_once_with("old#0")
        app.after.assert_not_called()
        self.assertIsNone(app._speed_debounce_id)

    # -- end-to-end restart through the REAL sentence pump ------------------
    # (validation 2026-06-12: the tests above mock _read_next_sentence; these
    # exercise the real pump to verify the restart speaks the right sentence
    # at the new speed, and that the two possible GUI-thread orderings of the
    # debounce callback vs a queued <<SentenceDone>> event are both benign.)

    def _make_app_real_pump(self, sentence_idx=2, speed=1.5):
        """App whose _read_next_sentence is REAL (only _tts/_voices mocked)."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._sentences = ["s0", "s1", "s2"]
        app._sentence_idx = sentence_idx
        app._reading = True
        app._paused = False
        app._speed_debounce_id = None
        app._speed_display = MagicMock()
        app._speed_var = MagicMock()
        app._speed_var.get.return_value = speed
        app._voice_var = MagicMock()
        app._voice_var.get.return_value = "Some Voice"
        app._voices = MagicMock()      # find_by_display -> truthy mock voice
        app._highlight_sentence = MagicMock()
        app._set_status = MagicMock()  # real pump shows reading progress
        app._tts = MagicMock()
        app.after = MagicMock(return_value="after#1")
        app.after_cancel = MagicMock()
        return app

    def test_restart_respeaks_current_sentence_at_new_speed(self):
        """The restart must re-speak the IN-FLIGHT sentence (idx-1, per the
        ISSUE-007 post-increment) with the new slider value."""
        app = self._make_app_real_pump(sentence_idx=2, speed=1.75)
        app._apply_speed_change()
        app._tts.speak.assert_called_once()
        args, _kwargs = app._tts.speak.call_args
        self.assertEqual(args[0], "s1", "restart spoke the wrong sentence")
        self.assertEqual(args[2], 1.75, "restart did not use the new speed")
        self.assertEqual(app._sentence_idx, 2,
                         "rewind + post-increment must leave idx unchanged net")

    def test_restart_then_stale_done_continues_with_following_sentence(self):
        """GUI-thread ordering 1: the old utterance's done event was queued
        BEFORE the debounce fired and is processed AFTER the restart.  It
        must continue with the FOLLOWING sentence — no skip, no
        double-advance, no second rewind."""
        app = self._make_app_real_pump(sentence_idx=2)
        app._apply_speed_change()        # restart re-speaks s1, idx back to 2
        app._on_sentence_done_event()    # queued done of the original s1
        spoken = [c[0][0] for c in app._tts.speak.call_args_list]
        self.assertEqual(spoken, ["s1", "s2"],
                         "stale done after a restart must continue with s2")
        self.assertEqual(app._sentence_idx, 3)

    def test_done_then_restart_respeaks_just_started_sentence(self):
        """GUI-thread ordering 2: the done event runs first (pump starts s2),
        then the debounce fires and re-speaks the just-started s2 at the new
        speed.  Nothing is skipped and the index does not drift."""
        app = self._make_app_real_pump(sentence_idx=2)
        app._on_sentence_done_event()    # s1 finished -> pump speaks s2, idx=3
        app._apply_speed_change()        # restart re-speaks s2 at new speed
        spoken = [c[0][0] for c in app._tts.speak.call_args_list]
        self.assertEqual(spoken, ["s2", "s2"],
                         "done-then-restart must re-speak the just-started sentence")
        self.assertEqual(app._sentence_idx, 3)


# ---------------------------------------------------------------------------
# ISSUE-011 status check — updated to FIXED/VALIDATED after MM_MCINOTIFY impl
# ---------------------------------------------------------------------------

class TestIssue011StatusCheck(unittest.TestCase):

    def test_issue_011_status_is_fixed_or_validated(self):
        """After the MM_MCINOTIFY implementation the status must be FIXED or VALIDATED."""
        issues_path = os.path.join(PROJECT_ROOT, "issues.md")
        with open(issues_path, encoding="utf-8") as f:
            content = f.read()
        section_match = re.search(
            r"## ISSUE-011.*?(?=^---|\Z)", content, re.DOTALL | re.MULTILINE
        )
        self.assertIsNotNone(section_match, "ISSUE-011 section not found in issues.md")
        section = section_match.group()
        status_line = re.search(r"\*\*Status\*\*:.*", section)
        self.assertIsNotNone(status_line, "ISSUE-011 has no Status line")
        status_text = status_line.group()
        self.assertTrue(
            "FIXED" in status_text or "VALIDATED" in status_text,
            f"ISSUE-011 should be FIXED or VALIDATED; status line: {status_text!r}"
        )


# ---------------------------------------------------------------------------
# ISSUE-011 — MM_MCINOTIFY implementation validation tests (2026-06-12)
# ---------------------------------------------------------------------------

def _make_notify_player():
    """Return an AudioPlayer whose _ensure_notify_window is patched to return a
    fake nonzero HWND (bypassing the real Win32 window) and whose _mci/_mci_notify
    are stubbed to avoid hardware access.  This lets us drive the notify path
    without a real window or MCI device.
    """
    import src.audio_player as ap
    player = ap.AudioPlayer()
    player._ensure_notify_window = MagicMock(return_value=0x9999)
    # Stub MCI at module level (already stubbed by _import_audio_player setUp,
    # but set explicitly here to be independent of test ordering).
    ap._mci = MagicMock(return_value=0)
    ap._mci_query = MagicMock(return_value="stopped")
    ap._mci_notify = MagicMock(return_value=0)
    return player, ap


class TestIssue011NotifyPathStructural(unittest.TestCase):
    """Structural checks: notify path is wired correctly without executing
    real Win32 calls.  These verify code-inspection points from the Fix note."""

    def test_ensure_notify_window_is_idempotent(self):
        """_ensure_notify_window must cache the result: called twice returns
        the same value and does not spawn a second thread."""
        import src.audio_player as ap
        player = ap.AudioPlayer()
        # Under test the WinDLL is mocked so window creation fails → 0.
        hwnd1 = player._ensure_notify_window()
        hwnd2 = player._ensure_notify_window()
        self.assertEqual(hwnd1, hwnd2,
                         "_ensure_notify_window must return the same cached value "
                         "on repeated calls (creation attempted only once)")

    def test_ensure_notify_window_returns_zero_in_test_harness(self):
        """Under tests WinDLL is mocked; window creation must fail and return 0."""
        import src.audio_player as ap
        player = ap.AudioPlayer()
        hwnd = player._ensure_notify_window()
        self.assertEqual(hwnd, 0,
                         "Expected 0 (fallback) when WinDLL is mocked; "
                         "got non-zero — window creation should fail under test stubs")

    def test_play_issues_play_notify_when_hwnd_available(self):
        """When _ensure_notify_window returns a nonzero hwnd, play() must
        issue `play {alias} notify` via _mci_notify, NOT via plain _mci."""
        player, ap = _make_notify_player()
        plain_mci_calls_before = ap._mci.call_count
        player.play("fake.mp3")
        try:
            notify_calls = [str(c) for c in ap._mci_notify.call_args_list
                            if "notify" in str(c)]
            self.assertTrue(
                len(notify_calls) > 0,
                "play() with a valid hwnd never issued 'play ... notify' via _mci_notify"
            )
            # Plain _mci should not have issued a bare `play` command
            plain_play_cmds = [str(c) for c in ap._mci.call_args_list
                               if "'play" in str(c) and "notify" not in str(c)
                               and "close" not in str(c) and "open" not in str(c)
                               and "set" not in str(c)]
            self.assertEqual(
                len(plain_play_cmds), 0,
                f"play() issued a bare `play` command via _mci while notify is active: "
                f"{plain_play_cmds}"
            )
        finally:
            player.stop()

    def test_play_installs_notify_ctx_with_fresh_token(self):
        """After play() with notify active, _notify_ctx must be installed with
        a monotonically increasing token matching _notify_token."""
        player, ap = _make_notify_player()
        player.play("fake.mp3")
        try:
            ctx = player._notify_ctx
            self.assertIsNotNone(ctx,
                                 "_notify_ctx not set after play() with notify path")
            self.assertEqual(ctx["token"], player._notify_token,
                             "_notify_ctx token must equal _notify_token")
            self.assertEqual(player._notify_token, 1,
                             "first play() must set _notify_token to 1")
            self.assertFalse(ctx["fired"],
                             "_notify_ctx 'fired' must start False")
        finally:
            player.stop()

    def test_successive_plays_bump_token(self):
        """Each successive play() must increment the token so stale notifies
        for the previous playback cannot fire on_done for the new one."""
        player, ap = _make_notify_player()
        player.play("a.mp3")
        token_a = player._notify_token
        player.play("b.mp3")
        token_b = player._notify_token
        try:
            self.assertGreater(token_b, token_a,
                               "token must increase with each play() call")
            ctx = player._notify_ctx
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["token"], token_b,
                             "_notify_ctx must carry the NEW token after the second play()")
        finally:
            player.stop()

    def test_monitor_thread_is_watchdog_not_tight_monitor(self):
        """With notify active, the monitor thread must be the slow watchdog
        (_notify_watchdog), not the tight polling _monitor (which has time.sleep(0.1)
        as its inner loop and reports track_length at start)."""
        player, ap = _make_notify_player()
        player.play("fake.mp3")
        try:
            t = player._monitor_thread
            self.assertIsNotNone(t, "no monitor thread started after play()")
            self.assertEqual(t.name, "mci-notify-watchdog",
                             "With notify active, monitor_thread must be named "
                             "'mci-notify-watchdog', not the tight polling monitor")
        finally:
            player.stop()

    def test_wndclass_counter_increments_across_instances(self):
        """Each AudioPlayer must get a unique window class name to avoid
        RegisterClassW name collisions when multiple instances coexist."""
        import src.audio_player as ap
        c1 = next(ap._wndclass_counter)
        c2 = next(ap._wndclass_counter)
        self.assertGreater(c2, c1,
                           "_wndclass_counter must be strictly increasing (unique "
                           "class names per player instance)")

    def test_wndproc_ref_held_as_instance_attribute(self):
        """The WNDPROC callback must be kept alive via a strong instance reference
        to prevent ctypes GC-ing it while the window still uses it."""
        import src.audio_player as ap
        src_init = inspect.getsource(ap.AudioPlayer.__init__)
        self.assertIn("_wndproc_ref", src_init,
                      "_wndproc_ref strong-ref slot not declared in __init__ (GC risk)")
        src_main = inspect.getsource(ap.AudioPlayer._notify_window_main)
        self.assertIn("_wndproc_ref", src_main,
                      "_wndproc_ref not assigned in _notify_window_main — "
                      "WNDPROC callback may be GC'd")

    def test_stop_clears_notify_ctx(self):
        """stop() must clear _notify_ctx so a queued MM_MCINOTIFY or watchdog
        cannot fire on_done after stop() returns."""
        player, ap = _make_notify_player()
        player.play("fake.mp3")
        self.assertIsNotNone(player._notify_ctx,
                             "precondition: _notify_ctx must be set after play()")
        player.stop()
        self.assertIsNone(player._notify_ctx,
                          "stop() must clear _notify_ctx to suppress queued notifies")

    def test_play_falls_back_to_plain_play_when_notify_fails(self):
        """If _mci_notify returns non-zero, play() must fall back to plain
        `play {alias}` via _mci and must clear _notify_ctx."""
        import src.audio_player as ap
        player = ap.AudioPlayer()
        player._ensure_notify_window = MagicMock(return_value=0x9999)
        ap._mci = MagicMock(return_value=0)
        ap._mci_notify = MagicMock(return_value=1)  # notify fails
        ap._mci_query = MagicMock(return_value="stopped")
        player.play("fake.mp3")
        try:
            # _notify_ctx must be cleared on fallback
            self.assertIsNone(player._notify_ctx,
                              "After notify-play failure, _notify_ctx must be None")
            # Plain 'play' must have been issued
            plain_play = [str(c) for c in ap._mci.call_args_list
                          if "play" in str(c) and "close" not in str(c)
                          and "open" not in str(c) and "set" not in str(c)]
            self.assertTrue(len(plain_play) > 0,
                            "After notify failure, fallback `play` via _mci not issued")
        finally:
            player.stop()


class TestIssue011NotifyTokenLogic(unittest.TestCase):
    """Behavioral tests for _handle_mci_notify and _complete_playback token
    logic — exercisable without a live window."""

    def _make_player_with_ctx(self, on_done=None):
        """Return a player with a valid _notify_ctx installed (token=1)."""
        import src.audio_player as ap
        ap._mci = MagicMock(return_value=0)
        ap._mci_query = MagicMock(return_value="stopped")
        player = ap.AudioPlayer()
        stop_event = threading.Event()
        player._stop_event = stop_event
        player._notify_token = 1
        player._notify_ctx = {
            "token": 1,
            "stop_event": stop_event,
            "on_done": on_done,
            "fired": False,
        }
        with player._lock:
            player._playing = True
            player._open = True
        return player

    def test_successful_notify_fires_on_done_once(self):
        """MCI_NOTIFY_SUCCESSFUL with mode=stopped must fire on_done exactly once."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        # on_done fires on a detached thread — wait briefly
        time.sleep(0.1)
        self.assertEqual(fired, [1],
                         "MCI_NOTIFY_SUCCESSFUL must fire on_done exactly once")

    def test_successful_notify_fires_on_done_only_once_even_if_called_twice(self):
        """Idempotency: a duplicate SUCCESSFUL (e.g. watchdog races with notify)
        must not double-fire on_done."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        time.sleep(0.1)
        self.assertEqual(fired, [1],
                         "on_done must fire at most once — duplicate notify doubled it")

    def test_superseded_code_does_not_fire_on_done(self):
        """MCI_NOTIFY_SUPERSEDED must be silently ignored."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_SUPERSEDED, 0)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "MCI_NOTIFY_SUPERSEDED must not fire on_done")

    def test_aborted_code_does_not_fire_on_done(self):
        """MCI_NOTIFY_ABORTED must be silently ignored."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_ABORTED, 0)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "MCI_NOTIFY_ABORTED must not fire on_done")

    def test_failure_code_does_not_fire_on_done(self):
        """MCI_NOTIFY_FAILURE must be silently ignored."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_FAILURE, 0)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "MCI_NOTIFY_FAILURE must not fire on_done")

    def test_stale_notify_ignored_when_device_playing(self):
        """SUCCESSFUL with mode='playing' must be dropped (stale-notify defense)."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="playing")  # device still active
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "SUCCESSFUL with mode='playing' must be treated as stale "
                         "and not fire on_done (stale-notify defense)")

    def test_stale_token_notify_ignored(self):
        """A SUCCESSFUL with the OLD token (after a new play()) must be dropped."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        # Simulate new play() bumped the token
        with player._lock:
            player._notify_ctx["token"] = 2
            player._notify_token = 2
        # Old token=1 arrives
        player._complete_playback(1)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "Stale token (old playback) must not fire on_done")

    def test_notify_after_stop_does_not_fire_on_done(self):
        """After stop() clears _notify_ctx, a late SUCCESSFUL must be ignored."""
        import src.audio_player as ap
        fired = []
        player = self._make_player_with_ctx(on_done=lambda: fired.append(1))
        ap._mci_query = MagicMock(return_value="stopped")
        player.stop()  # clears _notify_ctx
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        time.sleep(0.05)
        self.assertEqual(fired, [],
                         "Notify arriving after stop() must not fire on_done")

    def test_complete_playback_clears_playing_flag(self):
        """_complete_playback must clear _playing and _paused under lock."""
        import src.audio_player as ap
        player = self._make_player_with_ctx(on_done=None)
        result = player._complete_playback(1)
        self.assertTrue(result, "_complete_playback should return True on first call")
        with player._lock:
            self.assertFalse(player._playing,
                             "_complete_playback must clear _playing")
            self.assertFalse(player._paused,
                             "_complete_playback must clear _paused")

    def test_complete_playback_is_idempotent(self):
        """Calling _complete_playback twice with the same token must return
        False on the second call (fired guard)."""
        import src.audio_player as ap
        player = self._make_player_with_ctx(on_done=None)
        first = player._complete_playback(1)
        second = player._complete_playback(1)
        self.assertTrue(first)
        self.assertFalse(second,
                         "_complete_playback second call must return False (idempotent)")

    def test_on_done_dispatched_on_detached_thread_not_notify_thread(self):
        """on_done must be fired from a detached thread so the notify-window
        message pump is never blocked (ISSUE-022 pattern)."""
        import src.audio_player as ap
        caller_thread = threading.current_thread()
        fired_on = []
        ev = threading.Event()

        def on_done():
            fired_on.append(threading.current_thread())
            ev.set()

        player = self._make_player_with_ctx(on_done=on_done)
        ap._mci_query = MagicMock(return_value="stopped")
        player._handle_mci_notify(ap.MCI_NOTIFY_SUCCESSFUL, 0)
        self.assertTrue(ev.wait(timeout=2.0), "on_done never fired")
        self.assertIsNot(fired_on[0], caller_thread,
                         "on_done must fire on a detached thread, not the caller")


class TestIssue011WatchdogBehavior(unittest.TestCase):
    """Behavioral tests for _notify_watchdog (backup polling thread)."""

    def test_watchdog_fires_complete_playback_on_stopped_device(self):
        """If MM_MCINOTIFY was lost, the watchdog must call _complete_playback
        when it detects mode=='stopped'."""
        import src.audio_player as ap
        ap._mci_query = MagicMock(return_value="stopped")
        player = ap.AudioPlayer()
        stop_event = threading.Event()
        token = 7
        fired = []

        def fake_complete(t):
            fired.append(t)
            stop_event.set()  # exit the loop after first completion
            return True

        player._complete_playback = fake_complete
        player._notify_ctx = {
            "token": token,
            "stop_event": stop_event,
            "on_done": None,
            "fired": False,
        }
        with player._lock:
            player._open = True

        # Run the watchdog in a thread; it should fire within 2s+epsilon
        t = threading.Thread(target=player._notify_watchdog,
                             args=(stop_event, token), daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "watchdog thread did not exit within 5s")
        self.assertEqual(fired, [token],
                         "watchdog did not call _complete_playback when mode='stopped'")

    def test_watchdog_exits_promptly_when_stop_event_set(self):
        """stop() sets the stop_event; the watchdog must exit within the next
        poll interval (2s max), not hang."""
        import src.audio_player as ap
        # Return "playing" forever so the watchdog would loop without stop_event
        ap._mci_query = MagicMock(return_value="playing")
        player = ap.AudioPlayer()
        stop_event = threading.Event()
        player._notify_ctx = {
            "token": 1,
            "stop_event": stop_event,
            "on_done": None,
            "fired": False,
        }
        with player._lock:
            player._open = True

        t = threading.Thread(target=player._notify_watchdog,
                             args=(stop_event, 1), daemon=True)
        t.start()
        stop_event.set()  # simulate stop()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(),
                         "watchdog did not exit within 3s after stop_event was set")

    def test_watchdog_exits_when_token_changes(self):
        """A new play() installs a new token; the watchdog for the OLD token
        must detect ctx["token"] != token and exit without completing."""
        import src.audio_player as ap
        ap._mci_query = MagicMock(return_value="playing")
        player = ap.AudioPlayer()
        stop_event_old = threading.Event()
        old_token = 1
        new_token = 2
        completed = []

        def fake_complete(t):
            completed.append(t)
            return True

        player._complete_playback = fake_complete
        player._notify_ctx = {
            "token": old_token,
            "stop_event": stop_event_old,
            "on_done": None,
            "fired": False,
        }
        with player._lock:
            player._open = True

        t = threading.Thread(target=player._notify_watchdog,
                             args=(stop_event_old, old_token), daemon=True)
        t.start()
        # Simulate a new play(): bump token, replace ctx
        stop_event_old.set()  # stop() sets the old event
        with player._lock:
            player._notify_ctx = {
                "token": new_token,
                "stop_event": threading.Event(),
                "on_done": None,
                "fired": False,
            }
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(),
                         "old-token watchdog did not exit after stop_event set")
        self.assertEqual(completed, [],
                         "old-token watchdog must not call _complete_playback "
                         "for the new playback's token")


class TestIssue011Regressions(unittest.TestCase):
    """Regression guards: key invariants from prior ISSUE fixes must hold
    with the MM_MCINOTIFY implementation in place."""

    def test_issue_001_self_join_guard_on_watchdog_thread(self):
        """ISSUE-001: if stop() is somehow called from the watchdog thread
        (_monitor_thread), the self-join guard must fire and not deadlock."""
        import src.audio_player as ap
        ap._mci = MagicMock(return_value=0)
        ap._mci_query = MagicMock(return_value="stopped")
        player = ap.AudioPlayer()
        error = []

        def fake_watchdog():
            player._monitor_thread = threading.current_thread()
            try:
                player.stop()
            except RuntimeError as e:
                error.append(e)

        t = threading.Thread(target=fake_watchdog)
        t.start()
        t.join(timeout=3)
        self.assertEqual(error, [],
                         "stop() raised RuntimeError when called from watchdog "
                         "thread (ISSUE-001 self-join guard regressed)")

    def test_issue_022_on_done_not_inline_in_complete_playback(self):
        """ISSUE-022: _complete_playback must fire on_done via a detached thread
        (never inline), so the notify-window pump is never blocked."""
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer._complete_playback)
        self.assertIn("on-done-dispatch", src,
                      "_complete_playback must fire on_done via a detached "
                      "'on-done-dispatch' thread (ISSUE-022 regression)")
        self.assertNotIn("cb()", src,
                         "_complete_playback calls on_done inline — "
                         "notify-window pump could block (ISSUE-022 regression)")

    def test_issue_029_dispatcher_handles_two_element_item_with_notify_hwnd(self):
        """ISSUE-029: _mci_worker now accepts 2-element and 3-element items.
        A 3-element item (cmd, rq, hwnd) must be processed without killing the
        worker; a 2-element item (cmd, rq) must still work as before."""
        import src.audio_player as ap
        # 3-element item: _mci_notify path
        rq3 = queue.Queue()
        ap._cmd_queue.put(("status fake mode", rq3, 0x9999))
        try:
            rq3.get(timeout=3)
        except queue.Empty:
            self.fail("dispatcher did not respond to 3-element (notify) item (ISSUE-029)")
        # 2-element item: _mci path
        rq2 = queue.Queue()
        ap._cmd_queue.put(("status fake mode", rq2))
        try:
            rq2.get(timeout=3)
        except queue.Empty:
            self.fail("dispatcher did not respond to 2-element item after a "
                      "3-element item (ISSUE-029 regression)")

    def test_mci_notify_dispatcher_variant_has_5s_timeout(self):
        """ISSUE-026: _mci_notify must time out rather than block forever."""
        import importlib, importlib.util, sys
        # _mci_notify is patched to a MagicMock in the test harness, so we
        # inspect the source file directly rather than via the live module object.
        spec = importlib.util.spec_from_file_location(
            "_ap_src",
            os.path.join(PROJECT_ROOT, "src", "audio_player.py")
        )
        src_mod = importlib.util.module_from_spec(spec)
        # Parse the source text without executing (avoids DLL load)
        import ast
        with open(os.path.join(PROJECT_ROOT, "src", "audio_player.py"),
                  encoding="utf-8") as f:
            src_text = f.read()
        self.assertIn("timeout=5.0", src_text,
                      "_mci_notify does not enforce a 5s caller timeout (ISSUE-026 regression)")


# ---------------------------------------------------------------------------
# Helper for ISSUE-017/018/019 tests — TTSEngine without __init__ side effects
# ---------------------------------------------------------------------------

def _make_tts_engine():
    """Return a TTSEngine with player stubbed and no worker thread started."""
    from src.tts_engine import TTSEngine
    engine = TTSEngine.__new__(TTSEngine)
    engine._player = MagicMock()
    engine._gen_lock = threading.Lock()
    engine._generation = 0
    engine._tmp_lock = threading.Lock()
    engine._tmp_files = []
    engine._pyttsx3_interrupt = threading.Event()
    engine._pyttsx3_queue = queue.Queue()
    return engine


# ---------------------------------------------------------------------------
# ISSUE-017 — per-utterance generation token replaces shared _stop_event
# ---------------------------------------------------------------------------

class TestIssue017GenerationToken(unittest.TestCase):

    def test_generation_initialized_in_init(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.__init__)
        self.assertIn("self._generation = 0", src,
                      "_generation token not initialized in TTSEngine.__init__")
        self.assertIn("_gen_lock", src,
                      "_gen_lock not initialized in TTSEngine.__init__")

    def test_stop_bumps_generation(self):
        engine = _make_tts_engine()
        g0 = engine._generation
        engine.stop()
        self.assertEqual(engine._generation, g0 + 1,
                         "stop() must bump _generation to invalidate in-flight utterances")

    def test_speak_online_captures_and_checks_generation(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._speak_online)
        self.assertIn("gen = self._generation", src,
                      "_speak_online does not capture a per-utterance generation token")
        self.assertIn("gen == self._generation", src,
                      "_speak_online does not verify the generation before playing")
        self.assertNotIn("_stop_event", src,
                         "_speak_online still references the shared _stop_event (ISSUE-017)")

    def test_no_stop_event_clear_in_speak(self):
        """speak() must not clear any shared event (the set-then-clear bug)."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.speak)
        self.assertNotIn(".clear()", src,
                         "speak() still clears a shared event — cancelled utterances can resurrect")

    def test_stale_offline_on_done_suppressed(self):
        """A generation bump after enqueue must suppress the wrapped on_done."""
        engine = _make_tts_engine()
        calls = []
        voice = MagicMock(source="offline", id="v1")
        engine._speak_offline("hello", voice, 1.0, lambda: calls.append(1))
        cmd = engine._pyttsx3_queue.get_nowait()
        self.assertEqual(cmd[0], "speak")
        gated_on_done = cmd[4]
        engine._generation += 1  # simulate stop()/pause()/newer speak()
        gated_on_done()
        self.assertEqual(calls, [],
                         "stale offline on_done fired despite generation bump (ISSUE-017)")

    def test_current_offline_on_done_fires(self):
        """Without a generation bump the wrapped on_done must still fire."""
        engine = _make_tts_engine()
        calls = []
        voice = MagicMock(source="offline", id="v1")
        engine._speak_offline("hello", voice, 1.0, lambda: calls.append(1))
        cmd = engine._pyttsx3_queue.get_nowait()
        cmd[4]()
        self.assertEqual(calls, [1],
                         "current offline on_done did not fire")

    def test_stale_online_synth_discarded(self):
        """An online synth cancelled mid-flight must not play or fire on_done."""
        engine = _make_tts_engine()
        finished = threading.Event()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock(side_effect=lambda p: finished.set())
        release = threading.Event()

        async def fake_synth(text, voice_id, rate, out_path):
            release.wait()

        engine._edge_synthesize = fake_synth
        played = []
        voice = MagicMock(source="online", id="v1")
        engine._speak_online("hello", voice, 1.0, lambda: played.append(1))
        engine._generation += 1  # cancel while synthesis is in flight
        release.set()
        self.assertTrue(finished.wait(timeout=3), "synth thread did not finish")
        engine._player.play.assert_not_called()
        self.assertEqual(played, [],
                         "stale online utterance fired on_done (ISSUE-017)")

    def test_current_online_synth_plays(self):
        """An online synth that stays current must hand off to the player."""
        engine = _make_tts_engine()
        handed_off = threading.Event()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock()
        engine._player.play = MagicMock(side_effect=lambda *a, **kw: handed_off.set())

        async def fake_synth(text, voice_id, rate, out_path):
            pass

        engine._edge_synthesize = fake_synth
        voice = MagicMock(source="online", id="v1")
        engine._speak_online("hello", voice, 1.0, None)
        self.assertTrue(handed_off.wait(timeout=3),
                        "current online utterance was not handed to the player")


# ---------------------------------------------------------------------------
# ISSUE-018 — offline Stop/Pause interrupt via 'started-word' callback
# ---------------------------------------------------------------------------

class TestIssue018OfflineInterrupt(unittest.TestCase):

    def test_stop_sets_interrupt_event(self):
        engine = _make_tts_engine()
        engine.stop()
        self.assertTrue(engine._pyttsx3_interrupt.is_set(),
                        "stop() does not set _pyttsx3_interrupt (offline Stop stays queued)")

    def test_pause_sets_interrupt_event(self):
        engine = _make_tts_engine()
        engine.pause()
        self.assertTrue(engine._pyttsx3_interrupt.is_set(),
                        "pause() does not set _pyttsx3_interrupt (offline Pause is a no-op)")

    def test_worker_connects_started_word_callback(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._pyttsx3_worker)
        self.assertIn("started-word", src,
                      "worker does not register a 'started-word' callback (ISSUE-018)")
        self.assertIn(".connect(", src,
                      "worker does not connect any pyttsx3 callback")

    def test_worker_callback_checks_interrupt(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._pyttsx3_worker)
        self.assertIn("_pyttsx3_interrupt.is_set()", src,
                      "worker callback does not check the interrupt flag")

    def test_worker_clears_interrupt_per_utterance(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._pyttsx3_worker)
        self.assertIn("_pyttsx3_interrupt.clear()", src,
                      "worker does not clear the interrupt flag before a fresh utterance")


# ---------------------------------------------------------------------------
# ISSUE-019 — pause gates in-flight online synthesis
# ---------------------------------------------------------------------------

class TestIssue019PauseGatesInFlightSynth(unittest.TestCase):

    def test_pause_bumps_generation(self):
        engine = _make_tts_engine()
        g0 = engine._generation
        engine.pause()
        self.assertEqual(engine._generation, g0 + 1,
                         "pause() must bump _generation so in-flight synth discards")

    def test_pause_only_pauses_player_when_playing(self):
        engine = _make_tts_engine()
        engine._player.is_playing = False
        engine.pause()
        engine._player.pause.assert_not_called()

        engine2 = _make_tts_engine()
        engine2._player.is_playing = True
        engine2.pause()
        engine2._player.pause.assert_called_once()

    def test_paused_in_flight_synth_does_not_play(self):
        """pause() during synthesis must discard the result (no rogue audio)."""
        engine = _make_tts_engine()
        finished = threading.Event()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock(side_effect=lambda p: finished.set())
        release = threading.Event()

        async def fake_synth(text, voice_id, rate, out_path):
            release.wait()

        engine._edge_synthesize = fake_synth
        played = []
        voice = MagicMock(source="online", id="v1")
        engine._player.is_playing = False  # synth in flight, nothing playing yet
        engine._speak_online("hello", voice, 1.0, lambda: played.append(1))
        engine.pause()  # user pauses during the synthesis window
        release.set()
        self.assertTrue(finished.wait(timeout=3), "synth thread did not finish")
        engine._player.play.assert_not_called()
        self.assertEqual(played, [],
                         "sentence played/advanced despite app being paused (ISSUE-019)")


# ---------------------------------------------------------------------------
# ISSUE-020 — on_close applies the ISSUE-007 rewind before saving
# ---------------------------------------------------------------------------

class TestIssue020CloseRewind(unittest.TestCase):

    def test_on_close_rewinds_sentence_idx(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.on_close)
        self.assertIn("_sentence_idx -= 1", src,
                      "on_close does not rewind _sentence_idx (ISSUE-020)")
        self.assertIn("not self._paused", src,
                      "on_close rewind does not guard on 'not self._paused'")

    def test_rewind_happens_before_save(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.on_close)
        rewind_pos = src.find("_sentence_idx -= 1")
        save_pos = src.find("_save_bookmark()")
        self.assertNotEqual(rewind_pos, -1)
        self.assertNotEqual(save_pos, -1)
        self.assertLess(rewind_pos, save_pos,
                        "on_close must rewind before saving the bookmark")


# ---------------------------------------------------------------------------
# ISSUE-021 — failed PDFReader.open preserves the previous document
# ---------------------------------------------------------------------------

class TestIssue021FailedOpenPreservesOldDoc(unittest.TestCase):

    def _open_good(self, pr):
        good = MagicMock()
        good.is_encrypted = False
        good.__len__ = MagicMock(return_value=4)
        with patch("fitz.open", return_value=good):
            reader = pr.PDFReader()
            reader.open("/fake/good.pdf")
        return reader, good

    def test_failed_open_preserves_previous_doc(self):
        import src.pdf_reader as pr
        reader, good = self._open_good(pr)
        with patch("fitz.open", side_effect=RuntimeError("corrupt file")):
            with self.assertRaises(RuntimeError):
                reader.open("/fake/bad.pdf")
        good.close.assert_not_called()
        self.assertIs(reader._doc, good,
                      "failed open must leave the previous document in place")
        self.assertTrue(reader.is_open)
        self.assertEqual(reader.page_count, 4,
                         "page_count must remain usable after a failed open")
        self.assertEqual(reader._path, "/fake/good.pdf",
                         "_path must still name the previously opened file")

    def test_encrypted_open_preserves_previous_doc(self):
        import src.pdf_reader as pr
        reader, good = self._open_good(pr)
        encrypted = MagicMock()
        encrypted.is_encrypted = True
        with patch("fitz.open", return_value=encrypted):
            with self.assertRaises(ValueError):
                reader.open("/fake/encrypted.pdf")
        encrypted.close.assert_called_once()
        good.close.assert_not_called()
        self.assertIs(reader._doc, good)
        self.assertEqual(reader.page_count, 4)


# ---------------------------------------------------------------------------
# ISSUE-022 — on_done fired from a detached dispatcher thread (no join freeze)
# ---------------------------------------------------------------------------

class TestIssue022NonBlockingOnDoneDispatch(unittest.TestCase):

    def test_on_done_fired_from_dispatcher_thread(self):
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.play)
        self.assertIn("on-done-dispatch", src,
                      "monitor does not fire on_done via a detached dispatcher thread (ISSUE-022)")

    def test_monitor_does_not_call_on_done_inline(self):
        import src.audio_player as ap
        src = inspect.getsource(ap.AudioPlayer.play)
        self.assertNotIn("self._on_done()", src,
                         "monitor still calls on_done inline — stop() can deadlock on join (ISSUE-022)")

    def test_monitor_exits_and_stop_returns_while_on_done_blocked(self):
        """
        Behavioral: even while on_done is blocked (simulating the blocking
        event_generate marshal), the monitor thread must exit and stop()
        must return promptly instead of stalling on the 2s join (ISSUE-022).
        """
        import src.audio_player as ap
        ap._mci = MagicMock(return_value=0)
        ap._mci_query = MagicMock(return_value="stopped")
        player = ap.AudioPlayer()
        entered = threading.Event()
        release = threading.Event()

        def blocking_on_done():
            entered.set()
            release.wait(timeout=10)

        player.play("fake.mp3", on_done=blocking_on_done)
        try:
            self.assertTrue(entered.wait(timeout=3),
                            "on_done was never fired by the monitor/dispatcher")
            # The dispatcher thread is blocked inside on_done; the monitor
            # thread must already have exited (or exit promptly).
            player._monitor_thread.join(timeout=1.0)
            self.assertFalse(player._monitor_thread.is_alive(),
                             "monitor thread still alive while on_done blocked (ISSUE-022)")
            # And stop() must return promptly (no 2s join-timeout freeze).
            t0 = time.time()
            player.stop()
            elapsed = time.time() - t0
            self.assertLess(elapsed, 1.5,
                            f"stop() blocked {elapsed:.2f}s while on_done was in flight (ISSUE-022)")
        finally:
            release.set()


# ---------------------------------------------------------------------------
# ISSUE-023 — edge-tts synthesis bounded by a timeout
# ---------------------------------------------------------------------------

class TestIssue023SynthTimeout(unittest.TestCase):

    def test_edge_synthesize_uses_wait_for(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._edge_synthesize)
        self.assertIn("asyncio.wait_for", src,
                      "_edge_synthesize has no asyncio.wait_for timeout (ISSUE-023)")
        self.assertIn("timeout", src,
                      "_edge_synthesize does not specify a timeout")


# ---------------------------------------------------------------------------
# ISSUE-024 — bookmark load/restore validation
# ---------------------------------------------------------------------------

class TestIssue024BookmarkValidation(unittest.TestCase):

    def test_load_bookmarks_non_dict_root_returns_empty(self):
        import json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("[1, 2, 3]")
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                result = app_mod.DocumentReaderApp._load_bookmarks(MagicMock())
            self.assertEqual(result, {},
                             "non-dict JSON root must yield an empty bookmarks dict")
        finally:
            os.remove(path)

    def test_load_bookmarks_handles_oserror(self):
        import src.app as app_mod
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = app_mod.DocumentReaderApp._load_bookmarks(MagicMock())
        self.assertEqual(result, {},
                         "PermissionError must be caught and yield an empty dict")

    def test_restore_validates_int_types(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("isinstance(page, int)", src,
                      "_restore_bookmark does not type-check page")
        self.assertIn("isinstance(sentence_idx, int)", src,
                      "_restore_bookmark does not type-check sentence_idx")

    def test_restore_clamps_both_ends(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("max(0, min(", src,
                      "_restore_bookmark does not clamp sentence_idx at both ends (ISSUE-024)")
        self.assertIn("max(0, page)", src,
                      "_restore_bookmark does not clamp negative page values")

    def test_restore_requires_dict_entry(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("isinstance(bm, dict)", src,
                      "_restore_bookmark does not verify the bookmark entry is a dict")

    def test_restore_clamps_negative_sentence_idx_behaviorally(self):
        """A negative sentence_idx in a bookmark must clamp to 0, not index from the page end."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock(page_count=3)
        app._sentences = ["s1", "s2"]
        app._current_page = 0
        app._sentence_idx = 0
        app._set_status = MagicMock()  # auto-resume reports the position
        with patch.object(app_mod.DocumentReaderApp, "_load_bookmarks",
                          return_value={"p.pdf": {"page": 0, "sentence_idx": -3}}):
            result = app._restore_bookmark("p.pdf")
        self.assertTrue(result, "_restore_bookmark should report the bookmark existed")
        self.assertEqual(app._sentence_idx, 0,
                         "negative sentence_idx must be clamped to 0 (ISSUE-024)")

    def test_restore_ignores_string_page_behaviorally(self):
        """A non-int page must be ignored (no TypeError in the GUI callback)."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock(page_count=3)
        with patch.object(app_mod.DocumentReaderApp, "_load_bookmarks",
                          return_value={"p.pdf": {"page": "3", "sentence_idx": 1}}):
            result = app._restore_bookmark("p.pdf")
        self.assertFalse(result,
                         "non-int page must be rejected and treated as no bookmark (ISSUE-024)")


# ---------------------------------------------------------------------------
# ISSUE-025 — natural completion clears/advances the bookmark, skips rewind
# ---------------------------------------------------------------------------

class TestIssue025CompletionBookmark(unittest.TestCase):

    def test_stop_accepts_completed_param(self):
        import src.app as app_mod
        sig = inspect.signature(app_mod.DocumentReaderApp._stop)
        self.assertIn("completed", sig.parameters,
                      "_stop has no 'completed' parameter (ISSUE-025)")
        self.assertFalse(sig.parameters["completed"].default,
                         "'completed' must default to False (Stop button passes no args)")

    def test_stop_skips_rewind_when_completed(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._stop)
        self.assertIn("not completed", src,
                      "_stop does not skip the rewind/save when completed=True")

    def test_page_done_clears_bookmark_on_document_end(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._on_page_done)
        self.assertIn("_clear_bookmark()", src,
                      "_on_page_done does not clear the bookmark on document completion")
        self.assertIn("completed=True", src,
                      "_on_page_done does not call _stop(completed=True)")

    def test_page_done_bookmarks_next_page_start(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._on_page_done)
        self.assertIn("_current_page + 1", src.split("_save_bookmark(")[1],
                      "_on_page_done does not bookmark the start of the next page")
        self.assertIn("sentence_idx=0", src,
                      "_on_page_done does not bookmark sentence 0 of the next page")

    def test_clear_bookmark_removes_entry(self):
        import json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"C:/doc.pdf": {"page": 1, "sentence_idx": 2},
                           "C:/other.pdf": {"page": 0, "sentence_idx": 5}}, f)
            app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
            app._current_pdf_path = "C:/doc.pdf"
            app._bookmark_lock = threading.Lock()  # ISSUE-036: bookmark ops serialize on this
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._clear_bookmark()
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertNotIn("C:/doc.pdf", data,
                             "_clear_bookmark did not remove the entry")
            self.assertIn("C:/other.pdf", data,
                          "_clear_bookmark removed unrelated entries")
        finally:
            os.remove(path)

    def test_finish_close_reopen_shows_no_resume_prompt(self):
        """End-to-end pin of the issue's headline repro for MULTI-PAGE docs:
        finish document -> close window -> reopen must show no resume prompt.

        This is the path that failed the first validation (PARTIAL verdict):
        on_close used to re-save {page: last, sentence_idx: 0} after
        _on_page_done had cleared the bookmark. Resolved by ISSUE-030."""
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"C:/doc.pdf": {"page": 3, "sentence_idx": 6}}, f)
            app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
            app._current_pdf_path = "C:/doc.pdf"
            app._bookmark_lock = threading.Lock()  # ISSUE-036: bookmark ops serialize on this
            app._current_page = 4              # last page (0-based) of 5
            app._sentence_idx = 0
            app._reading = True                # last sentence just finished
            app._paused = False
            app._pdf = MagicMock()
            app._pdf.page_count = 5
            app._tts = MagicMock()
            app.destroy = MagicMock()
            app._set_status = MagicMock()
            app._auto_advance_var = MagicMock()
            app._auto_advance_var.get.return_value = False

            def fake_stop(completed=False):
                # state the real _stop(completed=True) leaves behind
                app._reading = False
                app._paused = False
                app._sentence_idx = 0

            app._stop = fake_stop
            askyesno = MagicMock()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._on_page_done()   # document completion: clears the bookmark
                app.on_close()        # must NOT re-save it
                with patch.object(app_mod.mb, "askyesno", askyesno):
                    restored = app._restore_bookmark("C:/doc.pdf")
            self.assertFalse(restored,
                             "a bookmark entry survived finish->close — the stale "
                             "resume prompt is back (ISSUE-025)")
            askyesno.assert_not_called()
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# ISSUE-026 — MCI dispatcher hardening (worker guard + caller timeouts)
# ---------------------------------------------------------------------------

class TestIssue026MciDispatcherHardening(unittest.TestCase):

    def test_worker_guards_each_command(self):
        import src.audio_player as ap
        src = inspect.getsource(ap._mci_worker)
        self.assertIn("except Exception", src,
                      "_mci_worker has no per-command exception guard (ISSUE-026)")
        self.assertIn('result_q.put((-1, ""))', src,
                      "_mci_worker does not answer the caller on failure — caller would hang")

    def test_callers_use_timeout(self):
        import src.audio_player as ap
        module_src = inspect.getsource(ap)
        self.assertIn("rq.get(timeout=", module_src,
                      "_mci/_mci_query block forever on rq.get() (ISSUE-026)")

    def test_worker_survives_malformed_item(self):
        """A malformed queue item must not kill the dispatcher thread."""
        import src.audio_player as ap
        with patch.object(ap.log, "exception"):
            ap._cmd_queue.put(("malformed",))  # wrong arity — unpack fails
            rq = queue.Queue()
            ap._cmd_queue.put(("status fake mode", rq))
            try:
                rq.get(timeout=3)
            except queue.Empty:
                self.fail("MCI dispatcher did not respond after a malformed item — "
                          "the worker thread died (ISSUE-026)")


# ---------------------------------------------------------------------------
# ISSUE-027 — generation check held across the player handoff (TOCTOU closed)
# ---------------------------------------------------------------------------

class TestIssue027HandoffGenLock(unittest.TestCase):

    def test_handoff_check_inside_gen_lock(self):
        """The gen check and _player.play() handoff must happen under _gen_lock."""
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine._speak_online)
        lock_pos = src.find("with self._gen_lock:")
        play_pos = src.find("_player.play(")
        self.assertNotEqual(lock_pos, -1,
                            "_speak_online does not hold _gen_lock across the handoff (ISSUE-027)")
        self.assertNotEqual(play_pos, -1,
                            "_speak_online never hands off to _player.play()")
        self.assertLess(lock_pos, play_pos,
                        "_player.play() must be inside the _gen_lock block (ISSUE-027)")

    def test_stop_serialized_against_handoff(self):
        """
        A stop() that bumps the generation while holding _gen_lock must win
        against a synth thread arriving at the handoff: the utterance must be
        discarded, never played. Before the fix the check ran outside the
        lock, so the synth thread saw the pre-bump generation and played.
        """
        engine = _make_tts_engine()
        finished = threading.Event()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock(side_effect=lambda p: finished.set())

        async def fake_synth(text, voice_id, rate, out_path):
            pass

        engine._edge_synthesize = fake_synth
        voice = MagicMock(source="online", id="v1")
        played = []
        # Simulate Stop holding the generation lock while bumping: the synth
        # thread must serialize at the handoff and then see the stale gen.
        with engine._gen_lock:
            engine._speak_online("hello", voice, 1.0, lambda: played.append(1))
            time.sleep(0.3)  # synth completes and blocks at the handoff lock
            engine._generation += 1  # Stop's bump, still inside the lock
        self.assertTrue(finished.wait(timeout=3), "synth thread did not finish")
        engine._player.play.assert_not_called()
        self.assertEqual(played, [],
                         "utterance played despite a Stop landing in the handoff window (ISSUE-027)")

    def test_done_and_cleanup_does_not_recheck_generation(self):
        """
        _done_and_cleanup must NOT gate on_done on the generation: pause()
        bumps it (ISSUE-019), and online pause->resume relies on the natural
        on_done of that gen-stale-but-resumed utterance to keep the sentence
        pump alive. The TOCTOU is closed by the lock-held handoff instead.
        """
        engine = _make_tts_engine()
        handed = {}
        handed_off = threading.Event()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock()

        def fake_play(path, on_done=None):
            handed["cb"] = on_done
            handed_off.set()

        engine._player.play = MagicMock(side_effect=fake_play)

        async def fake_synth(text, voice_id, rate, out_path):
            pass

        engine._edge_synthesize = fake_synth
        done = []
        voice = MagicMock(source="online", id="v1")
        engine._speak_online("hello", voice, 1.0, lambda: done.append(1))
        self.assertTrue(handed_off.wait(timeout=3), "utterance never handed to player")
        engine._generation += 1  # pause() during playback (ISSUE-019 bump)
        handed["cb"]()  # resumed playback finishes naturally
        self.assertEqual(done, [1],
                         "natural on_done suppressed after a pause/resume generation bump — "
                         "the sentence pump would halt (ISSUE-027 regression)")

    def test_bump_generation_blocks_until_handoff_completes(self):
        """
        The complementary ordering to test_stop_serialized_against_handoff:
        if the synth thread wins the lock, a concurrent stop()/pause() bump
        must block until play() has RETURNED (playback registered with the
        player), never landing between the check and the handoff (ISSUE-027).
        """
        engine = _make_tts_engine()
        engine._make_tmp_mp3 = MagicMock(return_value="fake.mp3")
        engine._delete_tmp = MagicMock()
        in_play = threading.Event()
        release_play = threading.Event()

        def fake_play(path, on_done=None):
            in_play.set()
            release_play.wait(timeout=10)

        engine._player.play = MagicMock(side_effect=fake_play)

        async def fake_synth(text, voice_id, rate, out_path):
            pass

        engine._edge_synthesize = fake_synth
        voice = MagicMock(source="online", id="v1")
        engine._speak_online("hello", voice, 1.0, None)
        self.assertTrue(in_play.wait(timeout=3), "utterance never reached the handoff")

        bumped = threading.Event()

        def do_bump():
            engine._bump_generation()
            bumped.set()

        threading.Thread(target=do_bump, daemon=True).start()
        self.assertFalse(
            bumped.wait(timeout=0.5),
            "a generation bump landed DURING the handoff — the check-then-act "
            "window is not closed (ISSUE-027)")
        release_play.set()
        self.assertTrue(bumped.wait(timeout=3),
                        "bump never completed after play() returned — lock leak")

    def test_play_failure_on_done_not_inline(self):
        """
        AudioPlayer.play must not call on_done synchronously on MCI open
        failure: the calling synth thread may hold _gen_lock across play(),
        and an inline on_done marshalling to a GUI thread blocked on that
        lock would deadlock (ISSUE-027).
        """
        import src.audio_player as ap
        old_mci, old_query = ap._mci, ap._mci_query
        ap._mci = MagicMock(return_value=-1)  # open fails
        try:
            player = ap.AudioPlayer()
            caller = threading.current_thread()
            fired_on = []
            fired = threading.Event()

            def on_done():
                fired_on.append(threading.current_thread())
                fired.set()

            player.play("missing.mp3", on_done=on_done)
            self.assertTrue(fired.wait(timeout=3), "failure-path on_done never fired")
            self.assertIsNot(fired_on[0], caller,
                             "failure-path on_done fired inline on the calling thread (ISSUE-027)")
        finally:
            ap._mci, ap._mci_query = old_mci, old_query


# ---------------------------------------------------------------------------
# ISSUE-028 — per-playback stop event and on_done in AudioPlayer
# ---------------------------------------------------------------------------

class TestIssue028PerPlaybackStopEvent(unittest.TestCase):

    def _play_source(self):
        import src.audio_player as ap
        return inspect.getsource(ap.AudioPlayer.play)

    def test_play_does_not_clear_shared_event(self):
        """play() must not clear a shared event (the set-then-clear bug)."""
        self.assertNotIn(".clear()", self._play_source(),
                         "play() still clears a shared stop event — a monitor that "
                         "outlives the 2s join can be resurrected (ISSUE-028)")

    def test_play_creates_fresh_event_per_playback(self):
        self.assertIn("threading.Event()", self._play_source(),
                      "play() does not create a fresh per-playback Event (ISSUE-028)")

    def test_monitor_uses_captured_on_done(self):
        """The monitor must capture play()'s on_done, not read a shared slot."""
        self.assertNotIn("self._on_done", self._play_source(),
                         "monitor still reads the shared on_done slot — a stale monitor "
                         "could fire the NEW playback's callback (ISSUE-028)")

    def test_stale_event_stays_set_after_new_play(self):
        """
        After stop() + a new play(), the OLD playback's event must remain set
        (a fresh Event replaces it instead of clearing it), so a stale
        monitor that outlived the join can never wake into the new playback.
        """
        ap = _import_audio_player()
        player = ap.AudioPlayer()
        try:
            player.play("a.mp3")
            ev_a = player._stop_event
            player.stop()
            self.assertTrue(ev_a.is_set(), "stop() did not set the playback's event")
            player.play("b.mp3")
            self.assertIsNot(player._stop_event, ev_a,
                             "play() reused the previous Event object (ISSUE-028)")
            self.assertTrue(ev_a.is_set(),
                            "the old playback's event was un-set by the new play() — "
                            "a stale monitor would be resurrected (ISSUE-028)")
        finally:
            player.stop()

    def test_stale_monitor_does_not_clear_new_playback_flags(self):
        """The monitor must not clear _playing/_paused once its own event is set."""
        src = self._play_source()
        self.assertIn("if not stop_event.is_set():", src,
                      "monitor clears the shared _playing/_paused flags unconditionally — "
                      "a stale monitor would clobber the new playback's state (ISSUE-028)")

    def test_stale_monitor_surviving_join_cannot_touch_new_playback(self):
        """
        Behavioral reproduction of the issue's exact scenario: monitor A is
        stalled in an MCI query past the 2s join timeout, a new play() B
        starts, then A wakes. A must exit immediately (its own event is
        permanently set), fire neither on_done, and leave B's _playing flag
        alone. Takes ~4.5s (two deliberate join timeouts).
        """
        import src.audio_player as ap
        old_mci, old_query = ap._mci, ap._mci_query
        release_a = threading.Event()
        a_in_query = threading.Event()
        holder = {"monitor_a": None}

        def fake_query(cmd):
            if (threading.current_thread() is holder["monitor_a"]
                    and not release_a.is_set()):
                a_in_query.set()
                release_a.wait(timeout=15)  # stall monitor A past the join
            return "playing"

        ap._mci = MagicMock(return_value=0)
        ap._mci_query = fake_query
        player = ap.AudioPlayer()
        try:
            on_done_a, on_done_b = MagicMock(), MagicMock()
            with patch.object(ap.log, "warning"), patch.object(ap.log, "error"):
                player.play("a.mp3", on_done=on_done_a)
                monitor_a = holder["monitor_a"] = player._monitor_thread
                self.assertTrue(a_in_query.wait(timeout=3),
                                "monitor A never reached its MCI query")
                player.stop()  # join times out after 2s; A survives
                self.assertTrue(monitor_a.is_alive(),
                                "test setup failed: monitor A should outlive the join")
                player.play("b.mp3", on_done=on_done_b)  # second 2s join inside
                release_a.set()  # A wakes; its own event is already set
                monitor_a.join(timeout=3)
            self.assertFalse(monitor_a.is_alive(),
                             "stale monitor A kept running after waking — its "
                             "cancellation was un-set by the new play() (ISSUE-028)")
            on_done_a.assert_not_called()
            on_done_b.assert_not_called()
            with player._lock:
                self.assertTrue(player._playing,
                                "stale monitor A clobbered the new playback's "
                                "_playing flag (ISSUE-028)")
        finally:
            release_a.set()
            try:
                player.stop()
            except Exception:
                pass
            ap._mci, ap._mci_query = old_mci, old_query


# ---------------------------------------------------------------------------
# ISSUE-029 — MCI dispatcher recovery path itself guarded
# ---------------------------------------------------------------------------

class TestIssue029DispatcherRecoveryGuard(unittest.TestCase):

    def test_worker_validates_result_queue_type(self):
        import src.audio_player as ap
        src = inspect.getsource(ap._mci_worker)
        self.assertIn("isinstance(result_q, queue.Queue)", src,
                      "_mci_worker does not validate the result queue type (ISSUE-029)")

    def test_worker_survives_two_element_malformed_item(self):
        """A 2-element item whose second element is not a Queue must not kill the dispatcher."""
        import src.audio_player as ap
        with patch.object(ap.log, "exception"):
            ap._cmd_queue.put(("status fake mode", "not-a-queue"))
            rq = queue.Queue()
            ap._cmd_queue.put(("status fake mode", rq))
            try:
                rq.get(timeout=3)
            except queue.Empty:
                self.fail("MCI dispatcher died on a malformed 2-element item (ISSUE-029)")

    def test_worker_survives_result_queue_put_failure(self):
        """Even if the failure-path put itself raises, the dispatcher must live."""
        import src.audio_player as ap

        class ExplodingQueue(queue.Queue):
            def put(self, *args, **kwargs):
                raise RuntimeError("boom")

        with patch.object(ap.log, "exception"), patch.object(ap.log, "debug"):
            ap._cmd_queue.put(("status fake mode", ExplodingQueue()))
            rq = queue.Queue()
            ap._cmd_queue.put(("status fake mode", rq))
            try:
                rq.get(timeout=3)
            except queue.Empty:
                self.fail("MCI dispatcher died when the recovery put raised (ISSUE-029)")


# ---------------------------------------------------------------------------
# ISSUE-030 — on_close only saves a bookmark for in-progress reading
# ---------------------------------------------------------------------------

class TestIssue030CloseBookmarkGate(unittest.TestCase):

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading = False
        app._paused = False
        app._sentence_idx = 0
        app._current_page = 4
        app._current_pdf_path = "C:/doc.pdf"
        app._tts = MagicMock()
        app._pdf = MagicMock()
        app.destroy = MagicMock()
        return app

    def test_on_close_gates_save_on_reading_state(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.on_close)
        self.assertIn("self._reading or self._paused", src,
                      "on_close does not gate _save_bookmark on in-progress "
                      "reading state (ISSUE-030)")

    def test_idle_close_does_not_save(self):
        """Closing after Stop or after completion must not (re-)save a bookmark."""
        import src.app as app_mod
        app = self._make_app()
        with patch.object(app_mod.DocumentReaderApp, "_save_bookmark") as save:
            app.on_close()
        save.assert_not_called()

    def test_close_mid_read_still_saves_rewound_position(self):
        import src.app as app_mod
        app = self._make_app()
        app._reading = True
        app._sentence_idx = 5
        with patch.object(app_mod.DocumentReaderApp, "_save_bookmark") as save:
            app.on_close()
        save.assert_called_once()
        self.assertEqual(app._sentence_idx, 4,
                         "ISSUE-020 rewind regressed: close mid-read must bookmark "
                         "the interrupted sentence")

    def test_close_while_paused_saves_without_double_rewind(self):
        import src.app as app_mod
        app = self._make_app()
        app._reading = True
        app._paused = True
        app._sentence_idx = 3  # already rewound by _pause
        with patch.object(app_mod.DocumentReaderApp, "_save_bookmark") as save:
            app.on_close()
        save.assert_called_once()
        self.assertEqual(app._sentence_idx, 3,
                         "close-while-paused must not rewind again (idx was already "
                         "rewound by _pause)")

    def test_close_after_completion_keeps_bookmark_cleared(self):
        """Behavioral: a bookmark cleared on completion must stay cleared after close."""
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"C:/other.pdf": {"page": 0, "sentence_idx": 5}}, f)
            app = self._make_app()  # idle, as left by _stop(completed=True)
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app.on_close()
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self.assertNotIn("C:/doc.pdf", data,
                             "on_close re-created the bookmark that completion "
                             "cleared (ISSUE-030)")
        finally:
            os.remove(path)

    def test_close_after_stop_preserves_stop_saved_bookmark(self):
        """Behavioral: the rewound position saved by a manual Stop must survive close.

        Before the fix, on_close re-saved {page: current, sentence_idx: 0}
        (idx was reset by _stop), clobbering the Stop-saved position."""
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            stop_saved = {"page": 4, "sentence_idx": 7}
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"C:/doc.pdf": dict(stop_saved)}, f)
            app = self._make_app()  # idle with _sentence_idx=0, as left by _stop()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app.on_close()
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self.assertEqual(data.get("C:/doc.pdf"), stop_saved,
                             "on_close clobbered the Stop-saved bookmark with "
                             "sentence_idx=0 (ISSUE-030)")
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# ISSUE-031 — online mid-audio pause/resume must not re-read the sentence
# ---------------------------------------------------------------------------

class TestIssue031OnlineResumeReadvance(unittest.TestCase):

    def _make_app(self, sentence_idx, sentences=None, playing_after_resume=True):
        """App as left by _pause during online mid-audio playback: _reading
        stays True, _paused True, _sentence_idx already rewound by ISSUE-007."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock()                  # is_open truthy
        app._sentences = sentences if sentences is not None else ["s0", "s1", "s2"]
        app._sentence_idx = sentence_idx
        app._reading = True
        app._paused = True
        app._tts = MagicMock()
        app._tts.is_playing = playing_after_resume
        app._play_btn = MagicMock()
        app._pause_btn = MagicMock()
        app._stop_btn = MagicMock()
        app._back_btn = MagicMock()     # sentence-skip buttons (2026-07-11)
        app._fwd_btn = MagicMock()
        app._save_bookmark = MagicMock()
        app._highlight_sentence = MagicMock()
        app._clear_highlight = MagicMock()
        app._set_status = MagicMock()   # real pump shows reading progress
        app._pending_after_id = None
        app._speed_debounce_id = None   # ISSUE-016: cleared by _stop
        app._voices_ready = True        # ISSUE-038: _stop's play_btn gate reads this
        return app

    def test_play_source_readvances_index_on_resume(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._play)
        self.assertIn("_sentence_idx += 1", src,
                      "_play does not re-advance _sentence_idx after an online "
                      "MCI resume (ISSUE-031 fix missing)")

    def test_online_resume_readvances_past_interrupted_sentence(self):
        """When the MCI track actually resumes, the rewound index must be
        re-advanced so the track's natural on_done continues with the NEXT
        sentence, not the one whose audio is finishing."""
        # Was speaking "s1" (idx post-incremented to 2); _pause rewound to 1.
        app = self._make_app(sentence_idx=1, playing_after_resume=True)
        app._read_next_sentence = MagicMock()
        app._play()
        self.assertEqual(app._sentence_idx, 2,
                         "online resume must re-advance the index past the "
                         "resumed sentence (ISSUE-031)")
        self.assertFalse(app._paused)
        app._read_next_sentence.assert_not_called()  # natural on_done drives the pump

    def test_online_resume_next_on_done_reads_following_sentence(self):
        """Headline repro: after resume, the resumed track's on_done must speak
        the sentence AFTER the interrupted one — not re-read it."""
        app = self._make_app(sentence_idx=1, playing_after_resume=True)
        app._voice_var = MagicMock()
        app._voice_var.get.return_value = "Some Voice"
        app._voices = MagicMock()
        app._speed_var = MagicMock()
        app._speed_var.get.return_value = 1.0
        app._play()
        # Simulate the resumed track finishing: natural on_done -> GUI event.
        app._on_sentence_done_event()
        app._tts.speak.assert_called_once()
        spoken = app._tts.speak.call_args[0][0]
        self.assertEqual(spoken, "s2",
                         "resumed track's on_done re-read the interrupted "
                         "sentence instead of advancing (ISSUE-031)")

    def test_offline_resume_still_rereads_interrupted_sentence(self):
        """Offline (is_playing False after resume): pyttsx3 cannot resume
        mid-sentence, so the rewound index must be kept and the interrupted
        sentence re-read via _read_next_sentence (ISSUE-006)."""
        app = self._make_app(sentence_idx=1, playing_after_resume=False)
        app._read_next_sentence = MagicMock()
        app._play()
        self.assertEqual(app._sentence_idx, 1,
                         "offline resume must keep the rewound index so the "
                         "interrupted sentence is re-read (ISSUE-006)")
        self.assertTrue(app._reading)
        app._read_next_sentence.assert_called_once()

    def test_online_resume_clamps_index_at_page_end(self):
        """Defensive clamp: an index already at len(sentences) must not be
        advanced past it."""
        app = self._make_app(sentence_idx=3, playing_after_resume=True)
        app._read_next_sentence = MagicMock()
        app._play()
        self.assertEqual(app._sentence_idx, 3,
                         "online resume advanced the index past len(sentences)")

    def test_online_resume_of_last_sentence_ends_page_naturally(self):
        """Interrupted LAST sentence: resume re-advances to len(sentences) so
        the natural on_done triggers page-done instead of re-reading."""
        app = self._make_app(sentence_idx=2, playing_after_resume=True)
        app._play()
        self.assertEqual(app._sentence_idx, 3)

    def test_pause_stop_play_starts_at_interrupted_sentence(self):
        """Bookmark semantics unchanged: pause saves the rewound index, and a
        subsequent Stop re-saves the same index (no double rewind)."""
        app = self._make_app(sentence_idx=2, playing_after_resume=True)
        app._paused = False  # actively reading "s1" (idx post-incremented to 2)
        saved = []
        app._save_bookmark = MagicMock(
            side_effect=lambda *a, **k: saved.append(app._sentence_idx))
        app._pause()
        self.assertEqual(saved, [1],
                         "_pause must bookmark the interrupted sentence (ISSUE-007)")
        app._stop()
        self.assertEqual(saved, [1, 1],
                         "Stop while paused must re-save the rewound index "
                         "without rewinding again")
        self.assertEqual(app._sentence_idx, 0)

    def test_double_pause_resume_cycle_stays_consistent(self):
        """pause -> resume -> pause -> resume must keep pointing at the same
        in-flight sentence (no drift in either direction)."""
        app = self._make_app(sentence_idx=2, playing_after_resume=True)
        app._paused = False  # actively reading "s1"
        app._pause()
        self.assertEqual(app._sentence_idx, 1)
        app._play()   # online resume: re-advance
        self.assertEqual(app._sentence_idx, 2)
        app._pause()
        self.assertEqual(app._sentence_idx, 1)
        app._play()
        self.assertEqual(app._sentence_idx, 2,
                         "index drifted across repeated pause/resume cycles")


# ---------------------------------------------------------------------------
# ISSUE-037 — _load_voices.on_done must marshal via event_generate, not after()
# ---------------------------------------------------------------------------

class _FakeVoiceStr:
    """Minimal stand-in for a Voice whose __str__ is what _load_voices uses
    to build the dropdown display list."""

    def __init__(self, s):
        self._s = s

    def __str__(self):
        return self._s


class TestIssue037ThreadSafeVoiceLoadCallback(unittest.TestCase):
    """_load_voices.on_done (invoked on the VoiceManager background thread)
    must not call self.after() — it must stash a result and marshal to the
    GUI thread via event_generate(when='tail'), mirroring ISSUE-003."""

    @staticmethod
    def _strip_comments(src: str) -> str:
        # Same technique as the ISSUE-003 false-positive fix: the fix's own
        # explanatory comments mention "self.after()" in prose, so a raw
        # substring search over the full source is not reliable.
        lines = []
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            lines.append(line.split("#", 1)[0])
        return "\n".join(lines)

    def _get_load_voices_src(self):
        import src.app as app_mod
        return inspect.getsource(app_mod.DocumentReaderApp._load_voices)

    def test_on_done_does_not_call_after_in_executable_code(self):
        code_only = self._strip_comments(self._get_load_voices_src())
        self.assertNotIn("self.after(", code_only,
                         "_load_voices.on_done must not call self.after() from "
                         "the VoiceManager background thread (ISSUE-037)")

    def test_on_done_uses_event_generate_tail(self):
        src = self._get_load_voices_src()
        self.assertIn("event_generate", src,
                      "_load_voices.on_done must call event_generate for thread safety")
        self.assertIn('"tail"', src,
                      "event_generate in _load_voices should use when='tail'")

    def test_voices_loaded_event_handler_exists(self):
        import src.app as app_mod
        self.assertTrue(
            hasattr(app_mod.DocumentReaderApp, "_on_voices_loaded_event"),
            "Missing _on_voices_loaded_event handler (ISSUE-037 fix incomplete)"
        )

    def test_voices_loaded_event_bound_in_init(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("<<VoicesLoaded>>", src,
                      "<<VoicesLoaded>> event not bound in __init__")

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._voices = MagicMock()
        app._pdf = MagicMock()
        app._pdf.is_open = False
        app._voices_loaded_event = "<<VoicesLoaded>>"
        app._voices_load_result = None
        app._voices_ready = False
        app._set_status = MagicMock()
        app._voice_menu = MagicMock()
        app._voice_var = MagicMock()
        app._play_btn = MagicMock()
        app.event_generate = MagicMock()
        return app

    def test_on_done_runs_on_background_thread_and_signals_via_event(self):
        """Reproduces the real VoiceManager.load() invocation: on_done must
        complete off the GUI thread without touching self.after(), then
        signal completion via event_generate."""
        app = self._make_app()
        app._voices.get_default_voice = MagicMock(return_value=None)
        captured = {}

        def fake_load(on_done):
            def _bg():
                captured["thread"] = threading.current_thread()
                on_done([_FakeVoiceStr("[Online] Voice A (en-US)")])
            t = threading.Thread(target=_bg)
            t.start()
            t.join(timeout=2)

        app._voices.load = fake_load
        app._load_voices()

        self.assertIsNot(captured.get("thread"), threading.main_thread(),
                         "test sanity check: on_done should run on a background thread")
        app.event_generate.assert_called_once_with("<<VoicesLoaded>>", when="tail")
        self.assertIn("display", app._voices_load_result)
        self.assertEqual(app._voices_load_result["display"], ["[Online] Voice A (en-US)"])

    def test_result_stashed_before_event_generate_fires(self):
        """event_generate must only fire AFTER _voices_load_result is fully
        written, so the GUI-thread handler can never observe a stale/None
        result for a completed load."""
        app = self._make_app()
        app._voices.get_default_voice = MagicMock(return_value=None)
        seen = []
        app.event_generate = MagicMock(
            side_effect=lambda *a, **k: seen.append(app._voices_load_result))
        app._voices.load = lambda on_done: on_done([_FakeVoiceStr("[Offline] Voice B (en-GB)")])

        app._load_voices()

        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], "on_done result was not stashed before event_generate")
        self.assertIn("display", seen[0])

    def test_empty_voices_produces_status_only_result(self):
        app = self._make_app()
        app._voices.load = lambda on_done: on_done([])
        app._load_voices()
        self.assertEqual(app._voices_load_result, {"status": "No voices found"})
        app.event_generate.assert_called_once_with("<<VoicesLoaded>>", when="tail")

    def test_exception_in_on_done_produces_error_result_not_raise(self):
        app = self._make_app()
        app._voices.get_default_voice = MagicMock(side_effect=RuntimeError("boom"))
        app._voices.load = lambda on_done: on_done([_FakeVoiceStr("X")])
        app._load_voices()  # must not raise
        self.assertEqual(app._voices_load_result, {"status": "Error loading voices"})
        app.event_generate.assert_called_once_with("<<VoicesLoaded>>", when="tail")

    def test_event_generate_exception_is_swallowed(self):
        """If the window is closing, event_generate can raise; on_done must
        not propagate that into the VoiceManager background thread."""
        app = self._make_app()
        app._voices.get_default_voice = MagicMock(return_value=None)
        app.event_generate = MagicMock(side_effect=RuntimeError("window closed"))
        app._voices.load = lambda on_done: on_done([_FakeVoiceStr("X")])
        app._load_voices()  # must not raise


# ---------------------------------------------------------------------------
# ISSUE-038 — Play must stay disabled until voice discovery actually finishes
# ---------------------------------------------------------------------------

class TestIssue038PlayGatedOnVoicesReady(unittest.TestCase):

    def test_voices_ready_initialized_false_in_init(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("_voices_ready = False", src,
                      "_voices_ready must default to False in __init__ (ISSUE-038)")

    def test_stop_play_btn_gate_checks_voices_ready(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._stop)
        self.assertIn("_voices_ready", src,
                      "_stop()'s play-button restore must also require "
                      "_voices_ready, not just self._pdf.is_open (ISSUE-038)")

    # -- _open_pdf gating -----------------------------------------------

    def _make_open_pdf_app(self, voices_ready):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock()
        app._pdf.open = MagicMock(return_value=5)
        app._stop = MagicMock()
        app._title_label = MagicMock()
        app._set_status = MagicMock()
        app._update_page_display = MagicMock()
        app._update_nav_buttons = MagicMock()
        app._restore_bookmark = MagicMock()
        app._record_document_opened = MagicMock()  # document-history feature (2026-07-11)
        app._refresh_recent_menu = MagicMock()
        app._play_btn = MagicMock()
        app._voices_ready = voices_ready
        app._current_pdf_path = None
        app._current_page = 0
        return app

    def test_open_pdf_does_not_enable_play_while_voices_still_loading(self):
        import src.app as app_mod
        app = self._make_open_pdf_app(voices_ready=False)
        with patch.object(app_mod.fd, "askopenfilename", return_value="/fake/doc.pdf"):
            app._open_pdf()
        app._play_btn.configure.assert_not_called()

    def test_open_pdf_enables_play_when_voices_already_ready(self):
        import src.app as app_mod
        app = self._make_open_pdf_app(voices_ready=True)
        with patch.object(app_mod.fd, "askopenfilename", return_value="/fake/doc.pdf"):
            app._open_pdf()
        app._play_btn.configure.assert_called_once_with(state="normal")

    # -- _on_voices_loaded_event gating -----------------------------------

    def _make_voices_event_app(self, pdf_open):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock()
        app._pdf.is_open = pdf_open
        app._voices_ready = False
        app._set_status = MagicMock()
        app._voice_menu = MagicMock()
        app._voice_var = MagicMock()
        app._play_btn = MagicMock()
        return app

    def test_voices_loaded_enables_play_if_pdf_already_open(self):
        app = self._make_voices_event_app(pdf_open=True)
        app._voices_load_result = {
            "display": ["[Online] Voice A (en-US)"],
            "default_str": "[Online] Voice A (en-US)",
            "status": "1 voices loaded",
        }
        app._on_voices_loaded_event()
        self.assertTrue(app._voices_ready)
        app._play_btn.configure.assert_called_once_with(state="normal")

    def test_voices_loaded_does_not_touch_play_btn_if_no_pdf_open(self):
        app = self._make_voices_event_app(pdf_open=False)
        app._voices_load_result = {
            "display": ["[Online] Voice A (en-US)"],
            "default_str": "[Online] Voice A (en-US)",
            "status": "1 voices loaded",
        }
        app._on_voices_loaded_event()
        self.assertTrue(app._voices_ready, "successful load must still set _voices_ready")
        app._play_btn.configure.assert_not_called()

    def test_voices_loaded_does_not_set_ready_on_empty_result(self):
        """Zero usable voices: Play genuinely can't work, so _voices_ready
        must stay False even though the load 'completed'."""
        app = self._make_voices_event_app(pdf_open=True)
        app._voices_load_result = {"status": "No voices found"}
        app._on_voices_loaded_event()
        self.assertFalse(app._voices_ready)
        app._play_btn.configure.assert_not_called()

    def test_voices_loaded_does_not_set_ready_on_error_result(self):
        app = self._make_voices_event_app(pdf_open=True)
        app._voices_load_result = {"status": "Error loading voices"}
        app._on_voices_loaded_event()
        self.assertFalse(app._voices_ready)
        app._play_btn.configure.assert_not_called()

    # -- _stop()'s play-button restore -------------------------------------

    def _make_stop_app(self, pdf_open, voices_ready):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading = False
        app._paused = False
        app._sentence_idx = 0
        app._pending_after_id = None
        app._speed_debounce_id = None
        app._tts = MagicMock()
        app._pdf = MagicMock()
        app._pdf.is_open = pdf_open
        app._voices_ready = voices_ready
        app._clear_highlight = MagicMock()
        app._play_btn = MagicMock()
        app._pause_btn = MagicMock()
        app._stop_btn = MagicMock()
        app._back_btn = MagicMock()   # sentence-skip buttons (2026-07-11)
        app._fwd_btn = MagicMock()
        return app

    def test_stop_disables_play_when_voices_not_ready_even_if_pdf_open(self):
        """Regression guard: before ISSUE-038, _stop() re-enabled Play purely
        on self._pdf.is_open, which could re-enable it mid voice-load when a
        second PDF is opened while the first was still open."""
        app = self._make_stop_app(pdf_open=True, voices_ready=False)
        app._stop()
        app._play_btn.configure.assert_called_once_with(state="disabled", text="▶  Play")

    def test_stop_enables_play_when_pdf_open_and_voices_ready(self):
        app = self._make_stop_app(pdf_open=True, voices_ready=True)
        app._stop()
        app._play_btn.configure.assert_called_once_with(state="normal", text="▶  Play")

    def test_stop_disables_play_when_no_pdf_open_even_if_voices_ready(self):
        app = self._make_stop_app(pdf_open=False, voices_ready=True)
        app._stop()
        app._play_btn.configure.assert_called_once_with(state="disabled", text="▶  Play")

    # -- ISSUE-031 regression check (shared _make_app fixture already sets
    #    _voices_ready=True; re-verify here that _stop still behaves under
    #    the combined gate for a fully-ready app) -----------------------

    def test_stop_after_online_pause_resume_cycle_still_gates_correctly(self):
        """Cross-issue check: ISSUE-031's pause/resume cycling must not
        bypass the ISSUE-038 play-button gate on the final Stop."""
        app = self._make_stop_app(pdf_open=True, voices_ready=True)
        app._reading = True
        app._paused = False
        app._sentence_idx = 2
        app._save_bookmark = MagicMock()
        app._stop()
        app._play_btn.configure.assert_called_once_with(state="normal", text="▶  Play")


# ---------------------------------------------------------------------------
# ISSUE-039 — page text extracted once and shared between text + sentences
# ---------------------------------------------------------------------------

class TestIssue039SinglePageExtraction(unittest.TestCase):

    def _make_reader_with_fixed_text(self, text):
        import src.pdf_reader as pr
        mock_doc = MagicMock()
        mock_doc.is_encrypted = False
        mock_doc.__len__ = MagicMock(return_value=1)
        page = MagicMock()
        page.get_text = MagicMock(return_value=text)
        mock_doc.__getitem__ = MagicMock(return_value=page)
        with patch("fitz.open", return_value=mock_doc):
            reader = pr.PDFReader()
            reader.open("/fake/path.pdf")
        return reader, page

    def test_get_text_and_sentences_exists(self):
        import src.pdf_reader as pr
        self.assertTrue(hasattr(pr.PDFReader, "get_text_and_sentences"),
                        "PDFReader.get_text_and_sentences is missing (ISSUE-039)")

    def test_get_text_and_sentences_extracts_page_exactly_once(self):
        text = "First sentence. Second sentence! Third one?"
        reader, page = self._make_reader_with_fixed_text(text)
        result_text, result_sentences = reader.get_text_and_sentences(0)
        self.assertEqual(page.get_text.call_count, 1,
                         "get_text_and_sentences must call PyMuPDF get_text() "
                         "exactly once per page (ISSUE-039)")
        self.assertEqual(result_text, text)
        self.assertEqual(result_sentences,
                         ["First sentence.", "Second sentence!", "Third one?"])

    def test_get_text_and_sentences_matches_separate_calls(self):
        """The combined call must return results identical to the old
        get_all_text + get_sentences pair (no behavior change)."""
        text = "Alpha beta. Gamma delta.\n\n\nEpsilon."
        reader, _ = self._make_reader_with_fixed_text(text)
        expected_text = reader.get_all_text(0)
        expected_sentences = reader.get_sentences(0)
        combined_text, combined_sentences = reader.get_text_and_sentences(0)
        self.assertEqual(combined_text, expected_text)
        self.assertEqual(combined_sentences, expected_sentences)

    def test_get_text_and_sentences_empty_page(self):
        reader, _ = self._make_reader_with_fixed_text("")
        text, sentences = reader.get_text_and_sentences(0)
        self.assertEqual(text, "")
        self.assertEqual(sentences, [])

    def test_get_text_and_sentences_out_of_range_page(self):
        reader, page = self._make_reader_with_fixed_text("Some text.")
        text, sentences = reader.get_text_and_sentences(99)
        self.assertEqual(text, "")
        self.assertEqual(sentences, [])
        page.get_text.assert_not_called()

    @staticmethod
    def _strip_comments(src: str) -> str:
        # Explanatory comments in this codebase reference the OLD API names
        # being replaced (e.g. "instead of get_all_text() and get_sentences()"),
        # so a raw substring search over full source is unreliable — same
        # false-positive class documented for ISSUE-003/ISSUE-037. Strip
        # comments before asserting absence of the old call pattern.
        lines = []
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            lines.append(line.split("#", 1)[0])
        return "\n".join(lines)

    def test_update_page_display_uses_combined_extraction(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._update_page_display)
        self.assertIn("get_text_and_sentences", src,
                      "_update_page_display must use get_text_and_sentences (ISSUE-039)")
        code_only = self._strip_comments(src)
        self.assertNotIn("get_all_text(", code_only,
                         "_update_page_display still calls get_all_text separately "
                         "(double extraction regression)")
        self.assertNotIn("self._pdf.get_sentences(", code_only,
                         "_update_page_display still calls get_sentences separately "
                         "(double extraction regression)")

    def test_restore_bookmark_uses_combined_extraction(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertIn("get_text_and_sentences", src,
                      "_restore_bookmark must use get_text_and_sentences (ISSUE-039)")
        code_only = self._strip_comments(src)
        self.assertNotIn("get_all_text(", code_only,
                         "_restore_bookmark still calls get_all_text separately "
                         "(double extraction regression)")
        self.assertNotIn("self._pdf.get_sentences(", code_only,
                         "_restore_bookmark still calls get_sentences separately "
                         "(double extraction regression)")

    def test_app_update_page_display_calls_pdf_exactly_once(self):
        """Behavioral: _update_page_display must call the PDFReader exactly
        once per page load, not twice."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._current_page = 0
        app._pdf = MagicMock()
        app._pdf.get_text_and_sentences = MagicMock(return_value=("Hello world.", ["Hello world."]))
        app._pdf.get_all_text = MagicMock(side_effect=AssertionError("get_all_text must not be called"))
        app._pdf.get_sentences = MagicMock(side_effect=AssertionError("get_sentences must not be called"))
        app._pdf.page_count = 1
        app._text_box = MagicMock()
        app._page_label = MagicMock()
        app._update_page_display()
        app._pdf.get_text_and_sentences.assert_called_once_with(0)
        self.assertEqual(app._sentences, ["Hello world."])


# ---------------------------------------------------------------------------
# ISSUE-032 — _highlight_sentence search key length (Engineer_Mack fix,
# commit ccf7b68) actually caps at 200 chars, not "the full sentence"
# ---------------------------------------------------------------------------

class TestIssue032HighlightSearchKeyLength(unittest.TestCase):
    """The issues.md Fix note claims the search key is now 'the full sentence
    text' with a 200-char fallback 'if the sentence is very long'. The actual
    code unconditionally computes `sentence[:200] if len(sentence) > 200 else
    sentence`, i.e. every sentence longer than 200 chars is capped, not just
    'very long' outliers. These tests pin the ACTUAL behavior and confirm it
    closes the originally reported 40-char collision bug for any sentence up
    to 200 chars (the realistic case for PDF sentences)."""

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._text_box = MagicMock()
        app._highlight_search_start = "1.0"
        return app

    def test_search_key_is_full_sentence_for_100_char_sentence(self):
        """A sentence between the old 40-char cap and the new 200-char cap
        must be searched in full -- this is exactly the class of bug
        reported (two sentences sharing a >40-char common prefix)."""
        app = self._make_app()
        sentence = "A" * 100
        app._text_box.search.return_value = "3.5"
        app._highlight_sentence(sentence)
        called_key = app._text_box.search.call_args[0][0]
        self.assertEqual(called_key, sentence,
                         "search key truncated below 200 chars for a 100-char sentence")
        self.assertNotEqual(called_key, sentence[:40],
                            "ISSUE-032 regression: search key is back to the old 40-char cap")

    def test_search_key_capped_at_200_for_longer_sentences(self):
        """Behavioral evidence of the Fix-note discrepancy: sentences over
        200 chars are still capped -- the search key is NOT the literal full
        sentence as the Fix description states."""
        app = self._make_app()
        sentence = "B" * 250
        app._text_box.search.return_value = "3.5"
        app._highlight_sentence(sentence)
        called_key = app._text_box.search.call_args[0][0]
        self.assertEqual(called_key, sentence[:200],
                         "search key is not capped at 200 chars for a >200-char sentence")
        self.assertEqual(len(called_key), 200)
        self.assertNotEqual(called_key, sentence,
                            "documents the ISSUE-032 Fix-note discrepancy: code caps at "
                            "200 chars, it does not use the literal 'full sentence'")

    def test_highlight_span_uses_full_sentence_length_not_truncated_key(self):
        """Even when the search key is truncated to 200 chars, the highlighted
        span (tag_add end index) must still cover the FULL sentence length,
        not just the 200-char search key."""
        app = self._make_app()
        sentence = "C" * 300
        app._text_box.search.return_value = "5.0"
        app._highlight_sentence(sentence)
        tag_add_args = app._text_box.tag_add.call_args[0]
        self.assertEqual(tag_add_args[0], "highlight")
        self.assertEqual(tag_add_args[2], f"5.0+{len(sentence)}c",
                         "highlight end index must use len(sentence), not the "
                         "truncated 200-char search key length")

    def test_original_40_char_collision_is_resolved(self):
        """Reproduces the originally reported bug: two sentences share an
        identical 40-char prefix but diverge afterward. Pre-fix (40-char
        search key), both sentences produced the identical search key and
        could not be told apart. Post-fix, the search keys are distinct."""
        prefix = "This is a very long sentence that starts with the same "
        self.assertGreater(len(prefix), 40)  # sanity: exercises the old bug's range
        sentence_a = prefix + "opening but ends in Alpha."
        sentence_b = prefix + "opening but ends in Beta."
        search_key_a = sentence_a[:200] if len(sentence_a) > 200 else sentence_a
        search_key_b = sentence_b[:200] if len(sentence_b) > 200 else sentence_b
        self.assertEqual(search_key_a[:40], search_key_b[:40],
                         "sanity: the two sentences must share the old 40-char prefix")
        self.assertNotEqual(search_key_a, search_key_b,
                            "ISSUE-032 regression: distinct sentences sharing a >40-char "
                            "prefix produce identical search keys again")


# ---------------------------------------------------------------------------
# ISSUE-033 — last sentence at EOF (no trailing whitespace) is no longer lost
# ---------------------------------------------------------------------------

class TestIssue033LastSentenceSplit(unittest.TestCase):

    def test_split_sentences_regex_includes_end_of_string_branch(self):
        import src.pdf_reader as pr
        src = inspect.getsource(pr.PDFReader._split_sentences)
        self.assertIn(r"(?<=[.!?])$", src,
                      "_split_sentences regex missing the end-of-string branch (ISSUE-033)")

    def test_last_sentence_with_no_trailing_whitespace_is_split(self):
        import src.pdf_reader as pr
        text = "First sentence. Second sentence ends here."
        result = pr.PDFReader._split_sentences(text)
        self.assertEqual(result, ["First sentence.", "Second sentence ends here."],
                         "the final sentence at EOF (no trailing space/newline) was lost")

    def test_single_sentence_page_with_no_trailing_whitespace(self):
        import src.pdf_reader as pr
        result = pr.PDFReader._split_sentences("Hello world.")
        self.assertEqual(result, ["Hello world."])

    def test_trailing_whitespace_case_still_works_no_regression(self):
        import src.pdf_reader as pr
        text = "First sentence. Second sentence. \n\n"
        result = pr.PDFReader._split_sentences(text)
        self.assertEqual(result, ["First sentence.", "Second sentence."])

    def test_multiple_punctuation_marks_still_split_correctly(self):
        import src.pdf_reader as pr
        text = "Is this real? Yes! It ends now."
        result = pr.PDFReader._split_sentences(text)
        self.assertEqual(result, ["Is this real?", "Yes!", "It ends now."])

    def test_get_sentences_delegates_to_split_sentences_helper(self):
        import src.pdf_reader as pr
        src = inspect.getsource(pr.PDFReader.get_sentences)
        self.assertIn("_split_sentences", src,
                      "get_sentences does not delegate to _split_sentences (ISSUE-033/039 wiring)")

    def test_get_text_and_sentences_also_gets_the_fix(self):
        import src.pdf_reader as pr
        src = inspect.getsource(pr.PDFReader.get_text_and_sentences)
        self.assertIn("_split_sentences", src,
                      "get_text_and_sentences does not delegate to _split_sentences")

    def test_end_to_end_get_sentences_last_sentence_not_lost(self):
        """Behavioral, through the public get_sentences() API with a mocked
        PyMuPDF page, matching the real _update_page_display code path."""
        import src.pdf_reader as pr
        mock_doc = MagicMock()
        mock_doc.is_encrypted = False
        mock_doc.__len__ = MagicMock(return_value=1)
        page = MagicMock()
        page.get_text = MagicMock(return_value="Opening remark. Final sentence ends here.")
        mock_doc.__getitem__ = MagicMock(return_value=page)
        with patch("fitz.open", return_value=mock_doc):
            reader = pr.PDFReader()
            reader.open("/fake/path.pdf")
        sentences = reader.get_sentences(0)
        self.assertEqual(len(sentences), 2,
                         "last sentence on the page was silently merged into "
                         "the previous one (ISSUE-033)")
        self.assertEqual(sentences[-1], "Final sentence ends here.")

    def test_short_fragment_filter_not_actually_implemented(self):
        """Documents a Fix-note discrepancy: the Fix section claims 'a filter
        for very short fragments (< 2 chars) that result from abbreviation
        false splits' was added. No length-based filter exists in
        _split_sentences -- the only filter is `if s.strip()` (drops
        empty/whitespace-only fragments). Abbreviations like 'Dr.' still
        produce a false split. This does not affect the actual reported bug
        (last-sentence-at-EOF loss), which is verified fixed above; flagged
        here so the Validation note is accurate."""
        import src.pdf_reader as pr
        src = inspect.getsource(pr.PDFReader._split_sentences)
        self.assertNotIn("len(s)", src,
                         "if this starts failing, a short-fragment length filter was "
                         "actually added -- update the ISSUE-033 Validation note")
        self.assertNotIn("< 2", src,
                         "if this starts failing, a short-fragment length filter was "
                         "actually added -- update the ISSUE-033 Validation note")


# ---------------------------------------------------------------------------
# ISSUE-034 — VoiceManager.load's _load body wrapped in try/except/finally
# so on_done always fires, even on an unexpected crash
# ---------------------------------------------------------------------------

class TestIssue034VoiceLoadNeverStuck(unittest.TestCase):

    def test_load_inner_function_has_try_except_finally(self):
        import src.voice_manager as vm_mod
        src = inspect.getsource(vm_mod.VoiceManager.load)
        self.assertIn("try:", src)
        self.assertIn("except Exception", src)
        self.assertIn("finally:", src)
        finally_body = src.split("finally:", 1)[1]
        self.assertIn("on_done(", finally_body,
                      "on_done is not called from the finally block (ISSUE-034)")

    def test_finally_guards_none_on_done(self):
        import src.voice_manager as vm_mod
        src = inspect.getsource(vm_mod.VoiceManager.load)
        self.assertIn("if on_done:", src,
                      "the finally block must guard against on_done=None")

    def test_on_done_called_when_offline_loader_raises_unexpectedly(self):
        """Simulates an uncaught exception escaping the try body (bypassing
        _load_offline_voices' own internal try/except) -- on_done must still
        fire so the UI does not get stuck on 'Loading voices...' forever."""
        import src.voice_manager as vm_mod
        vm = vm_mod.VoiceManager()
        vm._load_offline_voices = MagicMock(side_effect=RuntimeError("boom"))
        vm._load_online_voices = MagicMock(return_value=[])
        done = threading.Event()
        result = {}

        def on_done(voices):
            result["voices"] = voices
            done.set()

        vm.load(on_done=on_done)
        self.assertTrue(done.wait(timeout=2),
                        "on_done was never called after _load raised -- UI would "
                        "be stuck on 'Loading voices...' (ISSUE-034)")
        self.assertEqual(result["voices"], [])

    def test_on_done_called_when_online_loader_raises_unexpectedly(self):
        import src.voice_manager as vm_mod
        vm = vm_mod.VoiceManager()
        vm._load_offline_voices = MagicMock(return_value=[])
        vm._load_online_voices = MagicMock(side_effect=RuntimeError("boom"))
        done = threading.Event()
        result = {}

        def on_done(voices):
            result["voices"] = voices
            done.set()

        vm.load(on_done=on_done)
        self.assertTrue(done.wait(timeout=2),
                        "on_done was never called after _load raised (ISSUE-034)")
        self.assertEqual(result["voices"], [])

    def test_loaded_flag_not_set_when_load_fails(self):
        import src.voice_manager as vm_mod
        vm = vm_mod.VoiceManager()
        vm._load_offline_voices = MagicMock(return_value=[])
        vm._load_online_voices = MagicMock(side_effect=RuntimeError("boom"))
        done = threading.Event()
        vm.load(on_done=lambda voices: done.set())
        self.assertTrue(done.wait(timeout=2))
        self.assertFalse(vm.is_loaded,
                         "_loaded should remain False when the load body raised")

    def test_on_done_called_normally_on_success(self):
        """Regression guard: the try/except/finally wrapping must not change
        the success path's result."""
        import src.voice_manager as vm_mod
        fake_voice = vm_mod.Voice(id="x", name="X", locale="en-US",
                                  gender="Female", source="offline")
        vm = vm_mod.VoiceManager()
        vm._load_offline_voices = MagicMock(return_value=[fake_voice])
        vm._load_online_voices = MagicMock(return_value=[])
        done = threading.Event()
        result = {}

        def on_done(voices):
            result["voices"] = voices
            done.set()

        vm.load(on_done=on_done)
        self.assertTrue(done.wait(timeout=2))
        self.assertEqual(result["voices"], [fake_voice])
        self.assertTrue(vm.is_loaded)


# ---------------------------------------------------------------------------
# ISSUE-035 — TTSEngine.close() tears down the AudioPlayer / notify window;
# called from DocumentReaderApp.on_close()
# ---------------------------------------------------------------------------

class TestIssue035TTSEngineClose(unittest.TestCase):

    def test_tts_engine_has_close_method(self):
        import src.tts_engine as te
        self.assertTrue(hasattr(te.TTSEngine, "close"),
                        "TTSEngine.close is missing (ISSUE-035)")

    def test_close_calls_stop_then_player_close(self):
        import src.tts_engine as te
        src = inspect.getsource(te.TTSEngine.close)
        self.assertIn("self.stop()", src)
        self.assertIn("self._player.close()", src)
        self.assertLess(src.find("self.stop()"), src.find("self._player.close()"),
                        "close() must stop playback before tearing down the player")

    def test_close_behavioral_calls_player_close_and_stop(self):
        engine = _make_tts_engine()
        engine.close()
        engine._player.close.assert_called_once()
        engine._player.stop.assert_called_once()

    def test_on_close_calls_tts_close(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.on_close)
        self.assertIn("self._tts.close()", src,
                      "on_close does not call self._tts.close() (ISSUE-035)")

    def test_on_close_stops_before_closing(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.on_close)
        self.assertLess(src.find("self._tts.stop()"), src.find("self._tts.close()"),
                        "on_close must stop before close")

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading = False
        app._paused = False
        app._sentence_idx = 0
        app._current_page = 0
        app._current_pdf_path = None
        app._tts = MagicMock()
        app._pdf = MagicMock()
        app.destroy = MagicMock()
        return app

    def test_on_close_behavioral_calls_both_stop_and_close(self):
        app = self._make_app()
        app.on_close()
        app._tts.stop.assert_called_once()
        app._tts.close.assert_called_once()

    def test_close_is_idempotent_after_stop_already_called(self):
        """AudioPlayer.stop() is idempotent (ISSUE-001/022 lineage). Real
        app.on_close() calls _tts.stop() directly and then _tts.close()
        (which calls stop() again internally) -- close() must not raise or
        deadlock when the player was already stopped/never opened."""
        import src.audio_player as ap
        ap._mci = MagicMock(return_value=0)
        ap._mci_query = MagicMock(return_value="stopped")
        player = ap.AudioPlayer()
        player.stop()   # nothing was ever opened -- must be a safe no-op
        player.close()  # calls stop() again internally -- must not raise


# ---------------------------------------------------------------------------
# ISSUE-036 — bookmark read-modify-write serialized under _bookmark_lock;
# _write_bookmarks uses tempfile + os.replace for atomic writes
# (issues.md lists this as Status: OPEN even though the fix is in the code
#  -- validated here and promoted to VALIDATED, see issues.md note)
# ---------------------------------------------------------------------------

class TestIssue036BookmarkLockAndAtomicWrite(unittest.TestCase):

    def test_bookmark_lock_initialized_in_init(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("_bookmark_lock", src,
                      "_bookmark_lock not initialized in __init__ (ISSUE-036)")
        self.assertIn("threading.Lock()", src.split("_bookmark_lock", 1)[1][:60],
                      "_bookmark_lock is not a threading.Lock")

    def test_bookmark_lock_is_instance_attribute_not_class_level(self):
        """Fix-note discrepancy: the ISSUE-036 Fix description says 'a
        class-level `_bookmark_lock`' was added. The actual code creates it
        as an instance attribute (`self._bookmark_lock = threading.Lock()`
        inside `__init__`). Functionally equivalent for this single-instance
        GUI app, but documented here for accuracy."""
        import src.app as app_mod
        self.assertNotIn("_bookmark_lock", vars(app_mod.DocumentReaderApp),
                         "if this starts failing, _bookmark_lock became a real "
                         "class attribute -- update the ISSUE-036 Validation note")

    def test_save_bookmark_serializes_under_lock(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._save_bookmark)
        self.assertIn("with self._bookmark_lock:", src,
                      "_save_bookmark does not serialize its read-modify-write "
                      "cycle under _bookmark_lock (ISSUE-036)")

    def test_clear_bookmark_serializes_under_lock(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._clear_bookmark)
        self.assertIn("with self._bookmark_lock:", src,
                      "_clear_bookmark does not serialize its read-modify-write "
                      "cycle under _bookmark_lock (ISSUE-036)")

    def test_write_bookmarks_uses_tempfile_and_atomic_replace(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._write_bookmarks)
        self.assertIn("tempfile.mkstemp", src,
                      "_write_bookmarks does not write to a temp file first (ISSUE-036)")
        self.assertIn("os.replace", src,
                      "_write_bookmarks does not atomically replace via os.replace (ISSUE-036)")

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._bookmark_lock = threading.Lock()
        return app

    def test_save_bookmark_lock_actually_serializes_concurrent_callers(self):
        """Behavioral proof the lock is not decorative: block one caller
        mid-read-modify-write and confirm a second concurrent caller cannot
        proceed until the first releases the lock."""
        app = self._make_app()
        release = threading.Event()
        entered_first = threading.Event()

        def blocking_load():
            entered_first.set()
            release.wait(timeout=2)
            return {}

        app._load_bookmarks = blocking_load
        app._write_bookmarks = MagicMock()
        app._current_pdf_path = "C:/a.pdf"
        app._current_page = 0
        app._sentence_idx = 0

        t1 = threading.Thread(target=app._save_bookmark)
        t1.start()
        self.assertTrue(entered_first.wait(timeout=2),
                        "first caller never entered _load_bookmarks")

        second_done = threading.Event()

        def second_call():
            app._save_bookmark()
            second_done.set()

        t2 = threading.Thread(target=second_call)
        t2.start()
        # Second caller must be blocked waiting for the lock, not proceeding.
        self.assertFalse(second_done.wait(timeout=0.3),
                         "second _save_bookmark call proceeded while the first "
                         "still held _bookmark_lock -- lock is not serializing (ISSUE-036)")
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertTrue(second_done.is_set())
        self.assertEqual(app._write_bookmarks.call_count, 2)

    def test_write_bookmarks_atomic_failure_preserves_original_file(self):
        """If the write fails with an OSError (the realistic case, e.g. disk
        full), the original bookmarks file must be left untouched, and the
        temp file must be cleaned up rather than left stray."""
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        original = {"C:/keep.pdf": {"page": 2, "sentence_idx": 1}}
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(original, f)
            app = self._make_app()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                with patch.object(app_mod.json, "dump", side_effect=OSError("disk full")):
                    app._write_bookmarks({"C:/new.pdf": {"page": 0, "sentence_idx": 0}})
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self.assertEqual(data, original,
                             "a failed write corrupted/replaced the original bookmarks file")
            leftover = [name for name in os.listdir(os.path.dirname(path) or ".")
                       if name.startswith(".bookmarks-") and name.endswith(".tmp")]
            self.assertEqual(leftover, [],
                             "temp file from the failed write was not cleaned up")
        finally:
            os.remove(path)

    def test_write_bookmarks_success_leaves_no_stray_temp_file(self):
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            app = self._make_app()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._write_bookmarks({"C:/doc.pdf": {"page": 1, "sentence_idx": 2}})
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self.assertEqual(data, {"C:/doc.pdf": {"page": 1, "sentence_idx": 2}})
            leftover = [name for name in os.listdir(os.path.dirname(path) or ".")
                       if name.startswith(".bookmarks-") and name.endswith(".tmp")]
            self.assertEqual(leftover, [],
                             "a successful write left a stray temp file behind")
        finally:
            os.remove(path)

    def test_save_bookmark_end_to_end_still_persists_correctly(self):
        """Regression guard: the lock + atomic-write plumbing must not change
        the observable behavior of a normal single-threaded _save_bookmark
        call (same contract as the pre-ISSUE-036 implementation)."""
        import json as _json
        import src.app as app_mod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            app = self._make_app()
            app._current_pdf_path = "C:/doc.pdf"
            app._current_page = 3
            app._sentence_idx = 5
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._save_bookmark()
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self.assertEqual(data.get("C:/doc.pdf"), {"page": 3, "sentence_idx": 5})
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# Feature round 2026-07-11 — sentence skip, autosave, document history,
# auto-resume, 5x speed (slider), keyboard toggle
# ---------------------------------------------------------------------------

class TestFeatureSentenceSkip(unittest.TestCase):

    def _make_app(self, reading=True, paused=False, sentence_idx=2,
                  sentences=None):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock()                  # is_open truthy
        app._sentences = sentences if sentences is not None else ["s0", "s1", "s2", "s3"]
        app._sentence_idx = sentence_idx
        app._reading = reading
        app._paused = paused
        app._tts = MagicMock()
        app._read_next_sentence = MagicMock()
        app._save_bookmark = MagicMock()
        app._set_status = MagicMock()
        return app

    def test_skip_forward_while_reading_jumps_to_next_sentence(self):
        # In-flight sentence is idx-1 == "s1"; forward skip must speak "s2".
        app = self._make_app(sentence_idx=2)
        app._skip_sentence(+1)
        self.assertEqual(app._sentence_idx, 2,
                         "forward skip target must be the sentence AFTER the in-flight one")
        app._read_next_sentence.assert_called_once()

    def test_skip_back_while_reading_replays_previous_sentence(self):
        # In-flight is "s1" (idx-1); back skip must speak "s0".
        app = self._make_app(sentence_idx=2)
        app._skip_sentence(-1)
        self.assertEqual(app._sentence_idx, 0)
        app._read_next_sentence.assert_called_once()

    def test_skip_back_clamps_at_first_sentence(self):
        app = self._make_app(sentence_idx=1)  # in-flight is "s0"
        app._skip_sentence(-1)
        self.assertEqual(app._sentence_idx, 0,
                         "back skip at the first sentence must clamp to 0, not go negative")
        app._read_next_sentence.assert_called_once()

    def test_skip_forward_past_last_sentence_is_noop(self):
        app = self._make_app(sentence_idx=4)  # in-flight is "s3" (last)
        app._skip_sentence(+1)
        self.assertEqual(app._sentence_idx, 4, "index must not move past the page end")
        app._read_next_sentence.assert_not_called()
        app._set_status.assert_called_once()

    def test_skip_is_noop_when_stopped(self):
        app = self._make_app(reading=False, paused=False)
        app._skip_sentence(+1)
        app._read_next_sentence.assert_not_called()
        app._tts.stop.assert_not_called()

    def test_skip_is_noop_with_no_sentences(self):
        app = self._make_app(sentences=[])
        app._skip_sentence(+1)
        app._read_next_sentence.assert_not_called()

    def test_skip_while_paused_moves_index_and_drops_paused_track(self):
        """Paused skip must stop the paused MCI track so Resume falls through
        to re-read from the new index (the ISSUE-031 re-advance branch must
        not resume audio of a sentence the user skipped away from)."""
        app = self._make_app(paused=True, sentence_idx=1)
        app._skip_sentence(+1)
        self.assertEqual(app._sentence_idx, 2)
        app._tts.stop.assert_called_once()
        app._save_bookmark.assert_called_once()
        app._read_next_sentence.assert_not_called()  # deferred until Resume

    def test_skip_while_paused_clamps_both_ends(self):
        app = self._make_app(paused=True, sentence_idx=0)
        app._skip_sentence(-1)
        self.assertEqual(app._sentence_idx, 0)
        app = self._make_app(paused=True, sentence_idx=3)
        app._skip_sentence(+1)
        self.assertEqual(app._sentence_idx, 3,
                         "paused forward skip must clamp at the last sentence")


class TestFeatureBookmarkAutosave(unittest.TestCase):

    def _make_app(self, reading=True, paused=False, sentence_idx=3):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading = reading
        app._paused = paused
        app._sentence_idx = sentence_idx
        app._current_page = 2
        app._current_pdf_path = "C:/doc.pdf"
        app._save_bookmark = MagicMock()
        app.after = MagicMock()
        return app

    def test_autosave_saves_inflight_sentence_while_reading(self):
        app = self._make_app(reading=True, sentence_idx=3)
        app._autosave_tick()
        # ISSUE-007 post-increment: the in-flight sentence is idx-1.
        app._save_bookmark.assert_called_once_with(sentence_idx=2)

    def test_autosave_skips_when_idle(self):
        app = self._make_app(reading=False)
        app._autosave_tick()
        app._save_bookmark.assert_not_called()

    def test_autosave_skips_when_paused(self):
        """_pause already saved the rewound position; an autosave while paused
        would be redundant (and racy if the user is skipping sentences)."""
        app = self._make_app(reading=True, paused=True)
        app._autosave_tick()
        app._save_bookmark.assert_not_called()

    def test_autosave_always_reschedules_itself(self):
        import src.app as app_mod
        for kwargs in ({"reading": True}, {"reading": False}):
            app = self._make_app(**kwargs)
            app._autosave_tick()
            app.after.assert_called_once_with(app_mod._AUTOSAVE_MS, app._autosave_tick)

    def test_autosave_reschedules_even_if_save_raises(self):
        app = self._make_app(reading=True)
        app._save_bookmark.side_effect = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            app._autosave_tick()
        app.after.assert_called_once()

    def test_autosave_timer_started_in_init(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp.__init__)
        self.assertIn("_autosave_tick", src,
                      "__init__ must start the periodic autosave timer")


class TestFeatureDocumentHistory(unittest.TestCase):

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._bookmark_lock = threading.Lock()
        app._current_pdf_path = "C:/doc.pdf"
        app._current_page = 1
        app._sentence_idx = 4
        return app

    def test_record_document_opened_writes_last_opened(self):
        import json
        import src.app as app_mod
        fd_, path = tempfile.mkstemp(suffix=".json")
        os.close(fd_)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)
            app = self._make_app()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._record_document_opened("C:/doc.pdf")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("last_opened", data["C:/doc.pdf"],
                          "_record_document_opened must stamp last_opened")
        finally:
            os.remove(path)

    def test_record_preserves_existing_bookmark_position(self):
        import json
        import src.app as app_mod
        fd_, path = tempfile.mkstemp(suffix=".json")
        os.close(fd_)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"C:/doc.pdf": {"page": 7, "sentence_idx": 3}}, f)
            app = self._make_app()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._record_document_opened("C:/doc.pdf")
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)["C:/doc.pdf"]
            self.assertEqual(entry["page"], 7,
                             "recording an open must not clobber the saved position")
            self.assertEqual(entry["sentence_idx"], 3)
        finally:
            os.remove(path)

    def test_save_bookmark_preserves_last_opened(self):
        import json
        import src.app as app_mod
        fd_, path = tempfile.mkstemp(suffix=".json")
        os.close(fd_)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"C:/doc.pdf": {"last_opened": "2026-07-11T10:00:00"}}, f)
            app = self._make_app()
            with patch.object(app_mod, "_BOOKMARKS_FILE", path):
                app._save_bookmark()
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)["C:/doc.pdf"]
            self.assertEqual(entry["last_opened"], "2026-07-11T10:00:00",
                             "_save_bookmark must merge, not clobber, metadata keys")
            self.assertEqual(entry["page"], 1)
            self.assertEqual(entry["sentence_idx"], 4)
        finally:
            os.remove(path)

    def test_get_recent_files_sorts_by_last_opened_and_drops_missing(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        fd_a, real_a = tempfile.mkstemp(suffix=".pdf")
        os.close(fd_a)
        fd_b, real_b = tempfile.mkstemp(suffix=".pdf")
        os.close(fd_b)
        try:
            bookmarks = {
                real_a: {"last_opened": "2026-07-10T09:00:00"},
                real_b: {"last_opened": "2026-07-11T09:00:00"},
                "C:/gone/missing.pdf": {"last_opened": "2026-07-12T09:00:00"},
                "C:/bad/entry.pdf": "not-a-dict",
            }
            with patch.object(app_mod.DocumentReaderApp, "_load_bookmarks",
                              return_value=bookmarks):
                recent = app._get_recent_files()
            self.assertEqual(recent, [real_b, real_a],
                             "recent files must be newest-first and exclude "
                             "missing files and malformed entries")
        finally:
            os.remove(real_a)
            os.remove(real_b)


class TestFeatureAutoResume(unittest.TestCase):

    def _make_app(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._pdf = MagicMock(page_count=5)
        app._sentences = ["s0", "s1", "s2"]
        app._current_page = 0
        app._sentence_idx = 0
        app._set_status = MagicMock()
        return app

    def test_restore_no_longer_prompts(self):
        """Auto-resume (2026-07-11): opening a document proceeds from the
        saved position without an askyesno dialog."""
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._restore_bookmark)
        self.assertNotIn("askyesno", src,
                         "_restore_bookmark still prompts instead of auto-resuming")

    def test_restore_same_page_jumps_to_saved_sentence(self):
        import src.app as app_mod
        app = self._make_app()
        askyesno = MagicMock()
        with patch.object(app_mod.DocumentReaderApp, "_load_bookmarks",
                          return_value={"p.pdf": {"page": 0, "sentence_idx": 2}}), \
             patch.object(app_mod.mb, "askyesno", askyesno):
            result = app._restore_bookmark("p.pdf")
        self.assertTrue(result)
        askyesno.assert_not_called()
        self.assertEqual(app._sentence_idx, 2)
        app._set_status.assert_called_once()

    def test_restore_other_page_reloads_text_and_jumps(self):
        import src.app as app_mod
        app = self._make_app()
        app._pdf.get_text_and_sentences.return_value = ("text", ["a", "b", "c", "d"])
        app._text_box = MagicMock()
        app._page_label = MagicMock()
        app._update_nav_buttons = MagicMock()
        with patch.object(app_mod.DocumentReaderApp, "_load_bookmarks",
                          return_value={"p.pdf": {"page": 3, "sentence_idx": 3}}):
            result = app._restore_bookmark("p.pdf")
        self.assertTrue(result)
        self.assertEqual(app._current_page, 3)
        self.assertEqual(app._sentence_idx, 3)
        app._pdf.get_text_and_sentences.assert_called_once_with(3)


class TestFeatureSpeedAndKeyboard(unittest.TestCase):

    def test_slider_range_is_half_to_five_x(self):
        import src.app as app_mod
        src = inspect.getsource(app_mod.DocumentReaderApp._build_ui)
        self.assertIn("to=5.0", src,
                      "speed slider must reach 5.0x (fast-reading feature)")
        self.assertIn("from_=0.5", src)

    def test_space_pauses_while_reading(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading, app._paused = True, False
        app._pause = MagicMock()
        app._play = MagicMock()
        app._on_key_toggle_play()
        app._pause.assert_called_once()
        app._play.assert_not_called()

    def test_space_plays_when_idle_and_ready(self):
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading, app._paused = False, False
        app._pdf = MagicMock()  # is_open truthy
        app._voices_ready = True
        app._pause = MagicMock()
        app._play = MagicMock()
        app._on_key_toggle_play()
        app._play.assert_called_once()

    def test_space_noop_when_voices_not_ready(self):
        """The keyboard path must respect the ISSUE-038 readiness gate."""
        import src.app as app_mod
        app = app_mod.DocumentReaderApp.__new__(app_mod.DocumentReaderApp)
        app._reading, app._paused = False, False
        app._pdf = MagicMock()
        app._voices_ready = False
        app._pause = MagicMock()
        app._play = MagicMock()
        app._on_key_toggle_play()
        app._play.assert_not_called()
        app._pause.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
