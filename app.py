import os
import sys
import re
import json
import math
import asyncio
import logging
import threading
import random
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Dict, Any
import aiohttp
import gradio as gr
import uvicorn
import numpy as np
import pandas as pd
from collections import Counter
from fastapi import FastAPI
from gradio import mount_gradio_app
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError

# ==================== 1. 核心常量与工具函数 ====================
def get_type(s: int) -> str:
    return ('大' if s >= 14 else '小') + ('单' if s % 2 else '双')


def compute_sha_a_kills(nums: list, total: int) -> list:
    """
    杀a球规则：根据最新一期开奖号码生成下一期 5 个杀号数字。
    计算方式：total / abc * e，取小数部分，从小数点后第 2 位开始提取不重复数字，直到 5 个。
    """
    if len(nums) < 3:
        return random.sample(range(10), 5)
    abc = nums[0] * 100 + nums[1] * 10 + nums[2]
    if abc == 0:
        abc = 1
    value = (total / abc) * math.e
    frac = value - int(value)
    # 保留足够多的小数位
    frac_str = f"{frac:.20f}".replace("0.", "")
    kills = []
    # 从小数点后第 2 位开始（索引 1）
    for ch in frac_str[1:]:
        d = int(ch)
        if d not in kills:
            kills.append(d)
        if len(kills) >= 5:
            break
    if len(kills) < 5:
        # 兜底：补足不重复数字
        for d in range(10):
            if d not in kills:
                kills.append(d)
            if len(kills) >= 5:
                break
    return kills


# 杀组四组合
COMBOS = ["大单", "小单", "大双", "小双"]

# ==================== 2. 风控管理系统 ====================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DATA_API_URL = "https://pc28.help/api/kj.json?nbr=100"
SESSIONS_DIR = "telegram_sessions"
USER_DATA_DIR = "user_data"

# 北京时间 (UTC+8)，盈亏按北京时间每天 00:00 重置
BEIJING_TZ = timezone(timedelta(hours=8))

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

# ==================== 算法基类与 6 个内置算法 ====================

class BasePredictor:
    """所有预测算法的基类"""
    name = "base"
    version = "1.0"

    def predict(self, history: list) -> dict:
        raise NotImplementedError

    def update(self, actual: dict):
        pass

    def _base_scores(self, value=50):
        return {"大单": value, "大双": value, "小单": value, "小双": value}

    def _combo(self, size, odd):
        return f"{size}{odd}"


"""π 算法 - 基于最新和值、上一期和值与上期组合进行判定"""

class PiPredictor(BasePredictor):
    name = "pi"

    _OPPOSITE = {
        "大单": "小双",
        "大双": "小单",
        "小单": "大双",
        "小双": "大单",
    }

    def _digit_sum(self, n: int) -> int:
        s = 0
        while n > 0:
            s += n % 10
            n //= 10
        return s

    def _reduce_to_one_digit(self, n: int) -> int:
        while n >= 10:
            n = self._digit_sum(n)
        return n

    def _first_three_digits(self, value: float) -> list:
        s = f"{value:.15f}".replace(".", "")
        return [int(ch) for ch in s[:3]]

    def _combo_from_z(self, z: int) -> str:
        if z < 10:
            return "小单" if z % 2 == 1 else "小双"
        return "大单" if z % 2 == 1 else "大双"

    def predict(self, history: list) -> dict:
        if len(history) < 2:
            return self._base_scores()

        A = history[0]["total"]
        B = history[1]["total"]
        C = history[1]["size"] + history[1]["odd_even"]

        product = A * 3.1415926
        digits = self._first_three_digits(product)
        X = self._reduce_to_one_digit(sum(digits))
        Y = X * B

        if Y < 100:
            Z = (Y // 10) + (Y % 10)
        else:
            Z = self._reduce_to_one_digit(self._digit_sum(Y))
        if Z == 10:
            Z = 2

        combo = self._combo_from_z(Z)
        if combo == C:
            combo = self._OPPOSITE.get(combo, combo)

        scores = self._base_scores(0)
        scores[combo] = 100
        return scores


"""复杂双杀组算法 - 仅实现模块 1 的最终杀组判定"""

class ComplexDualKillPredictor(BasePredictor):
    name = "complex_dual_kill"

    _OPPOSITE = {
        "小单": "大双",
        "小双": "大单",
        "大单": "小双",
        "大双": "小单",
    }

    def _digit_sum(self, n: int) -> int:
        s = 0
        while n > 0:
            s += n % 10
            n //= 10
        return s

    def _combo_from_s(self, s: int) -> str:
        size = "小" if s <= 13 else "大"
        odd = "单" if s % 2 == 1 else "双"
        return f"{size}{odd}"

    def _calc_y1(self, nums, total):
        concat = int(f"{nums[0]}{nums[1]}{nums[2]}")
        return self._digit_sum(concat + total)

    def predict(self, history: list) -> dict:
        if len(history) < 10:
            return self._base_scores()

        latest = history[0]
        A1, B1, C1 = latest["nums"]
        H1 = latest["total"]

        Y1 = self._calc_y1([A1, B1, C1], H1)
        matched = None
        for item in history[1:]:
            if self._calc_y1(item["nums"], item["total"]) == Y1:
                matched = item
                break

        if matched is None:
            return self._base_scores()

        A2, B2, C2 = matched["nums"]
        S_diff = abs(A1 - A2) + abs(B1 - B2) + abs(C1 - C2)
        kill1 = self._combo_from_s(S_diff)

        step1 = H1 * 3 * H1
        last3 = step1 % 1000
        D2 = self._digit_sum(last3)
        S = D2 + A1
        if S > 27:
            S -= 27
        combo_z3 = self._combo_from_s(S)
        kill2 = self._OPPOSITE[combo_z3]

        if kill1 == kill2:
            final = kill1
        else:
            return self._base_scores()

        scores = self._base_scores(0)
        scores[final] = 100
        return scores


# 天子 / 5Y 算法共用工具函数

def _normalize_r(R):
    while R > 27:
        R -= 28
    while R < 0:
        R += 28
    return max(0, min(27, R))


def _get_combo(sum_value):
    return ("小" if sum_value <= 13 else "大") + ("双" if sum_value % 2 == 0 else "单")


"""天子算法 - compute_main_algorithm 移植"""

class TianZiPredictor(BasePredictor):
    name = "tianzi"

    def _compute(self, data, index):
        if index >= len(data) or index + 15 >= len(data):
            return None

        cur, back5, back10, back15 = data[index], data[index + 5], data[index + 10], data[index + 15]

        a, b, c = cur["nums"]
        S = sum(cur["nums"])
        S5 = sum(back5["nums"])
        S10 = sum(back10["nums"])
        S15 = sum(back15["nums"])

        if S == 0:
            S = 1

        T1 = (a + c) * b + back10["nums"][1]
        T2 = (back5["nums"][0] + back5["nums"][2]) * back5["nums"][1] + back15["nums"][1]
        R = (T1 + T2) // 2

        momentum = (S - S5) + (S5 - S10) + (S10 - S15)
        R += max(-5, min(5, momentum // 3))
        R = _normalize_r(R)

        recent_sums = [data[i]["total"] for i in range(index + 1, min(index + 50, len(data)))]
        if recent_sums:
            recent_avg = sum(recent_sums) / len(recent_sums)
            recent_std = (
                sum((x - recent_avg) ** 2 for x in recent_sums) / len(recent_sums)
            ) ** 0.5 if recent_sums else 5

            if recent_std > 6:
                R = _normalize_r(int(recent_avg) + random.randint(-3, 3))
            elif abs(R - recent_avg) > 8:
                R = _normalize_r(int(recent_avg) + (R - int(recent_avg)) // 2)

            recent_counts = Counter(recent_sums[-8:])
            if recent_counts and recent_counts.most_common(1)[0][1] >= 3:
                freq_val = recent_counts.most_common(1)[0][0]
                if abs(R - freq_val) < 3:
                    R = _normalize_r(R + 7)

            if len(recent_sums) >= 5:
                last5_avg = sum(recent_sums[:5]) / 5
                if R < 10 and last5_avg > 18:
                    R = _normalize_r(R + 14)
                elif R > 17 and last5_avg < 9:
                    R = _normalize_r(R - 14)

        return {"kill": "杀" + _get_combo(R), "sum": R}

    def predict(self, history: list) -> dict:
        if len(history) < 16:
            return self._base_scores()
        result = self._compute(history, 0)
        if not result:
            return self._base_scores()
        kill = result["kill"].replace("杀", "")
        scores = self._base_scores(0)
        scores[kill] = 100
        return scores


"""5Y 算法 - compute_5y_algorithm 移植"""

class FiveYPredictor(BasePredictor):
    name = "5y"

    def _compute(self, data, index):
        if index >= len(data) or index + 10 >= len(data):
            return None

        cur, back5, back10 = data[index], data[index + 5], data[index + 10]

        b = cur["nums"][1]
        S = sum(cur["nums"])
        S5 = sum(back5["nums"])
        S10 = sum(back10["nums"])

        if S == 0:
            S = 1

        valB = (b % 5 + 1)
        valS = (S % 5 + 1)
        base = (valB * valS) % 10

        volatility = abs(S - S5) + abs(S5 - S10)
        volatility_factor = (volatility % 5) + 1

        trend = 2 if (S > S5 and S5 > S10) else (0 if (S < S5 and S5 < S10) else 1)

        R = _normalize_r(base * 3 + volatility_factor * 2 + trend)

        recent_sums = [data[i]["total"] for i in range(index + 1, min(index + 50, len(data)))]
        if recent_sums:
            recent_avg = sum(recent_sums) / len(recent_sums)
            recent_var = sum((x - recent_avg) ** 2 for x in recent_sums) / len(recent_sums)

            if recent_var > 20:
                R = _normalize_r(int(recent_avg) + random.randint(-4, 4))

            recent_counts = Counter(recent_sums[-10:])
            if recent_counts:
                most_common = recent_counts.most_common(3)
                weights = [0.5, 0.3, 0.2]
                weighted_sum = 0
                total_w = 0
                for i, (val, cnt) in enumerate(most_common):
                    if i < len(weights):
                        weighted_sum += val * weights[i] * cnt
                        total_w += weights[i] * cnt
                if total_w > 0:
                    weighted_avg = weighted_sum / total_w
                    R = _normalize_r(int(weighted_avg * 0.6 + R * 0.4))

            if len(recent_sums) >= 6:
                first3 = sum(recent_sums[:3]) / 3
                last3 = sum(recent_sums[-3:]) / 3
                diff = last3 - first3
                if abs(diff) > 5:
                    R = _normalize_r(R + int(diff / 2))

        if index + 1 < len(data):
            recent_shapes = [_get_combo(data[i]["total"]) for i in range(index + 1, min(index + 6, len(data)))]
            kill_shape = _get_combo(R)
            if kill_shape in recent_shapes:
                R = _normalize_r(R + 7)
                if _get_combo(R) == kill_shape:
                    R = _normalize_r(R + 14)

        return {"kill": "杀" + _get_combo(R), "sum": R}

    def predict(self, history: list) -> dict:
        if len(history) < 11:
            return self._base_scores()
        result = self._compute(history, 0)
        if not result:
            return self._base_scores()
        kill = result["kill"].replace("杀", "")
        scores = self._base_scores(0)
        scores[kill] = 100
        return scores


"""小枫算法 - compute_xiaofeng_algorithm 移植"""

class _XiaoFengDraw:
    def __init__(self, hundreds, tens, ones):
        self.hundreds = hundreds
        self.tens = tens
        self.ones = ones

    @property
    def sum_value(self):
        return self.hundreds + self.tens + self.ones

    @property
    def seven_y(self):
        return self.sum_value % 7

    @property
    def group(self):
        s = self.sum_value
        if s <= 13:
            return "小单" if s % 2 == 1 else "小双"
        return "大单" if s % 2 == 1 else "大双"


class XiaoFengPredictor(BasePredictor):
    name = "xiaofeng"

    _OPPOSITE = {
        "小单": "大双",
        "小双": "大单",
        "大单": "小双",
        "大双": "小单",
    }

    def _compute(self, data, index):
        if index >= len(data) or len(data) < 3:
            return None

        draws = []
        for item in data:
            nums = item["nums"]
            draws.append(_XiaoFengDraw(nums[0], nums[1], nums[2]))

        current = draws[index] if index < len(draws) else draws[0]
        seven_y = current.seven_y

        refs = []
        for i, d in enumerate(draws[index + 1:], index + 1):
            if d.seven_y == seven_y:
                refs.append((d, i - index))
                if len(refs) >= 5:
                    break

        DIGIT_MAP = {
            0: ("十位", lambda d: [d.tens]),
            1: ("个位", lambda d: [d.ones]),
            2: ("百位", lambda d: [d.hundreds]),
            3: ("百位+十位", lambda d: [d.hundreds, d.tens]),
            4: ("个位", lambda d: [d.ones]),
            5: ("十位", lambda d: [d.tens]),
            6: ("百位", lambda d: [d.hundreds]),
        }
        POS_ATTR = {2: "hundreds", 6: "hundreds", 0: "tens", 5: "tens", 1: "ones", 4: "ones"}

        votes = {}
        for ref, distance in refs:
            _, get_digits = DIGIT_MAP[seven_y]
            taken = get_digits(ref)

            if seven_y == 3:
                new_digit = (current.hundreds + current.tens + sum(taken)) % 10
                new_draw = _XiaoFengDraw(new_digit, new_digit, current.ones)
            else:
                attr = POS_ATTR[seven_y]
                new_digit = (getattr(current, attr) + taken[0]) % 10
                h, t, o = current.hundreds, current.tens, current.ones
                if seven_y in (2, 6):
                    h = new_digit
                elif seven_y in (0, 5):
                    t = new_digit
                else:
                    o = new_digit
                new_draw = _XiaoFengDraw(h, t, o)

            y_n = new_draw.sum_value % 7
            group = None

            if y_n == 0:
                group = "小单"
            elif y_n == 1:
                group = "大单"
            elif y_n == 2:
                group = "小双"
            elif y_n == 3:
                group = "大双"
            elif y_n == 4:
                group = "小单"
            elif y_n == 5:
                group = new_draw.group
            elif y_n == 6:
                group = self._OPPOSITE.get(new_draw.group, new_draw.group)

            if group:
                weight = 3 if distance == 1 else (2 if distance == 2 else 1)
                votes[group] = votes.get(group, 0) + weight

        if votes:
            kill_group = max(votes, key=votes.get)
        else:
            counts = Counter(d.group for d in draws)
            kill_group = counts.most_common()[-1][0] if counts else "小单"

        if len(draws) >= 3:
            last1 = draws[1].group if len(draws) > 1 else None
            last2 = draws[2].group if len(draws) > 2 else None
            if last1 == last2 and last1 == kill_group:
                kill_group = self._OPPOSITE.get(kill_group, kill_group)

        return {"kill": "杀" + kill_group}

    def predict(self, history: list) -> dict:
        if len(history) < 3:
            return self._base_scores()
        result = self._compute(history, 0)
        if not result:
            return self._base_scores()
        kill = result["kill"].replace("杀", "")
        scores = self._base_scores(0)
        scores[kill] = 100
        return scores


"""小盾算法 - compute_xiaodun_algorithm / PC28PredictorV7 移植"""

class XiaoDunPredictor(BasePredictor):
    name = "xiaodun"

    def __init__(self):
        self.alpha = 1.0
        self.global_prior = {"小单": 0.20, "小双": 0.28, "大单": 0.24, "大双": 0.28}

    def _add_data(self, data_list):
        history = []
        for item in data_list:
            nums = item["nums"]
            total = item["total"]
            is_big = total >= 14
            is_single = total % 2 == 1
            combination = ("大" if is_big else "小") + ("单" if is_single else "双")
            history.append({
                "period": str(item.get("issue", "")),
                "total": total,
                "combination": combination,
                "is_big": is_big,
                "is_single": is_single,
                "nums": nums,
                "yu5": total % 5,
            })
        return history

    def _get_smoothed_trans_prob(self, from_combo, to_combo, history_slice):
        trans_count = 0
        total_from = 0
        for i in range(len(history_slice) - 1):
            if history_slice[i]["combination"] == from_combo:
                total_from += 1
                if history_slice[i + 1]["combination"] == to_combo:
                    trans_count += 1
        num_classes = 4
        return (trans_count + self.alpha) / (total_from + self.alpha * num_classes)

    def _get_cold_streak(self, combo, history_slice):
        streak = 0
        for i in range(len(history_slice) - 1, -1, -1):
            if history_slice[i]["combination"] == combo:
                break
            streak += 1
        return streak

    def _calculate_next_prob(self, combo, history_slice):
        if len(history_slice) < 1:
            return self.global_prior[combo]
        current = history_slice[-1]["combination"]
        n = len(history_slice)
        trans_prob = self._get_smoothed_trans_prob(current, combo, history_slice)
        global_freq = sum(1 for d in history_slice if d["combination"] == combo) / n
        recent = history_slice[-10:] if n >= 10 else history_slice
        recent_freq = sum(1 for d in recent if d["combination"] == combo) / len(recent)
        short = history_slice[-5:] if n >= 5 else history_slice
        short_freq = sum(1 for d in short if d["combination"] == combo) / len(short)
        return (
            trans_prob * 0.40 +
            global_freq * 0.15 +
            recent_freq * 0.25 +
            short_freq * 0.20
        )

    def _compute_probs_and_cold(self, history_slice):
        probs = {}
        cold_streaks = {}
        for combo in ["小单", "小双", "大单", "大双"]:
            probs[combo] = self._calculate_next_prob(combo, history_slice)
            cold_streaks[combo] = self._get_cold_streak(combo, history_slice)
        return probs, cold_streaks

    def _predict_kill_group(self, history):
        if len(history) < 10:
            return None
        probs, cold_streaks = self._compute_probs_and_cold(history)
        protected = set()
        for combo in ["小单", "小双", "大单", "大双"]:
            if cold_streaks[combo] >= 5:
                protected.add(combo)
        candidates = [c for c in ["小单", "小双", "大单", "大双"] if c not in protected]
        if not candidates:
            candidates = ["小单", "小双", "大单", "大双"]
        return min(candidates, key=lambda c: probs[c])

    def predict(self, history: list) -> dict:
        if len(history) < 10:
            return self._base_scores()

        xd_history = self._add_data(history)
        kill_group = self._predict_kill_group(xd_history)
        if kill_group is None:
            return self._base_scores()

        scores = self._base_scores(0)
        scores[kill_group] = 100
        return scores


"""轻量集成器 - 多算法投票融合"""

class EnsembleVoter(BasePredictor):
    """加权投票集成，非主组算法，独立轻量实现"""
    name = "ensemble_voter"

    def __init__(self, predictors: list, weights: list = None):
        self.predictors = predictors
        self.weights = weights or [1.0] * len(predictors)
        self.accuracy_log = {getattr(p, 'name', p.__class__.__name__): [] for p in predictors}

    def predict(self, history: list) -> dict:
        scores = {"大单": 0, "大双": 0, "小单": 0, "小双": 0}
        total_w = 0
        for p, w in zip(self.predictors, self.weights):
            try:
                pred = p.predict(history)
                for k in scores:
                    scores[k] += pred.get(k, 50) * w
                total_w += w
            except Exception:
                continue
        if total_w == 0:
            return self._base_scores()
        return {k: v/total_w for k, v in scores.items()}

    def update(self, actual: dict):
        for p in self.predictors:
            try:
                p.update(actual)
            except Exception:
                pass


# 全部 6 个算法类
ALGO_CLASSES = [PiPredictor, ComplexDualKillPredictor, TianZiPredictor, FiveYPredictor, XiaoFengPredictor, XiaoDunPredictor]

class KillGroupPredictor:
    """基于 6 种算法历史杀组胜率选择器的杀组预测器"""
    def __init__(self):
        self.predictors = []
        for cls in ALGO_CLASSES:
            try:
                self.predictors.append(cls())
            except Exception as e:
                logger.warning(f"[杀组] 算法 {cls.__name__} 实例化失败，已跳过: {e}")
        logger.info(f"杀组预测器已加载 {len(self.predictors)} 个算法引擎")

    @staticmethod
    def _get_kill(scores: dict) -> str:
        return max(scores, key=scores.get)

    def _backtest_win_rate(self, predictor, history: list) -> float:
        """取最近 50 期，在最近 20 期上做滚动回测，返回杀组胜率"""
        window = history[:50]
        if len(window) < 20:
            return 0.0
        wins = 0
        total = 0
        for i in range(20):
            train = window[i + 1:50]
            actual = window[i]["size"] + window[i]["odd_even"]
            try:
                scores = predictor.predict(train)
                predicted = self._get_kill(scores)
            except Exception:
                continue
            total += 1
            if predicted != actual:
                wins += 1
        return wins / total if total > 0 else 0.0

    def predict_kill(self, history: list) -> tuple[str, float]:
        """
        history: list of dict with keys: size, odd_even, total, nums, issue, dragon_tiger
        返回: (建议杀的组合, 置信度)
        策略: 使用最近 50 期数据，对 6 种算法在最近 20 期上做滚动回测，
             选择杀组胜率最高的单一算法，返回其在当前窗口的预测结果。
        """
        if not self.predictors or len(history) < 10:
            return "小单", 0.5

        best_predictor = None
        best_rate = -1.0
        for p in self.predictors:
            try:
                rate = self._backtest_win_rate(p, history)
                logger.info(f"[杀组回测] {getattr(p, 'name', p.__class__.__name__)} 杀组胜率 {rate:.2%}")
                if rate > best_rate:
                    best_rate = rate
                    best_predictor = p
            except Exception as e:
                logger.warning(f"[杀组回测] 算法 {getattr(p, 'name', p.__class__.__name__)} 回测失败: {e}")

        if best_predictor is None:
            return "小单", 0.5

        try:
            scores = best_predictor.predict(history[:50])
            best = self._get_kill(scores)
        except Exception:
            return "小单", 0.5

        confidence = min(0.99, max(0.25, best_rate))
        logger.info(f"[杀组选择器] 使用 {getattr(best_predictor, 'name', best_predictor.__class__.__name__)} -> 杀 {best} (胜率 {best_rate:.0%})")
        return best, confidence

# 全局杀组预测器实例
kill_group_predictor = KillGroupPredictor()


# ==================== ABC杀码（改进版 v10.2 — 自适应集成，移植自主文件） ====================
KILL_MODELS = {}

# ---------- 预测器工厂（10种策略） ----------
def _make_freq_pred(window, mode):
    """简单频率预测：mode=HOT杀热号, COLD杀冷号, MID杀中频号"""
    def pred(h):
        if len(h) < window: return random.randint(0, 9)
        cnt = Counter(h[:window])
        if mode == "HOT": return cnt.most_common(1)[0][0]
        elif mode == "COLD": return min(range(10), key=lambda x: cnt.get(x, 0))
        else:
            ranked = sorted(range(10), key=lambda x: cnt.get(x, 0))
            return ranked[len(ranked)//2]
    return pred

def _make_markov_pred(order, depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        if len(s) < order + 1: return random.randint(0, 9)
        trans = Counter()
        for i in range(order, len(s)):
            trans[(tuple(s[i-order:i]), s[i])] += 1
        cur = tuple(s[-order:])
        votes = Counter()
        for (k, nxt), c in trans.items():
            if k == cur: votes[nxt] += c
        return votes.most_common(1)[0][0] if votes else random.randint(0, 9)
    return pred

def _make_pattern_pred(plen, depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        if len(s) < plen + 1: return random.randint(0, 9)
        pat = tuple(s[-plen:])
        out = Counter()
        for i in range(len(s) - plen):
            if tuple(s[i:i+plen]) == pat: out[s[i+plen]] += 1
        return out.most_common(1)[0][0] if out else random.randint(0, 9)
    return pred

def _make_streak_pred(thresh, depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        streak = 1
        for v in s[1:]:
            if v == s[0]: streak += 1
            else: break
        if streak >= thresh:
            return min(range(10), key=lambda x: Counter(s).get(x, 0))
        return s[0]
    return pred

def _make_weighted_freq_pred(depth, bias, decay):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        scores = {n: 0.0 for n in range(10)}
        for i, x in enumerate(s):
            scores[x] += decay ** (len(s) - 1 - i)
        return max(scores, key=scores.get) if bias == "HOT" else min(scores, key=scores.get)
    return pred

def _make_gap_pred(depth, step):
    def pred(h):
        if len(h) < depth + step: return random.randint(0, 9)
        s = h[:depth + step]
        diffs = Counter()
        for i in range(len(s) - step):
            diffs[(s[i] - s[i+step]) % 10] += 1
        return (s[0] - diffs.most_common(1)[0][0]) % 10 if diffs else random.randint(0, 9)
    return pred

def _make_osc_pred(depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        if len(s) < 3: return random.randint(0, 9)
        diffs = [(s[i] - s[i+1]) % 10 for i in range(min(5, len(s)-1))]
        if diffs:
            return (s[0] - round(sum(diffs)/len(diffs))) % 10
        return random.randint(0, 9)
    return pred

def _make_cycle_pred(cycle_len):
    def pred(h):
        if len(h) < cycle_len + 1: return random.randint(0, 9)
        pos = len(h) % cycle_len
        same_pos = [h[i] for i in range(len(h)) if i % cycle_len == pos]
        return Counter(same_pos).most_common(1)[0][0] if same_pos else random.randint(0, 9)
    return pred

def _make_diff_direction_pred(depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        if len(s) < 4: return random.randint(0, 9)
        diffs = [(s[i] - s[i+1]) % 10 for i in range(min(8, len(s)-1))]
        if not diffs: return random.randint(0, 9)
        recent = diffs[:4]
        older = diffs[4:8] if len(diffs) >= 8 else diffs[4:]
        if older:
            avg_recent = sum(recent) / len(recent)
            avg_older = sum(older) / len(older)
            if abs(avg_recent - avg_older) > 2:
                return (s[0] + round(avg_recent)) % 10
            else:
                return (s[0] + random.choice([1, 9])) % 10
        return (s[0] + random.choice(diffs)) % 10
    return pred

def _make_simple_formula_pred(a, b, c, depth):
    def pred(h):
        if len(h) < depth: return random.randint(0, 9)
        s = h[:depth]
        avg = sum(s[:10]) / min(10, len(s[:10]))
        return int((a * s[0] + b * avg + c)) % 10
    return pred

# ---------- 构建模型池（每球200个，10种策略） ----------
def _build_abc_models():
    global KILL_MODELS
    KILL_MODELS.clear()
    rng = random.Random(random.randint(0, 2**32 - 1))
    sid = 0
    for ball in ["A", "B", "C"]:
        for i in range(30):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_freq_pred(rng.randint(5, 30), rng.choice(["HOT", "COLD", "MID"])),
                "ball": ball, "strategy": "freq"
            }
        for i in range(30):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_markov_pred(rng.randint(1, 4), rng.randint(10, 30)),
                "ball": ball, "strategy": "markov"
            }
        for i in range(25):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_pattern_pred(rng.randint(2, 5), rng.randint(10, 30)),
                "ball": ball, "strategy": "pattern"
            }
        for i in range(15):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_streak_pred(rng.randint(2, 5), rng.randint(10, 25)),
                "ball": ball, "strategy": "streak"
            }
        for i in range(25):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_weighted_freq_pred(rng.randint(10, 30), rng.choice(["HOT", "COLD"]), rng.uniform(0.8, 0.98)),
                "ball": ball, "strategy": "weighted"
            }
        for i in range(20):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_gap_pred(rng.randint(10, 25), rng.randint(1, 5)),
                "ball": ball, "strategy": "gap"
            }
        for i in range(15):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_osc_pred(rng.randint(8, 20)),
                "ball": ball, "strategy": "oscillation"
            }
        for i in range(15):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_cycle_pred(rng.randint(3, 15)),
                "ball": ball, "strategy": "cycle"
            }
        for i in range(10):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_diff_direction_pred(rng.randint(10, 25)),
                "ball": ball, "strategy": "diff"
            }
        for i in range(15):
            sid += 1
            KILL_MODELS[f"M{sid:04d}"] = {
                "func": _make_simple_formula_pred(rng.randint(1, 9), rng.randint(1, 5), rng.randint(0, 9), rng.randint(10, 25)),
                "ball": ball, "strategy": "formula"
            }
    logger.info(f"ABC杀码模型池构建完成: {len(KILL_MODELS)} 个模型 (10种策略)")

class HighWinRateManager:
    """ABC杀码管理器 v10.2 — 自适应集成
    1. 短期(10期)+长期(60期)双窗口权重，短期权重70%
    2. 逆反模式：近期准确率<42%时自动翻转预测
    3. 跨期记忆：追踪每球最近N期实际命中情况
    4. 自适应杀码数：低置信度时减少杀码
    5. 模型精选：只使用短期表现 Top-40% 的模型投票
    """

    SHORT_WINDOW = 10
    LONG_WINDOW = 60
    SHORT_WEIGHT = 0.7
    INVERSE_THRESHOLD = 0.42

    # 跨期记忆: {ball_type: {"recent_results": [True/False, ...], "consec_losses": int}}
    _memory = {}

    @classmethod
    def _get_memory(cls, ball_type):
        if ball_type not in cls._memory:
            cls._memory[ball_type] = {"recent_results": [], "consec_losses": 0}
        return cls._memory[ball_type]

    @classmethod
    def record_result(cls, ball_type, kill_nums, actual_num):
        """记录本期预测结果，用于跨期自适应"""
        mem = cls._get_memory(ball_type)
        hit = actual_num in kill_nums
        mem["recent_results"].append(hit)
        if len(mem["recent_results"]) > 20:
            mem["recent_results"] = mem["recent_results"][-20:]
        if not hit:
            mem["consec_losses"] += 1
        else:
            mem["consec_losses"] = 0

    @classmethod
    def _recent_accuracy(cls, ball_type):
        mem = cls._get_memory(ball_type)
        results = mem["recent_results"]
        if not results:
            return 0.5
        return sum(results) / len(results)

    @staticmethod
    def _history_to_ball(history, ball_type):
        """把 app.py 的 history 格式转换为该球位的数值列表（最新在前）"""
        bi = {"A": 0, "B": 1, "C": 2}[ball_type]
        return [item["nums"][bi] for item in history if "nums" in item and len(item.get("nums", [])) > bi]

    @staticmethod
    def get_strict_prediction(history, ball_type, kill_count=5):
        bh = HighWinRateManager._history_to_ball(history, ball_type)
        if not bh:
            return {"kill_nums": list(range(max(1, min(9, kill_count)))), "status": "数据不足"}

        kill_count = max(1, min(9, int(kill_count or 5)))
        models = {m: i for m, i in KILL_MODELS.items() if i["ball"] == ball_type}

        short_backtest = min(HighWinRateManager.SHORT_WINDOW, len(bh) - 1)
        long_backtest = min(HighWinRateManager.LONG_WINDOW, len(bh) - 1)

        model_stats = {}
        for mid, info in models.items():
            s_correct = 0
            for i in range(short_backtest):
                if i >= len(bh) - 1: break
                try:
                    if info["func"](bh[i+1:]) != bh[i]:
                        s_correct += 1
                except: continue
            short_acc = s_correct / short_backtest if short_backtest > 0 else 0.5

            l_correct = 0
            for i in range(long_backtest):
                if i >= len(bh) - 1: break
                try:
                    if info["func"](bh[i+1:]) != bh[i]:
                        l_correct += 1
                except: continue
            long_acc = l_correct / long_backtest if long_backtest > 0 else 0.5

            try:
                cur_pred = info["func"](bh)
            except:
                cur_pred = random.randint(0, 9)

            model_stats[mid] = {
                "short_acc": short_acc,
                "long_acc": long_acc,
                "combo": HighWinRateManager.SHORT_WEIGHT * short_acc + (1 - HighWinRateManager.SHORT_WEIGHT) * long_acc,
                "pred": cur_pred,
                "strategy": info.get("strategy", "unknown")
            }

        sorted_models = sorted(model_stats.items(), key=lambda x: x[1]["combo"], reverse=True)
        top_n = max(20, len(sorted_models) * 2 // 5)
        elite = sorted_models[:top_n]

        raw_kill_scores = {n: 0.0 for n in range(10)}
        for mid, stats in elite:
            w = stats["combo"]
            raw_kill_scores[stats["pred"]] += w

        max_score = max(raw_kill_scores.values()) or 1.0
        kill_scores = {n: round(s / max_score, 3) for n, s in raw_kill_scores.items()}
        sorted_kills = sorted(kill_scores.items(), key=lambda x: x[1], reverse=True)
        kill_nums = sorted([n for n, s in sorted_kills[:kill_count]])

        recent_acc = HighWinRateManager._recent_accuracy(ball_type)
        mem = HighWinRateManager._get_memory(ball_type)
        consec = mem["consec_losses"]

        should_inverse = (recent_acc < HighWinRateManager.INVERSE_THRESHOLD and len(mem["recent_results"]) >= 5) or consec >= 3

        if should_inverse:
            inverse_kills = sorted([n for n, s in sorted_kills[-kill_count:]])
            if set(inverse_kills) != set(kill_nums):
                kill_nums = inverse_kills
            else:
                all_nums = list(range(10))
                random.shuffle(all_nums)
                kill_nums = sorted(all_nums[:kill_count])

        top_k = [s for n, s in sorted_kills[:kill_count]]
        rest = [s for n, s in sorted_kills[kill_count:]]
        avg_top = sum(top_k) / len(top_k) if top_k else 0
        avg_rest = sum(rest) / len(rest) if rest else 0
        separation = max(0, avg_top - avg_rest)

        used_strategies = set()
        for n in kill_nums:
            for mid, stats in elite:
                if stats["pred"] == n:
                    used_strategies.add(stats["strategy"])
        strategy_count = len(used_strategies)
        diversity_bonus = min(strategy_count / 7.0, 1.0)
        confidence = kill_scores.get(kill_nums[0], 0) * 0.5 + separation * 0.3 + diversity_bonus * 0.2
        if should_inverse:
            confidence = confidence * 0.7

        if should_inverse: status = "逆反模式"
        elif confidence >= 0.85: status = "信心充足"
        elif confidence >= 0.70: status = "较为可靠"
        elif confidence >= 0.55: status = "谨慎参考"
        else: status = "盘面混乱"

        return {
            "model_id": f"Ensemble({len(elite)}elite/{len(models)}total)",
            "win_rate": round(kill_scores.get(kill_nums[0], 0), 3),
            "kill_num": kill_nums[0] if kill_nums else 0,
            "status": status,
            "bet_numbers": [n for n in range(10) if n not in kill_nums],
            "kill_nums": kill_nums,
            "confidence": round(confidence, 3),
            "strategy_count": strategy_count,
            "strategies_used": list(used_strategies),
            "kill_scores": kill_scores,
            "multi_kill_accuracy": round(kill_scores.get(kill_nums[0], 0), 3),
            "separation": round(separation, 3),
            "inverse": should_inverse,
            "consec_losses": consec,
            "recent_accuracy": round(recent_acc, 3)
        }

    @classmethod
    def get_all_predictions(cls, h, balls=None, kill_count=None):
        """获取所有球的预测结果。kill_count 支持 int 或 'A5,B3,C3' 字符串"""
        if balls is None:
            balls = ["A", "B", "C"]
        if kill_count is None:
            kill_map = {"A": 5, "B": 5, "C": 5}
        elif isinstance(kill_count, (int, float)):
            kc = int(kill_count)
            kill_map = {"A": kc, "B": kc, "C": kc}
        elif isinstance(kill_count, str):
            kill_map = cls._parse_kill_count_str(kill_count)
        elif isinstance(kill_count, dict):
            kill_map = {b: max(1, min(9, int(kill_count.get(b, 5)))) for b in ["A", "B", "C"]}
        else:
            kill_map = {"A": 5, "B": 5, "C": 5}
        return {b: cls.get_strict_prediction(h, b, kill_map.get(b, 5)) for b in balls}

    @staticmethod
    def _parse_kill_count_str(kc_str):
        """解析 'A5,B3,C3' 或 'A5' 格式的杀码数字符串"""
        result = {"A": 5, "B": 5, "C": 5}
        try:
            for part in str(kc_str).split(","):
                part = part.strip().upper()
                if not part:
                    continue
                ball = part[0]
                if ball in ("A", "B", "C"):
                    num = int(part[1:])
                    result[ball] = max(1, min(9, num))
        except (ValueError, IndexError):
            pass
        return result

    @classmethod
    def regenerate_abc_models(cls, history=None):
        """重新生成ABC杀码模型（与杀组模型同步刷新）"""
        _build_abc_models()
        cls._memory.clear()
        total = len(KILL_MODELS)
        logger.info(f"ABC杀码模型已重新生成！模型总数: {total} (10种策略), 跨期记忆已清空")
        return total


_build_abc_models()
abc_manager = HighWinRateManager()



@dataclass
class MarketData:
    issue_id: str
    number_str: str
    num_value: int
    combination: str

class RiskManager:
    def __init__(self, daily_stop_loss: float = 3000.0, daily_stop_profit: float = 5000.0):
        self.daily_stop_loss = daily_stop_loss
        self.daily_stop_profit = daily_stop_profit
        self.daily_pnl = 0.0
        self.last_pnl_reset_date: Optional[str] = None
        self._ensure_daily_reset()

    def _ensure_daily_reset(self):
        """按北京时间每天 00:00 自动把 daily_pnl 归零并更新日期标记"""
        today_str = datetime.now(BEIJING_TZ).date().isoformat()
        if self.last_pnl_reset_date != today_str:
            if self.last_pnl_reset_date is not None:
                logger.info(f"每日盈亏跨天重置(北京时间): {self.last_pnl_reset_date} -> {today_str}, 旧盈亏 {self.daily_pnl:+.2f} 归零")
            self.daily_pnl = 0.0
            self.last_pnl_reset_date = today_str

    def check_triggered(self) -> tuple[bool, str]:
        """检查是否已触发止盈或止损，返回 (是否触发, 原因)"""
        self._ensure_daily_reset()
        if self.daily_stop_loss > 0 and self.daily_pnl <= -self.daily_stop_loss:
            return True, f"已触及每日止损线 ({self.daily_stop_loss})"
        if self.daily_stop_profit > 0 and self.daily_pnl >= self.daily_stop_profit:
            return True, f"已触及每日止盈线 ({self.daily_stop_profit})"
        return False, ""

    def can_bet(self) -> tuple[bool, str]:
        triggered, reason = self.check_triggered()
        if triggered:
            return False, reason
        return True, "运行正常"

    def add_pnl(self, amount: float):
        self._ensure_daily_reset()
        self.daily_pnl += amount

    def to_dict(self):
        self._ensure_daily_reset()
        return {
            "daily_stop_loss": self.daily_stop_loss,
            "daily_stop_profit": self.daily_stop_profit,
            "daily_pnl": self.daily_pnl,
            "last_pnl_reset_date": self.last_pnl_reset_date
        }

    @classmethod
    def from_dict(cls, data):
        rm = cls(
            daily_stop_loss=data.get("daily_stop_loss", 3000.0),
            daily_stop_profit=data.get("daily_stop_profit", 5000.0),
        )
        rm.last_pnl_reset_date = data.get("last_pnl_reset_date", None)
        rm.daily_pnl = data.get("daily_pnl", 0.0)
        rm._ensure_daily_reset()
        return rm

# ==================== 3. 用户状态与登录上下文持久化 ====================
class UserState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.file_path = os.path.join(USER_DATA_DIR, f"{self.user_id}.json")
        self.lock = threading.Lock()
        self.is_logged_in = False
        self.is_active = False
        self.phone = ""
        self.groups = []
        self.history = []
        self.risk_mgr = RiskManager()
        self.client = None
        self.temp_phone_code_hash = None
        self.custom_delay = 12.0
        self.custom_suffix = ""  
        self.last_betted_issue = ""

        # 模式配置（ABC球 + 杀组）
        self.selected_modes = ["ball"]
        self.selected_balls = ["a"] 

        # ABC独立设置
        self.ball_bet_amount = 100.0
        self.abc_kill_count = 5           # 杀a球默认杀5码
        self.abc_martingale_multiplier = 2.0  # ABC倍投倍数
        self.abc_consecutive_losses = 0   # ABC连败次数

        # 上期ABC杀球记录 {b_char: [killed_digits]}
        self.last_ball_kills = {}

        # 杀组设置（基于30算法集成投票）
        self.kill_enabled = False
        self.kill_bet_amount = 100.0
        self.kill_martingale_multiplier = 2.0
        self.kill_consecutive_losses = 0
        self.kill_history = []            # 最近3期杀组记录，避免连杀同一组合
        self.last_killed_group = ""       # 上期实际杀的组合
        self.kill_last_settled_issue = "" # 上期已结算期号

        # 附加下注特码与豹子配置
        self.extra_special_numbers = []  
        self.extra_bauzi = False         
        self.extra_bet_amounts = {
            "0_27": 100.0,
            "1_26": 100.0,
            "baozi": 100.0
        }

        # 报数（播报）设置
        self.broadcast_enabled = False
        self.broadcast_channel = ""       # 播报目标频道 username 或 ID
        self.broadcast_title = "预测播报"
        self.broadcast_max_periods = 0    # 0 表示不限制
        self.broadcast_count = 0
        self.broadcast_history = []       # 播报历史记录
        self.broadcast_sent_issues = []   # 已播报期号
        self.broadcast_last_issue = ""    # 上一次处理的期号

        self.load()

    def load(self):
        with self.lock:
            if os.path.exists(self.file_path):
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.is_logged_in = data.get("is_logged_in", False)
                        self.is_active = data.get("is_active", False)
                        self.phone = data.get("phone", "")
                        self.groups = data.get("groups", [])
                        self.custom_delay = data.get("custom_delay", 12.0)
                        self.custom_suffix = data.get("custom_suffix", "")
                        # 兼容旧数据：过滤掉已删除的模式，支持 ball / kill
                        loaded_modes = data.get("selected_modes", ["ball"])
                        self.selected_modes = [m for m in loaded_modes if m in ("ball", "kill")]
                        if not self.selected_modes:
                            self.selected_modes = ["ball"]
                        self.selected_balls = data.get("selected_balls", ["a"])
                        self.ball_bet_amount = data.get("ball_bet_amount", 100.0)
                        self.abc_kill_count = data.get("abc_kill_count", 1)
                        self.abc_martingale_multiplier = data.get("abc_martingale_multiplier", 2.0)
                        self.abc_consecutive_losses = data.get("abc_consecutive_losses", 0)
                        self.last_ball_kills = data.get("last_ball_kills", {})
                        # 杀组
                        self.kill_enabled = data.get("kill_enabled", False)
                        self.kill_bet_amount = data.get("kill_bet_amount", 100.0)
                        self.kill_martingale_multiplier = data.get("kill_martingale_multiplier", 2.0)
                        self.kill_consecutive_losses = data.get("kill_consecutive_losses", 0)
                        self.kill_history = data.get("kill_history", [])
                        self.last_killed_group = data.get("last_killed_group", "")
                        self.kill_last_settled_issue = data.get("kill_last_settled_issue", "")
                        # 报数
                        self.broadcast_enabled = data.get("broadcast_enabled", False)
                        self.broadcast_channel = data.get("broadcast_channel", "")
                        self.broadcast_title = data.get("broadcast_title", "预测播报")
                        self.broadcast_max_periods = data.get("broadcast_max_periods", 0)
                        self.broadcast_count = data.get("broadcast_count", 0)
                        self.broadcast_history = data.get("broadcast_history", [])
                        self.broadcast_sent_issues = data.get("broadcast_sent_issues", [])
                        self.broadcast_last_issue = data.get("broadcast_last_issue", "")
                        # 附加
                        self.extra_bet_amounts = data.get("extra_bet_amounts", {"0_27": 100.0, "1_26": 100.0, "baozi": 100.0})
                        self.extra_special_numbers = data.get("extra_special_numbers", [])
                        self.extra_bauzi = data.get("extra_bauzi", False)
                        if "risk_mgr" in data:
                            self.risk_mgr = RiskManager.from_dict(data["risk_mgr"])
                except Exception as e:
                    logger.error(f"加载用户 {self.user_id} 档案出错: {e}")

    def save(self):
        with self.lock:
            try:
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "user_id": self.user_id, "is_logged_in": self.is_logged_in,
                        "is_active": self.is_active, "phone": self.phone, "groups": self.groups,
                        "custom_delay": self.custom_delay, "custom_suffix": self.custom_suffix,
                        "selected_modes": self.selected_modes, "selected_balls": self.selected_balls,
                        "ball_bet_amount": self.ball_bet_amount,
                        "abc_kill_count": self.abc_kill_count,
                        "abc_martingale_multiplier": self.abc_martingale_multiplier,
                        "abc_consecutive_losses": self.abc_consecutive_losses,
                        "last_ball_kills": self.last_ball_kills,
                        "kill_enabled": self.kill_enabled,
                        "kill_bet_amount": self.kill_bet_amount,
                        "kill_martingale_multiplier": self.kill_martingale_multiplier,
                        "kill_consecutive_losses": self.kill_consecutive_losses,
                        "kill_history": self.kill_history,
                        "last_killed_group": self.last_killed_group,
                        "kill_last_settled_issue": self.kill_last_settled_issue,
                        "broadcast_enabled": self.broadcast_enabled,
                        "broadcast_channel": self.broadcast_channel,
                        "broadcast_title": self.broadcast_title,
                        "broadcast_max_periods": self.broadcast_max_periods,
                        "broadcast_count": self.broadcast_count,
                        "broadcast_history": self.broadcast_history,
                        "broadcast_sent_issues": self.broadcast_sent_issues,
                        "broadcast_last_issue": self.broadcast_last_issue,
                        "extra_bet_amounts": self.extra_bet_amounts,
                        "extra_special_numbers": self.extra_special_numbers,
                        "extra_bauzi": self.extra_bauzi,
                        "risk_mgr": self.risk_mgr.to_dict()
                    }, f, ensure_ascii=False)
            except Exception as e:
                logger.error(f"保存用户 {self.user_id} 档案出错: {e}")

    async def try_reconnect(self):
        session_path = os.path.join(SESSIONS_DIR, f"user_{self.user_id}")
        if self.is_logged_in and os.path.exists(f"{session_path}.session"):
            try:
                self.client = TelegramClient(session_path, API_ID, API_HASH)
                await self.client.connect()
                if await self.client.is_user_authorized():
                    return True
                self.is_logged_in = False
                self.save()
            except Exception as e:
                logger.error(f"用户 {self.user_id} 重连失败: {e}")
        return False

# ==================== 4. 数据抓取核心 ====================
class DataFetcher:
    @staticmethod
    async def fetch_history_list():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(DATA_API_URL, timeout=15) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        return res.get("data", [])
        except Exception as e:
            logger.error(f"网络抓取异常: {e}")
            return []

    @staticmethod
    async def fetch_latest():
        raw_list = await DataFetcher.fetch_history_list()
        if raw_list:
            raw = raw_list[0]
            num_str = str(raw.get("number", ""))
            nums = [int(d) for d in num_str if d.isdigit()]
            total = int(raw.get("num", sum(nums[:3]) if nums else 0))
            return MarketData(str(raw.get("nbr")), num_str, total, str(raw.get("combination", "")))
        return None

    @staticmethod
    def parse_history(raw_data: list) -> list[dict]:
        parsed = []
        for item in raw_data:
            try:
                num_str = str(item.get("number", ""))
                nums = [int(d) for d in num_str if d.isdigit()]
                if len(nums) >= 3:
                    nums = nums[:3]
                    total = int(item.get("num", sum(nums)))
                    combo = item.get("combination", get_type(total))
                    parsed.append({"nums": nums, "sum": total, "type": combo, "issue": str(item.get("nbr", ""))})
            except:
                pass
        return parsed

# ==================== 4.5 杀组与播报辅助函数 ====================
def convert_to_algo_history(parsed_history: list) -> list:
    """将 parse_history 输出转换为 30 算法需要的格式"""
    algo_hist = []
    for rec in parsed_history:
        nums = rec.get("nums", [])
        total = rec.get("sum", 0)
        combo = rec.get("type", "")
        if len(combo) >= 2:
            size, parity = combo[0], combo[1]
        else:
            size = "大" if total >= 14 else "小"
            parity = "单" if total % 2 else "双"
        if len(nums) >= 3:
            if nums[0] > nums[2]:
                dt = "龙"
            elif nums[0] < nums[2]:
                dt = "虎"
            else:
                dt = "和"
        else:
            dt = "和"
        algo_hist.append({
            "issue": rec.get("issue", ""),
            "nums": nums,
            "total": total,
            "size": size,
            "odd_even": parity,
            "dragon_tiger": dt
        })
    return algo_hist

def get_next_qihao(qihao):
    """根据当前期号计算下一期号（支持纯数字或末尾数字）"""
    s = str(qihao)
    try:
        if s.isdigit():
            return str(int(s) + 1).zfill(len(s))
        match = re.search(r'(\d+)$', s)
        if match:
            num_part = match.group(1)
            prefix = s[:match.start()]
            next_num = str(int(num_part) + 1).zfill(len(num_part))
            return prefix + next_num
        return s
    except (ValueError, TypeError):
        return s

def build_broadcast_message(title: str, history_records: list, max_records: int = 10) -> str:
    """生成同款播报消息：期号.杀目标 状态+和值"""
    header = title.strip() if title else "预测播报"
    lines = [header]
    for rec in history_records[-max_records:]:
        q = str(rec.get('qihao', '--'))
        q = q[-4:] if len(q) >= 4 else q
        kill = rec.get('kill_target', '--') or '--'
        actual = rec.get('actual')
        s = str(rec.get('sum', '') or '')
        if actual is None:
            lines.append(f"{q}.杀{kill}")
        elif actual != kill:
            lines.append(f"{q}.杀{kill} 🀄{s}")
        else:
            lines.append(f"{q}.杀{kill} ❌{s}")
    return "\n".join(lines)

# ==================== 5. 系统中控与自动化调度中心 ====================
class SystemOrchestrator:
    def __init__(self):
        if not API_ID or not API_HASH:
            logger.warning("未配置 API_ID/API_HASH，Telegram Bot 功能不可用")
            self.bot = None
        else:
            self.bot = TelegramClient("telegram_sessions/bot_master", API_ID, API_HASH)
        self.users = {}
        self.user_login_states = {}
        self.last_issue_id = None

    def get_user_state(self, uid):
        if uid not in self.users:
            self.users[uid] = UserState(uid)
        return self.users[uid]

    def main_keyboard(self, u_state: UserState):
        status = "🟢 运行中" if u_state.is_active else "🔴 已暂停"
        login = "🚪 登出账号" if u_state.is_logged_in else "🔑 登录协议号"
        return [
            [Button.inline(f"状态: {status}", data=b"noop"), Button.inline(login, data=b"login")],
            [Button.inline("🚀 启动挂机", data=b"start"), Button.inline("⏹ 暂停挂机", data=b"stop")],
            [Button.inline("⚙️ 模式选择", data=b"select_mode")],
            [Button.inline("🎯 杀组设置", data=b"kill_settings"), Button.inline("📢 报数设置", data=b"broadcast_settings")],
            [Button.inline("💎 附加特码/豹子配置", data=b"extra_config")],
            [Button.inline("💰 独立金额与风控设置", data=b"set_amounts_menu")],
            [Button.inline("➕ 绑定群组", data=b"add_g"), Button.inline("➖ 移除群组", data=b"del_g"), Button.inline("📋 群组列表", data=b"list_g")],
            [Button.inline(f"⏱ 投递延迟: {u_state.custom_delay}s", data=b"set_delay"), Button.inline("📝 设置自定义尾缀", data=b"set_suffix")],
            [Button.inline("📖 模式介绍与说明", data=b"mode_intro_menu")],
            [Button.inline("📈 实时收益战报", data=b"stats")]
        ]

    def mode_selection_keyboard(self, u_state: UserState):
        def chk(m):
            return "✅ " if m in u_state.selected_modes else "⬜ "
        return [
            [Button.inline(f"{chk('ball')}启用 ABC杀球模式", data=b"toggle_mode_ball")],
            [Button.inline(f"{chk('kill')}启用 30算法杀组模式", data=b"toggle_mode_kill")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def mode_intro_keyboard(self):
        return [
            [Button.inline("ABC球模式介绍", data=b"intro_ball")],
            [Button.inline("30算法杀组模式介绍", data=b"intro_kill")],
            [Button.inline("特码与豹子介绍", data=b"intro_extra")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def extra_config_keyboard(self, u_state: UserState):
        c027 = "✅ " if "0_27" in u_state.extra_special_numbers else "⬜ "
        c126 = "✅ " if "1_26" in u_state.extra_special_numbers else "⬜ "
        cbz = "✅ " if u_state.extra_bauzi else "⬜ "
        return [
            [Button.inline(f"{c027}特码 0 / 27 (金额: {u_state.extra_bet_amounts.get('0_27', 100)})", data=b"toggle_extra_027")],
            [Button.inline(f"{c126}特码 1 / 26 (金额: {u_state.extra_bet_amounts.get('1_26', 100)})", data=b"toggle_extra_126")],
            [Button.inline(f"{cbz}豹子下注 (金额: {u_state.extra_bet_amounts.get('baozi', 100)})", data=b"toggle_extra_bauzi")],
            [Button.inline("✏️ 修改特码/豹子下注金额", data=b"set_extra_amounts")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def amounts_menu_keyboard(self, u_state: UserState):
        current_multiplier = u_state.abc_martingale_multiplier ** u_state.abc_consecutive_losses
        triggered, reason = u_state.risk_mgr.check_triggered()
        risk_status = f"🔴 {reason}" if triggered else "🟢 正常"
        return [
            [Button.inline(f"ABC杀球单注金额: {u_state.ball_bet_amount}", data=b"set_ball_amount")],
            [Button.inline(f"ABC倍投倍数: {u_state.abc_martingale_multiplier}x", data=b"set_abc_multiplier")],
            [Button.inline(f"ABC杀码数量: {u_state.abc_kill_count}个", data=b"set_abc_kill_count")],
            [Button.inline(f"杀组单注金额: {u_state.kill_bet_amount}", data=b"set_kill_amount")],
            [Button.inline(f"杀组倍投倍数: {u_state.kill_martingale_multiplier}x", data=b"set_kill_multiplier")],
            [Button.inline(f"每日止盈线: {u_state.risk_mgr.daily_stop_profit}", data=b"set_stop_profit")],
            [Button.inline(f"每日止损线: {u_state.risk_mgr.daily_stop_loss}", data=b"set_stop_loss")],
            [Button.inline(f"风控状态: {risk_status}", data=b"noop")],
            [Button.inline("特码与豹子独立金额设置", data=b"set_extra_amounts")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def kill_settings_keyboard(self, u_state: UserState):
        enabled = "✅ " if u_state.kill_enabled else "⬜ "
        return [
            [Button.inline(f"{enabled}启用杀组下注", data=b"toggle_kill_enabled")],
            [Button.inline(f"杀组单注金额: {u_state.kill_bet_amount}", data=b"set_kill_amount")],
            [Button.inline(f"杀组倍投倍数: {u_state.kill_martingale_multiplier}x", data=b"set_kill_multiplier")],
            [Button.inline(f"当前杀组连败: {u_state.kill_consecutive_losses}", data=b"noop")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def broadcast_settings_keyboard(self, u_state: UserState):
        enabled = "✅ " if u_state.broadcast_enabled else "⬜ "
        return [
            [Button.inline(f"{enabled}启用报数播报", data=b"toggle_broadcast")],
            [Button.inline(f"播报频道: {u_state.broadcast_channel or '未设置'}", data=b"set_broadcast_channel")],
            [Button.inline(f"播报标题: {u_state.broadcast_title}", data=b"set_broadcast_title")],
            [Button.inline(f"最大期数: {'∞' if u_state.broadcast_max_periods <= 0 else u_state.broadcast_max_periods}", data=b"set_broadcast_max")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def ball_selection_keyboard(self, current_balls: list):
        def mk_btn(b_char, name):
            checked = "✅ " if b_char in current_balls else "⬜ "
            return Button.inline(f"{checked}{name}", data=f"toggle_ball_{b_char}")
        return [
            [mk_btn("a", "A球（第1位）"), mk_btn("b", "B球（第2位）"), mk_btn("c", "C球（第3位）")],
            [Button.inline("💾 保存并返回设置", data=b"select_mode")]
        ]

    async def load_existing_users(self):
        if os.path.exists(USER_DATA_DIR):
            for file in os.listdir(USER_DATA_DIR):
                if file.endswith(".json"):
                    try:
                        uid = int(file.replace(".json", ""))
                        await self.get_user_state(uid).try_reconnect()
                    except:
                        pass

    async def do_broadcast(self, u: UserState, data: MarketData):
        """执行同款报数播报：核对上期并发送下期预测"""
        if not u.broadcast_enabled or not u.broadcast_channel or not u.client:
            return

        try:
            # 核对上期结果
            if u.broadcast_history:
                last_rec = u.broadcast_history[-1]
                last_rec['actual'] = data.combination
                last_rec['sum'] = data.num_value

            # 达到最大期数停止
            if u.broadcast_max_periods > 0 and u.broadcast_count >= u.broadcast_max_periods:
                logger.info(f"[用户 {u.user_id}] 播报已达上限 {u.broadcast_max_periods} 期，停止")
                u.broadcast_enabled = False
                u.save()
                try:
                    await self.bot.send_message(u.user_id, "【播报通知】已达到设定最大播报期数，已自动关闭报数。")
                except:
                    pass
                return

            # 生成下一期预测
            next_qihao = get_next_qihao(data.issue_id)
            rec = {'qihao': next_qihao, 'sum': data.num_value}
            try:
                algo_history = convert_to_algo_history(u.history)
                kill_target, _ = kill_group_predictor.predict_kill(algo_history)
                rec['kill_target'] = kill_target
            except Exception:
                rec['kill_target'] = '--'

            u.broadcast_history.append(rec)
            u.broadcast_history = u.broadcast_history[-20:]  # 保留最近20条

            msg = build_broadcast_message(u.broadcast_title, u.broadcast_history)
            if u.custom_delay > 0:
                await asyncio.sleep(u.custom_delay)
            await u.client.send_message(u.broadcast_channel, msg)
            u.broadcast_count += 1
            u.broadcast_sent_issues.append(data.issue_id)
            u.broadcast_sent_issues = u.broadcast_sent_issues[-200:]
            u.broadcast_last_issue = data.issue_id
            u.save()
            logger.info(f"[用户 {u.user_id}] 已播报 {next_qihao}，累计 {u.broadcast_count} 期")
        except Exception as e:
            logger.error(f"[用户 {u.user_id}] 播报失败: {e}")

    async def handle_new_issue_bet(self, u: UserState, issue_id: str, latest_market_data: MarketData = None):
        """根据最新开奖数据生成下一期实际下注内容并发送"""
        if u.last_betted_issue == issue_id:
            return

        can_bet, reason = u.risk_mgr.can_bet()
        if not can_bet:
            logger.info(f"[用户 {u.user_id}] 期号 {issue_id} 被风控拦截: {reason}")
            return
        if not u.groups:
            return

        all_bet_lines = []
        active_descriptions = []

        # 重置上期实际下注记录
        u.last_ball_kills = {}
        u.last_killed_group = ""

        # ========== 1. 生成所有预测（ABC球 + 杀组） ==========
        # ABC杀球模式：使用自适应集成模型（移植自主文件 v10.2）
        abc_multiplier = 1.0
        abc_pred_info = {}
        if "ball" in u.selected_modes:
            abc_multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
            count = max(1, min(9, u.abc_kill_count))
            try:
                preds = abc_manager.get_all_predictions(
                    u.history,
                    balls=[b_char.upper() for b_char in u.selected_balls],
                    kill_count=count
                )
                for b_char in u.selected_balls:
                    pred_info = preds.get(b_char.upper(), {})
                    kill_nums = pred_info.get("kill_nums")
                    if not kill_nums:
                        kill_nums = random.sample(range(10), count)
                    u.last_ball_kills[b_char] = kill_nums
                    abc_pred_info[b_char] = pred_info
                active_descriptions.append(f"ABC杀球(自适应集成杀{count}码,倍投{abc_multiplier:.1f}x)")
            except Exception as e:
                logger.error(f"[用户 {u.user_id}] ABC自适应集成预测失败: {e}")

        # 30算法杀组模式
        kill_target_for_bet = None
        kill_confidence_for_bet = 0.5
        kill_multiplier = 1.0
        if "kill" in u.selected_modes and u.kill_enabled:
            try:
                algo_history = convert_to_algo_history(u.history)
                kill_target, confidence = kill_group_predictor.predict_kill(algo_history)

                # 避免连续3期杀同一组合
                u.kill_history.append(kill_target)
                if len(u.kill_history) > 3:
                    u.kill_history.pop(0)
                if len(u.kill_history) == 3 and len(set(u.kill_history)) == 1:
                    other = [c for c in COMBOS if c != kill_target]
                    kill_target = random.choice(other)
                    u.kill_history = [kill_target]
                    logger.info(f"[用户 {u.user_id}] 连杀3期同一组合，强制换杀: {kill_target}")

                kill_target_for_bet = kill_target
                kill_confidence_for_bet = confidence
                u.last_killed_group = kill_target
                kill_multiplier = u.kill_martingale_multiplier ** u.kill_consecutive_losses
                active_descriptions.append(f"30算法杀组(杀{kill_target},置信{confidence:.0%},倍投{kill_multiplier:.1f}x)")
            except Exception as e:
                logger.error(f"[用户 {u.user_id}] 杀组预测失败: {e}")

        # ========== 2. 构造实际下注消息 ==========
        if "ball" in u.selected_modes and u.last_ball_kills:
            single_bet = u.ball_bet_amount * abc_multiplier
            for b_char in u.selected_balls:
                killed_digits = u.last_ball_kills.get(b_char)
                if killed_digits is None:
                    continue
                for d in range(10):
                    if d not in killed_digits:
                        all_bet_lines.append(f"{b_char}{d}/{int(single_bet)}")

        if "kill" in u.selected_modes and u.kill_enabled and kill_target_for_bet:
            single_bet = u.kill_bet_amount * kill_multiplier
            bet_combos = [c for c in COMBOS if c != kill_target_for_bet]
            for c in bet_combos:
                all_bet_lines.append(f"{c}/{int(single_bet)}")

        # 附加特码与豹子下注
        if "0_27" in u.extra_special_numbers:
            amt_027 = u.extra_bet_amounts.get("0_27", 100.0)
            all_bet_lines.append(f"0/{int(amt_027)}")
            all_bet_lines.append(f"27/{int(amt_027)}")
        if "1_26" in u.extra_special_numbers:
            amt_126 = u.extra_bet_amounts.get("1_26", 100.0)
            all_bet_lines.append(f"1/{int(amt_126)}")
            all_bet_lines.append(f"26/{int(amt_126)}")
        if u.extra_bauzi:
            amt_bz = u.extra_bet_amounts.get("baozi", 100.0)
            all_bet_lines.append(f"豹子/{int(amt_bz)}")

        if u.custom_suffix:
            all_bet_lines.append(u.custom_suffix)
        if not all_bet_lines:
            return

        bet_msg = "\n".join(all_bet_lines)

        if u.custom_delay > 0:
            await asyncio.sleep(u.custom_delay)
        if not u.is_active or not u.client:
            return

        sent_success = False
        for group in u.groups:
            try:
                await u.client.send_message(group, bet_msg)
                sent_success = True
                logger.info(f"[用户 {u.user_id}] 成功向群组 [{group}] 发送下注 (第 {issue_id} 期)")
            except Exception as e:
                logger.error(f"发送群组下注失败: {e}")

        if sent_success:
            u.last_betted_issue = issue_id
            u.save()
            try:
                mode_label = "+".join(active_descriptions)
                notify_lines = [
                    f"【自动化下注通知】",
                    f"--------------------",
                    f"期号: `{issue_id}`",
                    f"启用模式: `{mode_label}`",
                ]
                if kill_target_for_bet:
                    notify_lines.extend([
                        f"30算法杀组: `{kill_target_for_bet}`",
                        f"置信度: `{kill_confidence_for_bet:.0%}`",
                    ])
                notify_lines.extend([
                    f"下注排版:\n`{bet_msg.replace(chr(10), ' | ')}`",
                    f"--------------------"
                ])
                await self.bot.send_message(u.user_id, "\n".join(notify_lines))
            except:
                pass

    async def register_handlers(self):
        @self.bot.on(events.NewMessage(pattern="/start"))
        async def handler_start(event):
            u = self.get_user_state(event.sender_id)
            can_bet, reason = u.risk_mgr.can_bet()
            status_text = "运行中" if u.is_active else "已停止"
            if not can_bet:
                status_text += f" (风控: {reason})"
            kill_status = "启用" if ("kill" in u.selected_modes and u.kill_enabled) else "未启用"
            bc_status = "开启" if u.broadcast_enabled else "关闭"
            await event.respond(
                f"欢迎使用 PC28量子智能量化挂机系统\n"
                f"--------------------\n"
                f"运行状态概览:\n"
                f"• 挂机状态: `{status_text}`\n"
                f"• 绑定群组: `{len(u.groups)}` 个\n"
                f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
                f"• 30算法杀组: `{kill_status}`\n"
                f"• 报数播报: `{bc_status}`\n"
                f"• 今日盈亏: `{u.risk_mgr.daily_pnl:+.2f}`\n"
                f"--------------------",
                buttons=self.main_keyboard(u)
            )

        @self.bot.on(events.CallbackQuery)
        async def handler_callback(event):
            sid = event.sender_id
            u = self.get_user_state(sid)
            data = event.data.decode() if isinstance(event.data, bytes) else event.data

            if data == "noop":
                await event.answer()
                return

            if data == "select_mode":
                await event.edit("请选择要启用的模式", buttons=self.mode_selection_keyboard(u))
                return

            if data == "toggle_mode_ball":
                if "ball" in u.selected_modes:
                    if len(u.selected_modes) > 1:
                        u.selected_modes.remove("ball")
                else:
                    u.selected_modes.append("ball")
                u.save()
                await event.edit("请选择需要参与杀球的位次（可多选）", buttons=self.ball_selection_keyboard(u.selected_balls))
                return

            if data == "toggle_mode_kill":
                if "kill" in u.selected_modes:
                    u.selected_modes.remove("kill")
                else:
                    u.selected_modes.append("kill")
                u.save()
                await event.edit("请选择要启用的模式", buttons=self.mode_selection_keyboard(u))
                return

            if data.startswith("toggle_ball_"):
                b_char = data.replace("toggle_ball_", "")
                if b_char in u.selected_balls:
                    if len(u.selected_balls) > 1:
                        u.selected_balls.remove(b_char)
                else:
                    u.selected_balls.append(b_char)
                u.save()
                await event.edit("请选择需要参与杀球的位次（可多选）", buttons=self.ball_selection_keyboard(u.selected_balls))
                return

            if data == "set_amounts_menu":
                await event.edit("请选择需要修改的金额或风控参数", buttons=self.amounts_menu_keyboard(u))
                return

            if data == "mode_intro_menu":
                await event.edit("请选择要查看的模式介绍说明", buttons=self.mode_intro_keyboard())
                return

            if data == "intro_ball":
                await event.answer("杀a球模式：根据最新一期开奖号码（a+b+c=和值），按 和值÷abc×e 取小数部分，从小数点后第2位起提取5个不重复数字作为杀码。A/B/C球共用同一组杀码，系统自动投递剩余数字。中奖倍率9.99。", alert=True)
                return
            if data == "intro_kill":
                await event.answer("30算法杀组模式：集成30种预测算法（马尔可夫、随机森林、GBDT、SVM、贝叶斯、KNN等）投票，预测下一期最可能开出的组合并将其杀掉，自动投注其余3个组合。支持倍投与连败重置。", alert=True)
                return
            if data == "intro_extra":
                await event.answer("特码与豹子：支持独立设置金额并附加下注特码（0/27、1/26）以及豹子。", alert=True)
                return

            if data == "extra_config":
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_027":
                if "0_27" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("0_27")
                else:
                    u.extra_special_numbers.append("0_27")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_126":
                if "1_26" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("1_26")
                else:
                    u.extra_special_numbers.append("1_26")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_bauzi":
                u.extra_bauzi = not u.extra_bauzi
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "set_extra_amounts":
                self.user_login_states[sid] = "WAIT_EXTRA_AMOUNTS"
                await event.respond(
                    "请输入特码与豹子的独立下注金额格式（格式: 特码金额,豹子金额）\n"
                    f"当前设置 -> 特码: `{u.extra_bet_amounts.get('0_27', 100)}`, 豹子: `{u.extra_bet_amounts.get('baozi', 100)}`\n"
                    "例如输入: `200,100`"
                )
                return

            if data == "kill_settings":
                await event.edit("30算法杀组模式设置", buttons=self.kill_settings_keyboard(u))
                return

            if data == "toggle_kill_enabled":
                u.kill_enabled = not u.kill_enabled
                u.save()
                await event.edit("30算法杀组模式设置", buttons=self.kill_settings_keyboard(u))
                return

            if data == "set_kill_amount":
                self.user_login_states[sid] = "WAIT_KILL_AMOUNT"
                await event.respond(f"当前杀组单注金额: `{u.kill_bet_amount}`\n请输入新金额:")
                return

            if data == "set_kill_multiplier":
                self.user_login_states[sid] = "WAIT_KILL_MULTIPLIER"
                await event.respond(f"当前杀组倍投倍数: `{u.kill_martingale_multiplier}x`\n请输入新倍数(如 2.0 或 3.0):")
                return

            if data == "broadcast_settings":
                await event.edit("报数播报设置", buttons=self.broadcast_settings_keyboard(u))
                return

            if data == "toggle_broadcast":
                u.broadcast_enabled = not u.broadcast_enabled
                u.save()
                await event.edit("报数播报设置", buttons=self.broadcast_settings_keyboard(u))
                return

            if data == "set_broadcast_channel":
                self.user_login_states[sid] = "WAIT_BROADCAST_CHANNEL"
                await event.respond(f"当前播报频道: `{u.broadcast_channel or '未设置'}`\n请输入目标频道 Username 或 ID:")
                return

            if data == "set_broadcast_title":
                self.user_login_states[sid] = "WAIT_BROADCAST_TITLE"
                await event.respond(f"当前播报标题: `{u.broadcast_title}`\n请输入新标题:")
                return

            if data == "set_broadcast_max":
                self.user_login_states[sid] = "WAIT_BROADCAST_MAX"
                await event.respond(f"当前最大播报期数: `{'∞' if u.broadcast_max_periods <= 0 else u.broadcast_max_periods}`\n请输入新值（0 为不限制）:")
                return

            if data == "back_main":
                can_bet, reason = u.risk_mgr.can_bet()
                status_text = "运行中" if u.is_active else "已停止"
                if not can_bet:
                    status_text += f" (风控: {reason})"
                kill_status = "启用" if ("kill" in u.selected_modes and u.kill_enabled) else "未启用"
                bc_status = "开启" if u.broadcast_enabled else "关闭"
                await event.edit(
                    f"主控制面板\n"
                    f"--------------------\n"
                    f"• 挂机状态: `{status_text}`\n"
                    f"• 绑定群组: `{len(u.groups)}` 个\n"
                    f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                    f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
                    f"• 30算法杀组: `{kill_status}`\n"
                    f"• 报数播报: `{bc_status}`\n"
                    f"• 今日盈亏: `{u.risk_mgr.daily_pnl:+.2f}`\n"
                    f"--------------------",
                    buttons=self.main_keyboard(u)
                )
                return
            elif data == "start":
                if not u.is_logged_in or not u.groups:
                    await event.answer("请先登录账号并绑定至少一个目标群组!", alert=True)
                    return
                can_bet, reason = u.risk_mgr.can_bet()
                if not can_bet:
                    await event.answer(f"无法启动: {reason}，请修改止盈/止损线后重试", alert=True)
                    return
                u.is_active = True
                u.save()
                await event.edit("24小时挂机引擎已成功启动!", buttons=self.main_keyboard(u))
            elif data == "stop":
                u.is_active = False
                u.save()
                await event.edit("挂机已暂停。", buttons=self.main_keyboard(u))
            elif data == "set_delay":
                self.user_login_states[sid] = "WAIT_DELAY"
                await event.respond(f"当前延迟: `{u.custom_delay}s`\n请输入新投递延迟秒数:")
            elif data == "set_suffix":
                self.user_login_states[sid] = "WAIT_SUFFIX"
                await event.respond(f"当前尾缀: `{u.custom_suffix}`\n请输入新的独立尾缀内容(发送 `clear` 可清空):")
            elif data == "login":
                if u.is_logged_in:
                    u.is_logged_in = u.is_active = False
                    if u.client:
                        await u.client.disconnect()
                    u.save()
                    await event.edit("协议号已安全登出。", buttons=self.main_keyboard(u))
                else:
                    self.user_login_states[sid] = "WAIT_PHONE"
                    await event.respond("请发送您的 Telegram 手机号:")
            elif data == "add_g":
                self.user_login_states[sid] = "WAIT_GROUP"
                await event.respond("请发送目标群组的 Username 或 ID:")
            elif data == "del_g":
                if not u.groups:
                    await event.respond("当前没有绑定任何群组。")
                else:
                    self.user_login_states[sid] = "WAIT_DEL_GROUP"
                    await event.respond("发送对应的序号以移除群组:\n" + "\n".join([f"{i+1}. {g}" for i, g in enumerate(u.groups)]))
            elif data == "list_g":
                await event.respond("已绑定的目标群组列表:\n" + ("\n".join([f"{i+1}. {g}" for i, g in enumerate(u.groups)]) if u.groups else "无"))
            elif data == "set_ball_amount":
                self.user_login_states[sid] = "WAIT_BALL_AMOUNT"
                await event.respond(f"当前ABC杀球单注金额: `{u.ball_bet_amount}`\n请输入新金额:")
            elif data == "set_abc_multiplier":
                self.user_login_states[sid] = "WAIT_ABC_MULTIPLIER"
                await event.respond(f"当前ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n请输入新倍数(如 2.0 或 3.0):")
            elif data == "set_abc_kill_count":
                self.user_login_states[sid] = "WAIT_ABC_KILL_COUNT"
                await event.respond(f"当前ABC杀码数量: `{u.abc_kill_count}`个\n请输入数量(1-9):")
            elif data == "set_stop_profit":
                self.user_login_states[sid] = "WAIT_STOP_PROFIT"
                await event.respond(f"当前每日止盈线: `{u.risk_mgr.daily_stop_profit}`\n请输入新金额(输入 0 为不限制):")
            elif data == "set_stop_loss":
                self.user_login_states[sid] = "WAIT_STOP_LOSS"
                await event.respond(f"当前每日止损线: `{u.risk_mgr.daily_stop_loss}`\n请输入新金额(输入 0 为不限制):")
            elif data == "stats":
                rm = u.risk_mgr
                current_multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
                kill_multiplier = u.kill_martingale_multiplier ** u.kill_consecutive_losses
                can_bet, reason = rm.can_bet()
                triggered, trigger_reason = rm.check_triggered()
                await event.respond(
                    f"详细收益战报与风控统计\n"
                    f"--------------------\n"
                    f"• 今日总盈亏: `{rm.daily_pnl:+.2f}`\n"
                    f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                    f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
                    f"• ABC当前连败: `{u.abc_consecutive_losses}` 次\n"
                    f"• ABC当前计算单注: `{u.ball_bet_amount * current_multiplier:.2f}`\n"
                    f"• 杀组状态: `{'启用' if ('kill' in u.selected_modes and u.kill_enabled) else '未启用'}`\n"
                    f"• 杀组倍投倍数: `{u.kill_martingale_multiplier}x`\n"
                    f"• 杀组当前连败: `{u.kill_consecutive_losses}` 次\n"
                    f"• 杀组当前计算单注: `{u.kill_bet_amount * kill_multiplier:.2f}`\n"
                    f"• 报数播报: `{'开启' if u.broadcast_enabled else '关闭'}` (`{u.broadcast_count}` 期)\n"
                    f"• 每日止盈线: `{rm.daily_stop_profit}`\n"
                    f"• 每日止损线: `{rm.daily_stop_loss}`\n"
                    f"• 风控状态: `{'🔴 已触发: ' + trigger_reason if triggered else '🟢 正常'}`\n"
                    f"--------------------"
                )

        @self.bot.on(events.NewMessage)
        async def handler_text(event):
            if event.text.startswith("/"):
                return
            sid = event.sender_id
            state = self.user_login_states.get(sid)
            u = self.get_user_state(sid)

            if state == "WAIT_PHONE":
                u.phone = event.text.strip()
                try:
                    client = TelegramClient(os.path.join(SESSIONS_DIR, f"user_{sid}"), API_ID, API_HASH)
                    await client.connect()
                    req = await client.send_code_request(u.phone)
                    u.client, u.temp_phone_code_hash = client, req.phone_code_hash
                    self.user_login_states[sid] = "WAIT_CODE"
                    await event.respond("验证码已发送到您的 Telegram，请在 1 分钟内输入:")
                except Exception as e:
                    await event.respond(f"发送验证码失败: {e}")
                    self.user_login_states.pop(sid, None)
            elif state == "WAIT_CODE":
                code_text = event.text.strip()
                try:
                    await u.client.sign_in(u.phone, code_text, phone_code_hash=u.temp_phone_code_hash)
                    u.is_logged_in = True
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond("协议号登录成功!", buttons=self.main_keyboard(u))
                except SessionPasswordNeededError:
                    self.user_login_states[sid] = "WAIT_2FA"
                    await event.respond("检测到账户开启了两步验证 (2FA)，请输入密码:")
                except (PhoneCodeExpiredError, PhoneCodeInvalidError) as pce:
                    await event.respond(f"验证码已失效或错误: {pce}")
                    self.user_login_states.pop(sid, None)
                except Exception as e:
                    await event.respond(f"登录失败: {e}")
                    self.user_login_states.pop(sid, None)
            elif state == "WAIT_2FA":
                try:
                    await u.client.sign_in(password=event.text.strip())
                    u.is_logged_in = True
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond("2FA 验证通过，登录成功!", buttons=self.main_keyboard(u))
                except Exception as e:
                    await event.respond(f"密码错误: {e}")
            elif state == "WAIT_GROUP":
                grp = event.text.strip()
                if grp not in u.groups:
                    u.groups.append(grp)
                    u.save()
                await event.respond(f"成功绑定群组: `{grp}`", buttons=self.main_keyboard(u))
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_DEL_GROUP":
                val = event.text.strip()
                if val.isdigit() and 0 <= int(val) - 1 < len(u.groups):
                    rmv = u.groups.pop(int(val) - 1)
                    u.save()
                    await event.respond(f"已成功移除群组: `{rmv}`", buttons=self.main_keyboard(u))
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_DELAY":
                try:
                    u.custom_delay = max(0.0, float(event.text.strip()))
                    u.save()
                    await event.respond(f"投递延迟更新为: `{u.custom_delay}s`", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入有效的秒数数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_SUFFIX":
                txt = event.text.strip()
                u.custom_suffix = "" if txt.lower() == "clear" else txt
                u.save()
                await event.respond("独立尾缀已更新", buttons=self.main_keyboard(u))
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_BALL_AMOUNT":
                try:
                    u.ball_bet_amount = max(1.0, float(event.text.strip()))
                    u.save()
                    await event.respond("ABC杀球单注金额更新成功", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_ABC_MULTIPLIER":
                try:
                    val = float(event.text.strip())
                    u.abc_martingale_multiplier = max(1.0, val)
                    u.save()
                    await event.respond(f"ABC倍投倍数更新为: `{u.abc_martingale_multiplier}x`", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_ABC_KILL_COUNT":
                try:
                    val = int(event.text.strip())
                    u.abc_kill_count = max(1, min(9, val))
                    u.save()
                    await event.respond(f"ABC杀码数量更新为: `{u.abc_kill_count}`个", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入1-9之间的整数")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_STOP_PROFIT":
                try:
                    val = float(event.text.strip())
                    u.risk_mgr.daily_stop_profit = max(0.0, val)
                    u.save()
                    await event.respond(f"每日止盈线更新为: `{u.risk_mgr.daily_stop_profit}`", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_STOP_LOSS":
                try:
                    val = float(event.text.strip())
                    u.risk_mgr.daily_stop_loss = max(0.0, val)
                    u.save()
                    await event.respond(f"每日止损线更新为: `{u.risk_mgr.daily_stop_loss}`", buttons=self.main_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_EXTRA_AMOUNTS":
                try:
                    parts = event.text.strip().replace("，", ",").split(",")
                    amt1 = float(parts[0].strip())
                    amt2 = float(parts[1].strip()) if len(parts) > 1 else amt1
                    u.extra_bet_amounts["0_27"] = amt1
                    u.extra_bet_amounts["1_26"] = amt1
                    u.extra_bet_amounts["baozi"] = amt2
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond("特码与豹子独立金额更新成功", buttons=self.extra_config_keyboard(u))
                except:
                    await event.respond("格式错误，请重新输入，例如: `200,100`")
            elif state == "WAIT_KILL_AMOUNT":
                try:
                    u.kill_bet_amount = max(1.0, float(event.text.strip()))
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond(f"杀组单注金额更新为: `{u.kill_bet_amount}`", buttons=self.kill_settings_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                    self.user_login_states.pop(sid, None)
            elif state == "WAIT_KILL_MULTIPLIER":
                try:
                    val = float(event.text.strip())
                    u.kill_martingale_multiplier = max(1.0, val)
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond(f"杀组倍投倍数更新为: `{u.kill_martingale_multiplier}x`", buttons=self.kill_settings_keyboard(u))
                except:
                    await event.respond("请输入有效数字")
                    self.user_login_states.pop(sid, None)
            elif state == "WAIT_BROADCAST_CHANNEL":
                u.broadcast_channel = event.text.strip()
                u.save()
                self.user_login_states.pop(sid, None)
                await event.respond(f"播报频道更新为: `{u.broadcast_channel}`", buttons=self.broadcast_settings_keyboard(u))
            elif state == "WAIT_BROADCAST_TITLE":
                u.broadcast_title = event.text.strip() or "预测播报"
                u.save()
                self.user_login_states.pop(sid, None)
                await event.respond(f"播报标题更新为: `{u.broadcast_title}`", buttons=self.broadcast_settings_keyboard(u))
            elif state == "WAIT_BROADCAST_MAX":
                try:
                    u.broadcast_max_periods = max(0, int(event.text.strip()))
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond(f"最大播报期数更新为: `{'∞' if u.broadcast_max_periods <= 0 else u.broadcast_max_periods}`", buttons=self.broadcast_settings_keyboard(u))
                except:
                    await event.respond("请输入非负整数")
                    self.user_login_states.pop(sid, None)

    async def poll_api(self):
        """24小时全自动轮询与结算守护"""
        logger.info("24小时永动API轮询与结算守护线程已挂载...")
        while True:
            try:
                data = await DataFetcher.fetch_latest()
                if data and data.issue_id != self.last_issue_id:
                    self.last_issue_id = data.issue_id
                    for uid, u in self.users.items():
                        if u.is_logged_in:
                            # 初始化本期各模式盈亏
                            total_abc_pnl = 0.0
                            kill_pnl = 0.0

                            # ABC球模式结算逻辑（中奖倍率 9.99，按整体盈亏判定输赢）
                            if "ball" in u.selected_modes and u.last_ball_kills:
                                nums = [int(d) for d in data.number_str if d.isdigit()]
                                ball_index_map = {"a": 0, "b": 1, "c": 2}
                                has_any_bet = False

                                for b_char in u.selected_balls:
                                    if b_char in u.last_ball_kills and b_char in ball_index_map:
                                        killed_list = u.last_ball_kills[b_char]
                                        idx = ball_index_map[b_char]
                                        if len(nums) > idx:
                                            has_any_bet = True
                                            actual_digit = nums[idx]
                                            multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
                                            single_bet = u.ball_bet_amount * multiplier
                                            buy_count = 10 - u.abc_kill_count
                                            cost = buy_count * single_bet

                                            if actual_digit not in killed_list:
                                                win_amount = single_bet * 9.99
                                                total_abc_pnl += (win_amount - cost)
                                                logger.info(f"[用户 {uid}] ABC球 {b_char.upper()}球 中奖: 开奖{actual_digit} 不在杀码{killed_list}, 返还{win_amount:.2f}, 成本{cost:.2f}")
                                            else:
                                                total_abc_pnl -= cost
                                                logger.info(f"[用户 {uid}] ABC球 {b_char.upper()}球 未中: 开奖{actual_digit} 在杀码{killed_list}, 亏损{cost:.2f}")
                                            # 更新ABC自适应集成模型的跨期记忆
                                            try:
                                                abc_manager.record_result(b_char.upper(), killed_list, actual_digit)
                                            except Exception as e:
                                                logger.warning(f"记录ABC结果失败: {e}")

                                if has_any_bet:
                                    if total_abc_pnl != 0:
                                        u.risk_mgr.add_pnl(total_abc_pnl)
                                    if total_abc_pnl > 0:
                                        u.abc_consecutive_losses = 0
                                        logger.info(f"[用户 {uid}] ABC球本期整体盈利 {total_abc_pnl:.2f}，倍投重置归零")
                                    else:
                                        u.abc_consecutive_losses += 1
                                        logger.info(f"[用户 {uid}] ABC球本期整体亏损 {total_abc_pnl:.2f}，连败+1: {u.abc_consecutive_losses}")

                                u.last_ball_kills = {}

                            # 30算法杀组模式结算逻辑（杀中即亏损，杀错即盈利，赔率为 1:0.33 近似按投注3组中1组）
                            if "kill" in u.selected_modes and u.kill_enabled and u.last_killed_group:
                                if u.kill_last_settled_issue != data.issue_id:
                                    u.kill_last_settled_issue = data.issue_id
                                    actual_combo = data.combination
                                    last_kill = u.last_killed_group
                                    multiplier = u.kill_martingale_multiplier ** u.kill_consecutive_losses
                                    single_bet = u.kill_bet_amount * multiplier
                                    cost = 3 * single_bet  # 买3个组合

                                    if actual_combo == last_kill:
                                        # 杀中，亏损全部本金
                                        kill_pnl = -cost
                                        u.kill_consecutive_losses += 1
                                        logger.info(f"[用户 {uid}] 杀组命中: 杀{last_kill}=开{actual_combo}, 亏损{cost:.2f}, 连败{u.kill_consecutive_losses}")
                                    else:
                                        # 没杀中，3组中1组，净赢 single_bet * 2.85 - cost（按 PC28 组合赔率约 2.85 估算）
                                        win_amount = single_bet * 2.85
                                        kill_pnl = win_amount - cost
                                        u.kill_consecutive_losses = 0
                                        logger.info(f"[用户 {uid}] 杀组未中: 杀{last_kill}=开{actual_combo}, 盈利{kill_pnl:.2f}, 连败清零")

                                    u.risk_mgr.add_pnl(kill_pnl)
                                    u.save()

                                    try:
                                        await self.bot.send_message(
                                            u.user_id,
                                            f"【杀组结算通知】\n"
                                            f"--------------------\n"
                                            f"期号: `{data.issue_id}`\n"
                                            f"开奖组合: `{actual_combo}`\n"
                                            f"上期杀组: `{last_kill}`\n"
                                            f"本局盈亏: `{kill_pnl:+.2f}`\n"
                                            f"杀组连败: `{u.kill_consecutive_losses}`\n"
                                            f"--------------------"
                                        )
                                    except:
                                        pass

                            u.history.insert(0, {"nums": [int(d) for d in data.number_str if d.isdigit()], "sum": data.num_value, "type": data.combination, "issue": data.issue_id})
                            if len(u.history) > 120:
                                u.history = u.history[:120]
                            u.save()

                            # 报数播报（同款格式）
                            await self.do_broadcast(u, data)

                            # 结算后检查是否触发止盈/止损，触发则自动暂停挂机
                            triggered, trigger_reason = u.risk_mgr.check_triggered()
                            if triggered and u.is_active:
                                u.is_active = False
                                u.save()
                                logger.warning(f"[用户 {uid}] 触发风控自动暂停: {trigger_reason}, 盈亏={u.risk_mgr.daily_pnl:+.2f}")
                                try:
                                    await self.bot.send_message(
                                        u.user_id,
                                        f"【🚨 风控自动暂停通知】\n"
                                        f"--------------------\n"
                                        f"期号: `{data.issue_id}`\n"
                                        f"触发原因: `{trigger_reason}`\n"
                                        f"今日实时盈亏: `{u.risk_mgr.daily_pnl:+.2f}`\n"
                                        f"--------------------\n"
                                        f"挂机已自动暂停，如需继续请手动点击 🚀 启动挂机"
                                    )
                                except:
                                    pass

                            try:
                                await self.bot.send_message(
                                    u.user_id,
                                    f"【开奖结果通知】\n"
                                    f"--------------------\n"
                                    f"期号: `{data.issue_id}`\n"
                                    f"开奖: `{data.number_str}` (和值: `{data.num_value}` -> `{data.combination}`)\n"
                                    f"今日实时盈亏: `{u.risk_mgr.daily_pnl:+.2f}`\n"
                                    f"--------------------"
                                )
                            except:
                                pass

                            if u.is_active:
                                next_issue = get_next_qihao(data.issue_id)
                                asyncio.create_task(self.handle_new_issue_bet(u, next_issue, data))
            except Exception as e:
                logger.error(f"轮询守护异常自动隔离: {e}")

            await asyncio.sleep(4)

    async def start(self):
        if self.bot is None:
            logger.warning("Bot 未初始化，跳过 Telegram 启动，仅保留 Gradio 控制台")
            return
        await self.bot.start(bot_token=BOT_TOKEN)
        await self.register_handlers()
        await self.load_existing_users()
        logger.info("PC28量化挂机中控系统已成功全面上线!")
        asyncio.create_task(self.poll_api())
        await self.bot.run_until_disconnected()

# ==================== 6. Gradio 后台控制台 ====================
def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    orchestrator = SystemOrchestrator()
    try:
        loop.run_until_complete(orchestrator.start())
    except Exception as e:
        logger.error(f"Bot 运行异常: {e}")

with gr.Blocks(title="PC28量化智能挂机系统") as demo:
    gr.Markdown("# 🚀 PC28量化智能挂机系统 - 24小时永动中控")
    gr.Markdown("已集成6种算法动态回测选优杀组模式（π算法、双杀组、天子、5Y、小枫、小盾）与同款报数播报功能。ABC杀球模式支持自定义杀码数量、可配置倍投倍数（中奖倍率9.99），盈亏实时独立结算。达到止盈/止损线自动暂停，需手动重启。保留特码与豹子附加下注。")
    gr.Markdown("---")
    gr.Markdown("<div style='text-align: center; color: gray;'>PC28量化挂机中控台 © 2026</div>")

# 用 FastAPI 包装 Gradio，提供 /health 端点供 Railway 等平台做健康检查
fastapi_app = FastAPI(title="PC28量化智能挂机系统")

@fastapi_app.get("/health")
def health_check():
    return {"status": "healthy", "algorithms": len(ALGO_CLASSES)}

fastapi_app = mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    # 仅在直接运行时才启动 Telegram Bot 线程，避免部署平台导入模块时触发
    threading.Thread(target=start_bot_thread, daemon=True).start()
    port = int(os.getenv("PORT", "7860"))
    logger.info(f"启动 Gradio/FastAPI 服务，监听 0.0.0.0:{port}")
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_level="warning")
