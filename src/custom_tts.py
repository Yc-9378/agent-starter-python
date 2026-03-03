import logging
import time

import httpx
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

logger = logging.getLogger("agent")


def get_default_tts_config():
    """获取默认TTS配置，如果config模块存在则使用它"""
    try:
        from config import config

        return config.tts
    except ImportError:
        # 如果没有config模块，使用硬编码默认值
        class DefaultTTSConfig:
            api_url = "http://172.16.0.4:31184/spark-tts/streaming"
            voice = "woman_girl"
            sample_rate = 24000
            audio_format = "wav"
            timeout = 30
            max_retries = 2

        return DefaultTTSConfig()


class SparkTTSPlugin(tts.TTS):
    def __init__(
        self,
        api_url: str | None = None,
        voice: str | None = None,
        sample_rate: int | None = None,
        audio_format: str | None = None,
    ):
        # 获取默认配置
        default_config = get_default_tts_config()

        # 使用参数或默认值
        api_url = api_url or default_config.api_url
        voice = voice or default_config.voice
        sample_rate = sample_rate or default_config.sample_rate
        audio_format = audio_format or default_config.audio_format

        # Initialize parent with required parameters
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,  # Mono audio
        )

        self.api_url = api_url
        self.voice = voice
        self.audio_format = audio_format

    def synthesize(
        self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        """
        Synthesize text to speech using Spark-TTS API.

        Args:
            text: The text to synthesize
            conn_options: Connection options

        Returns:
            ChunkedStream: Stream of synthesized audio chunks
        """
        return SparkTTSChunkedStream(
            tts=self, input_text=text, conn_options=conn_options
        )

    @property
    def name(self):
        return f"SparkTTS-{self.voice}"

    @property
    def model(self):
        return "spark-tts"

    @property
    def provider(self):
        return "spark-tts"


class SparkTTSChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: SparkTTSPlugin, input_text: str, conn_options):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: SparkTTSPlugin = tts
        self._audio_generated = False

    async def _run(self, output_emitter: tts.AudioEmitter):
        """
        Generate audio using Spark-TTS API and emit it through the output emitter.
        """
        if self._audio_generated:
            return

        start_time = time.time()
        api_request_time = None
        try:
            # Prepare the request payload
            payload = {
                "text": self.input_text.strip(),
                "stream": False,
                "format": self._tts.audio_format,
                "speaker": self._tts.voice,
                "sample_rate": self._tts.sample_rate,
            }

            headers = {
                "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }

            # Make the API request
            api_request_start = time.time()
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._tts.api_url, json=payload, headers=headers, timeout=30.0
                )
                api_request_time = time.time() - api_request_start

                if response.status_code != 200:
                    raise RuntimeError(
                        f"Spark-TTS API request failed with status {response.status_code}: {response.text}"
                    )

                # Get the audio data
                audio_bytes = response.content

                # Initialize the output emitter
                output_emitter.initialize(
                    request_id=str(id(self)),
                    sample_rate=self._tts.sample_rate,
                    num_channels=1,  # Mono
                    mime_type="audio/mp3"
                    if self._tts.audio_format == "mp3"
                    else "audio/wav",
                )

                # Push the audio data through the emitter
                output_emitter.push(audio_bytes)

                # Flush the emitter to indicate completion
                output_emitter.flush()

                self._audio_generated = True

                # 记录TTS处理成功耗时
                end_time = time.time()
                total_time = end_time - start_time
                audio_data_size = len(audio_bytes)
                logger.info(
                    f"SparkTTS: 音频合成成功, "
                    f"总耗时: {total_time:.3f}秒, "
                    f"API请求耗时: {api_request_time:.3f}秒, "
                    f"音频数据大小: {audio_data_size}字节, "
                    f"文本长度: {len(self.input_text.strip())}字符"
                )

        except httpx.RequestError as e:
            end_time = time.time()
            total_time = end_time - start_time
            logger.error(
                f"SparkTTS: API连接失败, "
                f"耗时: {total_time:.3f}秒, "
                f"API请求耗时: {api_request_time if api_request_time else 0:.3f}秒, "
                f"错误: {e}"
            )
            raise RuntimeError(f"Spark-TTS API connection failed: {e!s}") from e
        except Exception as e:
            end_time = time.time()
            total_time = end_time - start_time
            logger.error(
                f"SparkTTS: 音频合成失败, "
                f"耗时: {total_time:.3f}秒, "
                f"API请求耗时: {api_request_time if api_request_time else 0:.3f}秒, "
                f"错误: {e}"
            )
            raise RuntimeError(f"Spark-TTS synthesis failed: {e!s}") from e
        finally:
            # Ensure the emitter is properly closed
            try:
                output_emitter.end_input()
            except Exception:
                pass
