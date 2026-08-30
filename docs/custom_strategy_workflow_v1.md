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
- 条件、截图识别和通用 AI 判断节点都有 `yes`、`no` 两个分支。
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

### AI条件

- `vision_condition`：从截图识别自定义指标、图形或文字信号，输出结构化识别结果。
- `ai_condition`：第一版暂时不能精确结构化的开放条件。该节点必须在后台标记，便于后续扩展节点库。

### 动作

开仓流程：`open_buy`、`open_sell`、`no_action`。

持仓流程：`close_all`、`close_partial`、`add_buy`、`add_sell`、`modify_sl`、`modify_tp`、`hold`。

## 数据模型原则

- `workflow_json` 是用户看到和修改的原始工作流，是唯一真实来源。
- `compiled_json` 由服务端从工作流生成，用于运行，不允许客户端直接编辑。
- `schema_version` 表示 JSON 结构版本。
- `workflow_version` 表示用户策略的修改版本。
- `source_text` 仅保留 AI 生成时的原始描述，不参与实际执行。

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
