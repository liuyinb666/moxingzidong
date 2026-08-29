import os
import sys
import re
import json
import math
import asyncio
import logging
import threading
import random
import gradio as gr
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Dict, Any
import aiohttp
import uvicorn
from collections import Counter
from fastapi import FastAPI
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

API_URL = "https://yu28.top/api/kj.json?nbr=100"
API_KEY = "yu28_0889c78ad74725b7"
SESSIONS_DIR = "telegram_sessions"
USER_DATA_DIR = "user_data"

# 北京时间 (UTC+8)，盈亏按北京时间每天 00:00 重置
BEIJING_TZ = timezone(timedelta(hours=8))

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

# ==================== 杀组算法（区间优化）====================
# ==================== 杀组预测算法 ====================
ALL_TYPES = ['小双', '小单', '大双', '大单']
SEQUENCES = [
    [0, 3, 9, 12, 15, 18, 21, 24, 27],
    [1, 4, 7, 10, 13, 16, 19, 22, 25],
    [2, 5, 8, 11, 14, 17, 20, 23, 26]
]
COMBINATION_RULES = {
    'sameSequence': {
        '小双': ['小双', '大双', '大单'],
        '小单': ['小单', '大单', '大双'],
        '大双': ['大双', '小双', '小单'],
        '大单': ['大单', '小单', '小双']
    },
    'diffSequence': {
        '小双': ['小双', '小单', '大单'],
        '小单': ['小单', '小双', '大双'],
        '大双': ['大双', '大单', '小单'],
        '大单': ['大单', '大双', '小双']
    }
}

def get_type_from_sum(sum_val: int) -> str:
    size = '小' if sum_val < 14 else '大'
    parity = '双' if sum_val % 2 == 0 else '单'
    return size + parity

def mulberry32(seed: int):
    s = seed & 0xFFFFFFFF
    def next_():
        nonlocal s
        s = (s + 0x6D2B79F5) & 0xFFFFFFFF
        t = (s ^ (s >> 15)) & 0xFFFFFFFF
        t = (t * (s | 1)) & 0xFFFFFFFF
        t = (t ^ (t + ((t ^ (t >> 7)) * (61 | t)))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return next_

def get_sequence_indexes(num: int) -> List[int]:
    idxs = []
    for i, seq in enumerate(SEQUENCES):
        if num in seq:
            idxs.append(i)
    return idxs

def is_same_sequence(num1: int, num2: int) -> bool:
    seq1 = get_sequence_indexes(num1)
    seq2 = get_sequence_indexes(num2)
    return any(i in seq2 for i in seq1)

def split_and_sum_str(s: str) -> int:
    s = s.replace('.', '')
    return sum(int(c) for c in s)

def predict_next_period(current_expect: str, current_sum: int, range_min: float = 0.25, range_max: float = 0.55) -> str:
    match = re.search(r'\d+', current_expect)
    seed = int(match.group()) if match else hash(current_expect) & 0xFFFFFFFF
    rnd = mulberry32(seed)

    random_num = rnd() * (range_max - range_min) + range_min
    product = random_num * current_sum
    rounded = round(product, 3)
    final_sum = split_and_sum_str(str(rounded))
    final_type = get_type_from_sum(final_sum)

    rand_str = f"{random_num:.8f}".split('.')[1]
    rand_first_three = rand_str[:3]
    rand_sum = split_and_sum_str(rand_first_three)
    same_seq = is_same_sequence(rand_sum, current_sum)

    rule_key = 'sameSequence' if same_seq else 'diffSequence'
    predict_groups = COMBINATION_RULES[rule_key][final_type]
    return next(t for t in ALL_TYPES if t not in predict_groups)

def generate_range_pool():
    pool = []
    step = 0.01
    min_width = 0.2
    max_width = 0.5
    vals = [round(i * step, 2) for i in range(0, 101)]
    for min_ in vals:
        for max_ in vals:
            if max_ - min_ >= min_width and max_ - min_ <= max_width:
                pool.append((min_, max_))
    return pool

RANGE_POOL = generate_range_pool()

class KillGroupPredictor:
    @staticmethod
    def predict_kill(history: List[dict]) -> str:
        if len(history) < 2:
            return "小单"
        max_test = min(27, len(history) - 1)
        best_range = (0.25, 0.55)
        best_hits = -1

        for rmin, rmax in RANGE_POOL:
            hits = 0
            for i in range(max_test):
                target = history[i]
                prev = history[i + 1]
                kill = predict_next_period(prev['issue'], prev['sum'], rmin, rmax)
                if target['type'] != kill:
                    hits += 1
            if hits > best_hits:
                best_hits = hits
                best_range = (rmin, rmax)

        latest = history[0]
        return predict_next_period(latest['issue'], latest['sum'], best_range[0], best_range[1])


def create_advanced_predictor(depth, offset, weight, formula_type, step):
    """单个精英预测器：基于历史球号的加权公式"""
    def predictor(history_balls):
        if len(history_balls) < depth:
            return offset % 10
        segment = history_balls[:depth]
        if formula_type == 0:
            core_val = sum(val * (weight + idx) for idx, val in enumerate(segment[::step]))
        elif formula_type == 1:
            core_val = sum(abs(segment[i] - segment[i+1]) * weight for i in range(len(segment)-1))
        else:
            core_val = sum(segment) * weight + offset
        return int(core_val) % 10
    return predictor


def _build_abc_models():
    """为 A/B/C 三球各构建 1000 个精英预测器"""
    global KILL_MODELS
    KILL_MODELS.clear()
    rng = random.Random(999)
    for ball in ["A", "B", "C"]:
        for i in range(1, 1001):
            KILL_MODELS[f"Elite_{ball}_{i:04d}"] = {
                "func": create_advanced_predictor(
                    rng.randint(3, 20),
                    rng.randint(0, 19),
                    rng.uniform(0.1, 10.0),
                    rng.randint(0, 2),
                    rng.randint(1, 3)
                ),
                "ball": ball
            }
    logger.info(f"ABC杀码精英模型池构建完成: {len(KILL_MODELS)} 个模型")


class HighWinRateManager:
    """ABC杀码管理器：每球 1000 个精英模型，取最近 100 期回测最优"""

    @staticmethod
    def _history_to_ball(history, ball_type):
        """把 app.py 的 history 格式转换为该球位的数值列表（最新在前）"""
        bi = {"A": 0, "B": 1, "C": 2}[ball_type]
        result = []
        for item in history:
            nums = item.get("nums")
            if nums and len(nums) > bi:
                result.append(int(nums[bi]))
            elif "number" in item and isinstance(item["number"], str) and "+" in item["number"]:
                parts = item["number"].split("+")
                if len(parts) > bi:
                    result.append(int(parts[bi]))
        return result

    @classmethod
    def get_strict_prediction(cls, history, ball_type, kill_count=1):
        bh = cls._history_to_ball(history, ball_type)
        kill_count = max(1, min(9, int(kill_count or 1)))
        if not bh:
            return {
                "model_id": "N/A",
                "win_rate": 0,
                "kill_num": 0,
                "status": "数据不足",
                "bet_numbers": list(range(10)),
                "kill_nums": list(range(kill_count))
            }
        models = {m: i for m, i in KILL_MODELS.items() if i["ball"] == ball_type}
        results = []
        backtest_len = min(100, len(bh) - 1)
        for mid, info in models.items():
            try:
                win = sum(1 for i in range(backtest_len) if bh[i] != info["func"](bh[i+1:]))
                rate = win / backtest_len if backtest_len > 0 else 0
                pred = info["func"](bh)
                results.append((mid, rate, pred))
            except Exception:
                continue
        if not results:
            return {
                "model_id": "N/A",
                "win_rate": 0,
                "kill_num": 0,
                "status": "模型异常",
                "bet_numbers": list(range(10)),
                "kill_nums": list(range(kill_count))
            }
        results.sort(key=lambda x: x[1], reverse=True)

        # 选取 top-kill_count 个不同预测号码（按模型胜率排序，确保多样性）
        kill_nums = []
        used_preds = set()
        best_rate = 0.0
        best_mid = "N/A"
        for mid, rate, pred in results:
            if pred not in used_preds:
                kill_nums.append(pred)
                used_preds.add(pred)
                if len(kill_nums) == 1:
                    best_rate = rate
                    best_mid = mid
                if len(kill_nums) >= kill_count:
                    break

        # 若模型预测的不同号码不足 kill_count，用未出现的号码按顺序补足
        if len(kill_nums) < kill_count:
            for n in range(10):
                if n not in used_preds:
                    kill_nums.append(n)
                    used_preds.add(n)
                    if len(kill_nums) >= kill_count:
                        break

        kill_nums = sorted(kill_nums)
        if len(kill_nums) != kill_count:
            logger.warning(f"[ABC精英模型] {ball_type}球杀码数量异常: 期望{kill_count}个,实际{len(kill_nums)}个, 结果={kill_nums}")
        status = "信心充足" if best_rate >= 0.92 else "盘面混乱"
        return {
            "model_id": best_mid,
            "win_rate": round(best_rate, 4),
            "kill_num": kill_nums[0] if kill_nums else 0,
            "status": status,
            "bet_numbers": [n for n in range(10) if n not in kill_nums],
            "kill_nums": kill_nums
        }

    @classmethod
    def get_all_predictions(cls, h, balls=None, kill_count=None):
        if balls is None:
            balls = ["A", "B", "C"]
        if isinstance(kill_count, (int, float)):
            kc = int(kill_count)
        elif isinstance(kill_count, str):
            try:
                kc = int(kill_count)
            except ValueError:
                kc = 1
        elif isinstance(kill_count, dict):
            kc = 1
        else:
            kc = 1
        if isinstance(kill_count, dict):
            return {b: cls.get_strict_prediction(h, b, kill_count.get(b, kc)) for b in balls}
        return {b: cls.get_strict_prediction(h, b, kc) for b in balls}

    @classmethod
    def record_result(cls, ball_type, kill_nums, actual_num):
        # 本管理器为静态回测选优，无需跨期记忆
        pass

    @classmethod
    def regenerate_abc_models(cls, history=None):
        _build_abc_models()
        total = len(KILL_MODELS)
        logger.info(f"ABC杀码精英模型已重新生成！模型总数: {total}")
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

    def reset_daily_pnl(self):
        """手动重置今日盈亏为0"""
        prev_pnl = self.daily_pnl
        self.daily_pnl = 0.0
        logger.info(f"手动重置今日盈亏: 原盈亏 {prev_pnl:+.2f} -> 0.0")
        return prev_pnl

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
        # ABC自定义倍投倍数列表：第0次(首注)=1，第1次(首亏后)=3，依此类推
        self.abc_martingale_multipliers = [1.0, 3.0, 7.0, 11.0, 15.0]
        self.abc_martingale_multiplier = 2.0  # 兼容旧数据（未配置列表时使用）
        self.abc_consecutive_losses = 0   # ABC连败次数

        # 上期ABC杀球记录 {b_char: [killed_digits]}
        self.last_ball_kills = {}

        # 杀组设置（基于30算法集成投票）
        self.kill_enabled = False
        self.kill_bet_amount = 100.0
        self.kill_martingale_multiplier = 2.0
        self.kill_consecutive_losses = 0            # 最近3期杀组记录，避免连杀同一组合       # 上期实际杀的组合
        self.kill_last_settled_issue = "" # 上期已结算期号

        # 附加下注特码与豹子配置（特码 0/27/1/26 各自独立）
        self.extra_special_numbers = []  # 例: ["0", "27", "1", "26"]
        self.extra_bauzi = False
        self.extra_bet_amounts = {
            "0": 100.0,
            "27": 100.0,
            "1": 100.0,
            "26": 100.0,
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
                        # 加载自定义倍投列表，旧数据自动迁移
                        loaded_mults = data.get("abc_martingale_multipliers")
                        if isinstance(loaded_mults, list) and loaded_mults:
                            self.abc_martingale_multipliers = [float(x) for x in loaded_mults]
                        else:
                            old_m = float(data.get("abc_martingale_multiplier", 2.0))
                            self.abc_martingale_multipliers = [1.0, old_m, old_m ** 2, old_m ** 3, old_m ** 4]
                        self.abc_martingale_multiplier = float(data.get("abc_martingale_multiplier", 2.0))
                        self.abc_consecutive_losses = data.get("abc_consecutive_losses", 0)
                        self.last_ball_kills = data.get("last_ball_kills", {})
                        # 杀组
                        self.kill_enabled = data.get("kill_enabled", False)
                        self.kill_bet_amount = data.get("kill_bet_amount", 100.0)
                        self.kill_martingale_multiplier = data.get("kill_martingale_multiplier", 2.0)
                        self.kill_consecutive_losses = data.get("kill_consecutive_losses", 0)
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
                        # 附加（兼容旧版分组数据并迁移为独立号码）
                        self.extra_bet_amounts = data.get("extra_bet_amounts", {"0": 100.0, "27": 100.0, "1": 100.0, "26": 100.0, "baozi": 100.0})
                        self.extra_special_numbers = data.get("extra_special_numbers", [])
                        # 迁移旧分组到独立号码
                        migrated = []
                        for x in self.extra_special_numbers:
                            if x == "0_27":
                                migrated.extend(["0", "27"])
                            elif x == "1_26":
                                migrated.extend(["1", "26"])
                            elif x in ("0", "27", "1", "26"):
                                migrated.append(x)
                        self.extra_special_numbers = list(dict.fromkeys(migrated))
                        # 迁移旧金额
                        if "0_27" in self.extra_bet_amounts:
                            self.extra_bet_amounts.setdefault("0", self.extra_bet_amounts["0_27"])
                            self.extra_bet_amounts.setdefault("27", self.extra_bet_amounts["0_27"])
                            del self.extra_bet_amounts["0_27"]
                        if "1_26" in self.extra_bet_amounts:
                            self.extra_bet_amounts.setdefault("1", self.extra_bet_amounts["1_26"])
                            self.extra_bet_amounts.setdefault("26", self.extra_bet_amounts["1_26"])
                            del self.extra_bet_amounts["1_26"]
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
                        "abc_martingale_multipliers": self.abc_martingale_multipliers,
                        "abc_martingale_multiplier": self.abc_martingale_multiplier,
                        "abc_consecutive_losses": self.abc_consecutive_losses,
                        "last_ball_kills": self.last_ball_kills,
                        "kill_enabled": self.kill_enabled,
                        "kill_bet_amount": self.kill_bet_amount,
                        "kill_martingale_multiplier": self.kill_martingale_multiplier,
                        "kill_consecutive_losses": self.kill_consecutive_losses,
                        
                        
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

    def get_abc_multiplier(self):
        """根据当前连败次数返回对应的自定义倍投倍数"""
        idx = min(self.abc_consecutive_losses, len(self.abc_martingale_multipliers) - 1)
        return float(self.abc_martingale_multipliers[idx])

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
            headers = {"Authorization": f"Bearer {API_KEY}", "X-API-Key": API_KEY}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(API_URL, timeout=15) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        return res.get("data", [])
                    else:
                        logger.warning(f"API 返回状态码: {resp.status}")
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
            [Button.inline("📈 实时收益战报", data=b"stats"), Button.inline("🔄 清空今日盈亏", data=b"reset_pnl")]
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
        c0 = "✅ " if "0" in u_state.extra_special_numbers else "⬜ "
        c27 = "✅ " if "27" in u_state.extra_special_numbers else "⬜ "
        c1 = "✅ " if "1" in u_state.extra_special_numbers else "⬜ "
        c26 = "✅ " if "26" in u_state.extra_special_numbers else "⬜ "
        cbz = "✅ " if u_state.extra_bauzi else "⬜ "
        return [
            [Button.inline(f"{c0}特码 0 (金额: {u_state.extra_bet_amounts.get('0', 100)})", data=b"toggle_extra_0")],
            [Button.inline(f"{c27}特码 27 (金额: {u_state.extra_bet_amounts.get('27', 100)})", data=b"toggle_extra_27")],
            [Button.inline(f"{c1}特码 1 (金额: {u_state.extra_bet_amounts.get('1', 100)})", data=b"toggle_extra_1")],
            [Button.inline(f"{c26}特码 26 (金额: {u_state.extra_bet_amounts.get('26', 100)})", data=b"toggle_extra_26")],
            [Button.inline(f"{cbz}豹子下注 (金额: {u_state.extra_bet_amounts.get('baozi', 100)})", data=b"toggle_extra_bauzi")],
            [Button.inline("✏️ 修改特码/豹子下注金额", data=b"set_extra_amounts")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def amounts_menu_keyboard(self, u_state: UserState):
        current_multiplier = u_state.get_abc_multiplier()
        triggered, reason = u_state.risk_mgr.check_triggered()
        risk_status = f"🔴 {reason}" if triggered else "🟢 正常"
        return [
            [Button.inline(f"ABC杀球单注金额: {u_state.ball_bet_amount}", data=b"set_ball_amount")],
            [Button.inline(f"ABC倍投序列: {u_state.abc_martingale_multipliers}", data=b"set_abc_multiplier")],
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
        # ABC杀球模式：使用小鶴神精英模型（每球1000模型选优，支持自定义杀码数）
        abc_multiplier = 1.0
        abc_pred_info = {}
        if "ball" in u.selected_modes:
            abc_multiplier = u.get_abc_multiplier()
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
                    if not kill_nums or len(kill_nums) != count:
                        if kill_nums and len(kill_nums) != count:
                            logger.warning(f"[用户 {u.user_id}] {b_char.upper()}球杀码数量异常: 期望{count}个,实际{len(kill_nums)}个,已修正")
                        kill_nums = random.sample(range(10), count)
                    u.last_ball_kills[b_char] = kill_nums
                    abc_pred_info[b_char] = pred_info
                active_descriptions.append(f"ABC杀球(精英模型杀{count}码,倍投{abc_multiplier:.1f}x)")
            except Exception as e:
                logger.error(f"[用户 {u.user_id}] ABC精英模型预测失败: {e}")

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
                active_descriptions.append(f"吮欲杀组(杀{kill_target},置信{confidence:.0%},倍投{kill_multiplier:.1f}x)")
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

        # 附加特码与豹子下注（特码各自独立）
        for num in ["0", "27", "1", "26"]:
            if num in u.extra_special_numbers:
                amt = u.extra_bet_amounts.get(num, 100.0)
                all_bet_lines.append(f"{num}/{int(amt)}")
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
                        f"吮欲杀组: `{kill_target_for_bet}`",
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
                f"• ABC倍投序列: `{u.abc_martingale_multipliers}`\n"
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
                await event.answer("杀组模式：集成2套吮欲杀组算法（算法1·基础定义带8条特殊规则、算法2·4y算法无特殊规则）。每期自动回测最近20期，选择胜率高的算法预测下一期最可能开出的组合并将其杀掉，自动投注其余3个组合。支持倍投与连败重置。", alert=True)
                return
            if data == "intro_extra":
                await event.answer("特码与豹子：支持独立设置金额并附加下注特码（0、27、1、26）以及豹子。", alert=True)
                return

            if data == "extra_config":
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_0":
                if "0" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("0")
                else:
                    u.extra_special_numbers.append("0")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_27":
                if "27" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("27")
                else:
                    u.extra_special_numbers.append("27")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_1":
                if "1" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("1")
                else:
                    u.extra_special_numbers.append("1")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_26":
                if "26" in u.extra_special_numbers:
                    u.extra_special_numbers.remove("26")
                else:
                    u.extra_special_numbers.append("26")
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
                    "请输入特码与豹子的独立下注金额格式（格式: 0金额,27金额,1金额,26金额,豹子金额）\n"
                    f"当前设置 -> 0:`{u.extra_bet_amounts.get('0', 100)}` 27:`{u.extra_bet_amounts.get('27', 100)}` "
                    f"1:`{u.extra_bet_amounts.get('1', 100)}` 26:`{u.extra_bet_amounts.get('26', 100)}` "
                    f"豹子:`{u.extra_bet_amounts.get('baozi', 100)}`\n"
                    "例如输入: `100,100,100,100,200`"
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
                    f"• ABC倍投序列: `{u.abc_martingale_multipliers}`\n"
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
                await event.respond("当前ABC倍投序列: `{seq}`\n请输入新的倍投序列，用英文逗号分隔（如: 1,3,7,11,15）:".format(seq=u.abc_martingale_multipliers))
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
                current_multiplier = u.get_abc_multiplier()
                kill_multiplier = u.kill_martingale_multiplier ** u.kill_consecutive_losses
                can_bet, reason = rm.can_bet()
                triggered, trigger_reason = rm.check_triggered()
                await event.respond(
                    f"详细收益战报与风控统计\n"
                    f"--------------------\n"
                    f"• 今日总盈亏: `{rm.daily_pnl:+.2f}`\n"
                    f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                    f"• ABC倍投序列: `{u.abc_martingale_multipliers}`\n"
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
            elif data == "reset_pnl":
                prev_pnl = u.risk_mgr.reset_daily_pnl()
                u.save()
                await event.respond(
                    f"✅ 已清空今日盈亏\n"
                    f"• 原盈亏: `{prev_pnl:+.2f}`\n"
                    f"• 当前盈亏: `0.00`"
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
                    text = event.text.strip().replace("，", ",")
                    parts = [p.strip() for p in text.split(",") if p.strip()]
                    if not parts:
                        raise ValueError("空输入")
                    mults = [max(1.0, float(p)) for p in parts]
                    # 若只输入一个数字，按旧逻辑生成等比序列
                    if len(mults) == 1:
                        base = mults[0]
                        mults = [1.0, base, base ** 2, base ** 3, base ** 4]
                    u.abc_martingale_multipliers = mults
                    u.abc_martingale_multiplier = mults[1] if len(mults) > 1 else mults[0]
                    u.save()
                    await event.respond(f"ABC倍投序列更新为: `{u.abc_martingale_multipliers}`", buttons=self.main_keyboard(u))
                except:
                    await event.respond("格式错误，请输入倍投序列，例如: `1,3,7,11,15`")
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
                    amts = [float(p.strip()) for p in parts if p.strip()]
                    keys = ["0", "27", "1", "26", "baozi"]
                    if len(amts) == 1:
                        for k in keys:
                            u.extra_bet_amounts[k] = amts[0]
                    elif len(amts) == 2:
                        for k in keys:
                            u.extra_bet_amounts[k] = amts[1] if k == "baozi" else amts[0]
                    elif len(amts) >= 5:
                        for i, k in enumerate(keys):
                            u.extra_bet_amounts[k] = amts[i]
                    else:
                        # 3 或 4 个金额时按顺序填充，其余保持不变
                        for i, k in enumerate(keys):
                            if i < len(amts):
                                u.extra_bet_amounts[k] = amts[i]
                    u.save()
                    self.user_login_states.pop(sid, None)
                    await event.respond("特码与豹子独立金额更新成功", buttons=self.extra_config_keyboard(u))
                except:
                    await event.respond("格式错误，请重新输入，例如: `100,100,100,100,200`")
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
                                            multiplier = u.get_abc_multiplier()
                                            single_bet = u.ball_bet_amount * multiplier
                                            buy_count = 10 - len(killed_list)
                                            cost = buy_count * single_bet

                                            if actual_digit not in killed_list:
                                                win_amount = single_bet * 9.99
                                                total_abc_pnl += (win_amount - cost)
                                                logger.info(f"[用户 {uid}] ABC球 {b_char.upper()}球 中奖: 开奖{actual_digit} 不在杀码{killed_list}, 返还{win_amount:.2f}, 成本{cost:.2f}")
                                            else:
                                                total_abc_pnl -= cost
                                                logger.info(f"[用户 {uid}] ABC球 {b_char.upper()}球 未中: 开奖{actual_digit} 在杀码{killed_list}, 亏损{cost:.2f}")
                                            # 记录ABC结果（精英模型为静态回测选优，无需跨期记忆）
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

                            # 30算法杀组模式结算逻辑（杀中即亏损，杀错即盈利，小单/大双赔率3.71，大单/小双赔率4.32）
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
                                        # 没杀中，3组中1组，按实际中奖组合赔率结算
                                        if actual_combo in ("小单", "大双"):
                                            odds = 3.71
                                        else:
                                            odds = 4.32
                                        win_amount = single_bet * odds
                                        kill_pnl = win_amount - cost
                                        u.kill_consecutive_losses = 0
                                        logger.info(f"[用户 {uid}] 杀组未中: 杀{last_kill}=开{actual_combo}(赔率{odds}), 盈利{kill_pnl:.2f}, 连败清零")

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
        # ========== 启动时批量预填充历史数据 ==========
        initial_data = await DataFetcher.fetch_history_list()
        if initial_data:
            parsed = DataFetcher.parse_history(initial_data)
            for uid, u in self.users.items():
                if u.is_logged_in:
                    u.history = parsed
                    u.save()
                    logger.info(f"[预填充] 用户 {uid} 历史数据已填充 {len(parsed)} 期")
        # ========== 预填充结束 ==========
        logger.info("PC28量化挂机中控系统已成功全面上线! 杀组引擎: 2套吮欲算法动态选优")
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
    gr.Markdown("已集成2套吮欲杀组算法（算法1·基础定义、算法2·4y算法）动态回测选优，每期自动选择胜率高的算法进行杀组。无两期等待，错了直接倍投。ABC杀球模式使用小鶴神精英模型（每球1000模型、支持自定义杀码数）、可配置自定义倍投序列（中奖倍率9.99），盈亏实时独立结算。达到止盈/止损线自动暂停，需手动重启。保留特码与豹子独立下注。")
    gr.Markdown("---")
    gr.Markdown("<div style='text-align: center; color: gray;'>PC28量化挂机中控台 © 2026 | 面板地址: /gradio</div>")

# 用 FastAPI 包装 Gradio，提供 /health 端点供 Railway 等平台做健康检查
# 纯 FastAPI 应用入口，Gradio 稍后挂载
app = FastAPI(title="PC28量化智能挂机系统")

@app.get("/")
@app.head("/")
@app.get("/health")
@app.head("/health")
@app.get("/ping")
@app.head("/ping")
def health_check():
    return {"status": "ok", "algorithms": len(ALGO_CLASSES)}

# Gradio 挂载到 /gradio 子路径，完全隔离

if __name__ == "__main__":
    # 仅在直接运行时才启动 Telegram Bot 线程，避免部署平台导入模块时触发
    threading.Thread(target=start_bot_thread, daemon=True).start()
    port = int(os.getenv("PORT", "7860"))
    logger.info(f"启动服务，监听 0.0.0.0:{port} | 健康检查: / /health /ping")
    logger.info(f"健康检查: http://0.0.0.0:{port}/ 或 /health")
    logger.info(f"Gradio 面板: http://0.0.0.0:{port}/ui")
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=True)
    except Exception as e:
        logger.error(f"Uvicorn 启动失败: {e}")
        raise