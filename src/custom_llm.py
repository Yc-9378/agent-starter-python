import json
import logging
import os
import time
from typing import Any, cast

import httpx
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

logger = logging.getLogger("agent")


def get_default_llm_config():
    """获取默认LLM配置，如果config模块存在则使用它"""
    try:
        from config import config

        return config.llm
    except ImportError:
        # 如果没有config模块，使用硬编码默认值
        class DefaultLLMConfig:
            model = "openai/gpt-4"
            base_url = "https://devapi-p.tp-ex.com/v1"
            timeout = 60
            max_retries = 3

        return DefaultLLMConfig()


class CustomLLMStream(llm.LLMStream):
    def __init__(
        self,
        llm_instance: "CustomLLM",
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
    ):
        super().__init__(
            llm_instance,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )
        self._llm = llm_instance
        self._chat_ctx = chat_ctx
        self._tools = tools

    async def _run(self) -> None:
        start_time = time.time()
        try:
            messages = []
            for msg in self._chat_ctx.messages():
                # Get text content from message
                content = ""
                if (
                    msg.content
                    and isinstance(msg.content, list)
                    and len(msg.content) > 0
                ):
                    content = msg.content[0] or ""
                elif isinstance(msg.content, str):
                    content = msg.content

                if content:
                    messages.append({"role": msg.role, "content": content})

            logger.info(
                f"CustomLLM: Sending request to {self._llm._base_url} with {len(messages)} messages"
            )

            payload = {"model": self._llm._model, "messages": messages, "stream": True}

            if self._tools:
                # 使用 ToolContext 来解析工具 schema
                tool_ctx = llm.ToolContext(self._tools)
                tool_schemas = cast(
                    list, tool_ctx.parse_function_tools("openai", strict=False)
                )
                if tool_schemas:
                    payload["tools"] = tool_schemas

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._llm._api_key}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }

            async with (
                httpx.AsyncClient() as client,
                client.stream(
                    "POST",
                    f"{self._llm._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=httpx.Timeout(
                        connect=30.0, read=60.0, write=60.0, pool=30.0
                    ),
                ) as response,
            ):
                if response.status_code != 200:
                    # 先读取响应内容, 再访问.text属性
                    error_text = await response.aread()
                    error_message = (
                        error_text.decode("utf-8") if error_text else "No error details"
                    )
                    logger.error(
                        f"API request failed with status {response.status_code}: {error_message}"
                    )
                    raise Exception(
                        f"API request failed with status {response.status_code}: {error_message}"
                    )

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break

                        try:
                            chunk_data = json.loads(data)
                            chunk_id = chunk_data.get("id", "unknown")

                            # 处理使用情况数据
                            if chunk_data.get("usage"):
                                usage_data = chunk_data["usage"]
                                usage_chunk = llm.ChatChunk(
                                    id=chunk_id,
                                    usage=llm.CompletionUsage(
                                        completion_tokens=usage_data.get(
                                            "completion_tokens", 0
                                        ),
                                        prompt_tokens=usage_data.get(
                                            "prompt_tokens", 0
                                        ),
                                        prompt_cached_tokens=usage_data.get(
                                            "prompt_cached_tokens", 0
                                        ),
                                        total_tokens=usage_data.get("total_tokens", 0),
                                    ),
                                )
                                self._event_ch.send_nowait(usage_chunk)

                            # 处理选择数据
                            if chunk_data.get("choices"):
                                choice = chunk_data["choices"][0]
                                if "delta" in choice:
                                    delta = choice["delta"]

                                    # 处理工具调用
                                    tool_calls = []
                                    if delta.get("tool_calls"):
                                        for tool_call in delta["tool_calls"]:
                                            if tool_call.get("function"):
                                                func = tool_call["function"]
                                                tool_calls.append(
                                                    llm.FunctionToolCall(
                                                        name=func.get("name", ""),
                                                        arguments=func.get(
                                                            "arguments", ""
                                                        ),
                                                        call_id=tool_call.get("id", ""),
                                                    )
                                                )

                                    # 创建聊天块
                                    if delta.get("content") or tool_calls:
                                        chat_chunk = llm.ChatChunk(
                                            id=chunk_id,
                                            delta=llm.ChoiceDelta(
                                                content=delta.get("content"),
                                                tool_calls=tool_calls
                                                if tool_calls
                                                else [],
                                            ),
                                        )
                                        self._event_ch.send_nowait(chat_chunk)
                        except json.JSONDecodeError:
                            continue

            # 记录LLM处理耗时
            end_time = time.time()
            total_time = end_time - start_time
            logger.info(f"CustomLLM: 处理完成, 总耗时: {total_time:.3f}秒")

        except Exception as e:
            end_time = time.time()
            total_time = end_time - start_time
            logger.error(f"CustomLLM: 处理失败, 耗时: {total_time:.3f}秒, 错误: {e}")
            raise


class CustomLLM(llm.LLM):
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        **kwargs,
    ):
        super().__init__()

        # 获取默认配置
        default_config = get_default_llm_config()

        # 使用参数或默认值
        self._model = model or default_config.model
        self._base_url = base_url or default_config.base_url
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not self._api_key:
            raise ValueError(
                "API key is required, either as argument or set OPENAI_API_KEY environment variable"
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "custom"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> llm.LLMStream:
        return CustomLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
