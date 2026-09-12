"""Price-action knowledge base for the PA Agent strategy.

The text below is written for this project and routes by *detected* market state
rather than being injected wholesale, so a decision call only carries the
playbooks that can actually apply to the bar being evaluated.

Layout:
  DIAGNOSIS_FRAMEWORK  - stage 1 taxonomy + transitions + gates
  CYCLE_PLAYBOOKS      - one entry per market cycle state
  DIRECTION_PLAYBOOKS  - trend-direction addenda for stage 2
  SETUP_PLAYBOOKS      - named price-action setups, routed by detected pattern
  EXECUTION_RULES      - stop / target / sizing invariants for stage 2
"""

from __future__ import annotations

from typing import Any

# ── Stage 1 taxonomy ──────────────────────────────────────────────────────────

DIAGNOSIS_FRAMEWORK = """\
# 市场诊断框架

## 目标
在给出任何交易决策之前，先把当前图表归类到唯一一个周期状态，并说明该状态正在向哪个状态演化。

## 周期状态（八选一，互斥）
1. 极速行情（spike）：连续数根同向大实体趋势棒，几乎无回撤，波动显著放大。此时追单风险最高，回撤往往又快又深。
2. 微型通道（micro_channel）：极少数（1-3 根）K 线构成的陡峭单边推进，结构极窄，一旦反向一根就把整段走完。
3. 窄通道（tight_channel）：回撤不超过上一根 K 线，价格贴着均线单边推进，属于强趋势中最稳的一段。
4. 普通通道（normal_channel）：回撤 1-3 根后继续同向，节奏清晰，是顺势入场的主要环境。
5. 宽通道（broad_channel）：回撤深、双向都有像样波段，方向仍明确但需要等更好的位置，追单容易被甩。
6. 趋势型震荡（trending_tr）：整体向上/向下倾斜，但内部充满双向大棒，方向与噪音并存。
7. 交易区间（trading_range）：上下边界清晰、来回穿越，中间区域没有优势。
8. 极端震荡（extreme_tr）：上下影线长、重叠严重、方向频繁翻转，任何方向都没有可用的跟随。

## 判定要点
- 先看最近 10-20 根的实体大小、重叠比例与回撤深度，再看均线斜率与位置（在均线上方还是下方、距离多远）。
- 通道的"窄/普通/宽"由**回撤深度**决定，不由涨跌幅决定。
- 大背景方向与最近几根的方向可能不同（回撤发生在更大的趋势里），两者都要报出来。
- 区间边界被有效突破后，状态通常先转为通道，而不是直接转为极速行情。

## 状态转移与风险
- 极速行情 → 宽通道/区间：第一段回撤常被误判为反转，实际多为获利了结。
- 窄通道 → 普通通道：健康的推进；窄通道 → 区间：趋势动力衰竭的信号。
- 区间 → 通道：必须有收盘价突破边界并跟随，否则是假突破。
- 任何状态下出现**连续同向动能棒后突然反向大棒**，都要把转移风险调高。

## 阶段闸门（未通过则不进入下单决策）
- 闸门一：状态是否属于"无交易环境"（极端震荡，或铁丝网式的密集重叠）？是 → 不下单。
- 闸门二：方向是否明确？背景方向、最近方向、结构方向是否互相冲突？冲突 → 不下单。
- 闸门三：是否处在极端位置（极速行情末端、区间边界反复被打穿、连续多根同向后的第一次反向棒）？是 → 至少不要市价追单。
- 闸门四：止损位置是否清晰可定义？若找不到明确的结构失效点 → 不下单。

## 输出要求
- 给出唯一周期状态、八状态概率分布（总和 100）、方向偏向，以及四个闸门的通过情况与理由。
- 诊断结论只描述市场，不给出价格，也不给出买卖动作。"""


# ── Always-on stage-2 blocks ──────────────────────────────────────────────────

BAR_CHECKLIST = """\
# 逐棒检查单（每次决策按顺序过一遍）

1. 最后一根 K 线是什么类型：趋势棒、十字星、内包棒，还是反转棒？
2. 它的收盘位置在哪：收在高点附近（多头强）、低点附近（空头强），还是中部（无优势）？
3. 它和前面 1-3 根的关系：延续、被包含，还是吞没？被包含说明动能不足。
4. 有没有跟随：上一根的方向棒之后，这一根是否继续同向？没有跟随的突破不可信。
5. 当前价格相对最近结构的距离：是刚离开结构（入场成本低），还是已经走远（追单）？
6. 若以上有任何一条指向"动能不足或位置已远"，就不要开仓，或改用限价单等回撤。"""


SIGNAL_BAR_RULES = """\
# 信号棒判读

- 实体占比（实体 / 全幅）：大于 0.5 算健康，小于 0.25 视为犹豫，不应作为入场依据。
- 收盘位置（(收-低) / (高-低)）：做多要收在 0.7 以上，做空要收在 0.3 以下。
- 上影线长 → 上方有抛压，做多要减分；下影线长 → 下方有承接，做空要减分。
- 反方向的长影线出现在关键位置（区间边界、前高前低）时，是反转的早期信号。
- 信号棒出现后必须有跟随。只有信号棒没有跟随时，把入场推迟到下一根确认。
- 内包棒、十字星、连续重叠的小实体：不构成入场信号。"""


# ── Cycle playbooks ───────────────────────────────────────────────────────────

CYCLE_PLAYBOOKS: dict[str, str] = {
    "spike": """\
## 极速行情
- 特征：连续同向大实体、几乎无回撤、波动放大。
- 识别顶点的三条线索：出现第一根反向趋势棒、随后一根未能创出新高/新低、K 线实体开始缩短而影线变长。
- 交易含义：这是最容易亏钱的位置。第一段回撤之前不追单。
- 入场：等回撤出现并完成（至少一根反向棒被下一根收复），或等回撤后的二次入场信号。
- 止损：放在极速行情起点之外，不能用固定点数，因为波动已被放大。
- 目标：极速行情后的第一次反转通常只能吃到一段，不要期待趋势直接延续。
- 明确禁止：在极速行情中段市价追进。若判断已是末端，宁可不下单。""",
    "micro_channel": """\
## 微型通道
- 特征：1-3 根陡峭单边棒，结构极窄，没有像样的回撤。
- 交易含义：结构太薄，任何反向棒都可能直接吃掉整段行情，风险回报比不可控。
- 入场：优先放弃；若要参与，只能在下一根回撤到前一根区间内部时用小仓位试。
- 止损：必须放在整段微型通道之外，因此距离很远——这本身就是不该做的理由。
- 原则：微型通道里的"顺势"往往是在给早入场的人做流动性。""",
    "tight_channel": """\
## 窄通道
- 特征：回撤不超过上一根 K 线，价格贴均线单边推进。
- 交易含义：最强的一段趋势，但也最没有回撤可等，容易被"等更好的价格"错过。
- 入场：小回撤即可介入，或在前一根 K 线被收复时顺势进。
- 止损：放在最近一根回撤棒的极值之外，通常较近，风险可控。
- 目标：窄通道常延续较久，可以让利润奔跑，但一旦出现第一根像样的反向棒就要收紧保护。
- 注意：窄通道末端的加速段属于极速行情，追进去就是把风险放到最大。""",
    "normal_channel": """\
## 普通通道
- 特征：回撤 1-3 根后继续同向，节奏清晰。
- 交易含义：顺势入场的主要环境，也是本策略最愿意出手的状态。
- 入场：回撤到前一段结构的支撑/阻力处，出现同向信号棒后进；突破前高/前低后可等回踩确认。
- 止损：结构失效点之外，保证风险是"可定义"的。
- 目标：通道对侧边界或测量移动目标；到达后不恋战。
- 加分：背景方向与最近方向一致、结构为更高的高点配更高的低点（或反之）。""",
    "broad_channel": """\
## 宽通道
- 特征：回撤深、双向都有像样波段，方向仍明确。
- 交易含义：方向可以做，但位置要求更高，追单必定吃亏。
- 入场：只在通道下沿（做多）或上沿（做空）附近、且出现同向信号棒时进。
- 止损：放在通道边界之外；因为回撤深，止损距离也大，仓位应相应缩小。
- 目标：通道对侧，但预期收益要按"只能吃到通道的一部分"来估。
- 风险：宽通道里的反向波段经常被误读为反转。""",
    "trending_tr": """\
## 趋势型震荡
- 特征：整体倾斜，但内部双向大棒并存，方向与噪音同时存在。
- 交易含义：可以做，但必须只做与倾斜方向一致的那一边，且只做边界位置。
- 入场：回到倾斜方向的有利边界并出现信号棒。
- 止损：结构之外，且要容忍被大棒扫一下。
- 目标：不宜设太远，区间中轴附近就该兑现。
- 明确禁止：在中轴附近开仓，那里没有优势。""",
    "trading_range": """\
## 交易区间
- 特征：上下边界清晰、价格来回穿越，中轴区域没有方向。
- 交易含义：只有在边界处才有优势，中轴一律不做。
- 入场：靠近边界并出现反向信号棒（区间边缘的反转尝试），或边界被有效突破并跟随。
- 止损：边界之外一点，紧贴结构。
- 目标：区间中轴到对侧边界。
- 注意：边界被反复测试后，第三次以上的测试更可能是突破；此时不要继续在边界处逆势做。""",
    "extreme_tr": """\
## 极端震荡
- 特征：长影线、实体重叠严重、方向频繁翻转。
- 交易含义：这是"无交易环境"。
- 唯一动作：不下单。已有的仓位靠保护性止损管理。
- 若被判为铁丝网（连续多根重叠小实体），同样不做——等待第一根放量的方向棒打破僵局。""",
}


# ── Direction addenda ─────────────────────────────────────────────────────────

DIRECTION_PLAYBOOKS: dict[str, str] = {
    "bullish": """\
## 多头方向附加规则
- 优先做多，只在多头结构里找入场：更高的高点配合更高的低点。
- 做多的加分项：价格在均线上方、背景为多头、最近方向为多头、回调幅度小于前一段上涨。
- 做多的减分项：连续多根多头棒之后的第一次大幅反转向下、上方紧邻明显阻力、处于区间上沿。
- 反向做空只在结构明确破坏（更低的高点出现并被确认）后考虑，不因为"涨太多"而逆势。""",
    "bearish": """\
## 空头方向附加规则
- 优先做空，只在空头结构里找入场：更低的高点配合更低的低点。
- 做空的加分项：价格在均线下方、背景为空头、最近方向为空头、反弹幅度小于前一段下跌。
- 做空的减分项：连续多根空头棒之后的第一次大幅反转向上、下方紧邻明显支撑、处于区间下沿。
- 反向做多只在结构明确破坏（更高的低点出现并被确认）后考虑，不因为"跌太多"而逆势。""",
    "neutral": """\
## 方向不明确
- 背景方向、最近方向与结构方向互相冲突时，方向视为不明确。
- 方向不明确时不下单：本策略不能在方向未定时靠"猜"获利。""",
}


# ── Named setups ──────────────────────────────────────────────────────────────

SETUP_PLAYBOOKS: dict[str, str] = {
    "breakout": """\
## 突破与突破回踩
- 有效突破 = 收盘价越过边界 + 跟随动能，缺一不可。盘中刺穿不算突破。
- 判定假突破的三条（满足任一即为可疑）：
  a) 突破棒实体占比低于 0.5，或收盘又回到边界内侧；
  b) 突破后 1-2 根内出现反向吞没棒；
  c) 突破发生在边界已被测试三次以上之后，且这次没有放量。
- 突破后立刻回踩边界不破，是风险最低的顺势入场点，止损紧贴边界。
- 边界被反复测试三次以上后的突破更可能成立；首次测试的突破更可能是假突破。
- 假突破确认后不立即反手：等反向信号棒 + 跟随，再按反转方向做，目标是最近的磁力位。
- 突破失败后价格常回到区间另一侧，这段"回到另一边"的行情就是利润来源。""",
    "pullback": """\
## 回调与二次入场
- 强势趋势里，第一次回调到前一段结构处通常是二次入场机会。
- 二次入场需要一根同向信号棒确认，不能只因为"跌回支撑"就进。
- 回撤越浅（1-2 根）说明趋势越强，但可用止损距离也越近，仓位可略放大；回撤越深，仓位应缩小。
- 二次入场失败（价格继续反向穿透前一段结构）说明趋势可能已结束，立即离场不加仓。
- 二次入场的最佳形态：回调过程中出现连续两根反向棒但未跌破前一段起点，随后被一根同向棒收复。
- 回调超过前一段幅度的 2/3 时，视为趋势可能转向，不再按二次入场处理。""",
    "failed_breakout": """\
## 突破失败与磁力位
- 突破失败后，价格常常反向奔向最近一个密集成交区（磁力位）。
- 若在磁力位附近有反向信号棒，可以顺势做向磁力位的一单，目标是磁力位而不是更远。
- 磁力位本身不是入场理由，必须配合信号棒与结构失效点。""",
    "wedge": """\
## 楔形
- 三推楔形出现在趋势末端，意味着动能递减、随时可能反转。
- 楔形内部不做逆势单；等楔形通道被反向突破并跟随，再做反转方向。
- 楔形作为顺势中继时，回调到楔形起点是入场点。""",
    "triangle": """\
## 三角形与收敛
- 收敛意味着区间在收窄，波动率将放大，方向未定。
- 收敛末端不做方向单；等突破并跟随，或等突破失败后做反向。
- 收敛越久，突破后的幅度越大，但假突破也越多，因此必须等跟随确认。""",
    "double_structure": """\
## 双顶 / 双底
- 第二次触及同一区域未能突破，且出现反向信号棒，构成双顶/双底。
- 入场：第二次触顶/触底后的反向信号棒；止损放在该顶部/底部之外。
- 关键前提：第二次测试时动能必须弱于第一次，否则那是继续突破而不是双顶双底。
- 目标：两顶之间的颈线，以及颈线被击穿后的测量移动距离。""",
    "measured_move": """\
## 测量移动
- 以一段推动行情的长度作为目标参考：从突破点或回撤低点起算同样的距离。
- 常用两种算法：a) 取突破前整理区间的宽度；b) 取突破点到回撤极点的距离。
- 测量移动是目标位参考，不是入场理由。
- 到达测量目标后应当兑现或大幅收紧保护，不要期待无限延伸。
- 若测量目标与前方明显结构阻力/支撑重叠，应当提前一档兑现。""",
    "mtr": """\
## 主要趋势反转
- 反转需要三个条件同时出现：原有结构被破坏（第一次反向突破）、出现反向的极值尝试、以及随后的第二次确认。
- 只满足其中一条时，按回撤处理，不按反转处理。
- 反转确认后的第一次回撤是主要入场点，此时止损可以放在反转极值之外。""",
    "final_flag": """\
## 最终旗形与趋势末端
- 趋势末端出现的小幅整理旗形，若随后被反向击穿，往往意味着趋势正式结束。
- 顺旗形方向入场在趋势末端风险极高，必须有明确的止损与较近的目标。
- 反向击穿旗形后，可以按趋势反转处理。""",
    "barbwire": """\
## 铁丝网与无交易环境
- 连续多根重叠小实体、方向交替，属于无交易环境。
- 唯一动作是不下单，直到出现一根放量方向棒打破僵局。
- 在铁丝网里反复交易是被手续费与点差吃掉的主要方式。""",
    "always_in": """\
## 单边持续（AlwaysIn）
- 价格长时间保持单边结构、每次回撤都被迅速收复时，市场处于单边持续状态。
- 单边持续中不应做反向单，任何反向都只是回撤。
- 入场选择回撤结束的第一根同向信号棒，止损放在回撤低点之外。""",
}


# ── Execution invariants ──────────────────────────────────────────────────────

EXECUTION_RULES = """\
# 下单与风控规则

## 订单类型
- 市价单：价格已在合理入场位置、需要立刻参与时使用。
- 限价单：想要更好价格、当前价格偏离合理入场点时使用；限价单价格必须位于当前价位更有利的一侧。
- 突破单：价格尚未越过关键边界、准备在突破时参与时使用；触发价必须位于当前价位之外，且在边界外侧。
- 不下单：任何闸门未通过、或找不到明确结构失效点时。

## 止损
- 止损只有一个来源：结构失效点。放在让当前交易逻辑不再成立的价格之外。
- 止损距离必须覆盖点差与正常噪音，过近的止损等于把仓位送给点差。
- 不允许因为"想减小亏损"而把止损放到结构内部。
- 各周期状态的止损参照：极速行情放在起点之外；窄通道放在最近回撤棒之外；
  普通通道放在前一个回撤低点/高点之外；宽通道与区间放在边界之外；
  趋势型震荡要有承受一次大棒扫损的余量。

## 止盈
- 目标优先取结构目标（前高/前低、通道对侧、测量移动、磁力位）。
- 目标必须与止损形成至少 1.5 倍的风险回报，否则放弃这次机会。
- 各周期状态的目标参照：窄通道与普通通道可让利润奔跑至通道对侧；
  区间与趋势型震荡取中轴到对侧边界；极速行情只取第一段回撤。

## 仓位
- 仓位由系统按用户的资金管理方式计算，你只需要给出止损价格。
- 若止损距离过大导致最小手数都超出风险额度，正确的选择是不下单。

## 输出纪律
- 必须先通过阶段闸门，再给价格；价格必须与所选订单类型自洽。"""


def _compact(text: str) -> str:
    return "\n".join(line for line in text.strip().splitlines() if line.strip())


CYCLE_LABELS: dict[str, str] = {
    "spike": "极速行情",
    "micro_channel": "微型通道",
    "tight_channel": "窄通道",
    "normal_channel": "普通通道",
    "broad_channel": "宽通道",
    "trending_tr": "趋势型震荡",
    "trading_range": "交易区间",
    "extreme_tr": "极端震荡",
}

DIRECTION_LABELS: dict[str, str] = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
}


def cycle_label(cycle: str) -> str:
    """Chinese name of a cycle state, for panel text. Unknown values yield ""."""
    return CYCLE_LABELS.get(str(cycle or "").strip().lower(), "")


def direction_label(direction: str) -> str:
    return DIRECTION_LABELS.get(str(direction or "").strip().lower(), "")


def cycle_playbook(cycle: str) -> str:
    return _compact(CYCLE_PLAYBOOKS.get(str(cycle or "").strip().lower(), ""))


def direction_playbook(direction: str) -> str:
    return _compact(DIRECTION_PLAYBOOKS.get(str(direction or "").strip().lower(), ""))


def setup_playbooks(codes: list[str] | tuple[str, ...], *, limit: int = 2) -> str:
    """Return the playbooks for the best-matching detected setups.

    Only ``limit`` playbooks are injected: each one is real prompt weight, and
    the top-scoring setups are the only ones that can still become a trade.
    """
    out: list[str] = []
    for code in codes:
        if len(out) >= limit:
            break
        text = SETUP_PLAYBOOKS.get(str(code or "").strip().lower())
        if text:
            out.append(_compact(text))
    return "\n\n".join(out)


def route(*, cycle: str, direction: str, setup_codes: list[str] | tuple[str, ...] = ()) -> str:
    """Assemble the stage-2 strategy text for the diagnosed market state.

    The checklist and signal-bar rules are always included: they are what stops
    the model from approving a setup it never actually read bar by bar. The rest
    is routed, so an unrelated cycle or pattern costs nothing.
    """
    blocks = [
        _compact(BAR_CHECKLIST),
        _compact(SIGNAL_BAR_RULES),
        cycle_playbook(cycle),
        direction_playbook(direction),
        setup_playbooks(setup_codes),
        _compact(EXECUTION_RULES),
    ]
    return "\n\n".join(block for block in blocks if block)


def cycle_options() -> list[str]:
    return list(CYCLE_PLAYBOOKS)


def knowledge_stats() -> dict[str, Any]:
    """Sizes used by tests to keep the routed prompt budget bounded."""
    return {
        "framework_chars": len(_compact(DIAGNOSIS_FRAMEWORK)),
        "always_on_chars": (
            len(_compact(BAR_CHECKLIST))
            + len(_compact(SIGNAL_BAR_RULES))
            + len(_compact(EXECUTION_RULES))
        ),
        "cycles": sorted(CYCLE_PLAYBOOKS),
        "setups": sorted(SETUP_PLAYBOOKS),
        "largest_route_chars": max(
            len(route(cycle=cycle, direction=direction, setup_codes=("breakout", "pullback")))
            for cycle in CYCLE_PLAYBOOKS
            for direction in DIRECTION_PLAYBOOKS
        ),
    }
