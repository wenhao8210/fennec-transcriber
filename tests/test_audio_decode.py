"""Decode real, generated audio without models, recordings, or network calls."""
import io
import unittest
import wave

import numpy as np
from faster_whisper.audio import decode_audio


def generated_wav(sample_rate: int, channels: int) -> io.BytesIO:
    """One second of PCM audio, generated in memory rather than bundled media."""
    times = np.arange(sample_rate, dtype=np.float64) / sample_rate
    samples = (0.25 * 32767 * np.sin(2 * np.pi * 440 * times)).astype("<i2")
    if channels > 1:
        samples = np.repeat(samples[:, None], channels, axis=1)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())
    audio.seek(0)
    return audio


class AudioDecodeTests(unittest.TestCase):
    def assert_valid_audio(self, audio: np.ndarray) -> None:
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)
        self.assertEqual(len(audio), 16000)
        self.assertTrue(np.isfinite(audio).all())
        self.assertGreater(float(np.max(np.abs(audio))), 0.1)
        self.assertLessEqual(float(np.max(np.abs(audio))), 1.0)

    def test_decode_generated_mono_wav(self):
        # Exercises the av.open call that breaks with faster-whisper 1.2.1 + av 19.
        with generated_wav(16000, 1) as source:
            self.assert_valid_audio(decode_audio(source, sampling_rate=16000))

    def test_decode_and_resample_generated_stereo_wav(self):
        with generated_wav(44100, 2) as source:
            self.assert_valid_audio(decode_audio(source, sampling_rate=16000))


if __name__ == "__main__":
    unittest.main()
