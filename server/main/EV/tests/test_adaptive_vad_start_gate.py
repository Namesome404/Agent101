from devices.voice.terminal_vad import AdaptiveVadStartGate


def test_low_energy_bluetooth_noise_does_not_start_speech():
    gate = AdaptiveVadStartGate(minimum_rms=60, noise_multiplier=2.5)
    for rms in [0, 4, 12, 27, 51, 18] * 4:
        assert gate.accept(rms, vad_speech=True) is False


def test_real_speech_clears_adaptive_noise_floor():
    gate = AdaptiveVadStartGate(minimum_rms=60, noise_multiplier=2.5)
    for rms in [80, 90, 100, 110] * 10:
        gate.accept(rms, vad_speech=False)
    assert gate.noise_rms == 95
    assert gate.threshold == 238
    assert gate.accept(900, vad_speech=True) is True


def test_non_vad_energy_never_starts_speech():
    gate = AdaptiveVadStartGate(minimum_rms=60, noise_multiplier=2.5)
    assert gate.accept(4000, vad_speech=False) is False
