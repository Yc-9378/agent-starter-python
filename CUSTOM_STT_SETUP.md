# 自定义 WebSocket STT 插件配置指南

## 概述
已成功创建自定义的WebSocket STT插件，用于替换LiveKit Inference的STT服务。该插件基于火山ASR WebSocket协议实现。

## 文件结构
```
src/
├── agent.py              # 主代理文件，已集成CustomSTT
├── custom_stt.py         # 自定义STT插件实现
├── huoshan_asr_ws.py     # 火山ASR WebSocket客户端参考实现
└── config.py             # 配置管理
```

## 配置步骤

### 1. 环境变量配置
在 `.env.local` 文件中添加以下配置：

```bash
# 火山ASR认证信息
HUOSHAN_ACCESS_TOKEN=your_access_token_here
HUOSHAN_APPID=your_app_id_here

# OpenAI API配置（用于LLM）
OPENAI_API_KEY=your_openai_api_key_here

# LiveKit配置
LIVEKIT_URL=your_livekit_url
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
```

### 2. STT配置参数
在 `agent.py` 的 `my_agent` 函数中，CustomSTT的配置如下：

```python
stt=CustomSTT(
    ws_url="wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    model="bigmodel",
    sample_rate=16000,
    num_channels=1,
    language="zh-CN",
    api_key=os.getenv("HUOSHAN_ACCESS_TOKEN"),
    app_id=os.getenv("HUOSHAN_APPID"),
    format="pcm",
    bits=16,
    codec="raw",
    seg_duration=100,  # 100ms音频分段
    streaming=True,
)
```

### 3. 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `ws_url` | WebSocket服务器URL | 火山ASR服务地址 |
| `model` | ASR模型名称 | "bigmodel" |
| `sample_rate` | 音频采样率 | 16000 Hz |
| `num_channels` | 音频声道数 | 1 (单声道) |
| `language` | 识别语言 | "zh-CN" |
| `api_key` | API访问密钥 | 从环境变量读取 |
| `app_id` | 应用ID | 从环境变量读取 |
| `format` | 音频格式 | "pcm" |
| `bits` | 音频位深度 | 16 |
| `codec` | 编码格式 | "raw" |
| `seg_duration` | 音频分段时长(ms) | 100 |
| `streaming` | 是否流式识别 | True |

## 协议实现

### 火山ASR WebSocket协议
自定义STT插件实现了火山ASR的二进制协议，包括：

1. **协议头部生成** - 4字节头部包含版本、消息类型、序列化方法等
2. **序列号管理** - 每个消息包含递增序列号
3. **数据压缩** - 使用GZIP压缩JSON数据
4. **音频分段** - 按固定时长分割音频数据

### 消息类型
- `FULL_CLIENT_REQUEST` (0x01): 完整客户端请求（初始化）
- `AUDIO_ONLY_REQUEST` (0x02): 纯音频数据请求
- `FULL_SERVER_RESPONSE` (0x09): 完整服务器响应
- `SERVER_ACK` (0x0B): 服务器确认
- `SERVER_ERROR_RESPONSE` (0x0F): 服务器错误响应

## 使用方式

### 运行代理
```bash
# 使用uv运行
uv run python src/agent.py

# 或直接运行
python src/agent.py
```

### 测试连接
可以创建一个简单的测试脚本来验证STT连接：

```python
import asyncio
import os
from custom_stt import CustomSTT

async def test_stt():
    stt = CustomSTT(
        ws_url="wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
        api_key=os.getenv("HUOSHAN_ACCESS_TOKEN"),
        app_id=os.getenv("HUOSHAN_APPID"),
    )
    print(f"STT initialized: {stt.name}")
    
    # 创建STT流
    stream = stt.stream()
    print(f"STT stream created")

asyncio.run(test_stt())
```

## 故障排除

### 1. 连接失败
- 检查 `HUOSHAN_ACCESS_TOKEN` 和 `HUOSHAN_APPID` 环境变量
- 验证网络连接和防火墙设置
- 确认WebSocket URL正确

### 2. 认证错误
- 确保API密钥和应用ID有效
- 检查火山ASR服务配额和权限

### 3. 音频处理问题
- 确认音频参数（采样率、声道数）与服务端要求一致
- 检查音频格式是否支持

### 4. 协议错误
- 参考 `huoshan_asr_ws.py` 中的完整协议实现
- 检查序列号和消息类型设置

## 扩展和自定义

### 支持其他ASR服务
要支持其他WebSocket ASR服务，需要：

1. 修改 `CustomSTT` 类中的协议实现
2. 调整消息格式和数据序列化方式
3. 更新响应解析逻辑

### 性能优化
- 调整 `seg_duration` 参数平衡延迟和准确性
- 实现连接池管理多个WebSocket连接
- 添加重试机制和错误恢复

## 注意事项

1. **实时性要求**: 语音识别对延迟敏感，确保网络稳定
2. **资源管理**: WebSocket连接需要及时关闭，避免资源泄漏
3. **错误处理**: 实现完善的错误处理和重试逻辑
4. **日志记录**: 关键操作添加日志，便于调试和监控

## 相关文档
- [火山ASR官方文档](https://www.volcengine.com/docs/6561/1252507)
- [LiveKit Agents STT接口](https://docs.livekit.io/agents/build/stt/)
- [WebSocket协议规范](https://tools.ietf.org/html/rfc6455)