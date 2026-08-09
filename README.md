# GainLab AI Trading API

GainLab AI 自动交易服务端 Base 版本。

当前版本先固定一套官方 `PA_MOCK_V1` 策略，目的是打通：

```text
网站创建部署 -> Deployment Key -> EA 激活
-> 开仓/持仓判断 -> MT5 执行 -> 执行结果回传
```

## 已实现

- `GET /health`
- `POST /api/v1/ea/activate`
- `POST /api/v1/ea/heartbeat`
- `POST /api/v1/trading/open/evaluate`
- `POST /api/v1/trading/position/evaluate`
- `POST /api/v1/executions/report`
- SQLite 部署、决策、心跳和执行回报存储
- Deployment Key 哈希存储
- 首次激活绑定 MT 账号和服务器
- `request_id` 幂等，防止 EA 重复请求产生重复决策
- 模拟 PA 策略，便于先开发 EA 和 UI

## 本地启动

需要 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8800
```

接口文档：

```text
http://127.0.0.1:8800/docs
```

开发环境会自动创建演示部署：

```text
Deployment Key: gl_demo_pa_key
Symbol: XAUUSD
Timeframe: M15
```

可通过 `GAINLAB_DEMO_DEPLOYMENT_KEY` 修改演示 Key。生产环境必须关闭演示部署并由网站后台创建真实部署。

## 测试

```powershell
pytest
```

## 下一步

1. 接入 GainLab K 线接口。
2. 将 `PA_MOCK_V1` 替换成独立实现的 PA 两阶段分析。
3. 接入统一模型网关和 AI 点数。
4. 增加网站部署管理 API。
5. 开发 MT5 EA dry-run 客户端。

