-- PA_AGENT_V1 观察用 SQL（每天跑一次，贴进客户端即可）
--
-- 背景：2026-09-18 之前，PA 的 AI 参数（order_type / entry_price / sl_price /
-- tp_price / estimated_win_rate）从未到达服务端，所以那个时期的数据是"每次都
-- 市价追"的残缺策略跑出来的，不能作为调参依据。9-18 起这些参数、追单保护、
-- 尖峰/美盘窗口、回吐保护才真正生效。
--
-- 使用：把 :deployment_id 换成 PA 部署的 id，时间起点按要对比的区间改。
--
-- ⚠️ 两个必读注意：
--   1) 统计口径：平均盈亏会被【个别大手数账户】带偏（曾有一笔 3 手黄金 -4197，
--      把 5 单的小时平均拉到 -804）。所以【胜率】和【每 0.01 手盈亏】才是主口径，
--      平均盈亏只在同一个部署内看才有意义。
--   2) 别名里带点号必须加反引号（`每0.01手盈亏`），否则 MySQL 报 #1064。

-- =====================================================================
-- ① 每日趋势（最先看这条：单数、胜率、平均、总盈亏）
-- 期望：胜率不一定上升（止盈变多会让胜率↑、盈亏比↓），看【总盈亏】与【期望】。
-- =====================================================================
SELECT DATE(created_at)                                    AS 日期,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(net_profit > 0) / COUNT(*) * 100, 1)      AS 胜率百分比,
       ROUND(AVG(net_profit), 2)                           AS 平均盈亏,
       ROUND(AVG(net_profit / NULLIF(volume, 0) * 0.01), 2) AS `每0.01手盈亏`,
       ROUND(SUM(net_profit), 2)                           AS 总盈亏
FROM mt5_history_deals
WHERE deployment_id = 'PA部署ID'
  AND entry IN ('out', 'out_by', 'inout')
  AND volume > 0
  AND close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY 日期 ORDER BY 日期;

-- =====================================================================
-- ② 按品种（找"结构性亏损"的品种 → 最省事的优化就是停掉它们）
-- 期望：如果某品种单数够（>=20）且期望长期为负，直接停止该品种。
-- =====================================================================
SELECT symbol                                              AS 品种,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(net_profit > 0) / COUNT(*) * 100, 1)      AS 胜率百分比,
       ROUND(AVG(net_profit), 2)                           AS 平均盈亏_易受大单影响,
       ROUND(AVG(net_profit / NULLIF(volume, 0) * 0.01), 2) AS `每0.01手盈亏`,
       ROUND(SUM(net_profit), 2)                           AS 总盈亏
FROM mt5_history_deals
WHERE deployment_id = 'PA部署ID'
  AND entry IN ('out', 'out_by', 'inout')
  AND volume > 0
  AND close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY 品种 ORDER BY 总盈亏;

-- =====================================================================
-- ③ 按小时（UTC）= 真实时区，服务端记录的 created_at 就是 UTC
-- 注意：成交时间（close_time）是【券商时钟】，UTC = close_time - 券商偏移
--       （夏令时一般 3、冬令时 2 —— 不确定就先按 3 算，结论只看相对差异）
-- =====================================================================
SELECT HOUR(FROM_UNIXTIME(close_time - 3 * 3600))          AS UTC小时,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(net_profit > 0) / COUNT(*) * 100, 1)      AS 胜率百分比,
       ROUND(AVG(net_profit), 2)                           AS 平均盈亏_易受大单影响,
       ROUND(AVG(net_profit / NULLIF(volume, 0) * 0.01), 2) AS `每0.01手盈亏`
FROM mt5_history_deals
WHERE deployment_id = 'PA部署ID'
  AND entry IN ('out', 'out_by', 'inout')
  AND volume > 0
  AND close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY UTC小时 ORDER BY UTC小时;

-- =====================================================================
-- ④ 防护触发次数（验证今天上线的功能是否真的在工作）
-- 期望：出现"已追离理想入场位"（追单保护）、"尖峰"（尖峰过滤）、
--       "止盈离场"（AI 主动止盈）、"浮盈回吐保护"（回吐保护）。
-- 全是 0 → 说明这些防护一次都没触发，要查原因。
-- =====================================================================
SELECT DATE(created_at) AS 日期,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%追离理想入场位%') AS 追单保护,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%尖峰%')           AS 尖峰过滤,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%止盈离场%')       AS AI主动止盈,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%浮盈回吐保护%')   AS 回吐保护,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%挂单失效取消%')   AS 挂单取消
FROM decisions
WHERE created_at >= '2026-09-18'
  AND JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.metadata.strategy_code')) = 'PA_AGENT_V1'
GROUP BY 日期 ORDER BY 日期;

-- =====================================================================
-- ⑤ 止损距离 ÷ ATR（看"止损是不是过紧"；需要 min_stop_distance 诊断字段）
-- =====================================================================
SELECT DATE(created_at) AS 日期,
       COUNT(*) AS 单数,
       ROUND(MIN(JSON_EXTRACT(response_json, '$.metadata.min_stop_distance_used')), 5) AS 最小距离,
       ROUND(MAX(JSON_EXTRACT(response_json, '$.metadata.min_stop_distance_enforced')), 5) AS 实际夹紧
FROM decisions
WHERE created_at >= '2026-09-18'
  AND JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.metadata.strategy_code')) = 'PA_AGENT_V1'
GROUP BY 日期 ORDER BY 日期;

-- =====================================================================
-- 观察纪律（避免用噪音下结论）
--   1. 每单盈亏差异很大 → 单数 < 20 时不要下结论；
--   2. 一次只改一个参数，改完观察 2~3 天再改下一个；
--   3. 先看【总盈亏】，再看胜率 —— 止盈变多会让胜率上升但盈亏比下降。
-- =====================================================================
