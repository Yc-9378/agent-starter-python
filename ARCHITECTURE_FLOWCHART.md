# 多用户Agent并发处理架构流程图

## 系统架构总览

```mermaid
flowchart TD
    A[Web/App客户端] --> B[LiveKit服务器]
    B --> C[Agent服务器集群]
    C --> D{资源限制器}
    D --> E[会话管理器]
    E --> F[用户会话1]
    E --> G[用户会话2]
    E --> H[用户会话N]
    
    F --> I[语音处理管道]
    G --> I
    H --> I
    
    I --> J[LLM处理]
    I --> K[TTS处理]
    
    J --> L[外部LLM API]
    K --> M[外部TTS API]
    
    L --> N[响应返回]
    M --> N
    N --> A
```

## 详细处理流程

### 1. 用户连接阶段

```mermaid
flowchart TD
    A[用户连接请求] --> B{LiveKit服务器}
    B --> C[创建WebRTC连接]
    C --> D[分配Room ID]
    D --> E[Agent服务器连接]
    
    E --> F{资源限制器检查}
    F -->|资源可用| G[创建会话ID]
    F -->|资源不足| H[等待队列]
    H -->|超时| I[连接失败]
    H -->|资源释放| G
    
    G --> J[初始化AgentSession]
    J --> K[加载预热的VAD模型]
    K --> L[建立语音处理管道]
    L --> M[会话就绪]
```

### 2. 并发会话处理

```mermaid
flowchart LR
    A[用户A音频流] --> B[Room A]
    C[用户B音频流] --> D[Room B]
    E[用户C音频流] --> F[Room C]
    
    B --> G[Worker进程1]
    D --> H[Worker进程2]
    F --> I[Worker进程3]
    
    subgraph Agent服务器
        G --> J{会话管理器}
        H --> J
        I --> J
    end
    
    J --> K[信号量限制: 200并发]
    
    subgraph 并发会话池
        L[会话A: 用户A<br/>session_id: user_a_123]
        M[会话B: 用户B<br/>session_id: user_b_456]
        N[会话C: 用户C<br/>session_id: user_c_789]
    end
    
    K --> L
    K --> M
    K --> N
```

### 3. 语音处理管道（每个会话）

```mermaid
flowchart TD
    A[用户语音输入] --> B[WebRTC音频流]
    B --> C[噪声消除]
    C --> D[语音活动检测 VAD]
    
    D --> E{检测到语音?}
    E -->|是| F[STT转换<br/>语音转文本]
    E -->|否| B
    
    F --> G[文本处理]
    G --> H[LLM调用<br/>生成响应]
    
    H --> I[TTS转换<br/>文本转语音]
    I --> J[音频流输出]
    J --> K[用户听到响应]
    
    L[性能监控] --> M[记录: 处理时间, API调用次数]
    M --> N[更新会话状态]
```

### 4. 资源管理和限制

```mermaid
flowchart TD
    A[新会话请求] --> B{资源限制器}
    
    B --> C[检查并发数]
    C --> D{当前并发 < 最大并发?}
    D -->|是| E[检查内存使用]
    D -->|否| F[进入等待队列]
    
    E --> G{内存 < 阈值?}
    G -->|是| H[检查CPU使用]
    G -->|否| F
    
    H --> I{CPU < 阈值?}
    I -->|是| J[分配会话资源]
    I -->|否| F
    
    J --> K[创建信号量锁]
    K --> L[启动会话处理]
    
    F --> M[设置等待超时: 30s]
    M --> N[周期性检查资源]
    N --> O{资源可用?}
    O -->|是| J
    O -->|否| P[超时返回错误]
```

### 5. 错误处理和容错

```mermaid
flowchart TD
    A[会话处理开始] --> B[try: 主要逻辑]
    B --> C[API调用]
    
    C --> D{API响应状态}
    D -->|200 OK| E[正常处理]
    D -->|429 限流| F[指数退避重试]
    D -->|5xx 错误| G[重试3次]
    D -->|超时| H[超时处理]
    
    F --> I[等待1-5秒]
    G --> J[重试计数器]
    H --> K[记录超时日志]
    
    I --> C
    J -->|重试<3| C
    J -->|重试=3| L[降级处理]
    
    E --> M[成功完成]
    K --> N[部分失败]
    L --> N
    
    M --> O[更新成功指标]
    N --> P[更新失败指标]
    
    O --> Q[释放资源]
    P --> Q
```

### 6. 性能监控和指标收集

```mermaid
flowchart TD
    A[会话事件] --> B[指标收集器]
    
    B --> C[实时指标]
    C --> D[活跃会话数]
    C --> E[会话成功率]
    C --> F[平均响应时间]
    C --> G[API错误率]
    
    B --> H[历史指标]
    H --> I[会话时间分布]
    H --> J[峰值并发统计]
    H --> K[资源使用趋势]
    
    B --> L[业务指标]
    L --> M[用户对话轮次]
    L --> N[主题分类统计]
    L --> O[满意度评分]
    
    D --> P[监控仪表盘]
    E --> P
    F --> P
    G --> P
    
    I --> Q[性能报告]
    J --> Q
    K --> Q
    
    M --> R[业务分析]
    N --> R
    O --> R
```

## 关键组件说明

### 1. **LiveKit服务器**
- 处理WebRTC连接
- 管理音频/视频流
- 房间和参与者管理
- SIP电话集成支持

### 2. **Agent服务器集群**
- **多个Worker进程**：每个进程独立处理会话
- **共享预热模型**：VAD等模型在进程间共享
- **负载均衡**：LiveKit自动分配连接到不同Worker

### 3. **资源限制器**
```python
class ResourceLimiter:
    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def acquire(self):
        """获取资源许可，控制并发"""
        async with self.semaphore:
            yield
```

### 4. **会话管理器**
- 为每个用户创建唯一的 `session_id`
- 维护会话状态和上下文
- 隔离用户数据，确保隐私
- 管理会话生命周期

### 5. **语音处理管道**
```
用户语音 → 噪声消除 → VAD检测 → STT转换 → 
文本处理 → LLM生成 → TTS合成 → 音频输出
```

### 6. **性能监控系统**
- **实时指标**：Prometheus格式
- **结构化日志**：JSON格式，便于分析
- **健康检查**：/health, /ready端点
- **告警机制**：阈值触发通知

## 并发处理示例

### 场景：3个用户同时对话

```python
# 用户A的会话
async def handle_user_a():
    session_id = "user_a_room_123"
    async with session_limiter.acquire(session_id):
        # 独立的语音管道
        session = AgentSession(
            llm=CustomLLM(),
            tts=SparkTTSPlugin(),
            vad=shared_vad_model  # 共享的预加载模型
        )
        await process_conversation(session)

# 用户B的会话（并行处理）
async def handle_user_b():
    session_id = "user_b_room_456"
    async with session_limiter.acquire(session_id):
        # 独立的语音管道
        session = AgentSession(
            llm=CustomLLM(),
            tts=SparkTTSPlugin(),
            vad=shared_vad_model  # 共享的预加载模型
        )
        await process_conversation(session)

# 两个会话并行执行
await asyncio.gather(
    handle_user_a(),
    handle_user_b()
)
```

## 性能指标基准

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 最大并发用户 | 200 | 4核CPU，8GB内存 |
| 平均响应时间 | < 3秒 | 端到端延迟 |
| 会话成功率 | > 95% | 成功完成的会话比例 |
| API错误率 | < 5% | 外部API调用失败率 |
| 内存使用 | < 1GB | 每个Worker进程 |
| CPU使用 | < 80% | 避免过载 |

## 扩展策略

### 垂直扩展
- 增加CPU核心数 → 提高单个实例并发数
- 增加内存容量 → 支持更多会话上下文

### 水平扩展
```yaml
# Kubernetes部署配置
replicas: 3  # 3个实例
resources:
  limits:
    cpu: "1"      # 每个实例1核
    memory: "1Gi" # 每个实例1GB内存
```

### 总并发计算
```
总并发用户数 = 实例数 × 单实例并发数
             = 3 × 200 = 600用户
```

## 总结

多用户Agent并发处理的核心原则：

1. **隔离性**：每个用户会话完全独立
2. **资源共享**：预加载模型在会话间共享
3. **资源限制**：严格控制并发数，防止过载
4. **监控告警**：实时跟踪性能指标
5. **容错处理**：自动重试和降级机制
6. **可扩展性**：支持水平和垂直扩展

这种架构确保了：
- ✅ 用户间数据隔离和安全
- ✅ 高并发下的稳定性能
- ✅ 资源的高效利用
- ✅ 系统的可观测性
- ✅ 故障的快速恢复