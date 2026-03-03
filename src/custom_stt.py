"""简化版的自定义STT插件，用于替换LiveKit Inference STT"""

import asyncio
import gzip
import json
import logging
import uuid
from typing import Any

import websockets
from livekit.agents import stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

logger = logging.getLogger("agent")


def get_default_stt_config():
    """获取默认STT配置，如果config模块存在则使用它"""
    try:
        from config import config

        return config.stt
    except ImportError:
        # 如果没有config模块，使用硬编码默认值
        class DefaultSTTConfig:
            ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
            model = "bigmodel"
            sample_rate = 16000
            num_channels = 1
            language = "zh-CN"
            format = "pcm"
            bits = 16
            codec = "raw"
            seg_duration = 100
            streaming = True
            timeout = 30
            max_retries = 3

        return DefaultSTTConfig()


# 协议常量（简化版）
PROTOCOL_VERSION = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
POS_SEQUENCE = 0b0001
NEG_WITH_SEQUENCE = 0b0011
JSON_SERIALIZATION = 0b0001
GZIP_COMPRESSION = 0b0001


class CustomSTT(stt.STT):
    """自定义WebSocket STT插件，用于连接火山ASR服务"""

    def __init__(
        self,
        ws_url: str | None = None,
        model: str | None = None,
        sample_rate: int | None = None,
        num_channels: int | None = None,
        language: str | None = None,
        api_key: str | None = None,
        app_id: str | None = None,
        **kwargs,
    ):
        # 获取默认配置
        default_config = get_default_stt_config()

        # 使用参数或默认值
        ws_url = ws_url or default_config.ws_url
        model = model or default_config.model
        sample_rate = sample_rate or default_config.sample_rate
        num_channels = num_channels or default_config.num_channels
        language = language or default_config.language

        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
            ),
            sample_rate=sample_rate,
            num_channels=num_channels,
            language=language,
        )

        self.ws_url = ws_url
        self.model = model
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.language = language
        self.api_key = api_key
        self.app_id = app_id

        # 其他配置，从kwargs或默认配置获取
        self.format = kwargs.get("format", default_config.format)
        self.bits = kwargs.get("bits", default_config.bits)
        self.codec = kwargs.get("codec", default_config.codec)
        self.seg_duration = kwargs.get("seg_duration", default_config.seg_duration)
        self.streaming = kwargs.get("streaming", default_config.streaming)

        # WebSocket连接状态
        self._ws = None
        self._seq = 1
        self._reqid = str(uuid.uuid4())
        self._lock = asyncio.Lock()

        # 请求头
        self.headers = {}
        if self.api_key:
            self.headers["X-Api-Access-Key"] = self.api_key
        if self.app_id:
            self.headers["X-Api-App-Key"] = self.app_id
        self.headers["X-Api-Request-Id"] = self._reqid

        logger.info(f"CustomSTT initialized for {ws_url}")

    @property
    def name(self) -> str:
        return f"CustomSTT-{self.model}"

    @property
    def provider(self) -> str:
        return "custom"

    def stream(
        self,
        *,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        """创建STT识别流"""
        return CustomSTTStream(self)


class CustomSTTStream(stt.RecognizeStream):
    """自定义STT流实现"""

    def __init__(self, stt_instance: CustomSTT):
        super().__init__(stt=stt_instance)
        self._stt = stt_instance

    async def _run(self) -> None:
        """主运行循环 - 由LiveKit框架调用"""
        # 这个方法是框架调用的，我们不需要实现具体的音频处理逻辑
        # LiveKit会自动处理音频输入并调用相应的方法
        logger.debug("CustomSTTStream started")

    async def _connect_websocket(self) -> None:
        """连接到WebSocket服务器"""
        async with self._stt._lock:
            if self._stt._ws is not None:
                return

            try:
                self._stt._ws = await websockets.connect(
                    self._stt.ws_url,
                    additional_headers=self._stt.headers,
                    max_size=1000000000,
                )
                logger.info("Connected to WebSocket STT service")

                # 发送初始化请求
                init_request = self._build_init_request()
                await self._send_request(init_request, init=True)

            except Exception as e:
                logger.error(f"WebSocket connection failed: {e}")
                self._stt._ws = None
                raise

    def _build_init_request(self) -> dict:
        """构建初始化请求"""
        return {
            "user": {"uid": "livekit-agent"},
            "audio": {
                "format": self._stt.format,
                "sample_rate": self._stt.sample_rate,
                "bits": self._stt.bits,
                "channel": self._stt.num_channels,
                "codec": self._stt.codec,
            },
            "request": {
                "model_name": self._stt.model,
                "enable_punc": True,
                "result_type": "full",
                "enable_ddc": False,
                "end_window_size": 1000,
                "force_to_speech_time": 500,
            },
        }

    async def _send_request(self, request_data: dict, init: bool = False) -> None:
        """发送请求到WebSocket服务器"""
        if self._stt._ws is None:
            await self._connect_websocket()

        # 序列化并压缩请求
        payload = json.dumps(request_data).encode()
        payload = gzip.compress(payload)

        # 构建协议消息
        message_type = FULL_CLIENT_REQUEST if init else AUDIO_ONLY_REQUEST
        message_type_specific_flags = POS_SEQUENCE

        # 生成头部
        header = bytearray()
        header_size = 1
        header.append((PROTOCOL_VERSION << 4) | header_size)
        header.append((message_type << 4) | message_type_specific_flags)
        header.append((JSON_SERIALIZATION << 4) | GZIP_COMPRESSION)
        header.append(0x00)  # reserved

        # 添加序列号
        self._stt._seq += 1
        header.extend(self._stt._seq.to_bytes(4, "big", signed=True))

        # 添加payload大小和内容
        header.extend(len(payload).to_bytes(4, "big"))
        header.extend(payload)

        await self._stt._ws.send(bytes(header))

    async def process_audio(self, audio_data: bytes) -> str | None:
        """处理音频数据并返回识别结果（简化接口）"""
        try:
            # 连接到WebSocket
            await self._connect_websocket()

            # 发送音频数据
            audio_request = {
                "type": "audio",
                "data": audio_data.hex(),
                "last": True,
            }
            await self._send_request(audio_request)

            # 接收响应
            response = await self._stt._ws.recv()
            if isinstance(response, bytes):
                # 解析响应
                result = self._parse_response(response)
                if result:
                    return self._extract_text(result)

            return None

        except Exception as e:
            logger.error(f"Audio processing failed: {e}")
            return None

    def _parse_response(self, response: bytes) -> dict | None:
        """解析WebSocket响应"""
        try:
            # 简化解析逻辑
            # 在实际实现中需要完整解析火山ASR协议
            if response[1] >> 4 == FULL_SERVER_RESPONSE:
                # 跳过头部和序列号
                header_size = response[0] & 0x0F
                payload_start = header_size * 4 + 4
                payload_size = int.from_bytes(
                    response[payload_start : payload_start + 4], "big", signed=False
                )
                payload_msg = response[
                    payload_start + 4 : payload_start + 4 + payload_size
                ]

                if payload_msg:
                    payload_msg = gzip.decompress(payload_msg)
                    return json.loads(payload_msg.decode("utf-8"))

        except Exception as e:
            logger.error(f"Response parsing failed: {e}")

        return None

    def _extract_text(self, result: dict) -> str | None:
        """从识别结果中提取文本"""
        if isinstance(result, dict):
            for field in ["asr_text", "result", "text", "transcript"]:
                if field in result:
                    text = result[field]
                    if isinstance(text, str) and text.strip():
                        return text.strip()

        return None

    async def close(self):
        """关闭WebSocket连接"""
        if self._stt._ws:
            await self._stt._ws.close()
            self._stt._ws = None
            logger.debug("WebSocket connection closed")
