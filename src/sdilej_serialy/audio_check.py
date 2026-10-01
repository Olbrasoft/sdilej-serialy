"""Fail early if the installed Whisper audio decoder is incompatible."""
import io
import wave


def check_audio_decoder():
    from faster_whisper.audio import decode_audio
    sample = io.BytesIO()
    with wave.open(sample, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\0' * 32000)
    sample.seek(0)
    decoded = decode_audio(sample)
    if len(decoded) != 16000:
        raise RuntimeError('Whisper audio decoder failed its startup check')


if __name__ == '__main__':
    check_audio_decoder()
    print('Whisper audio decoder startup check passed', flush=True)
