"""
生产环境Agent服务器 - 支持多用户并发
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    llm,
    room_io,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.plugins import noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from agent import Assistant
from config import config
from custom_llm import CustomLLM
from custom_tts import SparkTTSPlugin

# 加载生产环境配置
load_dotenv(".env.production")

# 配置日志
logging.basicConfig(
    level=getattr(
        logging,
        config.server.log_level if hasattr(config.server, "log_level") else "INFO",
    ),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("production_agent")


# 性能监控指标
class PerformanceMetrics:
    """性能指标收集"""

    def __init__(self):
        self.active_sessions = 0
        self.total_sessions = 0
        self.failed_sessions = 0
        self.total_processing_time = 0.0
        self.session_times = []

        # API调用统计
        self.llm_calls = 0
        self.tts_calls = 0
        self.api_errors = 0

    def session_started(self):
        """会话开始"""
        self.active_sessions += 1
        self.total_sessions += 1

    def session_completed(self, duration: float):
        """会话完成"""
        self.active_sessions -= 1
        self.total_processing_time += duration
        self.session_times.append(duration)
        # 保留最近100个会话的时间
        if len(self.session_times) > 100:
            self.session_times.pop(0)

    def session_failed(self):
        """会话失败"""
        self.failed_sessions += 1

    def llm_called(self):
        """LLM调用"""
        self.llm_calls += 1

    def tts_called(self):
        """TTS调用"""
        self.tts_calls += 1

    def api_error(self):
        """API错误"""
        self.api_errors += 1

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        avg_time = self.total_processing_time / max(len(self.session_times), 1)
        return {
            "active_sessions": self.active_sessions,
            "total_sessions": self.total_sessions,
            "failed_sessions": self.failed_sessions,
            "avg_session_time": avg_time,
            "llm_calls": self.llm_calls,
            "tts_calls": self.tts_calls,
            "api_errors": self.api_errors,
            "success_rate": (self.total_sessions - self.failed_sessions)
            / max(self.total_sessions, 1),
        }


# 全局性能指标
metrics = PerformanceMetrics()


class ProductionSparkTTSPlugin(SparkTTSPlugin):
    """生产环境TTS插件，带性能监控"""

    def __init__(self, **kwargs):
        # 使用配置中的TTS参数
        tts_config = config.tts
        super().__init__(
            api_url=tts_config.api_url,
            voice=tts_config.voice,
            sample_rate=tts_config.sample_rate,
            audio_format=tts_config.audio_format,
            **kwargs,
        )

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        """合成语音，记录性能指标"""
        metrics.tts_called()
        return super().synthesize(text, conn_options=conn_options)


class ProductionCustomLLM(CustomLLM):
    """生产环境LLM，带性能监控"""

    def __init__(self, **kwargs):
        # 使用配置中的LLM参数
        llm_config = config.llm
        super().__init__(
            model=llm_config.model,
            base_url=llm_config.base_url,
            api_key=os.getenv("OPENAI_API_KEY"),
            **kwargs,
        )

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools=None,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
        **kwargs,
    ) -> llm.LLMStream:
        """聊天，记录性能指标"""
        metrics.llm_called()
        return super().chat(
            chat_ctx=chat_ctx, tools=tools, conn_options=conn_options, **kwargs
        )


class ResourceLimiter:
    """资源限制器，防止资源耗尽"""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.waiting_tasks = 0

    @asynccontextmanager
    async def acquire(self, task_name: str = ""):
        """获取资源许可"""
        self.waiting_tasks += 1
        try:
            start_wait = time.time()
            async with self.semaphore:
                wait_time = time.time() - start_wait
                if wait_time > 1.0:  # 等待超过1秒
                    logger.warning(
                        f"资源等待时间较长: {task_name}, 等待: {wait_time:.2f}秒"
                    )
                yield
        finally:
            self.waiting_tasks -= 1

    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            "max_concurrent": self.max_concurrent,
            "available": self.semaphore._value,
            "waiting": self.waiting_tasks,
            "in_use": self.max_concurrent - self.semaphore._value,
        }


# 创建资源限制器
session_limiter = ResourceLimiter(config.concurrency.max_concurrent_users)


async def monitor_performance():
    """性能监控任务"""
    while True:
        try:
            stats = metrics.get_stats()
            limiter_stats = session_limiter.get_status()

            logger.info(
                f"性能监控 - 活跃会话: {stats['active_sessions']}, "
                f"成功率: {stats['success_rate']:.1%}, "
                f"平均时间: {stats['avg_session_time']:.2f}s, "
                f"资源使用: {limiter_stats['in_use']}/{limiter_stats['max_concurrent']}"
            )

            # 每30秒记录一次详细指标
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"性能监控错误: {e}")
            await asyncio.sleep(60)


# 创建Agent服务器
server = AgentServer()


def prewarm(proc: JobProcess):
    """预热函数 - 加载共享资源"""
    logger.info(f"Worker进程预热，PID: {os.getpid()}")

    # 加载VAD模型
    proc.userdata["vad"] = silero.VAD.load()

    # 记录worker信息
    proc.userdata["worker_id"] = f"worker_{os.getpid()}"
    proc.userdata["start_time"] = time.time()


server.setup_fnc = prewarm


@server.rtc_session(agent_name="s2s")
async def production_agent(ctx: JobContext):
    """生产环境Agent会话处理"""
    session_start_time = time.time()
    session_id = f"{ctx.room.name}_{int(time.time())}"

    try:
        # 检查并发限制
        async with session_limiter.acquire(f"session_{session_id}"):
            metrics.session_started()

            # 日志设置
            ctx.log_context_fields = {
                "room": ctx.room.name,
                "participant": ctx.participant.identity,
                "session_id": session_id,
                "worker_id": ctx.proc.userdata.get("worker_id", "unknown"),
            }

            logger.info(f"开始处理会话: {session_id}, 用户: {ctx.participant.identity}")

            # 创建TTS实例
            tts = ProductionSparkTTSPlugin()

            # 创建会话配置
            session = AgentSession(
                # STT配置（如果需要可以启用）
                # stt=inference.STT(model="deepgram/nova-3", language="multi"),
                # LLM配置
                llm=ProductionCustomLLM(
                    api_key=os.getenv("OPENAI_API_KEY"),
                ),
                # TTS配置
                tts=tts,
                # 语音活动检测和转轮检测
                turn_detection=MultilingualModel(),
                vad=ctx.proc.userdata["vad"],
                # 允许预生成响应
                preemptive_generation=True,
            )

            # 启动会话
            await session.start(
                agent=Assistant(),
                room=ctx.room,
                room_options=room_io.RoomOptions(
                    audio_input=room_io.AudioInputOptions(
                        noise_cancellation=lambda params: (
                            noise_cancellation.BVCTelephony()
                            if params.participant.kind
                            == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                            else noise_cancellation.BVC()
                        ),
                    ),
                ),
            )

            # 连接用户
            await ctx.connect()

            # 记录会话成功
            session_duration = time.time() - session_start_time
            metrics.session_completed(session_duration)
            logger.info(f"会话完成: {session_id}, 时长: {session_duration:.2f}秒")

    except asyncio.CancelledError:
        # 会话被取消
        session_duration = time.time() - session_start_time
        metrics.session_completed(session_duration)
        logger.warning(f"会话被取消: {session_id}, 时长: {session_duration:.2f}秒")
        raise

    except Exception as e:
        # 会话失败
        session_duration = time.time() - session_start_time
        metrics.session_failed()
        metrics.api_error()
        logger.error(
            f"会话失败: {session_id}, 时长: {session_duration:.2f}秒, 错误: {e}"
        )
        raise


def start_performance_monitor():
    """启动性能监控"""
    loop = asyncio.get_event_loop()
    loop.create_task(monitor_performance())
    logger.info("性能监控已启动")


if __name__ == "__main__":
    # 启动性能监控
    start_performance_monitor()

    # 记录启动信息
    logger.info("生产Agent服务器启动")
    logger.info(f"配置: {config}")
    logger.info(f"最大并发用户: {config.concurrency.max_concurrent_users}")
    logger.info(f"Worker进程数: {config.server.worker_processes}")

    # 运行服务器
    cli.run_app(server)
