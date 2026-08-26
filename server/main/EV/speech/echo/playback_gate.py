# -*- coding: utf-8 -*-
"""Playback-reference echo gate and barge-in classifier.

优先 WebRTC AEC3：用 TTS 参考消回声，再在残差上做 VAD/打断。
AEC 不可用时回退到频谱能量门。
"""
import audioop
import collections
import os

try:
    from devices.voice.env import migrate_camera_voice_environ as _migrate_voice_env
    _migrate_voice_env()
except Exception:
    pass
import threading
import time

import numpy as np
import webrtcvad

from speech.echo.webrtc_aec import get_webrtc_aec


class PlaybackEchoGate:
    """用播放参考信号屏蔽扬声器回声，并检测播放期间的真人插话。"""

    def __init__(
        self,
        sample_rate,
        frame_ms,
        interrupt_event,
        logger=None,
        event_logger=None,
        max_delay_ms=None,
    ):
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.frame_samples = self.sample_rate * self.frame_ms // 1000
        self.frame_bytes = self.frame_samples * 2
        self.interrupt_event = interrupt_event
        self.logger = logger
        self.event_logger = event_logger
        self.enabled = os.environ.get(
            "VOICE_BARGE_IN",
            "1",
        ).lower() not in ("0", "", "off", "no", "false")
        # 本机声卡链路物理延迟稳定，跨播放保留对齐/增益，省去每次 TTS
        # 重新校准的 200ms；摄像头/网络链路仍每次重校。
        self._keep_calibration_enabled = (
            os.environ.get("VOICE_ECHO_KEEP_CALIBRATION", "1").lower()
            not in ("0", "", "off", "no", "false")
        )
        configured_max_delay_ms = float(os.environ.get(
            "VOICE_ECHO_MAX_DELAY_MS",
            "2200",
        ))
        if max_delay_ms is not None:
            configured_max_delay_ms = min(
                configured_max_delay_ms,
                float(max_delay_ms),
            )
        self.max_delay_frames = max(
            1,
            int(configured_max_delay_ms / self.frame_ms),
        )
        self.tail_seconds = float(os.environ.get(
            "VOICE_ECHO_TAIL_MS",
            "900",
        )) / 1000.0
        self.calibration_seconds = float(os.environ.get(
            "VOICE_ECHO_CALIBRATION_MS",
            "500",
        )) / 1000.0
        self.min_correlation = float(os.environ.get(
            "VOICE_ECHO_MIN_CORRELATION",
            "0.28",
        ))
        self.min_rms = int(os.environ.get(
            "VOICE_BARGE_IN_MIN_RMS",
            "300",
        ))
        self.min_excess_rms = int(os.environ.get(
            "VOICE_BARGE_IN_EXCESS_RMS",
            "140",
        ))
        self.energy_ratio = float(os.environ.get(
            "VOICE_BARGE_IN_ENERGY_RATIO",
            "1.40",
        ))
        self.min_residual_rms = int(os.environ.get(
            "VOICE_BARGE_IN_RESIDUAL_RMS",
            "220",
        ))
        self.min_barge_near_rms = int(os.environ.get(
            "VOICE_BARGE_IN_NEAR_RMS",
            "380",
        ))
        self.max_spectral_similarity = float(os.environ.get(
            "VOICE_BARGE_IN_MAX_SPECTRAL_SIMILARITY",
            "0.88",
        ))
        self.echo_similarity_veto = float(os.environ.get(
            "VOICE_ECHO_SIMILARITY_VETO",
            "0.94",
        ))
        # 频谱仍很像回声时，相似度低于此值仍可放行（双讲时近端常被喇叭拖高相似度）。
        # 0.992 太松：实测 37 次打断中 7 次是误打断（打断后 ASR 无任何人声），
        # 其中 3 次落在 0.991~0.992；而真打断的相似度最高只到 0.990。
        # 收到 0.990 可挡掉那 3 次误打断且不误伤任何一次真打断。
        # 剩余误打断与真打断分布重叠，靠此阈值无法再分，不再强收以免插不进话。
        self.max_sim_for_accept = float(os.environ.get(
            "VOICE_BARGE_IN_MAX_SIM_FOR_ACCEPT",
            "0.990",
        ))
        self.min_residual_lift = float(os.environ.get(
            "VOICE_BARGE_IN_RESIDUAL_LIFT",
            "1.7",
        ))
        self.decision_window = max(
            3,
            int(os.environ.get("VOICE_BARGE_IN_WINDOW_FRAMES", "16")),
        )
        self.required_frames = max(
            2,
            min(
                self.decision_window,
                int(os.environ.get(
                    "VOICE_BARGE_IN_CONFIRM_FRAMES",
                    "3",
                )),
            ),
        )
        self.delay_search_radius = max(
            2,
            int(float(os.environ.get(
                "VOICE_ECHO_SEARCH_RADIUS_MS",
                "240",
            )) / self.frame_ms),
        )
        self.preroll_frames = max(
            self.decision_window,
            int(os.environ.get(
                "VOICE_BARGE_IN_PREROLL_FRAMES",
                "22",
            )),
        )
        self.preroll_lead_frames = max(
            0,
            int(os.environ.get(
                "VOICE_BARGE_IN_PREROLL_LEAD_FRAMES",
                "2",
            )),
        )
        self.diagnostic_interval = max(
            0.2,
            float(os.environ.get(
                "VOICE_DIAG_ECHO_INTERVAL",
                "1.0",
            )),
        )
        self.lock = threading.RLock()
        self.vad = webrtcvad.Vad(2)
        self.window = np.hanning(self.frame_samples).astype(np.float32)
        self.zero_frame = b"\0" * self.frame_bytes
        self.aec = get_webrtc_aec(self.sample_rate)
        self._reset_locked()
        if self.enabled:
            if self.aec.available:
                self._log("AEC=WebRTC AEC3（TTS 参考消回声后判打断）")
            else:
                self._log(
                    "AEC 不可用，回退频谱能量门：%s"
                    % (self.aec.error or "unknown")
                )

    def _reset_locked(self, keep_calibration=False):
        # keep_calibration：本机声卡链路物理延迟稳定，跨播放保留上次的对齐
        # 与回声增益，避免每次 TTS 都要重新校准 200ms 才能打断（二次打断零等待）。
        if not keep_calibration:
            self.estimated_delay = None
            self.echo_gain = 1.0
        self.started_at = 0.0
        self.playback_active = False
        self.tail_until = 0.0
        self.reference_rate = self.sample_rate
        self.rate_state = None
        self.reference_pending = bytearray()
        self.next_reference_index = None
        self.last_near_index = -1
        self.far_features = {}
        self.far_pcm = {}
        self.near_features = collections.deque(maxlen=180)
        self.decisions = collections.deque(maxlen=self.decision_window)
        self.raw_preroll = collections.deque(maxlen=self.preroll_frames)
        self.pending_preroll = []
        self.delay_misses = 0
        self.residual_floor = float(self.min_residual_rms)
        self.last_estimate_index = -1000
        self.last_logged_delay = None
        self.playback_id = None
        self.diagnostic_counts = collections.Counter()
        self.diagnostic_max_near_rms = 0
        self.diagnostic_last_emit = time.perf_counter()
        self.rejected_speech_frames = 0
        self.rejected_speech_logged = False
        if getattr(self, "aec", None) is not None:
            self.aec.reset()

    def begin(self, reference_rate, playback_id=None):
        if not self.enabled:
            return
        with self.lock:
            self._reset_locked(keep_calibration=self._keep_calibration_enabled)
            self.started_at = time.perf_counter()
            self.playback_active = True
            self.playback_id = playback_id
            self.reference_rate = int(reference_rate or self.sample_rate)
            self.interrupt_event.clear()
            self._event(
                "echo_playback_begin",
                reference_rate=self.reference_rate,
                aec=self.aec.backend,
                calibrated=self.estimated_delay is not None,
            )

    def finish(self):
        if not self.enabled:
            return
        with self.lock:
            if self.started_at:
                self.playback_active = False
                self.tail_until = time.perf_counter() + self.tail_seconds
                self._emit_diagnostic_locked(force=True)
                self._event(
                    "echo_playback_finish",
                    interrupted=self.interrupt_event.is_set(),
                    estimated_delay_ms=(
                        None
                        if self.estimated_delay is None
                        else self.estimated_delay * self.frame_ms
                    ),
                )

    def feed_reference(self, pcm, reference_rate=None):
        if not self.enabled or not pcm:
            return
        with self.lock:
            if not self.playback_active:
                return
            source_rate = int(reference_rate or self.reference_rate)
            converted, self.rate_state = audioop.ratecv(
                bytes(pcm),
                2,
                1,
                source_rate,
                self.sample_rate,
                self.rate_state,
            )
            self.reference_pending.extend(converted)
            clock_index = self._clock_index_locked(time.perf_counter())
            if self.next_reference_index is None:
                self.next_reference_index = clock_index
            elif clock_index > self.next_reference_index + 3:
                self.next_reference_index = clock_index
            while len(self.reference_pending) >= self.frame_bytes:
                frame = bytes(self.reference_pending[:self.frame_bytes])
                del self.reference_pending[:self.frame_bytes]
                self.far_features[self.next_reference_index] = self._features(frame)
                self.far_pcm[self.next_reference_index] = frame
                self.next_reference_index += 1
            self._trim_far_locked(clock_index)

    def is_active(self):
        """是否处于播放/回声保护期（此期间全零帧应被吞，避免打断后 ASR 灌静音）。

        平时（无播放、无打断）返回 False：此时全零静音帧必须放行到主循环，
        否则 VAD 判停攒不出静音帧，用户说完话后要等到时长硬上限才判停。
        """
        if not self.enabled:
            return False
        with self.lock:
            if not self.started_at:
                return False
            return (
                self.playback_active
                or time.perf_counter() <= self.tail_until
                or self.interrupt_event.is_set()
            )

    def process(self, frame):
        if not self.enabled or len(frame) != self.frame_bytes:
            return frame
        now = time.perf_counter()
        with self.lock:
            if not self.started_at:
                return frame
            active = self.playback_active or now <= self.tail_until
            if not active:
                self._reset_locked()
                return frame

            near_index = max(
                self.last_near_index + 1,
                self._clock_index_locked(now),
            )
            self.last_near_index = near_index
            self.raw_preroll.append(frame)
            near_rms, near_spectrum = self._features(frame)
            self.near_features.append((near_index, near_rms, near_spectrum))
            self._trim_far_locked(near_index)

            if (
                self.playback_active
                and not self.interrupt_event.is_set()
                and near_index - self.last_estimate_index >= 8
                and now - self.started_at >= self.calibration_seconds
            ):
                self._estimate_delay_locked(near_index)

            if self.estimated_delay is None:
                # 没对齐前不打断，避免错延迟把回声残差当成人声
                self._record_diagnostic_locked("no_alignment", near_rms)
                return self.zero_frame if self.playback_active else frame

            far_hit = self._nearest_far_entry_locked(
                near_index - self.estimated_delay,
            )
            if far_hit is None:
                self._record_diagnostic_locked("reference_missing", near_rms)
                return self.zero_frame if self.playback_active else frame

            far_rms, far_spectrum, far_frame = far_hit
            spectral_similarity = float(np.dot(near_spectrum, far_spectrum))
            if far_rms >= 90 and spectral_similarity >= self.echo_similarity_veto:
                observed_gain = near_rms / max(1.0, far_rms)
                self.echo_gain = min(
                    20.0,
                    max(
                        0.05,
                        self.echo_gain * 0.80 + observed_gain * 0.20,
                    ),
                )
            expected_echo = max(1.0, self.echo_gain * far_rms)
            excess_rms = near_rms - expected_echo
            ratio = near_rms / expected_echo
            try:
                near_is_speech = self.vad.is_speech(frame, self.sample_rate)
            except Exception:
                near_is_speech = near_rms >= self.min_rms

            # far_frame 已按 estimated_delay 对齐；AEC 内再设 delay 会双重补偿 → 残差假人声
            self.aec.set_delay_ms(0)
            cleaned = None
            residual_rms = 0
            residual_speech = False
            if self.aec.available and far_frame:
                cleaned = self.aec.process(frame, far_frame)
            if cleaned:
                residual_rms = int(audioop.rms(cleaned, 2))
                try:
                    residual_speech = self.vad.is_speech(
                        cleaned,
                        self.sample_rate,
                    )
                except Exception:
                    residual_speech = residual_rms >= self.min_residual_rms
            else:
                # 无 AEC：简单线性减参考作弱残差
                near_samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
                far_samples = np.frombuffer(far_frame, dtype="<i2").astype(np.float32)
                residual = near_samples - (self.echo_gain * far_samples)
                residual_rms = int(np.sqrt(np.mean(residual * residual) + 1.0))
                residual_pcm = np.clip(residual, -32768, 32767).astype("<i2").tobytes()
                try:
                    residual_speech = self.vad.is_speech(
                        residual_pcm,
                        self.sample_rate,
                    )
                except Exception:
                    residual_speech = residual_rms >= self.min_residual_rms

            far_is_quiet = far_rms < 90
            far_active = far_rms >= 220
            # 底板不能塌成很小，否则 residual_lift 虚高（日志里抬升 20~40 就是这个）
            floor_min = float(self.min_residual_rms) * 0.85
            if (
                spectral_similarity >= 0.90
                and excess_rms < self.min_excess_rms
                and residual_rms < max(self.residual_floor * 1.8, self.min_residual_rms * 1.5)
            ):
                self.residual_floor = max(
                    floor_min,
                    self.residual_floor * 0.92 + float(residual_rms) * 0.08,
                )
            else:
                self.residual_floor = max(floor_min, self.residual_floor)
            residual_lift = residual_rms / max(floor_min, self.residual_floor)
            # Similarity is numerically meaningless when the reference is
            # almost silent (the normalized spectra both collapse to the same
            # noise shape). In that state, a loud near-end voice must remain
            # eligible to interrupt playback.
            echo_like = far_active and spectral_similarity >= self.echo_similarity_veto
            # 近端不比预期回声更响 → 不是真人压过喇叭
            echo_dominated = ratio < 1.15 or near_rms < expected_echo

            # 主路径：AEC 残差。有 WebRTC 时禁止经典能量门——
            # 经典门在 TTS 空隙/回声增益漂移时会「自己打断自己」。
            aec_barge = False
            if far_active and cleaned is not None:
                min_resid = max(
                    self.min_residual_rms,
                    int(far_rms * 0.10),
                    int(expected_echo * 0.45),
                )
                aec_barge = (
                    near_is_speech
                    and residual_speech
                    and near_rms >= self.min_barge_near_rms
                    and residual_rms >= min_resid
                    and residual_lift >= self.min_residual_lift
                    and not echo_dominated
                )
                # 双讲时频谱常被喇叭拖高：残差够硬才放行，禁止残差≈0 的假阳性
                if aec_barge and echo_like:
                    aec_barge = (
                        ratio >= 1.28
                        and residual_lift >= max(self.min_residual_lift, 1.7)
                        and residual_rms >= max(min_resid, 320)
                        and near_rms >= max(
                            self.min_barge_near_rms,
                            int(far_rms * 0.32),
                        )
                        and spectral_similarity <= self.max_sim_for_accept
                    )

            classic_barge = (
                near_is_speech
                and near_rms >= self.min_rms
                and not echo_like
                and not echo_dominated
                and (
                    far_is_quiet
                    or (
                        excess_rms >= self.min_excess_rms
                        and (
                            ratio >= max(2.0, self.energy_ratio)
                            or (
                                ratio >= 1.35
                                and spectral_similarity
                                <= self.max_spectral_similarity
                            )
                        )
                    )
                )
            )
            if self.aec.available and cleaned is not None:
                double_talk = aec_barge
            else:
                double_talk = classic_barge or aec_barge

            if self.playback_id == "greet":
                double_talk = False

            if double_talk:
                decision_reason = "accepted_aec" if aec_barge else "accepted"
            elif echo_dominated or (echo_like and residual_rms < self.min_residual_rms * 2):
                decision_reason = "echo_like"
            elif not near_is_speech and not residual_speech:
                decision_reason = "vad_rejected"
            elif near_rms < self.min_rms and residual_rms < self.min_residual_rms:
                decision_reason = "rms_too_low"
            else:
                decision_reason = "insufficient_excess"
            self._record_diagnostic_locked(
                decision_reason,
                near_rms,
                far_rms=far_rms,
                expected_echo=expected_echo,
                ratio=ratio,
                spectral_similarity=spectral_similarity,
            )
            self.decisions.append(bool(double_talk))

            # 连续确认；回声形态时略加长，挡残差尖峰，但别回到死板的 8 帧
            need_frames = self.required_frames
            if self.aec.available and (echo_like or echo_dominated):
                need_frames = max(need_frames, min(self.decision_window, 5))
            if (
                self.playback_active
                and not self.interrupt_event.is_set()
                and now - self.started_at >= self.calibration_seconds
                and len(self.decisions) >= need_frames
                and sum(list(self.decisions)[-need_frames:]) >= need_frames
                and double_talk
            ):
                self.interrupt_event.set()
                self.pending_preroll = self._select_barge_preroll_locked()
                self._log(
                    "检测到真人插话：AEC=%s 延迟≈%dms，近端RMS=%d，远端RMS=%d，"
                    "残差RMS=%d，抬升=%.2f，能量比=%.2f，频谱相似度=%.2f，"
                    "交接音频=%dms"
                    % (
                        self.aec.backend,
                        self.estimated_delay * self.frame_ms,
                        near_rms,
                        far_rms,
                        residual_rms,
                        residual_lift,
                        ratio,
                        spectral_similarity,
                        len(self.pending_preroll) * self.frame_ms,
                    )
                )
                self._event(
                    "barge_in_triggered",
                    estimated_delay_ms=self.estimated_delay * self.frame_ms,
                    near_rms=near_rms,
                    far_rms=far_rms,
                    residual_rms=residual_rms,
                    expected_echo_rms=int(expected_echo),
                    energy_ratio=round(ratio, 3),
                    spectral_similarity=round(spectral_similarity, 3),
                    handoff_audio_ms=len(self.pending_preroll) * self.frame_ms,
                    via="aec" if aec_barge else "classic",
                    aec=self.aec.backend,
                )

            if self.interrupt_event.is_set():
                return frame if double_talk else self.zero_frame
            return self.zero_frame

    def take_preroll(self):
        with self.lock:
            frames = self.pending_preroll
            self.pending_preroll = []
            return frames

    def _select_barge_preroll_locked(self):
        decisions = list(self.decisions)[-self.decision_window:]
        frames = list(self.raw_preroll)[-len(decisions):]
        if not decisions or not frames:
            return []
        first_speech = next(
            (index for index, value in enumerate(decisions) if value),
            len(decisions) - 1,
        )
        start = max(0, first_speech - self.preroll_lead_frames)
        return frames[start:]

    def _record_diagnostic_locked(
        self,
        reason,
        near_rms,
        far_rms=0,
        expected_echo=0.0,
        ratio=0.0,
        spectral_similarity=0.0,
    ):
        self.diagnostic_counts[reason] += 1
        self.diagnostic_max_near_rms = max(
            self.diagnostic_max_near_rms,
            int(near_rms),
        )
        speech_like_rejected = reason in ("echo_like", "insufficient_excess")
        if speech_like_rejected:
            self.rejected_speech_frames += 1
            if (
                self.rejected_speech_frames >= self.required_frames
                and not self.rejected_speech_logged
            ):
                self.rejected_speech_logged = True
                self._event(
                    "barge_in_candidate_rejected",
                    reason=reason,
                    consecutive_ms=self.rejected_speech_frames * self.frame_ms,
                    near_rms=int(near_rms),
                    far_rms=int(far_rms),
                    expected_echo_rms=int(expected_echo),
                    energy_ratio=round(float(ratio), 3),
                    spectral_similarity=round(
                        float(spectral_similarity),
                        3,
                    ),
                    estimated_delay_ms=(
                        None
                        if self.estimated_delay is None
                        else self.estimated_delay * self.frame_ms
                    ),
                )
        else:
            self.rejected_speech_frames = 0
            self.rejected_speech_logged = False
        self._emit_diagnostic_locked()

    def _emit_diagnostic_locked(self, force=False):
        now = time.perf_counter()
        if (
            not force
            and now - self.diagnostic_last_emit < self.diagnostic_interval
        ):
            return
        if self.diagnostic_counts:
            self._event(
                "echo_decision_summary",
                interval_ms=round(
                    (now - self.diagnostic_last_emit) * 1000,
                    1,
                ),
                counts=dict(self.diagnostic_counts),
                max_near_rms=self.diagnostic_max_near_rms,
                estimated_delay_ms=(
                    None
                    if self.estimated_delay is None
                    else self.estimated_delay * self.frame_ms
                ),
            )
        self.diagnostic_counts.clear()
        self.diagnostic_max_near_rms = 0
        self.diagnostic_last_emit = now

    def _event(self, event, **fields):
        if not self.event_logger:
            return
        try:
            self.event_logger(
                event,
                turn_id=self.playback_id,
                playback_id=self.playback_id,
                **fields,
            )
        except Exception:
            pass

    def _clock_index_locked(self, now):
        return max(
            0,
            int((now - self.started_at) * 1000.0 / self.frame_ms),
        )

    def _features(self, frame):
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        rms = int(np.sqrt(np.mean(samples * samples) + 1.0))
        spectrum = np.abs(np.fft.rfft(samples * self.window))[1:129]
        spectrum = np.log1p(spectrum)
        if spectrum.size >= 128:
            spectrum = spectrum[:128].reshape(32, 4).mean(axis=1)
        norm = float(np.linalg.norm(spectrum))
        if norm > 1e-6:
            spectrum = spectrum / norm
        return rms, spectrum.astype(np.float32)

    def _estimate_delay_locked(self, near_index):
        self.last_estimate_index = near_index
        recent = list(self.near_features)[-70:]
        if len(recent) < 24 or len(self.far_features) < 24:
            return
        best = None
        if self.estimated_delay is None:
            delays = range(0, self.max_delay_frames + 1)
        else:
            # 锁定后始终只在可信值附近跟踪。真人插话会让能量相关暂时
            # 消失，此时扩大为全范围搜索只会更容易命中连续语音伪峰。
            delays = range(
                max(0, self.estimated_delay - self.delay_search_radius),
                min(
                    self.max_delay_frames,
                    self.estimated_delay + self.delay_search_radius,
                ) + 1,
            )
        for delay in delays:
            near_values = []
            far_values = []
            ratios = []
            for index, near_rms, _ in recent:
                far = self.far_features.get(index - delay)
                if far is None:
                    continue
                far_rms = far[0]
                if far_rms < 80 or near_rms < 80:
                    continue
                near_values.append(np.log1p(near_rms))
                far_values.append(np.log1p(far_rms))
                ratios.append(near_rms / max(1.0, far_rms))
            if len(near_values) < 18:
                continue
            near_array = np.asarray(near_values, dtype=np.float32)
            far_array = np.asarray(far_values, dtype=np.float32)
            near_array -= near_array.mean()
            far_array -= far_array.mean()
            denominator = float(
                np.linalg.norm(near_array) * np.linalg.norm(far_array)
            )
            if denominator <= 1e-6:
                continue
            correlation = float(np.dot(near_array, far_array) / denominator)
            if best is None or correlation > best[0]:
                best = (correlation, delay, float(np.median(ratios)))
        if best is None or best[0] < self.min_correlation:
            self.delay_misses += 1
            # 真人插话、TTS 停顿和系统增益处理都会暂时破坏相关性。
            # 已经锁定后保留最后可信延迟；清空会触发全范围重搜，容易把
            # 连续语音的相似能量包络误认成 1~2 秒的物理回声延迟。
            return
        self.delay_misses = 0
        correlation, delay, gain = best
        if self.estimated_delay is None:
            self.estimated_delay = delay
        else:
            # 延迟乱跳（0↔900ms）会搞坏 AEC；大跳变需要更高相关度才采纳
            jump = abs(delay - self.estimated_delay)
            if jump >= 8 and correlation < 0.55:
                delay = self.estimated_delay
            self.estimated_delay = int(round(
                self.estimated_delay * 0.82 + delay * 0.18
            ))
        self.echo_gain = min(20.0, max(0.05, gain))
        # 参考帧已对齐，AEC stream_delay 固定 0
        self.aec.set_delay_ms(0)
        if (
            self.last_logged_delay is None
            or abs(self.estimated_delay - self.last_logged_delay) >= 5
        ):
            self.last_logged_delay = self.estimated_delay
            self._log(
                "回声对齐：延迟≈%dms，相关度=%.2f，回声增益=%.2f，AEC=%s"
                % (
                    self.estimated_delay * self.frame_ms,
                    correlation,
                    self.echo_gain,
                    self.aec.backend,
                )
            )

    def _nearest_far_entry_locked(self, index):
        for offset in (0, -1, 1, -2, 2):
            feat = self.far_features.get(index + offset)
            pcm = self.far_pcm.get(index + offset)
            if feat is not None and pcm is not None:
                return feat[0], feat[1], pcm
        return None

    def _nearest_far_locked(self, index):
        hit = self._nearest_far_entry_locked(index)
        if hit is None:
            return None
        return hit[0], hit[1]

    def _trim_far_locked(self, current_index):
        minimum = current_index - self.max_delay_frames - 200
        stale = [index for index in self.far_features if index < minimum]
        for index in stale:
            self.far_features.pop(index, None)
            self.far_pcm.pop(index, None)

    def _log(self, message):
        if self.logger:
            self.logger("[echo_gate]", message)
