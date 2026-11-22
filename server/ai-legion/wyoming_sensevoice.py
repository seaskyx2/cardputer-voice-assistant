import re
import asyncio
import time
import logging
import numpy as np
import sherpa_onnx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
_LOGGER = logging.getLogger("wyoming_sensevoice")

MODEL_PATH = "/opt/stacks/ai-legion/models/sensevoice/model.int8.onnx"
TOKENS_PATH = "/opt/stacks/ai-legion/models/sensevoice/tokens.txt"
URI = "tcp://0.0.0.0:10300"

_LOGGER.info("Loading SenseVoiceSmall INT8 model via sherpa-onnx...")
recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=MODEL_PATH,
    tokens=TOKENS_PATH,
    num_threads=4,
    use_itn=True,
    language="zh",
    debug=False,
)
_LOGGER.info("SenseVoiceSmall INT8 loaded successfully!")

asr_lock = asyncio.Lock()

WYOMING_INFO = Info(
    asr=[
        AsrProgram(
            name="sensevoice",
            description="SenseVoiceSmall INT8 via sherpa-onnx (Ultra-Fast CPU ASR)",
            attribution=Attribution(name="FunAudioLLM / sherpa-onnx", url="https://github.com/k2-fsa/sherpa-onnx"),
            installed=True,
            version="2.0.2",
            models=[
                AsrModel(
                    name="sensevoice-small-int8",
                    description="SenseVoiceSmall INT8 (zh/en/yue)",
                    attribution=Attribution(name="FunAudioLLM", url="https://github.com/FunAudioLLM/SenseVoice"),
                    installed=True,
                    languages=["zh", "zh-CN", "en", "yue"],
                    version="2024-07-17",
                )
            ],
        )
    ]
)

def decode_pcm_sync(audio_bytes: bytes, sample_rate: int = 16000) -> str:
    if not audio_bytes or len(audio_bytes) < 3200:  # < 0.1s 极短噪声直接忽略
        return ""
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    # 物理声学 RMS 门限 (-50 dBFS)：纯静音或微弱底噪直接拦截，根除神经网络静音幻觉（如吐“没。”/“嗯。”）
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms < 0.003:
        return ""
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    raw_text = stream.result.text.strip()
    # 剥离标点与空白后若无任何有效汉字/字母/数字（如底噪吐出的单个“。”），过滤为空串
    meaningful = re.sub(r"[^\w\u4e00-\u9fff]+", "", raw_text)
    if not meaningful:
        return ""
    return raw_text

class SenseVoiceHandler(AsyncEventHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.audio_buffer = bytearray()
        self.sample_rate = 16000

    async def handle_event(self, event: Event) -> bool:
        try:
            if Describe.is_type(event.type):
                await self.write_event(WYOMING_INFO.event())
                return True

            if Transcribe.is_type(event.type):
                self.audio_buffer.clear()
                return True

            if AudioStart.is_type(event.type):
                start = AudioStart.from_event(event)
                self.sample_rate = start.rate or 16000
                self.audio_buffer.clear()
                return True

            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                self.audio_buffer.extend(chunk.audio)
                return True

            if AudioStop.is_type(event.type):
                t0 = time.time()
                raw = bytes(self.audio_buffer)
                self.audio_buffer.clear()
                async with asr_lock:
                    text = await asyncio.to_thread(decode_pcm_sync, raw, self.sample_rate)
                cost_ms = (time.time() - t0) * 1000
                _LOGGER.info(f"Transcribed ({len(raw)} bytes, {cost_ms:.1f}ms): {text!r}")
                await self.write_event(Transcript(text=text).event())
                return False
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            _LOGGER.info("Client cancelled/disconnected during transcription, closing stream cleanly.")
            return False

        return True

async def main():
    _LOGGER.info(f"Starting Wyoming SenseVoiceSmall Server at {URI}")
    server = AsyncServer.from_uri(URI)
    await server.run(SenseVoiceHandler)

if __name__ == "__main__":
    asyncio.run(main())
