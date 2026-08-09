-- GainLab AI Trading API - official seed data
-- Run this after docs/mysql_schema.sql.
-- This file seeds official AI providers/models/strategies only. It does not create user deployments or trading history.

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ai_providers
INSERT INTO `ai_providers` (`id`, `name`, `provider_type`, `base_url`, `api_key`, `enabled`, `sort`, `remark`, `created_at`, `updated_at`) VALUES
  ('aip_2e13091b4d7f42c486224ab7af11fa8e', '通义千问', 'openai_compatible', 'https://llm-dlmbkhliz5yh01cw.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', '', 1, 10, '阿里云百炼/通义千问 OpenAI 兼容接口，官方 Key 请在后台配置', '2026-07-28T15:53:04.741200+00:00', '2026-07-28T15:53:04.741200+00:00'),
  ('aip_905ad1d8a50c477c9675dca7ff5fbf67', 'DeepSeek', 'openai_compatible', '', '', 1, 20, 'DeepSeek OpenAI 兼容接口，官方 Key 可在这里配置', '2026-08-07T17:00:12.268828+00:00', '2026-08-07T17:00:12.268828+00:00'),
  ('aip_ca975619885047faadccb0a3da59578a', 'OpenAI', 'openai_compatible', '', '', 1, 30, 'OpenAI 官方接口，官方 Key 可在这里配置', '2026-08-07T17:00:12.274266+00:00', '2026-08-07T17:00:12.274266+00:00')
ON DUPLICATE KEY UPDATE
  `name` = VALUES(`name`),
  `provider_type` = VALUES(`provider_type`),
  `base_url` = VALUES(`base_url`),
  `api_key` = VALUES(`api_key`),
  `enabled` = VALUES(`enabled`),
  `sort` = VALUES(`sort`),
  `remark` = VALUES(`remark`),
  `created_at` = VALUES(`created_at`),
  `updated_at` = VALUES(`updated_at`);

-- ai_models
INSERT INTO `ai_models` (`id`, `provider_id`, `name`, `display_name`, `base_url`, `context_window`, `input_token_rate`, `output_token_rate`, `billing_multiplier`, `is_default`, `enabled`, `sort`, `remark`, `created_at`, `updated_at`) VALUES
  ('aim_aa1e10263e184318b9d4db5f065c152b', 'aip_2e13091b4d7f42c486224ab7af11fa8e', 'qwen-turbo', '通义千问 Turbo', 'https://llm-dlmbkhliz5yh01cw.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 128000, 0.5, 0.5, 0.5, 0, 1, 8, '速度优先，适合低成本高频分析', '2026-08-07T17:00:12.284642+00:00', '2026-08-07T17:00:12.284642+00:00'),
  ('aim_675d1f815f1f4243a528ba1846ca3d84', 'aip_2e13091b4d7f42c486224ab7af11fa8e', 'qwen-plus', '通义千问 Plus', 'https://llm-dlmbkhliz5yh01cw.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 128000, 1.0, 1.0, 1.0, 1, 1, 10, '默认平衡模型，适合交易分析测试', '2026-07-28T15:53:04.750051+00:00', '2026-08-07T17:00:12.292387+00:00'),
  ('aim_66624e385011443f9e960b9439e96b85', 'aip_2e13091b4d7f42c486224ab7af11fa8e', 'qwen-max', '通义千问 Max', 'https://llm-dlmbkhliz5yh01cw.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 128000, 2.0, 2.0, 2.0, 0, 1, 12, '能力优先，适合复杂行情分析', '2026-08-07T17:00:12.297093+00:00', '2026-08-07T17:00:12.297093+00:00'),
  ('aim_ee4e9bf74c67415d8e19b0bd515fb714', 'aip_905ad1d8a50c477c9675dca7ff5fbf67', 'deepseek-chat', 'DeepSeek Chat', 'https://api.deepseek.com', 64000, 0.8, 0.8, 0.8, 0, 1, 20, 'DeepSeek 通用对话模型，OpenAI 兼容', '2026-08-07T17:00:12.311057+00:00', '2026-08-07T17:00:12.311057+00:00'),
  ('aim_d9501467642044fbb56089e1886f9c41', 'aip_905ad1d8a50c477c9675dca7ff5fbf67', 'deepseek-reasoner', 'DeepSeek Reasoner', 'https://api.deepseek.com', 64000, 1.2, 1.2, 1.2, 0, 1, 22, 'DeepSeek 推理模型，适合复杂判断', '2026-08-07T17:00:12.316017+00:00', '2026-08-07T17:00:12.316017+00:00'),
  ('aim_696c9adb526f4ac88ce37a987a0de607', 'aip_ca975619885047faadccb0a3da59578a', 'gpt-4.1-mini', 'GPT-4.1 Mini', 'https://api.openai.com/v1', 1000000, 2.0, 2.0, 2.0, 0, 1, 30, 'OpenAI 平衡模型，适合较复杂分析', '2026-08-07T17:00:12.319670+00:00', '2026-08-07T17:00:12.319670+00:00'),
  ('aim_28f1b4b0c6d1471b9dfeba8347d64cb2', 'aip_ca975619885047faadccb0a3da59578a', 'gpt-4.1', 'GPT-4.1', 'https://api.openai.com/v1', 1000000, 4.0, 4.0, 4.0, 0, 1, 32, 'OpenAI 高能力模型，适合高精度分析', '2026-08-07T17:00:12.323441+00:00', '2026-08-07T17:00:12.323441+00:00'),
  ('aim_f0bfe4a1d77e413194e546fc40679c0d', 'aip_ca975619885047faadccb0a3da59578a', 'gpt-4o-mini', 'GPT-4o Mini', 'https://api.openai.com/v1', 128000, 1.0, 1.0, 1.0, 0, 1, 34, 'OpenAI 低成本模型，适合高频调用', '2026-08-07T17:00:12.327139+00:00', '2026-08-07T17:00:12.327139+00:00')
ON DUPLICATE KEY UPDATE
  `provider_id` = VALUES(`provider_id`),
  `name` = VALUES(`name`),
  `display_name` = VALUES(`display_name`),
  `base_url` = VALUES(`base_url`),
  `context_window` = VALUES(`context_window`),
  `input_token_rate` = VALUES(`input_token_rate`),
  `output_token_rate` = VALUES(`output_token_rate`),
  `billing_multiplier` = VALUES(`billing_multiplier`),
  `is_default` = VALUES(`is_default`),
  `enabled` = VALUES(`enabled`),
  `sort` = VALUES(`sort`),
  `remark` = VALUES(`remark`),
  `created_at` = VALUES(`created_at`),
  `updated_at` = VALUES(`updated_at`);

-- official_ai_strategies
INSERT INTO `official_ai_strategies` (`id`, `code`, `name`, `badge`, `version`, `status`, `summary`, `open_logic`, `position_logic`, `open_data_type`, `open_kline_count`, `position_data_type`, `position_kline_count`, `call_mode`, `call_value`, `open_model_id`, `position_model_id`, `default_config_json`, `enabled`, `sort`, `created_at`, `updated_at`) VALUES
  ('ofs_pa_agent_v1', 'PA_AGENT_V1', 'Gainlab-PA BreakTrend Autopilot（PA突破趋势自动驾驶）', 'Gainlab', '1.0', 'active', '本策略由 PA Agent（AI 分析代理）驱动，结合最新K线形态与关键指标，自动判断当前行情更可能属于以下三类状态之一：
突破（Breakout）：识别有效突破信号，跟随趋势推进，并执行进场开仓。
趋势（Trend）：当指标呈现顺势结构时，倾向趋势跟随，减少反复进出干扰。
震荡（Range）：当价格波动缺乏趋势延续条件时，策略降低追单频率，避免震荡段的无效开仓。
在完成行情判别后，策略会：
自动下单：生成符合当前行情结构的开仓方向与执行节奏；
设置止盈止损（TP/SL）：依据策略配置与风险框架动态生成目标与保护价；
风控提前平仓：若价格运行未按预期发展（例如突破失败、趋势退化或条件反转），将触发提前风控平仓，降低回撤扩大的概率。
整体目标是让交易决策具备“形态理解 + 指标过滤 + 风险约束”的闭环能力，在不同市场状态下实现更稳健的执行。', '先计算K线实体、影线、重叠、EMA、ATR、区间位置和突破状态，再判断是否出现趋势突破或趋势延续开仓机会。', '结合反向 PA 信号、浮动盈亏和结构止损位判断是否平仓或移动止损，后续接入 AI 后由两阶段分析输出统一风控动作。', 'kline', 100, 'kline', 100, 'bar', 1, 'aim_675d1f815f1f4243a528ba1846ca3d84', 'aim_675d1f815f1f4243a528ba1846ca3d84', '{"position_sizing_mode": "fixed", "fixed_lot": 0.01, "risk_mode": "fixed_stop_amount", "max_stop_amount": 100, "max_positions": 1, "allow_add_position": false}', 1, 10, '2026-08-07T14:07:32.459835+00:00', '2026-08-07 15:45:43')
ON DUPLICATE KEY UPDATE
  `code` = VALUES(`code`),
  `name` = VALUES(`name`),
  `badge` = VALUES(`badge`),
  `version` = VALUES(`version`),
  `status` = VALUES(`status`),
  `summary` = VALUES(`summary`),
  `open_logic` = VALUES(`open_logic`),
  `position_logic` = VALUES(`position_logic`),
  `open_data_type` = VALUES(`open_data_type`),
  `open_kline_count` = VALUES(`open_kline_count`),
  `position_data_type` = VALUES(`position_data_type`),
  `position_kline_count` = VALUES(`position_kline_count`),
  `call_mode` = VALUES(`call_mode`),
  `call_value` = VALUES(`call_value`),
  `open_model_id` = VALUES(`open_model_id`),
  `position_model_id` = VALUES(`position_model_id`),
  `default_config_json` = VALUES(`default_config_json`),
  `enabled` = VALUES(`enabled`),
  `sort` = VALUES(`sort`),
  `created_at` = VALUES(`created_at`),
  `updated_at` = VALUES(`updated_at`);

SET FOREIGN_KEY_CHECKS = 1;
