"""
生产环境配置管理模块
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ServerConfig:
    """服务器配置"""

    host: str = "0.0.0.0"
    port: int = 7880
    worker_processes: int = 4
    max_connections_per_worker: int = 50
    log_level: str = "INFO"


@dataclass
class ConcurrencyConfig:
    """并发配置"""

    max_concurrent_users: int = 200
    api_rate_limit_per_user: int = 5


@dataclass
class LLMConfig:
    """LLM配置"""

    model: str = "openai/gpt-4"
    base_url: str = "https://devapi-p.tp-ex.com/v1"
    timeout: int = 60
    max_retries: int = 3


@dataclass
class TTSConfig:
    """TTS配置"""

    api_url: str = "http://172.16.0.4:31184/spark-tts/streaming"
    voice: str = "woman_girl"
    sample_rate: int = 24000
    audio_format: str = "wav"
    timeout: int = 30
    max_retries: int = 2


@dataclass
class STTConfig:
    """STT配置"""

    ws_url: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
    model: str = "bigmodel"
    sample_rate: int = 16000
    num_channels: int = 1
    language: str = "zh-CN"
    format: str = "pcm"
    bits: int = 16
    codec: str = "raw"
    seg_duration: int = 100
    streaming: bool = True
    timeout: int = 30
    max_retries: int = 3


@dataclass
class ProductionConfig:
    """生产环境配置"""

    server: ServerConfig = field(default_factory=ServerConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)

    def __post_init__(self):
        """从环境变量加载配置"""
        self._load_from_env()

    def _load_from_env(self):
        """从环境变量加载配置"""
        # 服务器配置
        if os.getenv("SERVER_HOST"):
            self.server.host = os.getenv("SERVER_HOST")
        if os.getenv("SERVER_PORT"):
            self.server.port = int(os.getenv("SERVER_PORT"))
        if os.getenv("WORKER_PROCESSES"):
            self.server.worker_processes = int(os.getenv("WORKER_PROCESSES"))
        if os.getenv("MAX_CONNECTIONS"):
            self.concurrency.max_concurrent_users = int(os.getenv("MAX_CONNECTIONS"))

        # LLM配置
        if os.getenv("OPENAI_BASE_URL"):
            self.llm.base_url = os.getenv("OPENAI_BASE_URL")
        if os.getenv("OPENAI_MODEL"):
            self.llm.model = os.getenv("OPENAI_MODEL")
        if os.getenv("OPENAI_TIMEOUT"):
            self.llm.timeout = int(os.getenv("OPENAI_TIMEOUT"))

        # TTS配置
        if os.getenv("SPARK_TTS_API_URL"):
            self.tts.api_url = os.getenv("SPARK_TTS_API_URL")
        if os.getenv("SPARK_TTS_VOICE"):
            self.tts.voice = os.getenv("SPARK_TTS_VOICE")
        if os.getenv("SPARK_TTS_SAMPLE_RATE"):
            self.tts.sample_rate = int(os.getenv("SPARK_TTS_SAMPLE_RATE"))
        if os.getenv("SPARK_TTS_FORMAT"):
            self.tts.audio_format = os.getenv("SPARK_TTS_FORMAT")

        # STT配置
        if os.getenv("HUOSHAN_WS_URL"):
            self.stt.ws_url = os.getenv("HUOSHAN_WS_URL")
        if os.getenv("HUOSHAN_MODEL"):
            self.stt.model = os.getenv("HUOSHAN_MODEL")
        if os.getenv("HUOSHAN_SAMPLE_RATE"):
            self.stt.sample_rate = int(os.getenv("HUOSHAN_SAMPLE_RATE"))
        if os.getenv("HUOSHAN_LANGUAGE"):
            self.stt.language = os.getenv("HUOSHAN_LANGUAGE")
        if os.getenv("HUOSHAN_FORMAT"):
            self.stt.format = os.getenv("HUOSHAN_FORMAT")
        if os.getenv("HUOSHAN_BITS"):
            self.stt.bits = int(os.getenv("HUOSHAN_BITS"))
        if os.getenv("HUOSHAN_CODEC"):
            self.stt.codec = os.getenv("HUOSHAN_CODEC")
        if os.getenv("HUOSHAN_SEG_DURATION"):
            self.stt.seg_duration = int(os.getenv("HUOSHAN_SEG_DURATION"))


# 全局配置实例
_config: Optional[ProductionConfig] = None


def get_config() -> ProductionConfig:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = ProductionConfig()
        # 记录配置加载
        import logging

        logger = logging.getLogger(__name__)
        logger.info("生产配置加载完成")

    return _config


# 导出配置实例
config = get_config()
