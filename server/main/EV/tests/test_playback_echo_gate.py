import os
import threading
import unittest
from unittest import mock

import numpy as np

from speech.echo.playback_gate import PlaybackEchoGate


class _FakeAec:
    available = False
    backend = "none"
    error = "disabled for test"

    def reset(self):
        pass

    def set_delay_ms(self, _delay_ms):
        pass


class PlaybackEchoGateDelayTest(unittest.TestCase):
    def _gate(self, *, max_delay_ms=None):
        with mock.patch(
            "speech.echo.playback_gate.get_webrtc_aec",
            return_value=_FakeAec(),
        ):
            return PlaybackEchoGate(
                16000,
                20,
                threading.Event(),
                max_delay_ms=max_delay_ms,
            )

    def test_local_delay_cap_overrides_larger_environment_range(self):
        with mock.patch.dict(
            os.environ,
            {"VOICE_ECHO_MAX_DELAY_MS": "2200"},
        ):
            gate = self._gate(max_delay_ms=400)

        self.assertEqual(gate.max_delay_frames * gate.frame_ms, 400)

    def test_correlation_misses_keep_last_trusted_delay(self):
        gate = self._gate()
        gate.min_correlation = 2.0
        gate.estimated_delay = 5
        gate.delay_misses = 5
        spectrum = np.ones(32, dtype=np.float32)
        gate.near_features.extend(
            (index, 100 + index, spectrum) for index in range(24)
        )
        gate.far_features.update(
            {
                index: (200 + (index % 3), spectrum)
                for index in range(24)
            }
        )

        gate._estimate_delay_locked(23)

        self.assertEqual(gate.delay_misses, 6)
        self.assertEqual(gate.estimated_delay, 5)

    def test_locked_delay_does_not_reopen_global_search_after_misses(self):
        gate = self._gate()
        gate.estimated_delay = 5
        gate.delay_misses = 6
        spectrum = np.ones(32, dtype=np.float32)
        near_rms_values = [
            200, 900, 350, 1400, 500, 1100,
            260, 1250, 420, 980, 310, 1500,
        ] * 2
        gate.near_features.extend(
            (100 + offset, rms, spectrum)
            for offset, rms in enumerate(near_rms_values)
        )
        # 50 帧（1000ms）处放一个完美伪相关峰；锁定后的局部搜索不应看到它。
        gate.far_features.update(
            {
                50 + offset: (rms, spectrum)
                for offset, rms in enumerate(near_rms_values)
            }
        )

        gate._estimate_delay_locked(123)

        self.assertEqual(gate.estimated_delay, 5)


if __name__ == "__main__":
    unittest.main()
