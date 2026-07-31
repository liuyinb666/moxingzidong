import os
import sys
import re
import json
import math
import asyncio
import logging
import threading
import random
from collections import defaultdict, Counter
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
import aiohttp
import gradio as gr
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError

# ==================== 1. 核心常量与工具函数 ====================
def get_type(s: int) -> str:
    return ('大' if s >= 14 else '小') + ('单' if s % 2 else '双')

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

# ==================== 内置 4 算法动态选择矩阵 ====================

class Group(Enum):
    SMALL_ODD = "小单"
    SMALL_EVEN = "小双"
    BIG_ODD = "大单"
    BIG_EVEN = "大双"

OPPOSITE_GROUP = {
    Group.SMALL_ODD: Group.BIG_EVEN,
    Group.BIG_EVEN: Group.SMALL_ODD,
    Group.SMALL_EVEN: Group.BIG_ODD,
    Group.BIG_ODD: Group.SMALL_EVEN,
}

TYPICAL_CODES = {
    Group.SMALL_ODD: [5, 7, 9, 11, 3, 1, 13],
    Group.SMALL_EVEN: [4, 6, 8, 10, 2, 0, 12],
    Group.BIG_ODD: [15, 17, 19, 21, 23, 25, 27],
    Group.BIG_EVEN: [16, 18, 20, 22, 14, 24, 26],
}

class Draw:
    def __init__(self, period: str, hundreds: int, tens: int, ones: int):
        self.period = period
        self.hundreds = hundreds
        self.tens = tens
        self.ones = ones

    @property
    def sum_value(self) -> int:
        return self.hundreds + self.tens + self.ones

    @property
    def seven_y(self) -> int:
        return self.sum_value % 7

    @property
    def group(self) -> Group:
        s = self.sum_value
        if s <= 13:
            return Group.SMALL_ODD if s % 2 == 1 else Group.SMALL_EVEN
        else:
            return Group.BIG_ODD if s % 2 == 1 else Group.BIG_EVEN

    def __str__(self):
        return f"{self.hundreds}+{self.tens}+{self.ones}"


def compute_xiaofeng_algorithm(data, index):
    if index >= len(data) or len(data) < 3:
        return None

    draws = []
    for item in data:
        nums = item['nums']
        draws.append(Draw(
            period=str(item['issue']),
            hundreds=nums[0],
            tens=nums[1],
            ones=nums[2]
        ))

    if len(draws) < 3:
        return None

    current = draws[index] if index < len(draws) else draws[0]
    seven_y = current.seven_y

    refs = []
    for i, d in enumerate(draws[index+1:], index+1):
        if d.seven_y == seven_y:
            refs.append((d, i - index))
            if len(refs) >= 5:
                break

    votes: Dict[Group, int] = {}
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

    for ref, distance in refs:
        _, get_digits = DIGIT_MAP[seven_y]
        taken = get_digits(ref)

        if seven_y == 3:
            new_digit = (current.hundreds + current.tens + sum(taken)) % 10
            new_draw = Draw("新", new_digit, new_digit, current.ones)
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
            new_draw = Draw("新", h, t, o)

        y_n = new_draw.sum_value % 7
        is_kill = False
        group = None

        if y_n == 0:
            is_kill, group = True, Group.SMALL_ODD
        elif y_n == 1:
            is_kill, group = True, Group.BIG_ODD
        elif y_n == 2:
            is_kill, group = True, Group.SMALL_EVEN
        elif y_n == 3:
            is_kill, group = True, Group.BIG_EVEN
        elif y_n == 4:
            is_kill, group = True, Group.SMALL_ODD
        elif y_n == 5:
            is_kill, group = True, new_draw.group
        elif y_n == 6:
            is_kill, group = True, OPPOSITE_GROUP.get(new_draw.group, new_draw.group)

        if is_kill and group:
            weight = 3 if distance == 1 else (2 if distance == 2 else 1)
            votes[group] = votes.get(group, 0) + weight

    if votes:
        kill_group = max(votes, key=votes.get)
    else:
        counts = Counter(d.group for d in draws)
        kill_group = counts.most_common()[-1][0] if counts else Group.SMALL_ODD

    if len(draws) >= 3:
        last1 = draws[1].group if len(draws) > 1 else None
        last2 = draws[2].group if len(draws) > 2 else None
        if last1 == last2 and last1 == kill_group:
            kill_group = OPPOSITE_GROUP.get(kill_group, kill_group)

    kill_str = "杀" + kill_group.value

    recent_sums = [d.sum_value for d in draws[:50]]
    recent_avg = sum(recent_sums) / len(recent_sums) if recent_sums else 14

    typical = TYPICAL_CODES.get(kill_group, list(range(0, 28)))
    candidates = []
    for code in typical:
        deviation = abs(code - recent_avg)
        freq_penalty = 0
        if recent_sums:
            freq = recent_sums.count(code)
            freq_penalty = freq * 1.5
        candidates.append((code, deviation + freq_penalty))

    candidates.sort(key=lambda x: x[1])
    pred_sum = candidates[0][0] if candidates else 14

    if recent_sums and len(recent_sums) >= 3:
        last3_avg = sum(recent_sums[:3]) / 3
        if abs(pred_sum - last3_avg) > 10:
            if pred_sum < 14:
                pred_sum = min(typical, key=lambda x: abs(x - (recent_avg + 5)))
            else:
                pred_sum = min(typical, key=lambda x: abs(x - (recent_avg - 5)))

    pred_sum = max(0, min(27, pred_sum))

    return {'kill': kill_str, 'sum': pred_sum}


def get_combo(sum_value):
    return ("小" if sum_value <= 13 else "大") + ("双" if sum_value % 2 == 0 else "单")


def normalize_r(R):
    while R > 27:
        R -= 28
    while R < 0:
        R += 28
    return max(0, min(27, R))


def compute_main_algorithm(data, index):
    if index >= len(data) or index + 15 >= len(data):
        return None

    cur, back5, back10, back15 = data[index], data[index+5], data[index+10], data[index+15]

    a, b, c = cur['nums']
    a5, b5, c5 = back5['nums']
    a10, b10, c10 = back10['nums']
    a15, b15, c15 = back15['nums']

    S, S5, S10, S15 = sum(cur['nums']), sum(back5['nums']), sum(back10['nums']), sum(back15['nums'])

    if S == 0:
        S = 1

    T1 = (a + c) * b + b10
    T2 = (a5 + c5) * b5 + b15
    R = (T1 + T2) // 2

    momentum = (S - S5) + (S5 - S10) + (S10 - S15)
    R += max(-5, min(5, momentum // 3))
    R = normalize_r(R)

    recent_sums = [data[i]['sum'] for i in range(index + 1, min(index + 50, len(data)))]
    if recent_sums:
        recent_avg = sum(recent_sums) / len(recent_sums)
        recent_std = (sum((x - recent_avg) ** 2 for x in recent_sums) / len(recent_sums)) ** 0.5 if recent_sums else 5

        if recent_std > 6:
            R = normalize_r(int(recent_avg) + random.randint(-3, 3))
        elif abs(R - recent_avg) > 8:
            R = normalize_r(int(recent_avg) + (R - int(recent_avg)) // 2)

        recent_counts = Counter(recent_sums[-8:])
        if recent_counts and recent_counts.most_common(1)[0][1] >= 3:
            freq_val = recent_counts.most_common(1)[0][0]
            if abs(R - freq_val) < 3:
                R = normalize_r(R + 7)

        if len(recent_sums) >= 5:
            last5_avg = sum(recent_sums[:5]) / 5
            if R < 10 and last5_avg > 18:
                R = normalize_r(R + 14)
            elif R > 17 and last5_avg < 9:
                R = normalize_r(R - 14)

    kill = "杀" + get_combo(R)

    return {'kill': kill, 'sum': R}


def compute_5y_algorithm(data, index):
    if index >= len(data) or index + 10 >= len(data):
        return None

    cur, back5, back10 = data[index], data[index+5], data[index+10]

    a, b, c = cur['nums']
    a5, b5, c5 = back5['nums']
    a10, b10, c10 = back10['nums']

    S, S5, S10 = sum(cur['nums']), sum(back5['nums']), sum(back10['nums'])

    if S == 0:
        S = 1

    valB = (b % 5 + 1)
    valS = (S % 5 + 1)
    base = (valB * valS) % 10

    volatility = abs(S - S5) + abs(S5 - S10)
    volatility_factor = (volatility % 5) + 1

    trend = 2 if (S > S5 and S5 > S10) else (0 if (S < S5 and S5 < S10) else 1)

    R = normalize_r(base * 3 + volatility_factor * 2 + trend)

    recent_sums = [data[i]['sum'] for i in range(index + 1, min(index + 50, len(data)))]
    if recent_sums:
        recent_avg = sum(recent_sums) / len(recent_sums)
        recent_var = sum((x - recent_avg) ** 2 for x in recent_sums) / len(recent_sums)

        if recent_var > 20:
            R = normalize_r(int(recent_avg) + random.randint(-4, 4))

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
                R = normalize_r(int(weighted_avg * 0.6 + R * 0.4))

        if len(recent_sums) >= 6:
            first3 = sum(recent_sums[:3]) / 3
            last3 = sum(recent_sums[-3:]) / 3
            diff = last3 - first3
            if abs(diff) > 5:
                R = normalize_r(R + int(diff / 2))

    if index + 1 < len(data):
        recent_shapes = [get_combo(data[i]['sum']) for i in range(index + 1, min(index + 6, len(data)))]
        kill_shape = get_combo(R)
        if kill_shape in recent_shapes:
            R = normalize_r(R + 7)
            if get_combo(R) == kill_shape:
                R = normalize_r(R + 14)

    kill = "杀" + get_combo(R)

    return {'kill': kill, 'sum': R}


class PC28PredictorV7:
    def __init__(self):
        self.history = []
        self.global_prior = {
            '小单': 0.20, '小双': 0.28, '大单': 0.24, '大双': 0.28
        }
        self.alpha = 1.0

    def add_data(self, data_list):
        self.history = []
        for item in data_list:
            if 'expect' in item:
                period = item['expect']
                numbers = item.get('opennum', item.get('numbers', ''))
                if '+' in str(numbers):
                    nums = [int(n) for n in str(numbers).split('+')]
                else:
                    nums = [int(n) for n in str(numbers)]
                total = sum(nums)
            else:
                period = item.get('period', '')
                numbers = item.get('numbers', '')
                total = item.get('s', 0)
                if '+' in str(numbers):
                    nums = [int(n) for n in str(numbers).split('+')]
                else:
                    nums = []

            is_big = total >= 14
            is_single = total % 2 == 1
            combination = ('大' if is_big else '小') + ('单' if is_single else '双')

            self.history.append({
                'period': period,
                'numbers': numbers,
                'total': total,
                'combination': combination,
                'is_big': is_big,
                'is_single': is_single,
                'nums': nums,
                'yu5': total % 5
            })

    def get_smoothed_trans_prob(self, from_combo, to_combo, history_slice):
        trans_count = 0
        total_from = 0
        for i in range(len(history_slice) - 1):
            if history_slice[i]['combination'] == from_combo:
                total_from += 1
                if history_slice[i+1]['combination'] == to_combo:
                    trans_count += 1
        num_classes = 4
        smoothed_prob = (trans_count + self.alpha) / (total_from + self.alpha * num_classes)
        return smoothed_prob

    def get_cold_streak(self, combo, history_slice):
        n = len(history_slice)
        streak = 0
        for i in range(n - 1, -1, -1):
            if history_slice[i]['combination'] == combo:
                break
            streak += 1
        return streak

    def calculate_next_prob(self, combo, history_slice):
        if len(history_slice) < 1:
            return self.global_prior[combo]
        current = history_slice[-1]['combination']
        n = len(history_slice)
        trans_prob = self.get_smoothed_trans_prob(current, combo, history_slice)
        global_freq = sum(1 for d in history_slice if d['combination'] == combo) / n
        recent = history_slice[-10:] if n >= 10 else history_slice
        recent_freq = sum(1 for d in recent if d['combination'] == combo) / len(recent)
        short = history_slice[-5:] if n >= 5 else history_slice
        short_freq = sum(1 for d in short if d['combination'] == combo) / len(short)
        combined_prob = (
            trans_prob * 0.40 +
            global_freq * 0.15 +
            recent_freq * 0.25 +
            short_freq * 0.20
        )
        return combined_prob

    def _compute_probs_and_cold(self, history_slice):
        probs = {}
        cold_streaks = {}
        for combo in ['小单', '小双', '大单', '大双']:
            probs[combo] = self.calculate_next_prob(combo, history_slice)
            cold_streaks[combo] = self.get_cold_streak(combo, history_slice)
        return probs, cold_streaks

    def predict_kill_group(self):
        if len(self.history) < 10:
            return None, 0
        probs, cold_streaks = self._compute_probs_and_cold(self.history)
        protected = set()
        for combo in ['小单', '小双', '大单', '大双']:
            if cold_streaks[combo] >= 5:
                protected.add(combo)
        candidates = [c for c in ['小单', '小双', '大单', '大双'] if c not in protected]
        if not candidates:
            candidates = ['小单', '小双', '大单', '大双']
        kill_group = min(candidates, key=lambda c: probs[c])
        sorted_probs = sorted(probs.values())
        prob_gap = sorted_probs[1] - sorted_probs[0] if len(sorted_probs) > 1 else 0
        confidence = 0.70 + min(prob_gap * 2, 0.20)
        if prob_gap > 0.08:
            confidence += 0.03
        if len(self.history) >= 20:
            val_acc = self._validate()
            confidence = confidence * 0.6 + val_acc * 0.4
        confidence = min(confidence, 0.92)
        return kill_group, confidence

    def _validate(self):
        correct = 0
        total = 0
        for i in range(10, len(self.history)):
            hist_slice = self.history[:i]
            probs, cold_streaks = self._compute_probs_and_cold(hist_slice)
            protected = set()
            for combo in ['小单', '小双', '大单', '大双']:
                if cold_streaks[combo] >= 5:
                    protected.add(combo)
            candidates = [c for c in ['小单', '小双', '大单', '大双'] if c not in protected]
            if not candidates:
                candidates = ['小单', '小双', '大单', '大双']
            predicted_kill = min(candidates, key=lambda c: probs[c])
            actual = self.history[i]['combination']
            if actual == predicted_kill:
                correct += 1
            total += 1
        return (correct / total) if total > 0 else 0.7

    def find_double_group(self, kill_group):
        all_groups = ['小单', '小双', '大单', '大双']
        remaining = [g for g in all_groups if g != kill_group]
        if len(self.history) < 5:
            return remaining[:2], 0.5
        probs = {}
        for combo in remaining:
            probs[combo] = self.calculate_next_prob(combo, self.history)
        sorted_groups = sorted(probs.items(), key=lambda x: x[1], reverse=True)
        best_two = [sorted_groups[0][0], sorted_groups[1][0]]
        expected_rate = sorted_groups[0][1] + sorted_groups[1][1]
        return best_two, min(expected_rate, 0.80)

    def recommend_codes(self, double_group):
        codes = {}
        recent = self.history[-30:] if len(self.history) >= 30 else self.history
        for combo in double_group:
            size, parity = combo[0], combo[1]
            valid = []
            for t in range(28):
                t_big = t >= 14
                t_single = t % 2 == 1
                match = (t_big == (size == '大')) and (t_single == (parity == '单'))
                if match:
                    freq = sum(1 for d in recent if d['total'] == t)
                    valid.append((t, freq))
            valid.sort(key=lambda x: x[1], reverse=True)
            codes[combo] = [t[0] for t in valid[:2]]
        return codes

    def get_trend(self):
        if len(self.history) < 5:
            return None
        recent = self.history[-5:]
        return {
            'big_count': sum(1 for d in recent if d['is_big']),
            'single_count': sum(1 for d in recent if d['is_single']),
            'avg_total': sum(d['total'] for d in recent) / 5,
            'recent_totals': [d['total'] for d in recent]
        }


def xiaodun_predict(data_list):
    predictor = PC28PredictorV7()
    predictor.add_data(data_list)
    kill_group, confidence = predictor.predict_kill_group()
    double_group, double_rate = predictor.find_double_group(kill_group)
    special_codes = predictor.recommend_codes(double_group)
    return {
        'kill_group': kill_group,
        'double_group': double_group,
        'special_codes': special_codes
    }


def compute_xiaodun_algorithm(data, index):
    if len(data) < 10:
        return None
    xd_data = []
    for d in data:
        xd_data.append({
            'period': str(d['issue']),
            'opennum': f"{d['nums'][0]}+{d['nums'][1]}+{d['nums'][2]}"
        })
    result = xiaodun_predict(xd_data)
    if not result or result['kill_group'] is None:
        return None
    kill_group = result['kill_group']
    double_group = result['double_group']
    special_codes = result['special_codes']
    all_codes = []
    for codes in special_codes.values():
        all_codes.extend(codes)
    unique_codes = sorted(set(all_codes))
    recommend_sums = unique_codes[:6]
    pred_sum = all_codes[0] if all_codes else 14
    return {
        'kill': '杀' + kill_group,
        'sum': pred_sum,
        'recommendations': double_group,
        'recommend_sums': recommend_sums
    }


class KillGroupPredictor:
    """基于 4 算法回测动态选择的杀组预测器"""

    ALGORITHMS = {
        'tianzi': {'name': '天子算法', 'fn': compute_main_algorithm},
        '5y': {'name': '5Y算法', 'fn': compute_5y_algorithm},
        'xiaofeng': {'name': '小枫算法', 'fn': compute_xiaofeng_algorithm},
        'xiaodun': {'name': '小盾算法', 'fn': compute_xiaodun_algorithm},
    }

    def __init__(self):
        logger.info(f"杀组预测器已加载 {len(self.ALGORITHMS)} 个算法引擎")

    def _history_to_algo_data(self, history: list) -> list:
        """把最旧→最新的 history 转成算法需要的新est-first数据"""
        return [
            {
                'issue': str(rec.get('issue', '')),
                'nums': rec.get('nums', []),
                'sum': rec.get('total', 0),
            }
            for rec in reversed(history)
        ]

    def _predict_one(self, key: str, history: list) -> Optional[str]:
        """调用单个算法，返回杀组字符串（不含'杀'），失败返回 None"""
        fn = self.ALGORITHMS[key]['fn']
        data = self._history_to_algo_data(history)
        if not data:
            return None
        try:
            result = fn(data, 0)
        except Exception:
            return None
        if not result or not isinstance(result, dict):
            return None
        kill = result.get('kill')
        if kill is None:
            return None
        if isinstance(kill, str):
            kill = kill.replace('杀', '')
        return kill if kill in COMBOS else None

    def _majority_kill(self, predictions: List[str]) -> str:
        if not predictions:
            return '小单'
        counts = Counter(predictions)
        return counts.most_common(1)[0][0]

    def predict_kill(self, history: list) -> tuple[str, float]:
        """
        history: list of dict with keys: size, odd_even, total, nums, issue
                 时序须为 最旧 → 最新
        返回: (建议杀的组合, 置信度)
        逻辑: 回测最近10期，选择胜率最高的算法，并用其预测当前期；每期都重新回测并选择
        """
        if not history:
            return '小单', 0.5

        n_backtest = 10
        min_hist = max(2, n_backtest)

        # 历史不足时退化到 4 算法投票
        if len(history) < min_hist:
            preds = []
            for key in self.ALGORITHMS:
                pred = self._predict_one(key, history)
                if pred:
                    preds.append(pred)
            kill_target = self._majority_kill(preds)
            logger.info(f"[杀组动态选择] 历史不足{min_hist}期，退化为多数投票 -> 杀 {kill_target}")
            return kill_target, 0.5

        # 回测窗口：最近 n_backtest 期
        test_start = len(history) - n_backtest
        win_rates = {}

        for key in self.ALGORITHMS:
            wins = 0
            valid = 0
            for i in range(test_start, len(history)):
                train_hist = history[:i]
                actual = history[i]['size'] + history[i]['odd_even']
                pred = self._predict_one(key, train_hist)
                if pred is None:
                    continue
                valid += 1
                # 算法给出的是“最可能开出的组合”，杀掉它；实际不等于预测则视为该策略赢
                if pred != actual:
                    wins += 1
            if valid > 0:
                win_rates[key] = wins / valid

        if not win_rates:
            return '小单', 0.5

        best_key = max(win_rates, key=win_rates.get)
        best_rate = win_rates[best_key]
        kill_target = self._predict_one(best_key, history)
        if kill_target is None:
            kill_target = '小单'

        confidence = min(0.99, max(0.25, best_rate))
        logger.info(f"[杀组动态选择] 近{n_backtest}期回测最高胜率: {self.ALGORITHMS[best_key]['name']} ({best_rate:.1%}) -> 杀 {kill_target}")
        return kill_target, confidence


# 全局杀组预测器实例
kill_group_predictor = KillGroupPredictor()

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
        self.abc_kill_count = 1           # 每个球杀码数量（1-9）
        self.abc_martingale_multiplier = 2.0  # ABC倍投倍数
        self.abc_consecutive_losses = 0   # ABC连败次数

        # 上期ABC杀球记录 {b_char: [killed_digits]}
        self.last_ball_kills = {}

        # 杀组设置（基于4算法动态选择）
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
    """将 parse_history 输出转换为 30 算法需要的格式（时序：最旧 → 最新）"""
    algo_hist = []
    for rec in reversed(parsed_history):  # 转为最旧→最新，方便算法统一用 history[-N:] 取最近
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
            [Button.inline(f"{chk('kill')}启用 4算法杀组模式", data=b"toggle_mode_kill")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def mode_intro_keyboard(self):
        return [
            [Button.inline("ABC球模式介绍", data=b"intro_ball")],
            [Button.inline("4算法杀组模式介绍", data=b"intro_kill")],
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

        # 重置上期杀球记录，准备生成本期
        u.last_ball_kills = {}

        # ABC杀球模式（支持自定义杀码数量与倍投）
        if "ball" in u.selected_modes:
            multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
            single_bet = u.ball_bet_amount * multiplier

            for b_char in u.selected_balls:
                # 随机杀 abc_kill_count 个不同数字
                killed_digits = random.sample(range(10), u.abc_kill_count)
                u.last_ball_kills[b_char] = killed_digits
                for d in range(10):
                    if d not in killed_digits:
                        all_bet_lines.append(f"{b_char}{d}/{int(single_bet)}")
            active_descriptions.append(f"ABC杀球(杀{u.abc_kill_count}码,倍投{multiplier:.1f}x)")

        # 4算法杀组模式
        kill_target_for_bet = None
        kill_confidence_for_bet = 0.5
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
                multiplier = u.kill_martingale_multiplier ** u.kill_consecutive_losses
                single_bet = u.kill_bet_amount * multiplier
                bet_combos = [c for c in COMBOS if c != kill_target]
                for c in bet_combos:
                    all_bet_lines.append(f"{c}/{int(single_bet)}")
                active_descriptions.append(f"4算法杀组(杀{kill_target},置信{confidence:.0%},倍投{multiplier:.1f}x)")
            except Exception as e:
                logger.error(f"[用户 {u.user_id}] 杀组预测失败: {e}")

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
            if kill_target_for_bet:
                u.last_killed_group = kill_target_for_bet
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
                        f"4算法杀组: `{kill_target_for_bet}`",
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
                f"• 4算法杀组: `{kill_status}`\n"
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
                await event.answer("ABC球模式：针对开奖号码的前三位进行定位杀号。用户可多选A、B、C球，自定义杀码数量后系统自动随机杀掉对应数量的数字，并按独立金额与倍投设置自动投递剩余数字。中奖倍率9.99。", alert=True)
                return
            if data == "intro_kill":
                await event.answer("4算法杀组模式：集成天子、5Y、小枫、小盾4种预测算法，回测10期后选择胜率最高的算法，预测下一期最可能开出的组合并将其杀掉，自动投注其余3个组合。支持倍投与连败重置。", alert=True)
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
                await event.edit("4算法杀组模式设置", buttons=self.kill_settings_keyboard(u))
                return

            if data == "toggle_kill_enabled":
                u.kill_enabled = not u.kill_enabled
                u.save()
                await event.edit("4算法杀组模式设置", buttons=self.kill_settings_keyboard(u))
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
                    f"• 4算法杀组: `{kill_status}`\n"
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
                            # ABC球模式结算逻辑（中奖倍率 9.99，按整体盈亏判定输赢）
                            if "ball" in u.selected_modes and u.last_ball_kills:
                                nums = [int(d) for d in data.number_str if d.isdigit()]
                                ball_index_map = {"a": 0, "b": 1, "c": 2}
                                total_abc_pnl = 0.0
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

                            # 4算法杀组模式结算逻辑（杀中即亏损，杀错即盈利，赔率为 1:0.33 近似按投注3组中1组）
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
                                asyncio.create_task(self.handle_new_issue_bet(u, data.issue_id, data))
            except Exception as e:
                logger.error(f"轮询守护异常自动隔离: {e}")

            await asyncio.sleep(4)

    async def start(self):
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

threading.Thread(target=start_bot_thread, daemon=True).start()

with gr.Blocks(title="PC28量化智能挂机系统", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚀 PC28量化智能挂机系统 - 24小时永动中控")
    gr.Markdown("已集成30种算法投票杀组模式（马尔可夫、随机森林、GBDT、SVM、贝叶斯、KNN等）与同款报数播报功能。ABC杀球模式支持自定义杀码数量、可配置倍投倍数（中奖倍率9.99），盈亏实时独立结算。达到止盈/止损线自动暂停，需手动重启。保留特码与豹子附加下注。")
    gr.Markdown("---")
    gr.Markdown("<div style='text-align: center; color: gray;'>PC28量化挂机中控台 © 2026</div>")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port)
