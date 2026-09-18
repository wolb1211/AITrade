-- PA_AGENT_V1 观察用 SQL（每天跑一次，贴进客户端即可）
--
-- 背景：2026-09-18 之前，PA 的 AI 参数（order_type / entry_price / sl_price /
-- tp_price / estimated_win_rate）从未到达服务端，所以那个时期的数据是"每次都
-- 市价追"的残缺策略跑出来的，不能作为调参依据。9-18 起这些参数、追单保护、
-- 尖峰/美盘窗口、回吐保护、尾盘封禁才真正生效。
--
-- 用法：直接整条执行，不用改任何东西。
--       统计口径是"所有 PA 部署"（即所有客户）；只看某个客户时在后面加
--       AND d.account_login = '客户账号'。
--
-- ⚠️ 三个必读注意：
--   1) 这张表混了账户上【所有 EA】的单，所以每次都必须 JOIN deployments 过滤
--      策略，否则别的 EA 的亏损会算到 PA 头上。
--   2) 平均盈亏会被【个别大手数账户】带偏（曾有一笔 3 手黄金 -4197，把 5 单的
--      小时平均拉到 -804）。所以【胜率】和【每 0.01 手盈亏】才是主口径，
--      平均盈亏只在同一个客户内看才有意义。
--   3) 别名里带点号必须加反引号（`每0.01手盈亏`），否则 MySQL 报 #1064。

-- =====================================================================
-- ① 每日趋势（最先看这条：单数、胜率、每 0.01 手盈亏、总盈亏）
-- 期望：胜率不一定上升（止盈变多会让胜率↑、盈亏比↓），所以四个数一起看。
-- =====================================================================
SELECT DATE(FROM_UNIXTIME(d.close_time))                    AS 日期,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(d.net_profit > 0) / COUNT(*) * 100, 1)     AS 胜率百分比,
       ROUND(AVG(d.net_profit / NULLIF(d.volume, 0) * 0.01), 2) AS `每0.01手盈亏`,
       ROUND(SUM(d.net_profit), 2)                         AS 总盈亏
FROM mt5_history_deals d
JOIN deployments dep ON dep.id = d.deployment_id
WHERE dep.strategy_code = 'PA_AGENT_V1'
  AND d.entry IN ('out', 'out_by', 'inout')
  AND d.volume > 0
  AND d.close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY 日期 ORDER BY 日期;

-- =====================================================================
-- ② 按品种（找"结构性亏损"的品种 → 最省事的优化就是停掉它们）
-- 期望：单数 >= 20 且【胜率】和【每 0.01 手盈亏】长期都差 → 直接停掉该品种。
-- =====================================================================
SELECT d.symbol                                             AS 品种,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(d.net_profit > 0) / COUNT(*) * 100, 1)     AS 胜率百分比,
       ROUND(AVG(d.net_profit / NULLIF(d.volume, 0) * 0.01), 2) AS `每0.01手盈亏`,
       ROUND(SUM(d.net_profit), 2)                         AS 总盈亏
FROM mt5_history_deals d
JOIN deployments dep ON dep.id = d.deployment_id
WHERE dep.strategy_code = 'PA_AGENT_V1'
  AND d.entry IN ('out', 'out_by', 'inout')
  AND d.volume > 0
  AND d.close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY 品种 ORDER BY 总盈亏;

-- =====================================================================
-- ③ 按小时（UTC）= 真实时区，服务端记录的 created_at 就是 UTC
-- 注意：成交时间（close_time）是【券商时钟】，UTC = close_time - 券商偏移
--       （夏令时一般 3、冬令时 2 —— 不确定就先按 3 算，只看相对差异）
-- 对应关系：UTC 17-19 = 北京 01:00-03:00 = 美东 13:00-15:00（已封禁 ✓）
-- =====================================================================
SELECT HOUR(FROM_UNIXTIME(d.close_time - 3 * 3600))         AS UTC小时,
       COUNT(*)                                            AS 单数,
       ROUND(SUM(d.net_profit > 0) / COUNT(*) * 100, 1)     AS 胜率百分比,
       ROUND(AVG(d.net_profit / NULLIF(d.volume, 0) * 0.01), 2) AS `每0.01手盈亏`
FROM mt5_history_deals d
JOIN deployments dep ON dep.id = d.deployment_id
WHERE dep.strategy_code = 'PA_AGENT_V1'
  AND d.entry IN ('out', 'out_by', 'inout')
  AND d.volume > 0
  AND d.close_time >= UNIX_TIMESTAMP('2026-09-18')
GROUP BY UTC小时 ORDER BY UTC小时;

-- =====================================================================
-- ④ 防护触发次数（验证上线的功能是否真的在工作）
-- 期望：出现"追离理想入场位"（追单保护）、"尖峰"（尖峰过滤）、
--       "止盈离场"（AI 主动止盈）、"浮盈回吐保护"（回吐保护）、
--       "尾盘清淡时段"（尾盘封禁）。
-- 全是 0 → 说明这些防护一次都没触发，要查原因。
-- =====================================================================
SELECT DATE(created_at) AS 日期,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%追离理想入场位%') AS 追单保护,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%尖峰%')           AS 尖峰过滤,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%止盈离场%')       AS AI主动止盈,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%浮盈回吐保护%')   AS 回吐保护,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%动力不足%')       AS 尾盘封禁,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%假信号%')         AS 高风险时段封禁,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.reason')) LIKE '%挂单失效取消%')   AS 挂单取消
FROM decisions
WHERE created_at >= '2026-09-18'
  AND JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.metadata.strategy_code')) = 'PA_AGENT_V1'
GROUP BY 日期 ORDER BY 日期;

-- =====================================================================
-- ⑤ 持仓时长（判断"进场质量"最强的指标）
-- 期望：修正前黄金亏损单平均只活 4.8 分钟；修正后应明显变长（波段本该几十分钟+）。
-- 如果"可算时长"是 0 → 说明 open_time 没值，需要在 EA 补上报。
-- =====================================================================
SELECT COUNT(*)                                            AS 总单数,
       SUM(d.open_time > 0)                                AS 有开仓时间的,
       SUM(d.open_time > 0 AND d.close_time > d.open_time) AS 可算持仓时长,
       ROUND(AVG(CASE WHEN d.open_time > 0 AND d.close_time > d.open_time
                      THEN (d.close_time - d.open_time) / 60.0 END), 1) AS 平均持仓分钟
FROM mt5_history_deals d
JOIN deployments dep ON dep.id = d.deployment_id
WHERE dep.strategy_code = 'PA_AGENT_V1'
  AND d.entry IN ('out', 'out_by', 'inout')
  AND d.close_time >= UNIX_TIMESTAMP('2026-09-18');

-- =====================================================================
-- 观察纪律（避免用噪音下结论）
--   1. 每单盈亏差异很大 → 单数 < 20 时不要下结论；
--   2. 一次只改一个参数，改完观察 2~3 天再改下一个；
--   3. 先看【胜率】和【每 0.01 手盈亏】，再看总盈亏。
-- =====================================================================
