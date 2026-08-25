-- GainLab AI Trading API - MySQL 5.7 schema
-- Database charset: utf8mb4
-- This file creates tables, constraints and indexes only. It does not insert seed data.

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS `users` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `email` VARCHAR(255) NULL,
  `password_hash` VARCHAR(255) NULL,
  `nickname` VARCHAR(100) NOT NULL DEFAULT '',
  `status` VARCHAR(32) NOT NULL DEFAULT 'pending_activation',
  `vip_level` INT NOT NULL DEFAULT 0,
  `vip_expires_at` VARCHAR(40) NOT NULL DEFAULT '',
  `max_strategy_keys` INT NOT NULL DEFAULT 10,
  `agent_level` INT NOT NULL DEFAULT 0,
  `invite_code` VARCHAR(32) NULL,
  `referrer_user_id` BIGINT NULL,
  `referred_at` VARCHAR(40) NOT NULL DEFAULT '',
  `ai_balance` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `email_verified_at` VARCHAR(40) NOT NULL DEFAULT '',
  `last_login_at` VARCHAR(40) NOT NULL DEFAULT '',
  `remark` TEXT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_email` (`email`),
  UNIQUE KEY `uk_users_invite_code` (`invite_code`),
  KEY `idx_users_status_vip` (`status`, `vip_level`),
  KEY `idx_users_referrer` (`referrer_user_id`, `created_at`),
  KEY `idx_users_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `system_settings` (
  `setting_key` VARCHAR(64) NOT NULL,
  `setting_value` VARCHAR(255) NOT NULL DEFAULT '',
  `remark` VARCHAR(255) NOT NULL DEFAULT '',
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`setting_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_balance_ledger` (
  `id` VARCHAR(64) NOT NULL,
  `user_id` BIGINT NOT NULL,
  `entry_type` VARCHAR(32) NOT NULL,
  `amount` DECIMAL(18,6) NOT NULL,
  `balance_before` DECIMAL(18,6) NOT NULL,
  `balance_after` DECIMAL(18,6) NOT NULL,
  `operator_id` VARCHAR(64) NOT NULL DEFAULT '',
  `reference_id` VARCHAR(128) NULL,
  `remark` VARCHAR(1000) NOT NULL DEFAULT '',
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_balance_ledger_reference` (`reference_id`),
  KEY `idx_ai_balance_ledger_user_time` (`user_id`, `created_at`),
  KEY `idx_ai_balance_ledger_type_time` (`entry_type`, `created_at`),
  CONSTRAINT `fk_ai_balance_ledger_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `email_verification_codes` (
  `id` VARCHAR(64) NOT NULL,
  `email` VARCHAR(255) NOT NULL,
  `purpose` VARCHAR(32) NOT NULL,
  `code_hash` VARCHAR(128) NOT NULL,
  `expires_at` VARCHAR(40) NOT NULL,
  `consumed_at` VARCHAR(40) NOT NULL DEFAULT '',
  `attempt_count` INT NOT NULL DEFAULT 0,
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_verification_email_purpose` (`email`, `purpose`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `user_sessions` (
  `id` VARCHAR(64) NOT NULL,
  `user_id` BIGINT NOT NULL,
  `token_hash` VARCHAR(128) NOT NULL,
  `expires_at` VARCHAR(40) NOT NULL,
  `revoked_at` VARCHAR(40) NOT NULL DEFAULT '',
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_sessions_token_hash` (`token_hash`),
  KEY `idx_user_sessions_user_id` (`user_id`, `created_at`),
  CONSTRAINT `fk_user_sessions_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `deployments` (
  `id` VARCHAR(64) NOT NULL,
  `user_id` VARCHAR(64) NOT NULL,
  `strategy_code` VARCHAR(64) NOT NULL,
  `strategy_name` VARCHAR(255) NOT NULL,
  `key_hash` VARCHAR(128) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `symbol` VARCHAR(64) NOT NULL DEFAULT '',
  `timeframe` VARCHAR(32) NOT NULL DEFAULT '',
  `mt_platform` VARCHAR(32) DEFAULT NULL,
  `mt_login` VARCHAR(64) DEFAULT NULL,
  `mt_server` VARCHAR(128) DEFAULT NULL,
  `config_json` LONGTEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_deployments_key_hash` (`key_hash`),
  KEY `idx_deployments_user_id` (`user_id`),
  KEY `idx_deployments_strategy_code` (`strategy_code`),
  KEY `idx_deployments_status` (`status`),
  KEY `idx_deployments_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `decisions` (
  `id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL,
  `endpoint` VARCHAR(64) NOT NULL,
  `request_id` VARCHAR(160) NOT NULL,
  `response_json` LONGTEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_decisions_request` (`deployment_id`, `endpoint`, `request_id`),
  KEY `idx_decisions_created_at` (`created_at`),
  CONSTRAINT `fk_decisions_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `heartbeats` (
  `deployment_id` VARCHAR(64) NOT NULL,
  `payload_json` LONGTEXT NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`deployment_id`),
  KEY `idx_heartbeats_updated_at` (`updated_at`),
  CONSTRAINT `fk_heartbeats_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `execution_reports` (
  `id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL,
  `decision_id` VARCHAR(64) NOT NULL,
  `payload_json` LONGTEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_execution_reports_deployment_id` (`deployment_id`),
  KEY `idx_execution_reports_decision_id` (`decision_id`),
  KEY `idx_execution_reports_created_at` (`created_at`),
  CONSTRAINT `fk_execution_reports_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `mt5_history_deals` (
  `id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL,
  `account_login` VARCHAR(64) NOT NULL,
  `account_server` VARCHAR(128) NOT NULL DEFAULT '',
  `deal_id` VARCHAR(64) NOT NULL,
  `order_id` VARCHAR(64) NOT NULL DEFAULT '',
  `position_id` VARCHAR(64) NOT NULL DEFAULT '',
  `symbol` VARCHAR(64) NOT NULL,
  `mt_type` VARCHAR(32) NOT NULL DEFAULT '',
  `entry` VARCHAR(32) NOT NULL DEFAULT '',
  `volume` DOUBLE NOT NULL DEFAULT 0,
  `price` DOUBLE NOT NULL DEFAULT 0,
  `open_price` DOUBLE NOT NULL DEFAULT 0,
  `close_price` DOUBLE NOT NULL DEFAULT 0,
  `profit` DOUBLE NOT NULL DEFAULT 0,
  `commission` DOUBLE NOT NULL DEFAULT 0,
  `swap` DOUBLE NOT NULL DEFAULT 0,
  `net_profit` DOUBLE NOT NULL DEFAULT 0,
  `deal_time` BIGINT NOT NULL DEFAULT 0,
  `open_time` BIGINT NOT NULL DEFAULT 0,
  `close_time` BIGINT NOT NULL DEFAULT 0,
  `comment` VARCHAR(255) NOT NULL DEFAULT '',
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_mt5_history_account_deal` (`account_login`, `account_server`, `deal_id`),
  KEY `idx_mt5_history_deployment_id` (`deployment_id`),
  KEY `idx_mt5_history_account` (`account_login`, `account_server`),
  KEY `idx_mt5_history_symbol` (`symbol`),
  KEY `idx_mt5_history_close_time` (`close_time`),
  KEY `idx_mt5_history_deployment_close` (`deployment_id`, `close_time`),
  KEY `idx_mt5_history_deployment_symbol_close` (`deployment_id`, `symbol`, `close_time`),
  KEY `idx_mt5_history_created_at` (`created_at`),
  CONSTRAINT `fk_mt5_history_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_providers` (
  `id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  `provider_type` VARCHAR(64) NOT NULL,
  `base_url` VARCHAR(255) NOT NULL DEFAULT '',
  `api_key` VARCHAR(600) NOT NULL DEFAULT '',
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `sort` INT NOT NULL DEFAULT 9999,
  `remark` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_providers_name` (`name`),
  KEY `idx_ai_providers_enabled_sort` (`enabled`, `sort`),
  KEY `idx_ai_providers_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_models` (
  `id` VARCHAR(64) NOT NULL,
  `provider_id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(150) NOT NULL,
  `display_name` VARCHAR(150) NOT NULL,
  `base_url` VARCHAR(255) NOT NULL DEFAULT '',
  `context_window` INT NOT NULL DEFAULT 0,
  `input_token_rate` DOUBLE NOT NULL DEFAULT 0,
  `output_token_rate` DOUBLE NOT NULL DEFAULT 0,
  `billing_multiplier` DOUBLE NOT NULL DEFAULT 1,
  `is_default` TINYINT(1) NOT NULL DEFAULT 0,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `sort` INT NOT NULL DEFAULT 9999,
  `remark` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_models_provider_name` (`provider_id`, `name`),
  KEY `idx_ai_models_enabled_default_sort` (`enabled`, `is_default`, `sort`),
  KEY `idx_ai_models_updated_at` (`updated_at`),
  CONSTRAINT `fk_ai_models_provider`
    FOREIGN KEY (`provider_id`) REFERENCES `ai_providers` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_templates` (
  `code` VARCHAR(64) NOT NULL,
  `name` VARCHAR(128) NOT NULL,
  `request_type` VARCHAR(64) NOT NULL DEFAULT 'openai_compatible',
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `remark` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_endpoints` (
  `id` VARCHAR(64) NOT NULL,
  `owner_type` VARCHAR(32) NOT NULL DEFAULT 'gl',
  `user_id` VARCHAR(64) NOT NULL DEFAULT '',
  `template_code` VARCHAR(64) NOT NULL DEFAULT 'openai_compatible',
  `name` VARCHAR(128) NOT NULL DEFAULT '',
  `base_url` VARCHAR(512) NOT NULL DEFAULT '',
  `model` VARCHAR(128) NOT NULL DEFAULT '',
  `api_key` VARCHAR(512) NOT NULL DEFAULT '',
  `strict_json` TINYINT(1) NOT NULL DEFAULT 1,
  `context_window` INT NOT NULL DEFAULT 0,
  `input_token_rate` DOUBLE NOT NULL DEFAULT 1,
  `output_token_rate` DOUBLE NOT NULL DEFAULT 1,
  `billing_multiplier` DOUBLE NOT NULL DEFAULT 1,
  `input_price_per_million` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `output_price_per_million` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `supports_vision` TINYINT(1) NOT NULL DEFAULT 0,
  `vision_test_status` VARCHAR(16) NOT NULL DEFAULT 'untested',
  `vision_tested_at` VARCHAR(40) NOT NULL DEFAULT '',
  `vision_test_error` VARCHAR(500) NOT NULL DEFAULT '',
  `is_default` TINYINT(1) NOT NULL DEFAULT 0,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `selectable_by_user` TINYINT(1) NOT NULL DEFAULT 0,
  `sort` INT NOT NULL DEFAULT 9999,
  `remark` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_endpoints_owner` (`owner_type`, `user_id`),
  KEY `idx_ai_endpoints_enabled_sort` (`enabled`, `sort`),
  KEY `idx_ai_endpoints_model` (`model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_user_quotas` (
  `user_id` VARCHAR(64) NOT NULL,
  `monthly_quota` BIGINT NOT NULL DEFAULT 0,
  `extra_quota` BIGINT NOT NULL DEFAULT 0,
  `used_tokens` BIGINT NOT NULL DEFAULT 0,
  `reset_at` VARCHAR(40) NOT NULL DEFAULT '',
  `remark` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`user_id`),
  KEY `idx_ai_user_quotas_reset_at` (`reset_at`),
  KEY `idx_ai_user_quotas_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_usage_logs` (
  `id` VARCHAR(64) NOT NULL,
  `user_id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL DEFAULT '',
  `strategy_code` VARCHAR(64) NOT NULL DEFAULT '',
  `endpoint` VARCHAR(64) NOT NULL DEFAULT '',
  `provider_id` VARCHAR(64) NOT NULL DEFAULT '',
  `model_id` VARCHAR(64) NOT NULL DEFAULT '',
  `input_tokens` BIGINT NOT NULL DEFAULT 0,
  `output_tokens` BIGINT NOT NULL DEFAULT 0,
  `total_tokens` BIGINT NOT NULL DEFAULT 0,
  `official_tokens` BIGINT NOT NULL DEFAULT 0,
  `custom_tokens` BIGINT NOT NULL DEFAULT 0,
  `billing_source` VARCHAR(16) NOT NULL DEFAULT '',
  `input_price_snapshot` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `output_price_snapshot` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `charged_amount` DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
  `balance_after` DECIMAL(18,6) NULL,
  `success` TINYINT(1) NOT NULL DEFAULT 1,
  `error_message` TEXT NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_usage_user_id` (`user_id`),
  KEY `idx_ai_usage_deployment_id` (`deployment_id`),
  KEY `idx_ai_usage_strategy_code` (`strategy_code`),
  KEY `idx_ai_usage_model_id` (`model_id`),
  KEY `idx_ai_usage_created_at` (`created_at`),
  KEY `idx_ai_usage_user_time` (`user_id`, `created_at`),
  KEY `idx_ai_usage_user_model_time` (`user_id`, `model_id`, `created_at`),
  KEY `idx_ai_usage_user_deployment_time` (`user_id`, `deployment_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_usage_monthly_summaries` (
  `user_id` VARCHAR(64) NOT NULL,
  `month_key` CHAR(7) NOT NULL,
  `model_id` VARCHAR(128) NOT NULL DEFAULT '',
  `provider_id` VARCHAR(128) NOT NULL DEFAULT '',
  `deployment_id` VARCHAR(128) NOT NULL DEFAULT '',
  `strategy_code` VARCHAR(128) NOT NULL DEFAULT '',
  `billing_source` VARCHAR(16) NOT NULL DEFAULT '',
  `calls` BIGINT NOT NULL DEFAULT 0,
  `success_calls` BIGINT NOT NULL DEFAULT 0,
  `input_tokens` BIGINT NOT NULL DEFAULT 0,
  `output_tokens` BIGINT NOT NULL DEFAULT 0,
  `official_tokens` BIGINT NOT NULL DEFAULT 0,
  `custom_tokens` BIGINT NOT NULL DEFAULT 0,
  `charged_amount` DECIMAL(24,6) NOT NULL DEFAULT 0.000000,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`user_id`, `month_key`, `model_id`, `deployment_id`, `billing_source`),
  KEY `idx_ai_usage_monthly_user_month` (`user_id`, `month_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `official_ai_strategies` (
  `id` VARCHAR(64) NOT NULL,
  `code` VARCHAR(64) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `badge` VARCHAR(64) NOT NULL DEFAULT 'Gainlab',
  `version` VARCHAR(32) NOT NULL DEFAULT '1.0',
  `status` VARCHAR(32) NOT NULL DEFAULT 'active',
  `summary` LONGTEXT NOT NULL,
  `open_logic` LONGTEXT NOT NULL,
  `position_logic` LONGTEXT NOT NULL,
  `open_data_type` VARCHAR(32) NOT NULL DEFAULT 'kline',
  `open_kline_count` INT NOT NULL DEFAULT 100,
  `position_data_type` VARCHAR(32) NOT NULL DEFAULT 'kline',
  `position_kline_count` INT NOT NULL DEFAULT 100,
  `call_mode` VARCHAR(32) NOT NULL DEFAULT 'bar',
  `call_value` INT NOT NULL DEFAULT 1,
  `open_model_id` VARCHAR(64) NOT NULL DEFAULT '',
  `position_model_id` VARCHAR(64) NOT NULL DEFAULT '',
  `open_ai_endpoint_id` VARCHAR(64) NOT NULL DEFAULT '',
  `position_ai_endpoint_id` VARCHAR(64) NOT NULL DEFAULT '',
  `default_config_json` LONGTEXT NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `sort` INT NOT NULL DEFAULT 9999,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_official_ai_strategies_code` (`code`),
  KEY `idx_official_ai_strategies_enabled_sort` (`enabled`, `sort`),
  KEY `idx_official_ai_strategies_status` (`status`),
  KEY `idx_official_ai_strategies_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ea_downloads` (
  `id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `description` TEXT NOT NULL,
  `oss_url` VARCHAR(1000) NOT NULL,
  `file_name` VARCHAR(255) NOT NULL DEFAULT '',
  `file_size` BIGINT NOT NULL DEFAULT 0,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `sort` INT NOT NULL DEFAULT 9999,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ea_downloads_enabled_sort` (`enabled`, `sort`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `guide_articles` (
  `id` VARCHAR(64) NOT NULL,
  `title` VARCHAR(255) NOT NULL,
  `summary` VARCHAR(1000) NOT NULL DEFAULT '',
  `content_json` LONGTEXT NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `sort` INT NOT NULL DEFAULT 9999,
  `created_at` VARCHAR(40) NOT NULL,
  `updated_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_guide_articles_enabled_sort` (`enabled`, `sort`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `deployment_activity_logs` (
  `id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL,
  `strategy_code` VARCHAR(64) NOT NULL DEFAULT '',
  `event_type` VARCHAR(64) NOT NULL,
  `created_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_deployment_activity_deployment_id` (`deployment_id`),
  KEY `idx_deployment_activity_strategy_code` (`strategy_code`),
  KEY `idx_deployment_activity_event_type` (`event_type`),
  KEY `idx_deployment_activity_created_at` (`created_at`),
  CONSTRAINT `fk_deployment_activity_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `deployment_accounts` (
  `id` VARCHAR(64) NOT NULL,
  `deployment_id` VARCHAR(64) NOT NULL,
  `user_id` VARCHAR(64) NOT NULL DEFAULT '',
  `login` VARCHAR(64) NOT NULL,
  `platform` VARCHAR(32) NOT NULL DEFAULT 'MT5',
  `provider` VARCHAR(128) NOT NULL DEFAULT '',
  `server` VARCHAR(128) NOT NULL DEFAULT '',
  `is_demo` TINYINT(1) NOT NULL DEFAULT 0,
  `first_seen_at` VARCHAR(40) NOT NULL,
  `last_seen_at` VARCHAR(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_deployment_accounts_account` (`deployment_id`, `login`, `server`),
  KEY `idx_deployment_accounts_user_id` (`user_id`),
  KEY `idx_deployment_accounts_login_server` (`login`, `server`),
  KEY `idx_deployment_accounts_last_seen_at` (`last_seen_at`),
  CONSTRAINT `fk_deployment_accounts_deployment`
    FOREIGN KEY (`deployment_id`) REFERENCES `deployments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
