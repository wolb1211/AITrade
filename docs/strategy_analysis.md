# 策略分析：策略库 2 个官方策略

对象：`official_ai_strategies` 表中 enabled=1 的两个策略，以及它们的实现文件。

| 代码 | 实现 | 行数 | 决策方式 |
| --- | --- | --- | --- |
| `PA_AGENT_V1` | `app/strategies/pa_agent_lite.py` | 2174 | 服务端确定性闸门 + 打分 → 通过才调 AI，AI 只能批准/否决 |
| `GL_TREND_V1` | `app/strategies/turtle_agent.py` | 597 | 全服务端计算，AI 仅用于加仓过滤（可选） |

所有结论都标注了 `文件:行号`，可直接核对。未逐行读取的部分在文末单列。

> **设计前提（已与负责人确认）**
> - 开仓手数由用户配置：**固定手数**，或**以损定仓**。后者有两种口径——固定止损金额（`risk_amount`）与账号余额百分比（`risk_percent`），由 `risk_base_mode` 选择。因此"手数由风险反推"是设计意图，不是缺陷。
> - **截图能力只用于用户自定义策略**，两个官方策略都不使用截图。
> - **用户可配置项只有三类**：① 策略显示名 + 绑定的 MT 账号；② 开仓 / 持仓风控各自选择 AI 模型或自定义接口；③ 仓位和风险——手数算法、每次手数或单笔风险金额与风险口径、最大持仓数量、是否允许加仓。
> - 加仓规则：先看策略本身有无加仓逻辑；有逻辑时**以用户设置为准**（例：GL_TREND_V1 上限 4 单，用户设 2 则按 2）。
> - 因此 `entry_mode`、`swing_*`、`pullback_*`、`min_lot`/`max_lot`/`lot_step`、`contract_size` 这些"代码读取但面板不提供"的键**属于内部默认值，不是配置缺陷**。
>
> 本文档按上述前提校准：凡属"设计如此"的行为不计为缺陷，只保留其中**不一致**、**不可诊断**或**缺少安全边界**的部分。

---

## 一、统一契约（两个策略共用）

- 实现 `evaluate_open` / `evaluate_position`，由 `app/services/decision_service.py` 统一做部署鉴权、`request_id` 幂等和落库。
- 配置来自 `deployment["config"]`，是自由 dict。
- **关键坑**：官方种子里的 `default_config_json` 用的 key 名与运行时读取的**不一致**，靠 `app/services/auth_service.py:378-455` 在建部署时改名：

| 种子 key | 运行时 key |
| --- | --- |
| `position_sizing_mode` | `position_size_mode` |
| `fixed_lot` | `fixed_volume` / `lot` |
| `risk_mode` | `risk_base_mode` |
| `max_stop_amount` | `risk_amount` |
| `allow_add_position` | `allow_add` |
| `max_positions` | `max_positions`（同名） |

也就是说，**直接把 `default_config_json` 塞给策略不会生效**：手数会静默退化成 0.01 固定、加仓变成关闭。任何绕过 `auth_service` 建部署的路径（脚本、后台工具、数据迁移）都会踩这个坑。

---

## 二、PA_AGENT_V1

### 2.1 决策链（`evaluate_open`，L112-153）

1. `_compute_features(candles)`：少于 30 根 → HOLD。
2. `_is_choppy(features)`（L813-819）：`setup_score < 70` **且** `overlap_mean >= 0.65` **且** 区间宽度 `<= 3 ATR` → HOLD，**不调 AI**。
3. `_open_direction(features)`（L803-810）：`candidate_valid` 为假或 `setup_bias` 中性 → HOLD，**不调 AI**。
4. 构造本地决策 `_build_open_decision`。
5. 调 AI（`_evaluate_open_with_ai`，L205-315）。
6. AI 失败/未配置 → 回退到本地决策。

> 这是"省 token"的核心设计：常规无信号 bar 不消耗 AI 额度。

### 2.2 AI 的权限边界（L231-315）

- AI 必须 `should_open=true` 且方向与服务端候选**一致**，方向冲突直接 HOLD（L244-251）。
- 止损：`sl_distance = max(AI 给的距离, 服务端结构距离, min_distance)`，其中 `min_distance = max(spread*30, atr14*1.2)`（L255-274）→ **服务端给止损兜底**，AI 只能要求更宽。
- 止盈：`tp_distance = max(AI 距离, sl_distance * 1.8)`（L264/272）→ 强制最小盈亏比 1.8。
- 手数：risk 模式走 `_position_size_lot`，否则固定手数；`lot <= 0` → HOLD（L276-288）。
- 空间过滤 `_space_block_reason`（L886-915）：AI 批准后仍可能被拦——到阻力/支撑 `< 0.8 ATR`，或 `< 1.2 ATR` 且 `setup_score < 85`。

### 2.3 打分体系（L1901-2102）

三类候选：突破（含回踩，L1938-1980）、趋势延续（L1982-2026）、回调 H2/L2（L2028-2067）。
分数 = context + structure + trigger + space − penalty（`_setup_penalty` L1842-1864，上限扣 40）。

入选条件（L2080）：**最高分 ≥ 70 且 多空分差 ≥ 12**；带 `hard_blocks` 的候选直接剔除。
空间分（`_candidate_space` L1818-1839）：≥1.5 ATR 得 15，≥1.2 得 12，≥0.8 得 7，<0.8 得 0 并硬阻断。

### 2.4 问题清单

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 1 | **数据不足反而得满分**：支撑/阻力为 `None` 时 `_candidate_space` 直接返回 15 分（与"确认有 ≥1.5 ATR 空间"同级） | L1827-1828 | 样本不足的行情更容易通过 70 分门槛而开仓 |
| 2 | 同一宽松逻辑在 AI 后置过滤里：barrier 为 `None` 时**完全不拦** | L894-898 | 与 #1 叠加，放大无空间开仓概率 |
| 3 | **硬编码金额阈值**：`deep_loss = abs(profit) >= 50` | L938 | 账户货币、与账户规模/品种无关；小账户/大账户同一阈值 |
| 4 | 固定手数模式**没有任何金额止损**：`_position_money_limits` 直接返回 `(None, None)` | L1131-1134 | 固定手数策略只靠价格 SL/TP；SL 被移动或未设时无兜底 |
| 5 | ✅ 已消除｜加仓路径强制用服务端 `min_distance` 作 SL，AI 返回的 `sl` 被静默丢弃 | L388-398 | 加仓路径已整体移除，该不一致不再存在 |
| 6 | ✅ 已消除｜加仓同样要求 `same_direction` 与 `max_positions`，但**没有金额/风险二次校验** | L379 | 加仓路径已整体移除，风险敞口不再存在 |

### 2.5 配置契约（代码只读 8 个 key）

`allow_add`(L379)、`fixed_volume`/`lot`(L1124)、`max_positions`(L377)、`position_size_mode`(L276,1065,1131)、`risk_amount`(L1075,1139,1141)、`risk_base_mode`(L1072,1136)、`risk_percent`(L1073,1137)。

种子里 `allow_add_position` / `fixed_lot` / `max_stop_amount` / `position_sizing_mode` / `risk_mode` 这 5 个都是靠 `auth_service` 改名的**输入别名**，不是死配置——但容易被误认为失效。

---

## 三、GL_TREND_V1

### 3.1 决策链（`evaluate_open`）

> 行号会随修复位移；修复后的状态见第七节。

1. 最小 K 线：`max(entry_period+1, atr_period+2, SWING_MIN_BARS=30)`。
2. 两套入场系统**每根 K 线都各自判断**（不再有模式开关）：唐奇安＝收盘价突破前 N 根通道；波段＝回调结构 + 确认形态。
3. 任一成立即开仓；**方向相反则不开仓**。
4. 止损：`STOP_ATR × ATR`（常量，默认 2）；手数由 `_unit_lot` 计算；`lot <= 0` → HOLD。
5. 返回 `tp=None`——**不设止盈**，离场完全靠出场通道/2ATR。

**两套开仓逻辑的关系（已与负责人确认）**：并行运行，任一成立即开仓；只有当两者给出**相反方向**时才不开仓（两套都是趋势系统，方向矛盾说明行情不明确）。两套各自的方向写入 `metadata.swing_direction` / `metadata.donchian_direction`，便于事后观察是否出现过冲突。

> 原先存在 `entry_mode` 开关（`donchian` / `swing_only` / `combined`），是开发期测试遗留：种子里没有这个键、后台表单也没有字段，实际恒为 `combined`。既然产品确定只要并行，该开关及其两个单模式分支已删除。

### 3.2 出场与保护（`evaluate_position` L85-151）

- 出场：**整篮语义**。先按方向分组，任一成员跌破通道（BUY）/突破通道（SELL），或触及**整篮统一止损**（该方向最远入场价 ± `STOP_ATR`×ATR），就**一次平掉该方向全部持仓**（`batch_actions` 里每票一个 `close`）。这两条安全阀**先于 AI 执行，不等 AI**。
- 之后**每次都问 AI**（复核，见 3.3）：AI 说止盈出局 → 整篮全平；否则再执行保本/移动止损。
- `_protection_batch_decision` **遍历所有持仓**，取"更保护"的一侧（BUY 取最高 SL、SELL 取最低 SL），保本/移动止损是**逐仓**的（每个 Unit 有自己的入场价），在一次响应里批量下发。
- 保本（`break_even_atr`，触发 1.0 ATR，目标为开仓价 + 点差缓冲）与移动止损（`trailing_start_atr` 1.5 / `trailing_distance_atr` 1.0）。
- 加仓（`_maybe_add`）：需 `allow_add` + 同向 + 未达 `_max_units`；锚定最远开仓价，间隔 `add_step_atr`；**加仓同时把全部已有持仓的止损统一到新水平**；是否执行由持仓复核（见 3.3）决定。

### 3.3 AI 在 GL_TREND_V1 中的角色（已与负责人确认）

**结论：方向、入场、止损、手数、出场全部由服务端计算；AI 只作为"风险闸门"参与，且默认放行。**

| 环节 | AI 参与方式 |
| --- | --- |
| 开仓 | ✅ `turtle_open_risk_decision`（走**开仓 AI** 模型）：服务端已定好方向/入场/止损/手数，AI 只输出 `allow_open` + `risk_level` + 理由，**不能改动任何订单参数** |
| 持仓复核 | ✅ `turtle_position_review`（走**持仓风控 AI** 模型）：每次持仓请求调用一次，回答两件事——`close_now`（**止盈出局**或反转离场：要不要不等通道/止损触发就提前平掉整篮）与 `allow_add`（若存在加仓候选，是否可以执行）。它同样不能改任何订单参数 |
| 整篮统一止损、通道平仓 | ❌ 纯服务端，**且不等 AI**：这两条是安全阀，先于 AI 复核执行并直接返回 |
| 保本、移动止损 | ❌ 纯服务端策略逻辑，**AI 不参与判断**（也从不被询问要不要移动止损）；但它们在 AI 复核**之后**执行——只延迟"止损收紧"，不会延迟"兜底止损" |

**AI 的职责边界（已与负责人确认）**：止损是**兜底**，移动止损是**提前锁定部分利润**，两者都是策略逻辑；AI 只负责两件事——**现在还能不能加仓**、**涨得够多了要不要直接止盈出局**。因为下面始终有止损兜底，AI 说"不出"是安全的。

送进 AI 的上下文（`signal`）包含：`protective_stop_levels`（当前整篮止损位）、`atr`、每个持仓的 `open_price / current_price / profit / favorable_atr`（**已走多少倍 ATR**，用于判断"涨够了"）、`sl / tp / bars_since_open`，以及存在加仓候选时的 `add_candidate`。

**低门槛规则（关键设计）**：只有 AI 明确判定 `risk_level = high` 时才否决订单；若返回 `false` 但等级是 medium / low / 未给出，服务端**仍按程序规则执行**，并把"AI 建议否决但未判定高风险"写入 `metadata.ai_risk`。开仓与加仓适用同一规则。

**提前平仓是"可选动作"，不是最后防线**：每次开仓/加仓都已带止损，且整篮统一止损与出场通道始终有效。所以 AI 说"不平"是安全的（止损兜底），AI 说"平"才会提前离场。另外设了一道防抖：**开仓所在的 K 线内不允许提前平仓**（`PROACTIVE_EXIT_MIN_BARS = 1`，按已收盘 K 线计），被抑制时会记进 `metadata.ai_risk`。

其它细节：

- 提示词明确要求：**默认通过**、只有具名且具体的危险（入场处过度延伸/力竭、极端波动、最近几根剧烈反向）才算高风险、**不得给出方向/价格/止损/手数建议**。
- **失败放行**：AI 未配置或调用失败 → 按程序规则执行。
- **计费与频率**：持仓复核**每次持仓请求都会调用**（默认 `call_mode=bar`，即每根 K 线一次，约 850 token/次），这是"让 AI 参与"的直接成本；如需降本可改成每 N 根 K 线复核一次（`bars_since_open` 已算好，无需额外状态）。
- 结果写入 `metadata.ai_risk`，决策日志里能看到 AI 的结论、或"被覆盖的否决"、或"被防抖抑制的提前平仓"。

### 3.4 仓位模型（`_unit_lot` L361-395）

- `position_size_mode != "risk"` → 固定手数（`fixed_volume`/`lot`，默认 0.01），按 `volume_step` 取整并夹到 `volume_min`。
- risk 模式：`risk_money`（`balance_percent` 或 `risk_amount`）÷ `risk_per_lot`。
- `risk_per_lot`（L398-415）优先级：`tick_size`+`tick_value` > `value_per_price` > `point`+`value_per_point` > `contract_size` > 0。
- **`risk_per_lot <= 0`（即 EA 没传 symbol_info 合约信息）→ 返回 0 → HOLD**（L389-390）。**这个行为本身是正确的**（算不出风险就不下单），问题只在提示语不可诊断：EA 侧只看到"无法按当前止损和品种合约参数计算有效手数"，看不出缺的是哪个字段。

### 3.4 问题清单

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 1 | **`stop_atr` 是死配置**：种子给 `stop_atr: 2.0`，代码从不读取，止损硬编码 2×ATR。用户面板里没有这个键，但**后台编辑官方案略的 `default_config` 时可以写它**（`store.py:2420-2422`） | L66,101-104,553,558；`auth_service` 也不映射 | 运营在后台调这个值不会生效，而种子值恰好也是 2.0，所以"看不出问题" |
| 2 | **`risk_fraction` 是多余的死键**：以损定仓的两条口径实际由 `risk_base_mode` + `risk_amount` / `risk_percent` 实现（L383-384、L1136-1141），`risk_fraction` 从不被读取 | 全仓库仅 `store.py:1383/6937` 出现 | 种子里同时存在 `risk_percent:1` 与 `risk_fraction:0.01`，语义重叠，容易让人改错那个不起作用的 |
| 3 | ✅ 已修复（删除）｜**批量"保护+加仓"分支不可达** | L111-112 先把 `add_decision` 置 `None`，L119 又要求两者都非 `None` | 该功能实际不存在，代码里却留着 19 行 |
| 4 | ✅ 已修复（删除）｜重复且不可达的硬止损判断 | L147-150 与 L101-104 条件完全相同 | 死代码 |
| 5 | ✅ 已修复｜**只管理 `positions[0]`**：出场/硬止损只用第一个持仓，而保护逻辑遍历全部 | L86,96-104 vs L455 | 第 2 个及以后的 Unit 没有通道/2ATR 硬止损 |
| 6 | 手数缺少平台侧安全上限：`max_lot` 缺省为 0.0 时不裁剪。**仅在 EA 未提供 `volume_max` 时存在**；固定手数是用户自己配的，不受影响 | L376,441-442 | 以损定仓时若 `risk_per_lot` 因合约信息不准而偏小，算出的手数只会被经纪商拒绝，平台侧无兜底 |
| 7 | `confidence` 恒为 1.0 | L72,477,504,533,564 | 下游任何按置信度过滤的逻辑都失效 |
| 8 | AI 允许加仓时**不记录 usage**（只在被 AI 拦下时记录） | L142-146 | 计费/审计缺一条 |
| 9 | ✅ 已随 `entry_mode` 开关一起删除｜`entry_mode` 未知值静默按 `combined` | 已无该键 | 配置写错不会再被静默吞掉——因为不再有可写错的键 |
| 10 | ✅ 已修复（GL）｜**最大持仓数量没有被策略上限约束**（与新确认的产品规则不符）：`_max_units` 让 `max_positions` **完全覆盖** `max_units`，取的是 `max_positions or max_units or 4`，**不是 `min()`**；`auth_service` 也只校验 `>= 1`，没有上界 | L446-450；`auth_service.py` 的最大持仓校验 | 按产品意图应是"用户设置优先，但不得超过策略上限（GL 为 4）"。当前若用户在面板填 6，服务端会允许 6 个 Unit，每个各自 2 ATR 止损且无总风险上限。**PA_AGENT_V1 同类问题仍待定** |
| 11 | ✅ 已修复｜加仓止损按**新单元入场价**重算，不重锚已有单元 | 原 L553,558 | 与海龟"单元级统一止损"语义不同。现已改为：新 Unit 的止损即**整篮统一止损**（锚定最远入场价），并在同一次响应里把所有已有持仓移到该位（更紧的保持不动） |
| 12 | `_swing_pullback_signal` 里 `previous_close` 赋值后未使用 | L223 | 死变量 |
| 13 | AI 未配置时加仓**默认放行** | L183-186 | 与"AI 作为可选确认"一致，但无日志/元数据可查 |

---

## 四、两个策略的共同问题

1. **配置键双层命名**（见第一节）：种子名 ≠ 运行时名，全靠 `auth_service` 改名。绕过它建部署会静默降级。
2. **同一数据缺失、两个策略两种处理**：缺 `symbol_info` 合约信息时，GL 返回 0 手数直接不开仓（L389-390），PA 则**静默退回固定手数**（L1111-1112）。两者的取舍各自都说得通，但不一致——需要明确哪个是期望行为。共同问题是提示语都不可诊断，EA 侧看不出缺的是哪个字段。
3. **手数上限依赖 EA 提供 `volume_max`**：EA 不传就没有平台侧上限（两者实现一致）。
4. **重复实现**：`_positive_float`、`_normalize_volume`、`_atr`、`_close` 在两个文件各有一份，签名还不一致（`_normalize_volume` 一个用关键字参数、一个用位置参数）。
5. **置信度语义不一致**：PA 用 AI 给的 confidence（0-1），GL 恒为 1.0。

---

## 五、改进优先级建议

**P0 — 正确性，建议尽快**
1. GL：出场与硬止损改为遍历所有持仓（现只处理 `positions[0]`）——第 2 个及以后的 Unit 目前没有硬止损。
2. GL：删除不可达分支 L119-137 与重复的 L147-150；或按原意修好"保护+加仓"合并。
3. **最大持仓数量应取 `min(用户设置, 策略上限)`**：产品规则是"用户设置优先但不超过策略上限"，而当前 `_max_units` 让用户值完全覆盖策略上限（GL 设 6 就会允许 6 个 Unit）。需先确认前端是否已限制输入范围；若前端已限制，服务端仍建议补上兜底。

**P1 — 风险边界与一致性**
4. PA：数据不足时不应给满分空间分（L1827）与完全不拦（L894-898），建议改为"未知即不给分/记为待确认"。
5. 两者：缺 `symbol_info` 时行为不一致（GL 不开仓 / PA 退固定手数）。需明确哪个是期望行为并统一，同时把"缺哪个字段"写进 `metadata` 和 HOLD 理由——目前 EA 侧看不出原因。
6. GL 与 PA：为以损定仓加平台侧手数上限（仅在 EA 未提供 `volume_max` 时才会用到）。属于兜底加固，不是现网缺陷。
7. PA：AI 给的 `sl_distance_price` 没有上限（L256/263），固定手数模式下单笔风险可能远超预期，建议夹到服务端上限。
8. PA：`bool(content.get("should_open", False))`（L232）会把字符串 `"false"` 判为真；未知 ticket 静默回退到 `positions[0]`（L337-338）会平/改错仓——这两处应做类型校验并显式拒绝。

**P2 — 一致性与可观测性**
9. GL：`confidence` 按信号强度给出，而非恒 1.0；AI 加仓放行时也记录 usage。
10. ~~GL：`entry_mode` 加白名单校验，未知值报错而不是静默降级为 combined。~~ → 已通过**删除该开关**解决。
11. PA：提示词里让 AI 给 `sl`，运行时却丢弃（L388-398）——要么用，要么从提示词去掉。
12. PA：冷却期对 AI 平仓路径不生效（L340-344）；`MODIFY_TP` 从不产生，且止盈可被放宽、止损只能收紧（L2142-2158）——需要与设计意图对齐。

**P3 — 结构与可维护性**
13. 抽公共工具模块，合并 `_atr` / `_positive_float` / `_normalize_volume` / `_normalize_epoch_seconds`。
14. 打分阈值（70/12/0.8/1.2/1.5、penalty 上限 40）集中到一处常量，便于调参与回测。
15. 加一道配置校验：禁止把不使用截图的官方策略设成 `data_type=screenshot`（当前误设会永久 HOLD）。
16. 清理两个"后台可写但不生效"的键：`stop_atr`（要么接上、要么撤掉）与 `risk_fraction`（与已实现的 `risk_percent` 语义重叠、从不被读取）——用户面板里没有它们，但后台编辑官方案略时会误导运营。

---

## 六、PA_AGENT_V1 深度补充（以下每条均已按行号核对）

这一节来自对 `pa_agent_lite.py` 全文的二次审计，我只收录了自己逐行确认过的条目。

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| S1 | **`_is_choppy` 永远不会改变结果**：它的条件含 `setup_score < 70`，而 `candidate_valid` 要求 `total_score >= 70`（L2080），且 `setup_score` 就是候选总分（L788）→ 命中与否都必然是 HOLD，只影响理由文案与 confidence | L813-819、L788、L2080 | 一个"看起来在防震荡、实际不起作用"的闸门；调它的阈值不会有任何效果 |
| S2 | AI 字符串 `"false"` 会被当成真：`bool(content.get("should_open", False))` | L232（同类：`bool(config.get("allow_add"))` L379） | AI 明确拒绝时仍可能开仓 |
| S3 | **幻觉 ticket 静默回退到第一个持仓**：`next((...), request.positions[0])` | L337-338 | AI 给错 ticket 时，平/改的是**另一个仓位**，且无任何提示 |
| S4 | **AI 存在时，确定性持仓逻辑是死代码**：L174-187 只要 AI 结果非空就一定 return | L174-187 vs L189-203 | 线上（AI 必配）实际由 AI 决定平仓，L189-203 的反向信号平仓/移动止损只在 AI 失败时生效 |
| S5 | **冷却期只作用于确定性平仓，AI 平仓绕过它**：L340-344 直接返回，未调用 `_cooldown_blocks_close` | L340-344 vs L191/195、L928 | AI 可以在开仓后 1 根 K 线就平仓，与"3 根冷却"的设计矛盾 |
| S6 | **`risk_amount` 默认值前后不一致**：开仓手数用 10，金额限额用 100；且 `_positive_float` 把 0 当"缺失" | L1075、L1139/L1141、L1148-1153 | 用户设 `risk_amount=0` 会被静默替换为 10/100，两个环节还不一致 |
| S7 | 以损定仓缺合约信息时**静默退回固定手数**（不是拒绝开仓） | L1102-1112 | 与 GL_TREND_V1 的处理**相反**（GL 返回 0 手数不开仓）。两者都不算错，但不一致；且 0.01 手 × 1.2 ATR 止损的隐含风险与用户设的 risk_amount 无关 |
| S8 | AI 给的 `sl_distance_price` **没有上限** | L256、L263/L271 | 固定手数模式下单笔风险无上界，而止盈被强制 1.8 倍止损 |
| S9 | **`MODIFY_TP` 从不产生**：全仓库只有 `MODIFY_SL`（pa L519、turtle L477/504/533、custom_ai L172），尽管 `models.py:111` 定义了它、`router.py:1891` 也处理它。且 `_validated_position_modification` 对止损要求"只能收紧"，对止盈**只校验方向不要求收紧** | L2142-2158；grep 全仓 | 只改止盈会被当成 `MODIFY_SL` 上报；止盈可被无限放宽，止损不能——不对称 |
| S10 | 截图能力对 PA 未接通：`pa_open_decision` / `pa_position_decision` 均未传 `user_image_url`（对比自定义策略路径 `ai_service.py:560`） | L216-226、L325-329 | **不是实际风险**——已确认两个官方策略都不使用截图。仅为配置校验缺口：目前没有任何地方阻止把 PA 部署设成 `data_type=screenshot`，一旦误设就会因拿不到 K 线而永久 HOLD |
| S11 | `_compute_features` 在 `atr14 <= 0` 时返回 None，但理由文案写的是"requires at least 30 candles" | L595-596、L118-119、L170-171 | 把"数据异常/行情无效"误报成"K 线不足"，排查方向会被带偏 |

---

## 七、修复记录（GL_TREND_V1）

本轮已修复的 P0 项。**注意：本报告其余章节的行号均为修复前的版本**，修复后的行号已发生位移。

| 项 | 改动 | 验证 |
| --- | --- | --- |
| 多持仓出场（原问题 #5） | `evaluate_position` 改为遍历**全部**持仓检查出场通道与 2ATR 硬止损；每次调用仍只返回一个 close（当时 EA 契约的 `batch_actions` 不支持 close），多单在后续调用中依次平掉。**此项随后被"整篮管理"取代，见下一行** | 新增 3 个测试：通道跌破、硬止损、SELL 方向均落在**第二个** Unit 上 |
| **整篮管理（取代上一条）** | 产品要求"把所有持仓当一个仓位算、要平就一起平"。现在：① 止损是**整篮统一水平**（锚定该方向最远入场价 ± STOP_ATR×ATR，多头取 `max(open_price)`、空头取 `min`）；② 通道/止损触发时**一次平掉该方向全部持仓**；③ 加仓时在同一次响应里把已有持仓的止损一起移到新水平（已更紧的保持不动）。为此给 `router._mt5_position_response` 补上了 `batch_actions` 的 **`close`** 支持 | 6 个测试：整篮通道平仓、整篮统一止损、SELL 方向整篮平仓、加仓统一上移止损、不加仓时不放松已有更紧止损、router 把整篮平仓展开成每票一个 close。全量 148 passed |
| 不可达分支（原问题 #3、#4） | 删除 L119-137 的"保护+加仓"合并块（保留"保护优先、加仓下次调用再评估"的原意）与 L147-150 的重复硬止损判断 | 全量测试无回归；行为等价 |
| 策略硬上限（原问题 #10） | 新增模块常量 `MAX_UNITS = 4`；`_max_units()` 改为 `max(1, min(用户 max_positions, MAX_UNITS))`，`_maybe_add` 守卫与给 AI 的 payload 都使用该生效值 | 新增 4 个测试：用户设 6 时被压回 4、设 2 时生效、到达上限拒绝加仓、未到上限正常加仓 |
| 两套入场逻辑冲突 | `combined` 模式下，当波段与唐奇安给出**相反方向**时返回 HOLD 并说明两侧方向（原实现是波段静默胜出）；两套方向同时写入 `metadata` | 新增 2 个测试：冲突时空仓（打桩让两套相悖）、方向一致时正常开仓并记录两侧信号 |
| 参数集中（不改行为） | 把散落在调用点的写死数字提为模块级常量：`STOP_ATR`、`SWING_*`（含写死的 EMA5/10）、`PIN_BAR_WICK_RATIO`、`DEFAULT_*_PERIOD`、保本/移动/加仓与手数兜底值。已配 config 键的仍优先，常量作兜底 | 新增 1 个测试：把 `STOP_ATR` 打桩为 1.0，止损距离随之减半，证明常量真的生效而非残留字面量。全量 142 passed |
| 种子死键清理 | 删除 `stop_atr`、`risk_fraction`、`max_units`，随后一并删除无表单字段且 `auth_service` 不拷贝的 8 个键（`entry_period`、`exit_period`、`atr_period`、`add_step_atr`、`break_even_*`、`trailing_*`）。GL 种子从 18 个键精简为**只有 7 个真正生效的键** | 两条种子路径都是 `INSERT OR IGNORE` / `INSERT IGNORE`，**不会更新已有行**，故只影响新建库。全量 142 passed |
| 移除 `entry_mode` 开关 | 产品确定只要"两套并行"，删除该键的读取、`donchian` / `swing_only` 两个单模式分支与 `metadata.entry_mode`，并删除 `auth_service` 里的拷贝行 | 并行行为不变（原先恒为 `combined`）。测试改为不传该键；全量 142 passed |
| 保本价覆盖点差 | 保本目标不再等于开仓价：`offset = max(配置值, 当前点差 × BREAK_EVEN_SPREAD_BUFFER)`，默认缓冲系数 1.5，并夹到触发距离的一半以内，避免把止损放到触发位之外。理由文案与 action comment 会带出实际偏移 | 新增 3 个测试：多头保本带点差缓冲、配置的更大偏移优先、空头方向同样生效。全量 145 passed |
| 开仓接入 AI 风险闸门 | 产品要求"AI 必须参与"。新增 `turtle_open_risk_decision`（走开仓 AI）与 `_turtle_open_risk_system_prompt`；加仓提示词原本写"数据不清就否决"，改为默认放行。开仓与加仓统一采用**低门槛规则**：仅当 AI 明确 `risk_level=high` 才否决，否则服务端按程序规则执行并把结论/覆盖写入 `metadata.ai_risk` | 新增 8 个测试：通过、高风险的否决、非高风险否决被覆盖、文本 `"false"` 不被当作通过、AI 失败仍开仓、AI 不能改方向/手数/止损、加仓的覆盖与拦截。全量 156 passed |
| 持仓复核（提前平仓 + 加仓合并为一次调用） | 原 `turtle_add_decision` 只在有加仓候选时调用；改为 `turtle_position_review`，**每次持仓请求调用一次**，同时回答 `close_now`（止盈出局/反转离场）与 `allow_add`。提示词明确 AI 只负责这两件事、**不得建议止损位/移动止损/方向/价格/手数**；上下文补充 `favorable_atr`（已走多少倍 ATR）供其判断"涨够了"。并加防抖：开仓所在 K 线内不允许提前平仓 | 新增 5 个测试：AI 提前平仓整篮、防抖抑制且记录、无加仓候选时仍复核、复核上下文含 `favorable_atr`/`protective_stop_levels`。全量 161 passed |
| 复核顺序调整（保本/移动止损也先问 AI） | 原实现"保护动作优先"会导致**强势趋势里移动止损几乎每根 K 线都触发，于是几乎每根 K 线都不问 AI**——恰好在最该判断止盈的时候屏蔽了 AI。现改为：① 整篮止损/通道（安全阀，不等 AI）→ ② **每次都问 AI** → ③ 保本/移动止损 → ④ 加仓。延迟的只是"止损收紧"，兜底止损不受影响 | 新增 2 个测试：有移动止损待发时仍会问 AI 且照常改单；AI 说止盈时改单被止盈取代 |
| 返回 EA 的文案业务化 | "分析内容"不再出现内部术语（`combined`、唐奇安、小波段、Unit），改为业务语言：突破趋势 / 回调趋势 / 趋势转弱 / 浮盈保护 / 止盈离场；并把 **AI 判断内容拼进结论**（开仓追加"；AI 风险评估通过（风险正常）：…"，持仓追加"；AI 分析：…"），使 EA 面板能看到 AI 的判断，而不只是策略动作或只藏在 metadata 里 | 测试同步更新断言 + 新增 1 个用例；全量 163 passed |

修复后 `GL_TREND_V1` 从**零测试覆盖**变为 11 个测试；全量套件 **142 passed**。

### 参数策略（已与负责人确认）

用户端**只配置 AI 与开仓手数**，不会暴露策略参数。因此所有阈值作为模块级常量集中在 `turtle_agent.py` 顶部：既可读、调参只改一处，也不增加配置面；将来若真要放开，常量已经就是现成入口。

### 硬止损与通道出场的关系（更正）

> 本文档早前版本写过"通道平仓通常先触发"——那个结论是在自造的测试数据上得出的，**不成立**，在此更正。两者是**互相独立的两个保护位，哪个更紧哪个先生效**：

以多头为例：

| 条件 | 哪一个先生效 |
| --- | --- |
| `开仓价 − 2×ATR > 通道低点`（通道较窄、或入场价离通道低点超过 2 ATR） | **硬止损更紧，先生效** |
| `开仓价 − 2×ATR < 通道低点`（通道较宽） | 价格先跌破通道低点，**通道先触发**，硬止损成为更深一层的兜底 |

因为入场是突破通道高点（`开仓价 > 通道高点`），所以 `开仓价 − 通道低点 = 通道宽度 + 突破幅度`。**换句话说：出场窗口的宽度大于 2 ATR 时，2ATR 硬止损才是更紧的那一道。** 这在趋势行情里很常见，所以硬止损并非冗余。

代码里"先查通道、再查硬止损"的顺序只在**价格一次性跳空穿过两者**时决定理由文案（会记成通道平仓），不影响触发的是哪个止损位。

编写回归测试时，第一版硬止损用例失败正是因为夹具的通道只有 1 ATR 宽，于是走了通道分支。

### 官方种子配置的生效范围（重要）

已与负责人确认：admin 里的"官方策略配置"是**用户新建策略时表单的默认值**。据此核对，GL_TREND_V1 种子的 `default_config` 里只有一部分真正生效：

| 类别 | 键 | 是否生效 |
| --- | --- | --- |
| 有表单字段、被 `auth_service` 改名后拷贝进部署 | `position_sizing_mode`→`position_size_mode`、`fixed_lot`→`fixed_volume`/`lot`、`risk_mode`→`risk_base_mode`、`max_stop_amount`→`risk_amount`、`risk_percent`、`max_positions`、`allow_add_position`→`allow_add` | ✅ 生效（种子里**只保留这 7 个**） |
| 已删除：既无表单字段、`auth_service` 也不拷贝 | `stop_atr`、`risk_fraction`、`max_units`、`entry_period`、`exit_period`、`atr_period`、`add_step_atr`、`break_even_atr`、`break_even_offset`、`trailing_start_atr`、`trailing_distance_atr` | ❌ 已从种子移除。这些值当前只存在于 `turtle_agent.py` 顶部的常量 |
| 已删除：无表单字段的开关 | `entry_mode`（曾被 `auth_service` 拷贝，但恒为 `combined`） | ❌ 开关与两个单模式分支均已删除，只保留并行行为 |

结论：GL 的策略参数**唯一真相是 `turtle_agent.py` 顶部的常量**。若将来要让某个参数能按部署调整，必须同时补两处：后台表单字段 + `auth_service` 的拷贝映射（只做一处会出现"改了没反应"）。

（`PA_AGENT_V1` 的种子只含第一类的 7 个键，没有这个问题。）

### 仍未处理

- ✅ 已处理｜**PA_AGENT_V1 的同类上限问题**：PA 的加仓路径已整体移除（产品确认 PA 为单笔持仓、不加仓）。代码不再读取 `allow_add` / `max_positions`，客户表单上这两个开关对 PA 不再产生任何效果。
  - 移除前的事实依据：6 个部署开着 `allow_add`（上限 2/3/5）、约 1400 次持仓决策机会，**加仓触发 0 次**——服务端从不算加仓候选，只给 AI 一句模糊规则，模型几乎不可能主动返回 `add`。保留它等于给客户一个永远不生效的开关。
  - 现在 AI 若返回 `add`，服务端明确回 HOLD 并说明"本策略为单笔持仓，不支持加仓"，提示词也写明了 `add is not an available action`。
- **PA_AGENT_V1 的参数同样散落**：本次只整理了 `GL_TREND_V1`；PA 的打分阈值（70/12/0.8/1.2/1.5、penalty 上限 40 等）仍写在调用点，建议按同样方式提取。
- 种子里的 `max_units` 键现在**彻底成为死键**（代码已改为常量），建议与 `stop_atr`、`risk_fraction` 一并清理。

---

## 八、本报告的边界

- 已逐行确认：两策略的全部决策分支、配置读取、仓位计算、出场与保护逻辑，`_validated_position_modification`，以及 `auth_service` 的配置改名映射。
- 第六节条目均由我按行号复核后收录；PA 的 `_compute_features`（L585-800）内部特征计算细节由二次审计覆盖，我未逐行读该函数本体。
- 未做回测，无法判断上述阈值在具体品种/周期上的实际胜率。
