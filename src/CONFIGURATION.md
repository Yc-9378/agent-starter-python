# 统一配置系统说明

## 概述

本项目现在使用统一的配置系统，所有 LLM、STT、TTS 配置都通过 `src/config.py` 管理。这使得配置管理更加集中化、可维护。

## 配置文件结构

### `src/config.py`

主要配置类：
- `ServerConfig`: 服务器配置（主机、端口、日志级别等）
- `ConcurrencyConfig`: 并发配置（最大并发用户数等）
- `LLMConfig`: LLM 配置（模型、API地址、超时等）
- `TTSConfig`: TTS 配置（API地址、语音、采样率等）
- `STTConfig`: STT 配置（WebSocket地址、模型、采样率等）
- `ProductionConfig`: 所有配置的容器类

### 配置加载顺序
1. 从 `config.py` 中的默认配置
2. 从环境变量覆盖（如果设置了）
3. 从代码调用时的参数覆盖

## 如何使用统一配置

### 1. 直接使用配置实例

```python
from config import config

# 使用LLM配置
model = config.llm.model
base_url = config.llm.base_url

# 使用TTS配置
api_url = config.tts.api_url
voice = config.tts.voice

# 使用STT配置
ws_url = config.stt.ws_url
sample_rate = config.stt.sample_rate
```

### 2. 在自定义模块中使用

所有自定义模块（`custom_llm.py`、`custom_tts.py`、`custom_stt.py`）都会自动使用配置中的默认值：

```python
# 使用默认配置创建实例
from custom_llm import CustomLLM
from custom_tts import SparkTTSPlugin
from custom_stt import CustomSTT

llm = CustomLLM()  # 自动使用 config.llm 中的配置
tts = SparkTTSPlugin()  # 自动使用 config.tts 中的配置
stt = CustomSTT()  # 自动使用 config.stt 中的配置
```

### 3. 覆盖配置

如果需要覆盖配置，可以在创建实例时传递参数：

```python
# 覆盖特定配置
llm = CustomLLM(
    model="openai/gpt-3.5-turbo",
    base_url="https://api.openai.com/v1"
)

tts = SparkTTSPlugin(
    voice="man_old",
    sample_rate=48000
)

stt = CustomSTT(
    language="en-US",
    sample_rate=22050
)
```

## 环境变量支持

配置系统支持通过环境变量覆盖默认值：

### LLM 配置
- `OPENAI_BASE_URL`: LLM API基础URL
- `OPENAI_MODEL`: LLM模型名称
- `OPENAI_TIMEOUT`: LLM API超时时间

### TTS 配置
- `SPARK_TTS_API_URL`: TTS API地址
- `SPARK_TTS_VOICE`: TTS语音类型
- `SPARK_TTS_SAMPLE_RATE`: TTS采样率
- `SPARK_TTS_FORMAT`: TTS音频格式

### STT 配置
- `HUOSHAN_WS_URL`: STT WebSocket地址
- `HUOSHAN_MODEL`: STT模型名称
- `HUOSHAN_SAMPLE_RATE`: STT采样率
- `HUOSHAN_LANGUAGE`: STT语言
- `HUOSHAN_FORMAT`: STT音频格式
- `HUOSHAN_BITS`: STT音频位数
- `HUOSHAN_CODEC`: STT编码格式
- `HUOSHAN_SEG_DURATION`: STT分段时长

### 服务器配置
- `SERVER_HOST`: 服务器主机
- `SERVER_PORT`: 服务器端口
- `WORKER_PROCESSES`: Worker进程数
- `MAX_CONNECTIONS`: 最大连接数

## 各文件使用情况

### `src/agent.py`
- 导入 `from config import config`
- 所有配置都通过 `config.llm`、`config.tts`、`config.stt` 访问
- 保持与之前相同的功能，但使用统一配置

### `src/production_agent.py`
- 已经使用配置，但更新了导入路径
- `ProductionSparkTTSPlugin` 和 `ProductionCustomLLM` 类继承自基础类并自动使用配置
- 移除了硬编码的配置值

### `src/custom_llm.py`
- 新增 `get_default_llm_config()` 函数
- `CustomLLM` 类构造函数支持可选参数，默认从配置获取
- 如果没有 config 模块，使用硬编码默认值

### `src/custom_tts.py`
- 新增 `get_default_tts_config()` 函数
- `SparkTTSPlugin` 类构造函数支持可选参数，默认从配置获取
- 如果没有 config 模块，使用硬编码默认值

### `src/custom_stt.py`
- 新增 `get_default_stt_config()` 函数
- `CustomSTT` 类构造函数支持可选参数，默认从配置获取
- 如果没有 config 模块，使用硬编码默认值

## 优点

1. **集中管理**: 所有配置在一个地方管理
2. **环境变量支持**: 可以通过环境变量轻松覆盖配置
3. **向后兼容**: 现有代码继续工作，自动使用新配置
4. **灵活覆盖**: 可以在代码中随时覆盖配置
5. **易于维护**: 修改配置只需更新一个文件

## 迁移指南

如果你有现有代码使用硬编码配置，建议更新为使用统一配置：

```python
# 之前
llm = CustomLLM(
    model="openai/gpt-4",
    base_url="https://devapi-p.tp-ex.com/v1"
)

# 之后（使用默认配置）
llm = CustomLLM()

# 或者（覆盖部分配置）
llm = CustomLLM(model="openai/gpt-3.5-turbo")
```