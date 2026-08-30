# 自定义策略可视化工作流 V1

## 目标

流程图是策略的唯一正式逻辑。自然语言只用于生成流程图草稿，运行时不再直接解释原始输入框内容。

两种入口最终生成同一个 `workflow_json`：

1. 用户直接搭建流程。
2. 用户填写开仓与风控描述，由 AI 生成流程草稿，再由用户确认和修改。

## 基本约束

- 一个策略包含 `open`（开仓）和 `position`（持仓风控）两个独立流程。
- 每个流程只能有一个入口节点。
- 入口节点只有 `next` 分支。
- 条件和通用 AI 判断节点都有 `yes`、`no` 两个分支。
- 截图信息提取节点只有 `next` 分支；它只输出用户预先定义的枚举结果，不直接决定交易动作。
- 动作节点没有后续分支，执行后结束本次分析。
- 多个分支可以汇入同一个动作节点。
- 不允许循环、回连、断开节点和不可到达节点。
- 开仓流程不能读取持仓字段，也不能使用持仓风控动作。
- 工作流编辑位置保存在节点 `position` 中，但编译结果会移除界面位置。

## 节点类型

### 入口

- `entry/open`：开仓分析。
- `entry/position`：持仓风控。

### 精确条件

- `comparison`：指标、价格、K线、持仓字段和常量比较。
- `cross`：上穿或下破。
- `consecutive`：连续 N 根满足。
- `indicator_trend`：指标连续上升或下降。
- `candle_pattern`：吞没、Pin Bar、十字星等形态。
- `market_structure`：HH、HL、LH、LL。
- `breakout`：突破或回踩。
- `atr_distance`：ATR 倍数距离。
- `position_state`：方向、盈亏、开仓价、止损价、持仓数量。
- `group`：AND/OR 条件组。

### AI与截图节点

- `vision_extract`：从截图提取自定义指标、图形或文字事实，每次只能返回用户定义枚举中的一个结果；无法确认时返回固定兜底值。
- `ai_condition`：第一版暂时不能精确结构化的开放条件。该节点必须在后台标记，便于后续扩展节点库。

截图输出通过普通条件节点的 `vision_result` 条件引用。该条件只能选择流程上游截图节点已经定义的结果，不能填写自由文本。删除正在被引用的枚举结果或引用非上游截图节点都会导致工作流校验失败。

### 动作

开仓流程：`open_buy`、`open_sell`、`no_action`。

持仓流程：`close_all`、`close_partial`、`add_buy`、`add_sell`、`modify_sl`、`modify_tp`、`hold`。

## 数据模型原则

- `workflow_json` 是用户看到和修改的原始工作流，是唯一真实来源。
- `compiled_json` 由服务端从工作流生成，用于运行，不允许客户端直接编辑。
- `schema_version` 表示 JSON 结构版本。
- `workflow_version` 表示用户策略的修改版本。
- `source_text` 仅保留 AI 生成时的原始描述，不参与实际执行。

## 指标能力目录

指标选择、输出项和比较限制统一由服务端目录驱动，客户端不得自行维护另一套指标规则。每个指标输出必须定义：

- `component`、`title`：逻辑输出名和用户可见名称。
- `column_prefix`：对应 Pandas TA Classic 的实际输出列。
- `value_type`、`comparison_group`：输出语义及可兼容的比较组。
- `operators`、`condition_kinds`：允许的大于、小于、等于、交叉、连续和趋势判断。
- `right_operand_kinds`、`compatible_groups`：右侧允许使用常量、价格、K线或哪些指标输出。
- `minimum_points`：完成该类判断至少需要的有效数据点。
- `default_constant`、`constant_options`：数字比较默认值或方向、布尔状态选项。

例如，EMA 属于价格线，可以与价格、K线价格和其他价格线比较；MACD 柱状值只能与固定数字或兼容的 MACD 输出比较，不能与市场价格比较。目录由 `/custom-strategy/workflow/catalog` 和 `/custom-strategy/indicators` 返回。

## 编译结果

编译器会：

1. 验证节点、分支、动作范围和无循环结构。
2. 移除只服务于界面的节点坐标。
3. 生成节点映射和分支跳转表。
4. 汇总指标及其参数。
5. 计算 EA 至少需要提供的 K 线数量。
6. 判断是否需要截图和 AI。
7. 汇总 `ai_condition`，供后台展示未精确化条件。

V1 的 Pydantic Schema、节点目录、校验器和基础编译器位于 `app/services/custom_workflow.py`。
