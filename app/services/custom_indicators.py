from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import pandas_ta_classic as ta

from app.models import Candle


@dataclass(frozen=True)
class IndicatorDefinition:
    name: str
    title: str
    default_params: dict[str, int | float]
    description: str


INDICATOR_DEFINITIONS = (
    IndicatorDefinition("ema", "EMA 指数移动平均线", {"length": 20}, "趋势与交叉判断"),
    IndicatorDefinition("sma", "SMA（MA）简单移动平均线", {"length": 20}, "策略中写 MA 时按 SMA 计算"),
    IndicatorDefinition("wma", "WMA 加权移动平均线", {"length": 20}, "加权趋势判断"),
    IndicatorDefinition("rsi", "RSI 相对强弱指标", {"length": 14}, "超买超卖与背离判断"),
    IndicatorDefinition("atr", "ATR 平均真实波幅", {"length": 14}, "波动、止损距离判断"),
    IndicatorDefinition("macd", "MACD", {"fast": 12, "slow": 26, "signal": 9}, "趋势、动量与交叉判断"),
    IndicatorDefinition("bbands", "布林带", {"length": 20, "std": 2.0}, "通道、突破与波动判断"),
    IndicatorDefinition("stoch", "随机指标 KDJ/Stochastic", {"k": 14, "d": 3, "smooth_k": 3}, "超买超卖与交叉判断"),
    IndicatorDefinition("adx", "ADX 趋势强度", {"length": 14}, "趋势强弱判断"),
    IndicatorDefinition("cci", "CCI 顺势指标", {"length": 20}, "趋势与超买超卖判断"),
    IndicatorDefinition("roc", "ROC 变动率", {"length": 10}, "价格动量判断"),
    IndicatorDefinition("mom", "Momentum 动量", {"length": 10}, "价格动量判断"),
    IndicatorDefinition("dema", "DEMA 双指数移动平均线", {"length": 20}, "低延迟趋势判断"),
    IndicatorDefinition("tema", "TEMA 三指数移动平均线", {"length": 20}, "低延迟趋势判断"),
    IndicatorDefinition("hma", "HMA 赫尔移动平均线", {"length": 20}, "平滑快速趋势判断"),
    IndicatorDefinition("kama", "KAMA 自适应移动平均线", {"length": 10, "fast": 2, "slow": 30}, "自适应趋势判断"),
    IndicatorDefinition("rma", "RMA 平滑移动平均线", {"length": 20}, "平滑趋势判断"),
    IndicatorDefinition("zlma", "ZLMA 零延迟移动平均线", {"length": 20}, "低延迟趋势判断"),
    IndicatorDefinition("vwma", "VWMA 成交量加权移动平均线", {"length": 20}, "量价趋势判断"),
    IndicatorDefinition("supertrend", "Supertrend 超级趋势", {"length": 10, "multiplier": 3.0}, "趋势方向与跟踪止损"),
    IndicatorDefinition("psar", "PSAR 抛物线转向", {"af0": 0.02, "af": 0.02, "max_af": 0.2}, "趋势反转与跟踪止损"),
    IndicatorDefinition("aroon", "Aroon 阿隆指标", {"length": 14}, "趋势出现与强弱判断"),
    IndicatorDefinition("vortex", "Vortex 涡旋指标", {"length": 14}, "趋势方向判断"),
    IndicatorDefinition("stochrsi", "StochRSI 随机相对强弱", {"length": 14, "rsi_length": 14, "k": 3, "d": 3}, "超买超卖与交叉判断"),
    IndicatorDefinition("mfi", "MFI 资金流量指标", {"length": 14}, "量价超买超卖判断"),
    IndicatorDefinition("willr", "Williams %R", {"length": 14}, "超买超卖判断"),
    IndicatorDefinition("cmo", "CMO 钱德动量摆动", {"length": 14}, "价格动量判断"),
    IndicatorDefinition("trix", "TRIX 三重指数平滑", {"length": 30, "signal": 9}, "趋势与动量判断"),
    IndicatorDefinition("tsi", "TSI 真实强弱指标", {"fast": 13, "slow": 25, "signal": 13}, "趋势动量判断"),
    IndicatorDefinition("ppo", "PPO 百分比价格振荡器", {"fast": 12, "slow": 26, "signal": 9}, "均线动量与交叉判断"),
    IndicatorDefinition("ao", "AO 动量振荡器", {"fast": 5, "slow": 34}, "市场动量判断"),
    IndicatorDefinition("uo", "Ultimate Oscillator 终极振荡器", {"fast": 7, "medium": 14, "slow": 28}, "多周期动量判断"),
    IndicatorDefinition("kc", "Keltner Channel 肯特纳通道", {"length": 20, "scalar": 2.0}, "趋势通道与突破判断"),
    IndicatorDefinition("donchian", "Donchian Channel 唐奇安通道", {"lower_length": 20, "upper_length": 20}, "高低点通道与突破判断"),
    IndicatorDefinition("natr", "NATR 标准化真实波幅", {"length": 14}, "标准化波动率判断"),
    IndicatorDefinition("true_range", "True Range 真实波幅", {}, "单根K线真实波幅"),
    IndicatorDefinition("obv", "OBV 能量潮", {}, "成交量累积趋势判断"),
    IndicatorDefinition("ad", "AD 累积派发线", {}, "资金累积与派发判断"),
    IndicatorDefinition("cmf", "CMF 蔡金资金流量", {"length": 20}, "资金流入流出判断"),
    IndicatorDefinition("efi", "EFI 强力指数", {"length": 13}, "量价力量判断"),
    IndicatorDefinition("eom", "EOM 简易波动指标", {"length": 14}, "量价移动效率判断"),
    IndicatorDefinition("pvt", "PVT 量价趋势", {}, "成交量与价格趋势判断"),
    IndicatorDefinition("chop", "Choppiness 震荡指数", {"length": 14, "atr_length": 1}, "趋势与震荡状态判断"),
    IndicatorDefinition("fisher", "Fisher Transform 费雪变换", {"length": 9, "signal": 1}, "价格转折判断"),
    IndicatorDefinition("dpo", "DPO 去趋势价格振荡器", {"length": 20}, "周期波动判断"),
    IndicatorDefinition("linreg", "Linear Regression 线性回归", {"length": 14}, "趋势拟合判断"),
    IndicatorDefinition("slope", "Slope 线性斜率", {"length": 14}, "趋势方向与速度判断"),
    IndicatorDefinition("qstick", "QStick K线动量", {"length": 10}, "开收盘动量判断"),
    IndicatorDefinition("bias", "BIAS 乖离率", {"length": 26}, "价格偏离均线程度判断"),
)

_DEFINITIONS = {item.name: item for item in INDICATOR_DEFINITIONS}
_ALIASES = {
    "ma": "sma", "moving_average": "sma", "bollinger": "bbands", "boll": "bbands",
    "bollinger_bands": "bbands", "kdj": "stoch", "stochastic": "stoch",
}

_INDICATOR_INPUTS = {
    "ema": "收盘价（也可指定开盘价、最高价、最低价或成交量）",
    "sma": "收盘价（也可指定开盘价、最高价、最低价或成交量）",
    "wma": "收盘价（也可指定开盘价、最高价、最低价或成交量）",
    "rsi": "收盘价",
    "atr": "最高价、最低价、收盘价",
    "macd": "收盘价",
    "bbands": "收盘价",
    "stoch": "最高价、最低价、收盘价",
    "adx": "最高价、最低价、收盘价",
    "cci": "最高价、最低价、收盘价",
    "roc": "收盘价",
    "mom": "收盘价",
}

_PRICE_SOURCES = (
    {"value": "close", "label": "收盘价", "formula": "close"},
    {"value": "open", "label": "开盘价", "formula": "open"},
    {"value": "high", "label": "最高价", "formula": "high"},
    {"value": "low", "label": "最低价", "formula": "low"},
    {"value": "hl2", "label": "高低均价 HL2", "formula": "(high + low) / 2"},
    {"value": "hlc3", "label": "典型价格 HLC3", "formula": "(high + low + close) / 3"},
    {"value": "ohlc4", "label": "四价均值 OHLC4", "formula": "(open + high + low + close) / 4"},
    {"value": "oc2", "label": "开收均价 OC2", "formula": "(open + close) / 2"},
    {"value": "wclprice", "label": "加权收盘价", "formula": "(high + low + close × 2) / 4"},
)

_SINGLE_SERIES_INDICATORS = {
    "ema", "sma", "wma", "rsi", "macd", "bbands", "roc", "mom",
    "dema", "tema", "hma", "kama", "rma", "zlma", "vwma", "stochrsi",
    "cmo", "trix", "tsi", "ppo", "efi", "dpo", "linreg", "slope", "bias",
}

_PARAMETER_DETAILS = {
    "length": ("周期", "参与计算的K线数量"),
    "fast": ("快线周期", "MACD 快速移动平均线周期"),
    "slow": ("慢线周期", "MACD 慢速移动平均线周期"),
    "signal": ("信号线周期", "MACD 信号线平滑周期"),
    "std": ("标准差倍数", "布林带上下轨使用的标准差倍数"),
    "k": ("K值周期", "随机指标计算最高价和最低价区间的周期"),
    "d": ("D值周期", "随机指标D线的平滑周期"),
    "smooth_k": ("K值平滑", "随机指标K线的平滑周期"),
}


def public_indicator_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "title": item.title,
            "default_params": item.default_params,
            "description": item.description,
            "aliases": ["MA"] if item.name == "sma" else [],
            "input": _INDICATOR_INPUTS.get(item.name, "收盘价"),
            "parameters": [
                {
                    "name": name,
                    "label": _PARAMETER_DETAILS.get(name, (name, "指标计算参数"))[0],
                    "default": default,
                    "description": _PARAMETER_DETAILS.get(name, (name, "指标计算参数"))[1],
                }
                for name, default in item.default_params.items()
            ],
            "sources": list(_PRICE_SOURCES) if item.name in _SINGLE_SERIES_INDICATORS else [],
        }
        for item in INDICATOR_DEFINITIONS
    ]


def required_candle_count(specs: list[dict[str, Any]]) -> int:
    periods = [
        float(value)
        for spec in specs
        for value in (spec.get("params") or {}).values()
        if isinstance(value, (int, float))
    ]
    if not periods:
        return 100
    return max(100, min(1000, int(max(periods) * 3 + 100)))


def normalize_indicator_specs(value: Any, *, limit: int = 15) -> tuple[list[dict[str, Any]], list[str]]:
    specs: list[dict[str, Any]] = []
    unsupported: list[str] = []
    seen: set[str] = set()
    seen_aliases: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        requested_name = str(raw.get("name") or raw.get("type") or "").strip().lower()
        name = _ALIASES.get(requested_name, requested_name)
        if name not in _DEFINITIONS:
            if requested_name and requested_name not in unsupported:
                unsupported.append(requested_name)
            continue
        source = str(raw.get("source") or "close").strip().lower()
        if source not in {item["value"] for item in _PRICE_SOURCES} | {"volume"}:
            source = "close"
        if name not in _SINGLE_SERIES_INDICATORS:
            source = "ohlc"
        params = dict(_DEFINITIONS[name].default_params)
        supplied = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        for key, default in params.items():
            try:
                number = float(supplied.get(key, raw.get(key, default)))
            except (TypeError, ValueError):
                number = float(default)
            params[key] = max(1, int(number)) if isinstance(default, int) else max(0.000001, number)
        alias = str(raw.get("alias") or _indicator_alias(name, params, source)).strip()[:64]
        if alias in seen_aliases:
            alias = f"{alias}_{source}"[:64]
        signature = f"{name}:{source}:{sorted(params.items())}"
        if signature in seen:
            continue
        seen.add(signature)
        seen_aliases.add(alias)
        specs.append({"name": name, "source": source, "params": params, "alias": alias})
        if len(specs) >= limit:
            break
    return specs, unsupported


def calculate_indicator_payload(
    candles: list[Candle],
    specs: list[dict[str, Any]],
    *,
    output_count: int = 100,
) -> dict[str, Any]:
    if not specs or not candles:
        return {"order": "oldest_to_latest", "timestamps": [], "values": {}}
    ordered = sorted(candles, key=lambda item: item.timestamp)
    frame = pd.DataFrame({
        "timestamp": [item.timestamp for item in ordered],
        "open": [item.open for item in ordered],
        "high": [item.high for item in ordered],
        "low": [item.low for item in ordered],
        "close": [item.close for item in ordered],
        "volume": [item.volume for item in ordered],
    })
    values = pd.DataFrame(index=frame.index)
    for spec in specs:
        try:
            result = _calculate_one(frame, spec)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if isinstance(result, pd.Series):
            values[str(spec["alias"])] = result
        elif isinstance(result, pd.DataFrame):
            for column in result.columns:
                values[f"{spec['alias']}.{_component_name(str(column))}"] = result[column]
    if values.empty:
        return {"order": "oldest_to_latest", "timestamps": [], "values": {}}
    valid = values.dropna(how="all").tail(max(10, min(int(output_count or 100), 300)))
    compact_values = {
        column: [_compact_number(item) for item in valid[column].tolist()]
        for column in valid.columns
    }
    return {
        "order": "oldest_to_latest",
        "timestamps": [int(frame.loc[index, "timestamp"]) for index in valid.index],
        "values": compact_values,
        "recent_values": {
            column: items[-3:]
            for column, items in compact_values.items()
        },
    }


def _calculate_one(frame: pd.DataFrame, spec: dict[str, Any]) -> pd.Series | pd.DataFrame | None:
    name = str(spec["name"])
    params = dict(spec.get("params") or {})
    source = _source_series(frame, str(spec.get("source") or "close"))
    if name in {
        "ema", "sma", "wma", "rsi", "roc", "mom", "dema", "tema", "hma",
        "kama", "rma", "zlma", "cmo", "trix", "tsi", "ppo", "stochrsi",
        "dpo", "linreg", "slope", "bias",
    }:
        return getattr(ta, name)(source, **params)
    if name == "vwma":
        return ta.vwma(source, frame.volume, **params)
    if name == "atr":
        return ta.atr(frame.high, frame.low, frame.close, **params)
    if name == "macd":
        return ta.macd(source, **params)
    if name == "bbands":
        return ta.bbands(source, **params)
    if name == "stoch":
        return ta.stoch(frame.high, frame.low, frame.close, **params)
    if name == "adx":
        return ta.adx(frame.high, frame.low, frame.close, **params)
    if name == "cci":
        return ta.cci(frame.high, frame.low, frame.close, **params)
    if name == "supertrend":
        return ta.supertrend(frame.high, frame.low, frame.close, **params)
    if name == "psar":
        return ta.psar(frame.high, frame.low, frame.close, **params)
    if name == "aroon":
        return ta.aroon(frame.high, frame.low, **params)
    if name == "vortex":
        return ta.vortex(frame.high, frame.low, frame.close, **params)
    if name == "mfi":
        return ta.mfi(frame.high, frame.low, frame.close, frame.volume, **params)
    if name == "willr":
        return ta.willr(frame.high, frame.low, frame.close, **params)
    if name == "ao":
        return ta.ao(frame.high, frame.low, **params)
    if name == "uo":
        return ta.uo(frame.high, frame.low, frame.close, **params)
    if name == "kc":
        return ta.kc(frame.high, frame.low, frame.close, **params)
    if name == "donchian":
        return ta.donchian(frame.high, frame.low, **params)
    if name == "natr":
        return ta.natr(frame.high, frame.low, frame.close, **params)
    if name == "true_range":
        return ta.true_range(frame.high, frame.low, frame.close, **params)
    if name == "obv":
        return ta.obv(frame.close, frame.volume, **params)
    if name == "ad":
        return ta.ad(frame.high, frame.low, frame.close, frame.volume, open_=frame.open, **params)
    if name == "cmf":
        return ta.cmf(frame.high, frame.low, frame.close, frame.volume, open_=frame.open, **params)
    if name == "efi":
        return ta.efi(source, frame.volume, **params)
    if name == "eom":
        return ta.eom(frame.high, frame.low, frame.close, frame.volume, **params)
    if name == "pvt":
        return ta.pvt(frame.close, frame.volume, **params)
    if name == "chop":
        return ta.chop(frame.high, frame.low, frame.close, **params)
    if name == "fisher":
        return ta.fisher(frame.high, frame.low, **params)
    if name == "qstick":
        return ta.qstick(frame.open, frame.close, **params)
    return None


def _source_series(frame: pd.DataFrame, source: str) -> pd.Series:
    if source in {"open", "high", "low", "close", "volume"}:
        return frame[source]
    if source == "hl2":
        return (frame.high + frame.low) / 2
    if source == "hlc3":
        return (frame.high + frame.low + frame.close) / 3
    if source == "ohlc4":
        return (frame.open + frame.high + frame.low + frame.close) / 4
    if source == "oc2":
        return (frame.open + frame.close) / 2
    if source == "wclprice":
        return (frame.high + frame.low + frame.close * 2) / 4
    return frame.close


def _indicator_alias(name: str, params: dict[str, int | float], source: str) -> str:
    if "length" in params:
        base = f"{name}{params['length']}"
    else:
        base = name
    return base if source in {"close", "ohlc"} else f"{base}_{source}"


def _component_name(value: str) -> str:
    return value.lower().replace("_", ".")[:48]


def _compact_number(value: Any) -> int | float | None:
    if pd.isna(value):
        return None
    number = float(value)
    rounded = round(number, 8)
    return int(rounded) if rounded.is_integer() else rounded
