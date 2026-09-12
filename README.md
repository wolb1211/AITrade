# GainLab AI Trading API

GainLab AI 自动交易服务端。MT5 EA 通过固定接口获取策略配置、提交行情数据，服务端完成数据处理、AI 判断与结果校验，再返回可执行的开仓与持仓风控动作，并接收执行结果与历史成交回传。

## 架构概览

| 层 | 说明 |
| --- | --- |
| EA 接入 | `/mt5/strategy/init`、`/mt5/strategy/open-decision`、`/mt5/strategy/position-decision`，另有回传历史成交的 `/mt5/executions/history-sync` |
| 策略引擎 | 官方策略库与用户自定义策略，共用同一套 EA 接口 |
| AI 网关 | 官方模型端点或用户自带端点，支持多模态截图请求、响应缓存与按点计费 |
| 存储 | 默认 SQLite（`runtime/gainlab_ai.db`），可切换 MySQL |
| 前端接口 | 用户端 `/api/v1/auth/*`，站点端 `/api/v1/web/*`，管理后台 `/api/admin/ai/*` |

基础 API（早期版本，保留）：`/health`、`/api/v1/ea/activate`、`/api/v1/ea/heartbeat`、`/api/v1/trading/open/evaluate`、`/api/v1/trading/position/evaluate`、`/api/v1/executions/report`。

## 策略

| 代码 | 类型 | 决策方式 |
| --- | --- | --- |
| `PA_AGENT_V1` | 官方 | 服务端先做确定性过滤（K 线形态、EMA、ATR、结构打分），有候选方向才进入**两阶段 AI**：阶段一（`pa_diag`）把行情归入八种周期状态之一、给出状态概率并关闭四道闸门；阶段二（`open`）在状态对应的策略库文本下产出订单类型（市价单/限价单/突破单/不下单）、入场价、止损与止盈。AI 只能批准或否决服务端给出的方向，不能反向；结构止损、最小盈亏比与手数始终由服务端保留。**单笔持仓、不加仓**，因此不读取 `allow_add` / `max_positions`（客户表单上这两个开关对本策略无效果） |
| `GL_TREND_V1` | 官方 | 通道突破与 ATR 仓位、保护止损全部在服务端计算；AI 只负责两件事——**能否加仓**、**是否止盈出局**（每次都问，默认放行，只有 AI 明确判定高风险才否决加仓）。移动止损与止损是策略逻辑，AI 不参与，且保护性动作永远先于 AI 执行 |
| `CUSTOM_AI_V1` | 用户自定义 | 以页面上的流程图为唯一执行依据 |
| `PA_MOCK_V1` | 占位 | 早期演示策略，**仅非生产环境注册**，用于本地 `gl_demo_pa_key` 联调 |

`/mt5/strategy/init` 返回的是运行时数据需求（数据类型、K 线根数、调用模式、策略摘要与状态），不返回手数、风控金额或 AI 端点等信息，也不锁定 symbol 与 timeframe——每个决策请求都会重新携带。

## 自定义策略：流程图是唯一真实来源

- 用户端页面搭建开仓流程与持仓风控流程，保存时必须提交 `workflow`。
- 服务端将其编译为 `compiled_json` 用于运行；运行时不解释自然语言，自然语言只用于生成流程图草稿。
- 缺少流程图的自定义策略会被直接拒绝，避免存出永远无法执行的部署。
- 详细约束与节点类型见 `docs/custom_strategy_workflow_v1.md`。

## 本地启动

需要 Python 3.10 或更高版本：

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

非生产环境会自动创建演示部署，可直接用 EA 联调：

```text
Deployment Key: gl_demo_pa_key
Symbol: XAUUSD
Timeframe: M15
策略: PA_MOCK_V1
```

可通过 `GAINLAB_DEMO_DEPLOYMENT_KEY` 修改演示 Key。生产环境既不创建演示部署，也不注册 `PA_MOCK_V1` 引擎。

## 测试

```powershell
pytest
```

## 配置

配置来自环境变量或项目根目录 `.env` 文件（同名环境变量优先，`.env` 不会被覆盖）。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `GAINLAB_ENV` | 运行环境；`production` 会关闭演示部署与 mock 引擎，并强制后台鉴权 | `development` |
| `GAINLAB_HOST` / `GAINLAB_PORT` | 监听地址与端口 | `127.0.0.1` / `8800` |
| `GAINLAB_DATABASE_TYPE` | `sqlite` 或 `mysql` | `sqlite` |
| `GAINLAB_DATABASE_PATH` | SQLite 文件路径 | `runtime/gainlab_ai.db` |
| `GAINLAB_MYSQL_HOST` / `_PORT` / `_DATABASE` / `_USER` / `_PASSWORD` | MySQL 连接参数 | — |
| `GAINLAB_AI_TIMEOUT` | AI 请求超时（秒） | `30` |
| `GAINLAB_AUTH_SECRET` | 用户端会话签名密钥 | 空 |
| `GAINLAB_ADMIN_JWT_SECRET` | 后台 JWT 密钥；配置后强制校验后台 Bearer Token | 空 |
| `GAINLAB_SESSION_DAYS` | 用户会话有效期（天） | `30` |
| `GAINLAB_VERIFICATION_MINUTES` | 邮箱验证码有效期（分钟） | `10` |
| `GAINLAB_MAIL_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_SECURE` | 邮件发送配置 | — |
| `GAINLAB_MAIL_ENV_FILE` | 额外的邮件配置文件路径，只读取其中的 `MAIL_*` | 空 |
| `GAINLAB_DEMO_DEPLOYMENT_KEY` | 本地演示 Deployment Key | `gl_demo_pa_key` |

MySQL 建表与种子数据见 `docs/mysql_schema.sql` 与 `docs/mysql_seed_official.sql`。

## AI 计费

- 官方 AI 按端点配置的单价实时扣减用户余额，并写入 `ai_usage_logs` 与 `ai_balance_ledger`。
- 端点有三个价格字段：`input_price_per_million`（未命中缓存的输入）、`output_price_per_million`（输出）、`cache_input_price_per_million`（**命中提示词缓存的输入**）。
- 缓存命中 token 数取自供应商返回的 `prompt_cache_hit_tokens`，兼容 OpenAI 风格的 `prompt_tokens_details.cached_tokens`，并按 `prompt_tokens` 上限截断。
- `cache_input_price_per_million` 留空（0）时，命中 token 按普通输入价计费——即未配置的端点账单与改造前完全一致。
- **当前计费策略：命中价一律为 0（不向用户让折扣）。** 供应商给的缓存折扣由平台留存，用于补贴那些不向用户计费的公用 AI 判断。
- 命中 token 数会同时写入 `ai_usage_logs.cached_input_tokens` 与 `ai_usage_monthly_summaries.cached_input_tokens`，因此平台留存了多少折扣在月度汇总里长期可查（调用明细只保留 60 天，汇总不删）。
- 该字段是策略开关：若将来要对用户让出部分折扣，把某个端点的命中价调低即可，无需改代码。注意**不要把命中价设成等于输入价的固定值**——输入价上调后它会变成意外的折扣，留 0 才会自动跟随输入价。
- `PA_AGENT_V1` 的一次开仓评估会调用两次 AI（`pa_diag` + `open`），两笔分别记录，决策里返回的是两个阶段 usage 的合计。阶段一闸门未通过时不会发起第二次调用。

### 成本对照表（后台）

`POST /api/v1/admin/ai/cost-comparison`（body 可选 `{"months": 6}`）按月返回两侧的口径：

| 字段 | 含义 |
| --- | --- |
| `user_charged` | 向用户收取的金额 |
| `platform_cost` | 平台自付的 AI 成本（`user_id` 为空的调用，如自定义策略流程生成） |
| `input_tokens` / `output_tokens` | 用户侧 token（不含平台自付部分） |
| `cached_input_tokens` / `cache_hit_rate` | 供应商上报的缓存命中（**事实数据**） |
| `estimated_cache_saving` | 缓存留存的估算金额 |
| `net` | `estimated_cache_saving − platform_cost` |

折扣比例由 `POST /api/v1/admin/ai/cache-discount/save`（`{"ratio": 0.75}`）设置，仅用于估算，**不影响用户计费**。

**估算的边界**：`cached_input_tokens` 来自供应商上报，是事实；金额是按**当前**端点单价折算的估值，因为供应商的实际采购价对本系统不可见。因此模型商调价后若端点单价没跟着更新，金额会偏差——**token 数与命中率不受影响，看趋势应以它们为准**。平台自付成本同样按配置单价计算。

### 输出预算与截断

- 决策类端点（`open` / `position` / `pa_diag`）的输出上限为 **3000** token，工作流生成类为 6000，其余 500。见 `_max_tokens_for_endpoint`。
- 上限是**天花板不是目标**：模型写完即停，实测平均只写 84~521 token，因此提高上限本身不产生额外开销，只会让原本被截断的回答有机会写完。
- **推理模型（如 DeepSeek）的思考与答案共用这一份预算**，思考一长，JSON 就没机会生成。这是"DeepSeek 常报格式错误"的根因，不是模型能力问题。
- 服务端会读取供应商返回的 `finish_reason`：为 `length` 时打警告日志，并在调用明细的返回内容前加上 `⚠ 输出被截断（finish_reason=length，输出 N/M tokens）`，以便和真正的格式错误区分开。
- 每次调用的耗时写入 `ai_usage_logs.elapsed_ms`，并显示在调用明细的返回内容前（`⏱ 本次 AI 调用耗时 N.Ns`）。PA 两阶段的两个耗时之和记入同一条决策。
- EA 面板文案末尾会附上 `（本次AI分析耗时：N.N秒）`。该后缀在 `_panel_description` 中拼装，**不写回决策记录**；未调用 AI 的本地决策不加此后缀（"0.0 秒"会被误读成失败）。
- 提高上限会增加**生成时间**，推理模型尤其明显。若出现更多 `TimeoutError`，需要相应调整 `GAINLAB_AI_TIMEOUT`。

### 超时预算

EA 对**整个决策请求**有 180 秒上限（写死在 EA 里，用户不可改），服务端必须在这个时间内一定给出响应——返回兜底决策也比让 EA 收到空响应好。

EA 是单线程的：它在等待响应期间不做其他事。持仓的止损止盈挂在下单时写好的 MT5 服务端 SL/TP 上，不受等待影响；受影响的只是新的决策机会。因此单次 AI 调用超时不建议继续调高——**它同时也是 EA 在一次检查里的最长停摆时间**。

服务端按"最长调用链"取值，不依赖运行时计时：

| 端点 | 每次请求的 AI 调用 | 最坏耗时 |
| --- | --- | --- |
| `open` / `position`（GL、自定义） | 主调用 + 修复重试 | 45 + 10 = **55s** |
| `pa_diag` + `open`（PA 两阶段） | 两次主调用 + 各自一次修复重试 | 2×45 + 2×10 = **110s** |

- `GAINLAB_AI_TIMEOUT`（默认 45）是**每一次** AI 调用的超时，不是一次请求的总和。
- `REPAIR_TIMEOUT_SECONDS`（10，写在 `ai_service.py`）只给 JSON 修复重试——那是个 `max_tokens=700` 的小任务，不需要完整超时。它比主调用短，正是让上面这张表能算清的原因。
- 改任何一个值前请先跑 `test_worst_case_call_chain_fits_inside_the_ea_timeout`，它会校验最坏链路仍能塞进 EA 的 180 秒。EA 超时值与测试里的 `EA_REQUEST_TIMEOUT_SECONDS` 必须保持一致，否则测试校验的就不是真实约束。
- 自定义策略每次请求只调用一次 AI（`rule_plan` 与纯 AI 两条分支互斥），所以不属于更长的链路。

## 鉴权与访问控制

- Deployment Key 只以 SHA-256 哈希存储；首次 `init` 绑定 MT 账号与服务器，之后账号不匹配会被拒绝。
- 真实用户部署需通过：账号状态 active、VIP 未过期、使用官方 AI 时余额未耗尽。本地演示部署（owner 非数字）跳过这些校验。
- 决策请求使用 `request_id` 幂等：同一 `request_id` 重复提交直接返回已保存的决策并标记 `idempotent=true`。
- 后台接口在 `production` 或配置了 `GAINLAB_ADMIN_JWT_SECRET` 时强制 Bearer JWT，且 `roles` 必须包含 `admin`。

## 调试用法

MT5 决策接口支持用 `request_id` 前缀触发本地测试分支（不调用 AI）：

- 开仓：`test_pending_buy_limit`、`test_pending_sell_limit`、`test_random`
- 持仓风控：`test_random`、`test_random_all`、`test_add_buy`、`test_add_sell`、`test_modify`、`test_cancel`

细节见 `docs/mt5-v1-api.md`。**生产环境请勿使用这些前缀。**

## 目录结构

```text
app/
  main.py            应用装配、路由注册、CORS、启动与清理任务
  config.py          环境变量与 .env 配置
  models.py          请求/响应模型（基础 API 与 EA v1）
  security.py        Deployment Key 哈希、密码哈希、HMAC
  store.py           存储层（SQLite / MySQL、迁移、统计、计费）
  api/router.py      全部 HTTP 路由
  services/          AI 网关、用户鉴权、指标计算、规则引擎、工作流编译、截图、邮件
  strategies/        策略引擎（PA Agent、GL Trend、自定义、Mock）
docs/                接口与数据库文档
tests/               pytest 测试
runtime/             运行时数据（数据库、日志、截图预览），已被 .gitignore 忽略
```

## 运行时数据

| 路径 | 说明 |
| --- | --- |
| `runtime/gainlab_ai.db` | SQLite 数据库 |
| `runtime/mt5_validation_errors.log` | EA 请求校验失败记录，滚动保存（单文件约 2MB，保留 2 份），只记录出错字段与原因，不记录请求体 |
| `runtime/screenshot_previews/` | 截图预览缓存 |

## 运维注意

- 生产环境务必设置 `GAINLAB_ENV=production`，并配置 `GAINLAB_AUTH_SECRET` 与 `GAINLAB_ADMIN_JWT_SECRET`。
- `runtime/` 下的文件会持续增长，需要定期清理。
- 后台/站点创建部署时请确保 `strategy_code` 指向已启用的官方策略或 `CUSTOM_AI_V1`。
