# 生产环境部署指南

本文档详细说明如何将Agent服务器部署到生产环境，支持多用户并发访问。

## 架构概述

生产环境Agent服务器具有以下特性：

1. **多用户并发支持**：支持200+并发用户
2. **资源管理**：CPU、内存、连接数限制
3. **性能监控**：实时监控会话状态和性能指标
4. **容错处理**：自动重试、错误处理、会话隔离
5. **可扩展性**：支持水平扩展

## 目录结构

```
.
├── src/
│   ├── agent.py              # 基础Agent实现
│   ├── production_agent.py   # 生产环境Agent（带监控和限制）
│   └── config.py            # 配置管理
├── config/
│   └── production.yaml      # 生产环境配置
├── .env.production          # 生产环境变量
├── Dockerfile              # 生产环境Docker配置
├── docker-compose.production.yaml  # Docker Compose配置
├── k8s-deployment.yaml     # Kubernetes部署配置
├── deploy.sh              # 部署脚本
└── PRODUCTION_DEPLOYMENT.md # 本文档
```

## 部署前准备

### 1. 环境变量配置

创建 `.env.production` 文件：

```bash
# LiveKit配置
LIVEKIT_URL=wss://your-livekit-server.livekit.cloud
LIVEKIT_API_KEY=your_api_key_here
LIVEKIT_API_SECRET=your_api_secret_here

# OpenAI API配置
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_BASE_URL=https://devapi-p.tp-ex.com/v1
OPENAI_MODEL=openai/gpt-4

# TTS API配置
SPARK_TTS_API_URL=http://172.16.0.4:31184/spark-tts/streaming
SPARK_TTS_VOICE=woman_girl
SPARK_TTS_SAMPLE_RATE=24000
SPARK_TTS_FORMAT=wav

# 服务器配置
SERVER_HOST=0.0.0.0
SERVER_PORT=7880
WORKER_PROCESSES=4
MAX_CONNECTIONS=200

# 日志配置
LOG_LEVEL=INFO
LOG_FORMAT=json
```

### 2. 配置文件

创建 `config/production.yaml`：

```yaml
server:
  host: "0.0.0.0"
  port: 7880
  worker_processes: 4
  max_connections_per_worker: 50

concurrency:
  max_concurrent_users: 200
  api_rate_limit_per_user: 5

model_config:
  llm:
    model: "openai/gpt-4"
    base_url: "https://devapi-p.tp-ex.com/v1"
    timeout: 60
    max_retries: 3
  
  tts:
    api_url: "http://172.16.0.4:31184/spark-tts/streaming"
    voice: "woman_girl"
    sample_rate: 24000
    audio_format: "wav"
    timeout: 30
    max_retries: 2
```

## 部署方式

### 方式一：使用部署脚本（推荐）

```bash
# 设置环境变量
export LIVEKIT_URL="wss://your-livekit-server.livekit.cloud"
export LIVEKIT_API_KEY="your_api_key"
export LIVEKIT_API_SECRET="your_api_secret"
export OPENAI_API_KEY="your_openai_key"

# 运行部署脚本
chmod +x deploy.sh
./deploy.sh all
```

脚本将：
1. 检查环境变量
2. 生成配置文件
3. 构建Docker镜像
4. 生成Docker Compose和Kubernetes配置
5. 显示部署指南

### 方式二：Docker Compose部署

```bash
# 生成Docker Compose文件
./deploy.sh compose

# 启动服务
docker-compose -f docker-compose.production.yaml up -d

# 查看日志
docker logs -f agent-server
```

### 方式三：Kubernetes部署

```bash
# 生成Kubernetes配置
./deploy.sh k8s

# 创建Kubernetes secret
kubectl create secret generic agent-secrets \
  --from-literal=livekit-url=$LIVEKIT_URL \
  --from-literal=livekit-api-key=$LIVEKIT_API_KEY \
  --from-literal=livekit-api-secret=$LIVEKIT_API_SECRET \
  --from-literal=openai-api-key=$OPENAI_API_KEY

# 部署应用
kubectl apply -f k8s-deployment.yaml
```

### 方式四：直接Docker运行

```bash
# 构建镜像
docker build -t agent-server:latest .

# 运行容器
docker run -d \
  -p 7880:7880 \
  -e LIVEKIT_URL=$LIVEKIT_URL \
  -e LIVEKIT_API_KEY=$LIVEKIT_API_KEY \
  -e LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  --name agent-server \
  agent-server:latest
```

## 性能监控

### 1. 健康检查端点

```
# 健康检查
GET http://localhost:9090/health

# 就绪检查
GET http://localhost:9090/ready

# 性能指标（Prometheus格式）
GET http://localhost:9090/metrics
```

### 2. 日志监控

生产环境Agent会输出结构化JSON日志：

```json
{
  "timestamp": "2026-03-02T08:00:00Z",
  "level": "INFO",
  "logger": "production_agent",
  "message": "会话完成",
  "session_id": "room_123_1677772800",
  "duration": 12.34,
  "active_sessions": 45,
  "success_rate": 0.98
}
```

### 3. 关键性能指标

- **活跃会话数**：当前处理的并发用户数
- **会话成功率**：成功完成的会话比例
- **平均响应时间**：从接收到响应的平均时间
- **资源使用率**：CPU、内存使用情况
- **API错误率**：外部API调用失败比例

## 配置调优

### 1. 并发配置

根据服务器资源调整 `config/production.yaml`：

```yaml
concurrency:
  # 根据CPU核心数调整
  max_concurrent_users: 200  # 4核CPU建议值
  
  # 根据内存调整（每个会话约5-10MB）
  # 1GB内存：100个会话
  # 2GB内存：200个会话
  # 4GB内存：400个会话
```

### 2. 资源限制

```yaml
resource_limits:
  memory_limit_mb: 1024  # 内存限制
  cpu_limit_percent: 80  # CPU使用限制
  
  # 超时设置
  max_audio_processing_time: 30
  max_tts_response_time: 10
```

### 3. 性能优化

```yaml
performance:
  connection_pool:
    max_size: 100  # HTTP连接池大小
    
  cache:
    enabled: true  # 启用响应缓存
    ttl_seconds: 300  # 缓存有效期
```

## 故障排除

### 1. 常见问题

#### 问题：会话超时
**解决**：增加超时设置
```yaml
model_config:
  llm:
    timeout: 120  # 增加到2分钟
  tts:
    timeout: 60   # 增加到1分钟
```

#### 问题：内存不足
**解决**：减少并发数或增加内存
```yaml
concurrency:
  max_concurrent_users: 100  # 减少并发数
```

#### 问题：API调用频繁失败
**解决**：增加重试机制
```yaml
model_config:
  llm:
    max_retries: 5  # 增加重试次数
    retry_delay: 3  # 重试延迟
```

### 2. 监控命令

```bash
# 查看容器状态
docker ps
docker stats agent-server

# 查看日志
docker logs --tail 100 agent-server
docker logs -f agent-server | grep -E "(ERROR|WARNING|性能监控)"

# 健康检查
curl http://localhost:9090/health
curl http://localhost:9090/ready
```

### 3. 紧急处理

```bash
# 重启服务
docker-compose -f docker-compose.production.yaml restart

# 停止服务
docker-compose -f docker-compose.production.yaml down

# 清理资源
docker system prune -af
```

## 扩展部署

### 1. 水平扩展

增加更多实例：

```yaml
# docker-compose.production.yaml
services:
  agent-server:
    deploy:
      replicas: 3  # 运行3个实例
      resources:
        limits:
          cpus: '0.5'  # 每个实例0.5核
          memory: 512M  # 每个实例512MB内存
```

### 2. 负载均衡

使用Nginx或HAProxy：

```nginx
upstream agent_servers {
    server agent-server-1:7880;
    server agent-server-2:7880;
    server agent-server-3:7880;
}

server {
    listen 80;
    location / {
        proxy_pass http://agent_servers;
    }
}
```

### 3. 数据库集成

添加Redis或PostgreSQL进行会话管理：

```yaml
# docker-compose.production.yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
      
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: agent_db
      POSTGRES_USER: agent_user
      POSTGRES_PASSWORD: agent_password
    volumes:
      - postgres-data:/var/lib/postgresql/data
```

## 安全建议

1. **API密钥管理**：使用Kubernetes Secrets或Docker Secrets
2. **网络隔离**：将Agent服务器放在内网，通过负载均衡器暴露
3. **访问控制**：配置CORS和API密钥验证
4. **日志脱敏**：确保日志中不包含敏感信息
5. **定期更新**：定期更新依赖和基础镜像

## 支持与维护

### 1. 监控告警

配置Prometheus + Grafana监控：

- 会话成功率 < 95% 时告警
- 响应时间 > 10秒 时告警
- 内存使用率 > 80% 时告警
- API错误率 > 5% 时告警

### 2. 备份策略

- 每日备份配置文件
- 每周备份数据库（如果使用）
- 监控日志文件大小，自动轮转

### 3. 更新流程

1. 测试环境验证
2. 灰度发布（先更新部分实例）
3. 监控性能指标
4. 全量更新

---

如需进一步帮助，请参考：
- [LiveKit文档](https://docs.livekit.io/)
- [Docker文档](https://docs.docker.com/)
- [Kubernetes文档](https://kubernetes.io/docs/)