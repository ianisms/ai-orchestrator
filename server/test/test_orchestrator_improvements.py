from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

MODULES_ROOT = Path('/ai/server/modules')
SERVER_ROOT = Path('/ai/server')
ORCH_ROOT = Path('/ai/server/modules/orchestrator')
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
if str(MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULES_ROOT))
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

IMPORT_ERROR = None
try:
    from orchestrator.core.utterance import _normalize_tool_output
    from orchestrator.core import metrics
    from aiserver import server as orch_server
except Exception as exc:  # pragma: no cover - environment-dependent import
    IMPORT_ERROR = exc


class _DummyLog:
    def info(self, *_args, **_kwargs):
        return None


class OrchestratorImprovementsTests(unittest.TestCase):
    @unittest.skipIf(IMPORT_ERROR is not None, f"missing runtime deps: {IMPORT_ERROR!r}")
    def test_normalize_tool_output(self) -> None:
        normalized, text = _normalize_tool_output('weather', 'Tool error: boom')
        self.assertEqual(normalized['tool'], 'weather')
        self.assertEqual(normalized['status'], 'error')
        self.assertIn('status=error', text)

    @unittest.skipIf(IMPORT_ERROR is not None, f"missing runtime deps: {IMPORT_ERROR!r}")
    def test_self_check_payload_shape(self) -> None:
        payload = orch_server._build_self_check_payload(tools_connected=True)
        self.assertEqual(payload['status'], 'ok')
        self.assertIn('config', payload)
        self.assertIn('sessions', payload)
        self.assertTrue(payload['tools_connected'])

    @unittest.skipIf(IMPORT_ERROR is not None, f"missing runtime deps: {IMPORT_ERROR!r}")
    def test_metrics_helpers_callable(self) -> None:
        metrics.inc_barge_in_interrupts('audio')
        metrics.inc_turn_watchdog_timeouts('total')
        metrics.inc_session_evictions('ttl', 2)
        metrics.set_queue_depth('out_q', 3)
        metrics.inc_queue_drops('out_q', 1)
        metrics.record_tool_output('ok', 128)
        metrics.inc_turn_outcome('ok')
        metrics.set_sessions_active(4)
        metrics.inc_sessions_created(1)
        metrics.inc_sessions_destroyed('ttl', 1)
        metrics.observe_barge_in_to_audio_last_seconds(0.12)

    @unittest.skipIf(IMPORT_ERROR is not None, f"missing runtime deps: {IMPORT_ERROR!r}")
    def test_session_eviction_ttl(self) -> None:
        old_env_ttl = os.environ.get('ORCH_SESSION_TTL_S')
        old_env_max = os.environ.get('ORCH_SESSION_MAX')
        os.environ['ORCH_SESSION_TTL_S'] = '1'
        os.environ['ORCH_SESSION_MAX'] = '1000'

        prev_hist = dict(orch_server._SESSION_HISTORIES)
        prev_seen = dict(orch_server._SESSION_LAST_SEEN)
        try:
            orch_server._SESSION_HISTORIES.clear()
            orch_server._SESSION_LAST_SEEN.clear()

            orch_server._SESSION_HISTORIES['s1'] = object()  # type: ignore[assignment]
            orch_server._SESSION_LAST_SEEN['s1'] = time.time() - 10.0
            orch_server._evict_sessions(_DummyLog())

            self.assertNotIn('s1', orch_server._SESSION_HISTORIES)
            self.assertNotIn('s1', orch_server._SESSION_LAST_SEEN)
        finally:
            orch_server._SESSION_HISTORIES.clear()
            orch_server._SESSION_HISTORIES.update(prev_hist)
            orch_server._SESSION_LAST_SEEN.clear()
            orch_server._SESSION_LAST_SEEN.update(prev_seen)

            if old_env_ttl is None:
                os.environ.pop('ORCH_SESSION_TTL_S', None)
            else:
                os.environ['ORCH_SESSION_TTL_S'] = old_env_ttl

            if old_env_max is None:
                os.environ.pop('ORCH_SESSION_MAX', None)
            else:
                os.environ['ORCH_SESSION_MAX'] = old_env_max


if __name__ == '__main__':
    unittest.main()
