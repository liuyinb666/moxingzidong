#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, aiohttp, aiofiles, re, time, random, hashlib, numpy as np, csv
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple, Set
from collections import deque, Counter
import logging, pickle
from dataclasses import dataclass, field, asdict
import traceback, signal, math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters, ConversationHandler
from telegram.error import BadRequest
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneCodeExpiredError

# ==================== 配置 ====================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH")
    PC28_API_BASE = "https://pc28.help/api/kj.json?nbr=500"
    ADMIN_USER_IDS = [5338954122]
    DATA_DIR = Path("data")
    SESSIONS_DIR = DATA_DIR / "sessions"
    LOGS_DIR = DATA_DIR / "logs"
    CACHE_DIR = DATA_DIR / "cache"
    INITIAL_HISTORY_SIZE = 100
    CACHE_SIZE = 200
    DEFAULT_BASE_AMOUNT = 20000
    DEFAULT_MAX_AMOUNT = 1000000
    DEFAULT_MULTIPLIER = 2.5
    DEFAULT_STOP_LOSS = 0
    DEFAULT_STOP_WIN = 0
    DEFAULT_STOP_BALANCE = 0
    DEFAULT_RESUME_BALANCE = 0
    MIN_BET_AMOUNT = 1
    MAX_BET_AMOUNT = 10000000
    EXCHANGE_RATE = 100000
    BALANCE_BOT = "kkpayPc28Bot"
    REQUEST_TIMEOUT = 15
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2
    MAX_HISTORY = 61
    GAME_CYCLE_SECONDS = 210
    CLOSE_BEFORE_SECONDS = 50
    SCHEDULER_CHECK_INTERVAL = 5
    HEALTH_CHECK_INTERVAL = 60
    BALANCE_CACHE_SECONDS = 30
    MAX_CONCURRENT_BETS = 5
    LOG_RETENTION_DAYS = 7
    ACCOUNT_SAVE_INTERVAL = 30
    MAX_CONCURRENT_PREDICTIONS = 3
    LOGIN_SELECT, LOGIN_CODE, LOGIN_PASSWORD = range(3)
    ADD_ACCOUNT = 10
    CHASE_NUMBERS, CHASE_PERIODS, CHASE_AMOUNT = range(11, 14)
    MAX_ACCOUNTS_PER_USER = 5
    PREDICTION_HISTORY_SIZE = 20
    RISK_PROFILES = {'保守': 0.005, '稳定': 0.01, '激进': 0.02, '稳健型': 0.008, '平衡型': 0.015, '进取型': 0.03}
    AVAILABLE_CURRENCIES = ["KKCOIN", "USDT", "CNY"]
    DEFAULT_CURRENCY = "KKCOIN"
    CURRENCY_BET_LIMITS = {
        "KKCOIN": {"min": 1, "max": 10000000},
        "USDT": {"min": 0.01, "max": 100},
        "CNY": {"min": 0.1, "max": 1000}
    }
    CURRENCY_SYMBOLS = {
        "KKCOIN": "KK",
        "USDT": "USDT",
        "CNY": "¥"
    }
    # 新增：预测置信度阈值
    MIN_PREDICTION_CONFIDENCE = 0.35
    # 新增：集成模型权重配置
    ENSEMBLE_WEIGHTS = {"trend": 0.35, "probability": 0.35, "original": 0.15, "v3": 0.15}

    @classmethod
    def init_dirs(cls):
        cls.DATA_DIR.mkdir(exist_ok=True)
        cls.SESSIONS_DIR.mkdir(exist_ok=True)
        cls.LOGS_DIR.mkdir(exist_ok=True)
        cls.CACHE_DIR.mkdir(exist_ok=True)

    @classmethod
    def validate(cls):
        errors = []
        if not cls.BOT_TOKEN: errors.append("BOT_TOKEN未配置")
        if cls.API_ID <= 0: errors.append("API_ID必须为正整数")
        if not cls.API_HASH: errors.append("API_HASH未配置")
        if errors: raise ValueError("配置验证失败: " + ", ".join(errors))
        return True

Config.init_dirs()

def increment_qihao(current_qihao: str) -> str:
    if not current_qihao: return "1"
    match = re.search(r'(\d+)$', current_qihao)
    if match:
        num_part = match.group(1)
        prefix = current_qihao[:match.start()]
        try: return prefix + str(int(num_part) + 1).zfill(len(num_part))
        except: return current_qihao + "1"
    else:
        try: return str(int(current_qihao) + 1)
        except: return current_qihao + "1"

def format_amount(amount: float, currency: str) -> str:
    symbol = Config.CURRENCY_SYMBOLS.get(currency, "")
    if currency == "KKCOIN":
        return f"{int(amount):,}{symbol}"
    else:
        return f"{amount:.2f}{symbol}"

class ColoredFormatter(logging.Formatter):
    grey, green, red, yellow, blue, reset = "\x1b[38;20m", "\x1b[32;20m", "\x1b[31;20m", "\x1b[33;20m", "\x1b[34;20m", "\x1b[0m"
    FORMATS = {
        logging.INFO: grey + "%(asctime)s [%(levelname)s] %(message)s" + reset,
        logging.ERROR: red + "%(asctime)s [%(levelname)s] %(message)s" + reset,
        'BETTING': green + "%(asctime)s [投注] %(message)s" + reset,
        'PREDICTION': blue + "%(asctime)s [预测] %(message)s" + reset,
    }
    def format(self, record):
        if hasattr(record, 'betting') and record.betting: self._style._fmt = self.FORMATS['BETTING']
        elif hasattr(record, 'prediction') and record.prediction: self._style._fmt = self.FORMATS['PREDICTION']
        else: self._style._fmt = self.FORMATS.get(record.levelno, self.grey + "%(asctime)s [%(levelname)s] %(message)s" + self.reset)
        return super().format(record)

class BotLogger:
    def __init__(self):
        self.logger = logging.getLogger('PC28Bot')
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(ColoredFormatter(datefmt='%H:%M:%S'))
        self.logger.addHandler(console)
        log_file = Config.LOGS_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        self.logger.addHandler(file_handler)
        self._clean_old_logs()
    def _clean_old_logs(self):
        now = datetime.now()
        for f in Config.LOGS_DIR.glob("bot_*.log"):
            try:
                date_str = f.stem.split('_')[1]
                file_date = datetime.strptime(date_str, '%Y%m%d')
                if (now - file_date).days > Config.LOG_RETENTION_DAYS: f.unlink()
            except: pass
    def log_system(self, msg): self.logger.info(f"[系统] {msg}")
    def log_account(self, user_id, phone, action): self.logger.info(f"[账户] 用户:{user_id} 手机:{self._mask_phone(phone)} {action}")
    def log_game(self, msg): self.logger.info(f"[游戏] {msg}")
    def log_betting(self, user_id, action, detail):
        extra = {'betting': True}
        self.logger.info(f"用户:{user_id} {action} {detail}", extra=extra)
    def log_prediction(self, user_id, action, detail):
        extra = {'prediction': True}
        self.logger.info(f"用户:{user_id} {action} {detail}", extra=extra)
    def log_error(self, user_id, action, error):
        error_trace = traceback.format_exc()
        self.logger.error(f"[错误] 用户:{user_id} {action}: {error}\n{error_trace}")
    def log_api(self, action, detail): self.logger.debug(f"[API] {action} {detail}")
    def _mask_phone(self, phone: str) -> str:
        if len(phone) >= 8: return phone[:5] + "****" + phone[-3:]
        return phone

logger = BotLogger()

COMBOS = ["小单", "小双", "大单", "大双"]

# ==================== 新增：优化的预测算法 ====================

def trend_based_prediction(history: List[Dict]) -> List[str]:
    """基于趋势的预测算法"""
    if len(history) < 15:
        return ["小单"]
    
    combos = [h.get("combo", h.get("combination", "小单")) for h in history[:20]]
    
    # 检测连开模式
    last_3 = combos[:3]
    if len(set(last_3)) == 1:  # 三连相同
        opposite = {"大单":"小双", "小双":"大单", "大双":"小单", "小单":"大双"}
        return [opposite.get(last_3[0], "小单")]
    
    # 检测大小单双的冷热
    size_counter = Counter([c[0] for c in combos])  # 大/小
    parity_counter = Counter([c[1] for c in combos])  # 单/双
    
    # 根据近期趋势决定追冷还是追热
    recent_size = [c[0] for c in combos[:10]]
    recent_parity = [c[1] for c in combos[:10]]
    
    if recent_size.count(recent_size[0]) >= 7:  # 大小偏态严重
        predicted_size = "小" if recent_size[0] == "大" else "大"
    else:
        predicted_size = min(size_counter, key=size_counter.get)  # 追冷
        
    if recent_parity.count(recent_parity[0]) >= 7:
        predicted_parity = "双" if recent_parity[0] == "单" else "单"
    else:
        predicted_parity = min(parity_counter, key=parity_counter.get)
    
    return [predicted_size + predicted_parity]

def probability_distribution_prediction(history: List[Dict]) -> List[str]:
    """基于概率转移矩阵的预测"""
    if len(history) < 20:
        return ["小单"]
    
    combos = [h.get("combo", h.get("combination", "小单")) for h in history[:30]]
    
    # 计算转移概率矩阵
    transitions = {}
    for i in range(len(combos) - 1):
        curr, next_c = combos[i], combos[i+1]
        if curr not in transitions:
            transitions[curr] = {}
        transitions[curr][next_c] = transitions[curr].get(next_c, 0) + 1
    
    # 归一化
    for curr in transitions:
        total = sum(transitions[curr].values())
        if total > 0:
            for next_c in transitions[curr]:
                transitions[curr][next_c] /= total
    
    # 基于当前状态预测下一期
    current = combos[0]
    if current in transitions and transitions[current]:
        # 返回概率最低的（杀组思维：押其他三个）
        probs = transitions[current]
        predicted = min(probs, key=probs.get)
        return [predicted]
    
    return ["小单"]

def v3_enhanced_prediction(history: List[Dict]) -> List[str]:
    """增强版V3预测"""
    if len(history) < 10:
        return ["小单"]
    
    forms = ["大单", "小单", "大双", "小双"]
    h = [x.get("combo", x.get("combination", "小单")) for x in history[:30]]
    
    if not h:
        return ["小单"]
    
    # 计算加权计数（近期权重更高）
    weighted_counts = {f: 0 for f in forms}
    for i, combo in enumerate(h):
        weight = 1.0 / (i + 1)  # 越近期权重越高
        weighted_counts[combo] = weighted_counts.get(combo, 0) + weight
    
    # 检测模式
    unique_recent = len(set(h[:5]))
    
    if unique_recent == 1:  # 5连相同
        opposite = {"大单":"小双", "小双":"大单", "大双":"小单", "小单":"大双"}
        return [opposite.get(h[0], "小单")]
    elif unique_recent <= 2:  # 偏态
        # 预测冷门
        return [min(weighted_counts, key=weighted_counts.get)]
    else:
        # 正常情况，预测最冷
        return [min(weighted_counts, key=weighted_counts.get)]

def original_armor_prediction(history: List[Dict]) -> List[str]:
    """原始的Armor V23预测"""
    return algo_v23_armor(history)[0] if len(history) >= 15 else ["小单"]

def algo_v23_armor(history):
    try:
        if len(history)<15: return ["小单"],"数据不足"
        r10=[i.get("combo", i.get("combination", "小单")) for i in history[:10]]
        r40=[i.get("combo", i.get("combination", "小单")) for i in history[:min(40,len(history))]]
        c40=Counter(r40); curr,prev=r10[0],r10[1]
        opp={"大单":"小双","小双":"大单","大双":"小单","小单":"大双"}
        af=["大单","小单","大双","小双"]
        if curr==prev: s=opp.get(curr,"小单")
        elif len(set(r10[:5]))>=3: s=sorted(af,key=lambda x:abs(c40.get(x,10)-10))[0]
        else:
            om={}
            for f in af:
                try: om[f]=r40.index(f)
                except: om[f]=40
            s=sorted(om,key=om.get,reverse=True)[0]
        return [s], "预测"
    except: return ["小单"],"数据异常"

# ==================== 新增：集成预测器 ====================

class EnsemblePredictor:
    """多模型集成预测器，支持在线学习权重调整"""
    
    def __init__(self):
        self.models = {
            "trend": trend_based_prediction,
            "probability": probability_distribution_prediction,
            "v3": v3_enhanced_prediction,
            "original": original_armor_prediction,
        }
        self.weights = Config.ENSEMBLE_WEIGHTS.copy()
        self.performance_history = {name: deque(maxlen=50) for name in self.models}
        self.prediction_history = deque(maxlen=Config.PREDICTION_HISTORY_SIZE)
        self.confidence_threshold = Config.MIN_PREDICTION_CONFIDENCE
        
    def predict(self, history: List[Dict]) -> Tuple[str, float]:
        """
        集成预测
        返回: (杀组目标, 置信度)
        """
        if len(history) < 15:
            return "小单", 0.5
        
        predictions = {}
        for name, model in self.models.items():
            try:
                pred = model(history)[0]
                predictions[name] = pred
            except Exception as e:
                logger.log_error(0, f"模型{name}预测失败", e)
                predictions[name] = "小单"
        
        # 加权投票
        vote_count = {}
        for name, pred in predictions.items():
            weight = self.weights.get(name, 1.0)
            # 根据近期表现动态调整权重
            perf = self._get_recent_performance(name)
            adjusted_weight = weight * (1 + perf)
            vote_count[pred] = vote_count.get(pred, 0) + adjusted_weight
        
        # 返回得票最少的（杀组思维）
        kill_target = min(vote_count, key=vote_count.get)
        
        # 计算置信度
        total_weight = sum(vote_count.values())
        if total_weight > 0:
            # 杀组目标的得票率越低，置信度越高（因为杀的是冷门）
            confidence = 1 - (vote_count.get(kill_target, 0) / total_weight)
        else:
            confidence = 0.5
        
        # 记录预测
        self.prediction_history.append({
            'time': datetime.now(),
            'kill': kill_target,
            'confidence': confidence,
            'predictions': predictions.copy()
        })
        
        return kill_target, confidence
    
    def _get_recent_performance(self, model_name: str) -> float:
        """获取模型近期表现评分（-0.5 到 0.5）"""
        history = self.performance_history.get(model_name, [])
        if len(history) < 5:
            return 0
        
        # 近期准确率加权平均
        recent = list(history)[-20:]
        weights = [1.0 / (i + 1) for i in range(len(recent))]
        total_weight = sum(weights)
        if total_weight == 0:
            return 0
        
        weighted_acc = sum(w * acc for w, acc in zip(weights, recent)) / total_weight
        # 转换为 -0.5 到 0.5 的范围
        return (weighted_acc - 0.5) * 1.0
    
    def update_performance(self, actual_combo: str):
        """
        更新模型表现（在线学习）
        在每期开奖后调用
        """
        if not self.prediction_history:
            return
        
        last_pred = self.prediction_history[-1]
        predicted_kill = last_pred['kill']
        predictions = last_pred.get('predictions', {})
        
        # 判断胜负：杀组正确意味着实际开出的不是杀组目标
        is_win = (actual_combo != predicted_kill)
        
        # 更新每个模型的性能
        for name, pred in predictions.items():
            model_win = (actual_combo != pred)
            self.performance_history[name].append(1.0 if model_win else 0.0)
        
        # 动态调整权重
        for name in self.models:
            perf = self._get_recent_performance(name)
            # 表现好增加权重，表现差减少权重
            adjustment = perf * 0.05
            self.weights[name] = max(0.05, min(0.5, self.weights.get(name, 0.1) + adjustment))
        
        # 归一化权重
        total = sum(self.weights.values())
        if total > 0:
            for name in self.weights:
                self.weights[name] /= total
        
        # 记录胜负
        logger.log_prediction(0, "预测结果反馈", 
                             f"杀:{predicted_kill} 开:{actual_combo} {'✓赢' if is_win else '✗输'} 置信度:{last_pred['confidence']:.2f}")
    
    def get_stats(self) -> Dict:
        """获取预测器统计信息"""
        recent_perf = list(self.performance_history.get("trend", []))[-20:]
        win_rate = sum(recent_perf) / len(recent_perf) if recent_perf else 0
        return {
            'weights': self.weights.copy(),
            'recent_win_rate': f"{win_rate:.1%}",
            'prediction_count': len(self.prediction_history)
        }

# ==================== 701个杀组模型 ====================
ALL_MODELS = {}

def old_slayer_factory(history_data, cfg):
    forms = ["大单", "小单", "大双", "小双"]
    h_slice = [h.get("combo", h.get("combination", "小单")) for h in history_data[:cfg['depth']]]
    counts = Counter(h_slice)
    if cfg['type'] == "FREQ":
        target = max(forms, key=lambda x: counts.get(x, 0)) if cfg['bias'] == "HOT" else min(forms, key=lambda x: counts.get(x, 0))
    elif cfg['type'] == "GAP":
        last_idx = forms.index(h_slice[0]) if h_slice else 0
        target = forms[(last_idx + cfg['offset']) % 4]
    else:
        nbr = int(history_data[0].get('nbr', history_data[0].get('qihao', 0))) if history_data else 0
        target = forms[(nbr * cfg['m'] + cfg['s']) % 4]
    return [target]

for i in range(1, 301):
    cfg = {'depth': 10 + (i % 90), 'type': "FREQ" if i <= 100 else ("GAP" if i <= 200 else "MATH"), 'bias': "HOT" if i % 2 == 0 else "COLD", 'offset': (i * 7) % 4, 'm': (i * 13) % 17, 's': i % 5}
    ALL_MODELS[i] = {"func": lambda h, c=cfg: old_slayer_factory(h, c), "info": {"id": i, "name": f"杀组 M{i}", "type": "杀组"}}

NEW_FORMS = ["大单", "小单", "大双", "小双"]

def slice_data_hist(hist_data, mode, depth):
    h = [x.get("combo", x.get("combination", "小单")) for x in hist_data[-depth:]] if hist_data else []
    if not h: return [random.choice(NEW_FORMS)]
    if mode == 0: return h
    elif mode == 1: return h[::-1]
    elif mode == 2: return h[::2] if len(h)>=2 else h
    elif mode == 3: return h[1::2] if len(h)>=2 else h
    else: return h[len(h)//2:]

def calc_feature(hist, ftype):
    res = {f: 0 for f in NEW_FORMS}
    if not hist: return res
    if ftype == 0:
        for x in hist: res[x] = res.get(x, 0) + 1
    elif ftype == 1:
        last = {f: -1 for f in NEW_FORMS}
        for i, x in enumerate(hist): last[x] = i
        for f in NEW_FORMS: res[f] = len(hist) - last[f]
    elif ftype == 2:
        for i in range(1, len(hist)):
            if hist[i] == hist[i-1]: res[hist[i]] = res.get(hist[i], 0) + 1
    elif ftype == 3:
        for i in range(1, len(hist)):
            if hist[i] != hist[i-1]: res[hist[i]] = res.get(hist[i], 0) + 1
    return res

def new_kill_model(hist_data, cfg, mid):
    data = slice_data_hist(hist_data, cfg["slice"], cfg["depth"])
    feat = calc_feature(data, cfg["feature"])
    scores = {}
    for i, f in enumerate(NEW_FORMS):
        base = feat[f]
        noise = math.sin(mid * 0.31 + i) + math.cos(mid * 0.17 * (i+1)) + ((mid % 7) - 3) * 0.1
        if cfg["mode"] == 0: score = base + noise
        elif cfg["mode"] == 1: score = -base + noise
        else: score = math.log(base + 1) + noise
        scores[f] = score
    return [min(scores, key=scores.get)]

for i in range(1, 301):
    mid = i + 300
    cfg = {"depth": 10 + (i % 90), "slice": i % 5, "feature": i % 4, "mode": i % 3}
    ALL_MODELS[mid] = {"func": lambda h, c=cfg, m=mid: new_kill_model(h, c, m), "info": {"id": mid, "name": f"新杀组 M{i}", "type": "杀组"}}

def new_kill_v3(history, mid):
    forms = ["大单", "小单", "大双", "小双"]
    h = [x.get("combo", x.get("combination", "小单")) for x in history[-30:]] if history else forms
    counts = Counter(h)
    idx = mid % 5
    if idx == 0: target = max(forms, key=lambda x: counts.get(x, 0))
    elif idx == 1: target = min(forms, key=lambda x: counts.get(x, 0))
    elif idx == 2: target = {"大单":"小双","小双":"大单","大双":"小单","小单":"大双"}.get(h[0] if h else "小单", "小单")
    elif idx == 3: target = forms[int(history[0].get('nbr', history[0].get('qihao', 0)) if history else 0) % 4]
    else: total = sum(counts.values()) + 1; target = min(forms, key=lambda x: (counts.get(x,0)+1)/total)
    return [target]

for i in range(1, 101):
    mid = i + 600
    ALL_MODELS[mid] = {"func": lambda h, m=mid: new_kill_v3(h, m), "info": {"id": mid, "name": f"V3杀组 M{i}", "type": "杀组"}}

ALL_MODELS[701] = {"func": lambda h: algo_v23_armor(h)[0], "info": {"id": 701, "name": "Armor V23 杀组(原)", "type": "杀组"}}

class ModelManager:
    def __init__(self):
        self.all_models = ALL_MODELS
        # 使用新的集成预测器
        self.ensemble = EnsemblePredictor()

    def predict_kill(self, history: List[Dict]) -> Tuple[str, float]:
        """
        预测杀组
        返回: (杀组目标, 置信度)
        """
        if len(history) < 10:
            return "小单", 0.5
        return self.ensemble.predict(history)
    
    def predict_kill_simple(self, history: List[Dict]) -> str:
        """简化版预测，只返回杀组目标"""
        kill, _ = self.predict_kill(history)
        return kill
    
    def update_prediction_result(self, actual_combo: str):
        """更新预测结果用于在线学习"""
        self.ensemble.update_performance(actual_combo)
    
    def get_ensemble_stats(self) -> Dict:
        """获取集成预测器统计信息"""
        return self.ensemble.get_stats()
    
    # 保留原有的滑动窗口验证方法作为备用
    def predict_kill_legacy(self, history):
        if len(history) < 10: return "小单"
        best_id, best_rate = None, 0
        total = min(50, len(history) - 1)
        for mid, md in self.all_models.items():
            win = 0
            for i in range(1, total):
                try:
                    pred = md["func"](history[i:])
                    actual = history[i-1].get("combo", history[i-1].get("combination", ""))
                    if actual and actual != pred[0]: win += 1
                except: continue
            rate = win / total if total > 0 else 0
            if rate > best_rate: best_rate, best_id = rate, mid
        return self.all_models[best_id]["func"](history)[0] if best_id else "小单"

# ==================== API模块 ====================
class PC28API:
    def __init__(self):
        self.base_url = "https://pc28.help/api"
        self.session = None
        self.call_stats = {'total_calls': 0, 'successful_calls': 0, 'failed_calls': 0, 'last_call_time': None, 'last_success_time': None, 'response_times': deque(maxlen=100)}
        self.cache_file = Config.CACHE_DIR / "history_cache.pkl"
        self.keno_cache_file = Config.CACHE_DIR / "keno_cache.pkl"
        self.history_cache = deque(maxlen=Config.CACHE_SIZE)
        self.keno_cache = deque(maxlen=5000)
        self.load_cache()
        logger.log_system("异步API模块初始化完成")

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT))

    def load_cache(self):
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                self.history_cache.extend(cache_data[:Config.CACHE_SIZE])
            if self.keno_cache_file.exists():
                with open(self.keno_cache_file, 'rb') as f:
                    keno_data = pickle.load(f)
                self.keno_cache.extend(keno_data[:5000])
        except Exception as e: logger.log_error(0, "加载缓存失败", e)

    def save_cache(self):
        try:
            with open(self.cache_file, 'wb') as f: pickle.dump(list(self.history_cache), f)
            with open(self.keno_cache_file, 'wb') as f: pickle.dump(list(self.keno_cache), f)
        except Exception as e: logger.log_error(0, "保存缓存失败", e)

    async def _make_api_call(self, endpoint, params=None):
        await self.ensure_session()
        for retry in range(Config.MAX_RETRIES):
            self.call_stats['total_calls'] += 1
            start = time.time()
            try:
                url = f"{self.base_url}/{endpoint}.json"
                if params:
                    query_string = "&".join(f"{k}={v}" for k, v in params.items())
                    url = f"{url}?{query_string}"
                async with self.session.get(url) as resp:
                    resp.raise_for_status()
                    try: data = await resp.json()
                    except json.JSONDecodeError:
                        if retry < Config.MAX_RETRIES - 1:
                            await asyncio.sleep(Config.RETRY_BACKOFF ** retry)
                            continue
                        else: self.call_stats['failed_calls'] += 1; return None
                    if data.get('message') != 'success':
                        self.call_stats['failed_calls'] += 1
                        if retry < Config.MAX_RETRIES - 1:
                            await asyncio.sleep(Config.RETRY_BACKOFF ** retry)
                            continue
                        else: return None
                    elapsed = time.time() - start
                    self.call_stats['successful_calls'] += 1
                    self.call_stats['response_times'].append(elapsed)
                    self.call_stats['last_call_time'] = datetime.now()
                    self.call_stats['last_success_time'] = datetime.now()
                    return data.get('data', [])
            except asyncio.TimeoutError:
                if retry < Config.MAX_RETRIES - 1: await asyncio.sleep(Config.RETRY_BACKOFF ** retry)
                else: self.call_stats['failed_calls'] += 1; return None
            except Exception:
                if retry < Config.MAX_RETRIES - 1: await asyncio.sleep(Config.RETRY_BACKOFF ** retry)
                else: self.call_stats['failed_calls'] += 1; return None
        return None

    async def fetch_kj(self, nbr=1):
        data = await self._make_api_call('kj', {'nbr': nbr})
        if not data: return []
        processed = []
        for item in data:
            try:
                qihao = str(item.get('nbr', '')).strip()
                if not qihao: continue
                number = item.get('number') or item.get('num')
                if not number: continue
                if isinstance(number, str) and '+' in number:
                    parts = number.split('+')
                    if len(parts) == 3: total = sum(int(p) for p in parts)
                    else: continue
                else:
                    try: total = int(number)
                    except: continue
                combo = item.get('combination', '')
                if combo and len(combo) >= 2: size, parity = combo[0], combo[1]
                else:
                    size = "大" if total >= 14 else "小"
                    parity = "单" if total % 2 else "双"
                    combo = size + parity
                processed.append({'qihao': qihao, 'sum': total, 'size': size, 'parity': parity, 'combo': combo, 'nbr': qihao, 'opentime': f"{item.get('date','')} {item.get('time','')}", 'parsed_time': datetime.now()})
            except Exception as e: logger.log_error(0, f"处理开奖数据项失败", e); continue
        processed.sort(key=lambda x: x.get('parsed_time', datetime.now()), reverse=True)
        return processed

    async def get_history(self, count=50):
        return list(self.history_cache)[:count]

    async def get_latest_result(self):
        latest_api = await self.fetch_kj(nbr=1)
        if not latest_api: return None
        latest = latest_api[0]
        if not any(x.get('qihao') == latest['qihao'] for x in self.history_cache):
            self.history_cache.appendleft(latest)
            if len(self.history_cache) > Config.CACHE_SIZE: self.history_cache.pop()
            self.save_cache()
        return latest

    async def initialize_history(self, count=100, max_retries=3):
        for attempt in range(max_retries):
            kj_data = await self.fetch_kj(nbr=count)
            if kj_data:
                self.history_cache.clear()
                for item in kj_data:
                    if not any(x.get('qihao') == item['qihao'] for x in self.history_cache):
                        self.history_cache.append(item)
                self.save_cache()
                return len(self.history_cache) >= 30
            await asyncio.sleep(2)
        return False

    async def close(self):
        if self.session and not self.session.closed: await self.session.close()

    def get_statistics(self):
        avg = np.mean(self.call_stats['response_times']) if self.call_stats['response_times'] else 0
        success_rate = (self.call_stats['successful_calls'] / self.call_stats['total_calls']) if self.call_stats['total_calls'] else 0
        return {'缓存数据量': len(self.history_cache), '总API调用': self.call_stats['total_calls'], '成功调用': self.call_stats['successful_calls'], '成功率': f"{success_rate:.1%}", '平均响应时间': f"{avg:.2f}秒", '最新期号': self.history_cache[0].get('qihao') if self.history_cache else '无'}

# ==================== 数据模型 ====================
@dataclass
class BetParams:
    base_amount: float = Config.DEFAULT_BASE_AMOUNT
    max_amount: float = Config.DEFAULT_MAX_AMOUNT
    multiplier: float = Config.DEFAULT_MULTIPLIER
    stop_loss: float = Config.DEFAULT_STOP_LOSS
    stop_win: float = Config.DEFAULT_STOP_WIN
    stop_balance: float = Config.DEFAULT_STOP_BALANCE
    resume_balance: float = Config.DEFAULT_RESUME_BALANCE

@dataclass
class Account:
    phone: str
    owner_user_id: int
    created_time: str = field(default_factory=lambda: datetime.now().isoformat())
    is_logged_in: bool = False
    auto_betting: bool = False
    prediction_broadcast: bool = False
    display_name: str = ""
    telegram_user_id: int = 0
    game_group_id: int = 0
    game_group_name: str = ""
    prediction_group_id: int = 0
    prediction_group_name: str = ""
    betting_strategy: str = "保守"
    bet_params: BetParams = field(default_factory=BetParams)
    balance: float = 0
    initial_balance: float = 0
    total_profit: float = 0
    total_loss: float = 0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_bets: int = 0
    total_wins: int = 0
    last_bet_time: Optional[str] = None
    last_bet_period: Optional[str] = None
    last_bet_types: List[str] = field(default_factory=list)
    last_bet_amount: float = 0
    last_bet_total: float = 0
    last_prediction: Dict = field(default_factory=dict)
    input_mode: Optional[str] = None
    input_buffer: str = ""
    stop_reason: Optional[str] = None
    martingale_reset: bool = True
    fibonacci_reset: bool = True
    needs_2fa: bool = False
    login_temp_data: dict = field(default_factory=dict)
    chase_enabled: bool = False
    chase_numbers: List[int] = field(default_factory=list)
    chase_periods: int = 0
    chase_current: int = 0
    chase_amount: int = 0
    chase_stop_reason: Optional[str] = None
    streak_records_double: List[Dict] = field(default_factory=list)
    streak_records_kill: List[Dict] = field(default_factory=list)
    current_streak_type_double: Optional[str] = None
    current_streak_count_double: int = 0
    current_streak_type_kill: Optional[str] = None
    current_streak_count_kill: int = 0
    risk_profile: str = "稳定"
    last_message_id: Optional[int] = None
    prediction_content: str = "kill"
    broadcast_stop_requested: bool = False
    currency: str = Config.DEFAULT_CURRENCY
    last_prediction_confidence: float = 0.0  # 新增：记录上次预测置信度

    def get_display_name(self) -> str:
        return self.display_name if self.display_name else self.phone

    def get_currency_symbol(self) -> str:
        return Config.CURRENCY_SYMBOLS.get(self.currency, "")

    def get_bet_limits(self) -> Tuple[float, float]:
        limits = Config.CURRENCY_BET_LIMITS.get(self.currency, {"min": 1, "max": 10000000})
        return limits["min"], limits["max"]

    def get_risk_factor(self) -> float:
        return Config.RISK_PROFILES.get(self.risk_profile, 0.01)

# ==================== 账户管理器 ====================
class AccountManager:
    def __init__(self):
        self.accounts_file = Config.DATA_DIR / "accounts.json"
        self.user_states_file = Config.DATA_DIR / "user_states.json"
        self.accounts: Dict[str, Account] = {}
        self.user_states: Dict[int, Dict] = {}
        self.clients: Dict[str, TelegramClient] = {}
        self.login_sessions: Dict[str, Dict] = {}
        self.update_lock = asyncio.Lock()
        self.account_locks: Dict[str, asyncio.Lock] = {}
        self.balance_cache: Dict[str, Dict] = {}
        self._dirty: Set[str] = set()
        self._save_task: Optional[asyncio.Task] = None
        self.load_data()
        logger.log_system("账户管理器初始化完成")

    def load_data(self):
        try:
            if self.accounts_file.exists():
                with open(self.accounts_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for phone, acc_data in data.items():
                    bet_params_data = acc_data.pop('bet_params', {})
                    bet_params = BetParams(**bet_params_data)
                    acc_data['bet_params'] = bet_params
                    if 'currency' not in acc_data:
                        acc_data['currency'] = Config.DEFAULT_CURRENCY
                    if 'last_prediction_confidence' not in acc_data:
                        acc_data['last_prediction_confidence'] = 0.0
                    self.accounts[phone] = Account(**acc_data)
            if self.user_states_file.exists():
                with open(self.user_states_file, 'r', encoding='utf-8') as f:
                    self.user_states = {int(k): v for k, v in json.load(f).items()}
        except Exception as e:
            logger.log_error(0, "加载账户数据失败", e)
            self.accounts = {}
            self.user_states = {}

    async def _periodic_save(self):
        while True:
            try:
                await asyncio.sleep(Config.ACCOUNT_SAVE_INTERVAL)
                await self.save_data()
            except asyncio.CancelledError:
                await self.save_data()
                break
            except Exception as e:
                logger.log_error(0, "定期保存失败", e)

    async def save_data(self):
        async with self.update_lock:
            try:
                data = {}
                for phone, acc in self.accounts.items():
                    acc_dict = asdict(acc)
                    acc_dict['bet_params'] = asdict(acc.bet_params)
                    data[phone] = acc_dict
                with open(self.accounts_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                with open(self.user_states_file, 'w', encoding='utf-8') as f:
                    json.dump(self.user_states, f, ensure_ascii=False, indent=2)
                self._dirty.clear()
            except Exception as e:
                logger.log_error(0, "保存账户数据失败", e)

    def get_account(self, phone: str) -> Optional[Account]:
        return self.accounts.get(phone)

    def get_user_accounts(self, user_id: int) -> List[Account]:
        return [acc for acc in self.accounts.values() if acc.owner_user_id == user_id]

    async def add_account(self, user_id: int, phone: str) -> Tuple[bool, str]:
        phone = phone.strip()
        if not phone.startswith('+'):
            return False, "手机号必须包含国际区号(如+86)"
        if phone in self.accounts:
            return False, "该账户已存在"
        user_accounts = self.get_user_accounts(user_id)
        if len(user_accounts) >= Config.MAX_ACCOUNTS_PER_USER:
            return False, f"每个用户最多添加{Config.MAX_ACCOUNTS_PER_USER}个账户"
        acc = Account(phone=phone, owner_user_id=user_id)
        self.accounts[phone] = acc
        self.account_locks[phone] = asyncio.Lock()
        await self.save_data()
        logger.log_account(user_id, phone, "添加账户")
        return True, "账户添加成功"

    async def update_account(self, phone: str, **kwargs):
        async with self.account_locks.setdefault(phone, asyncio.Lock()):
            acc = self.accounts.get(phone)
            if not acc:
                return False
            for key, value in kwargs.items():
                if key == 'bet_params' and isinstance(value, dict):
                    for bp_key, bp_value in value.items():
                        if hasattr(acc.bet_params, bp_key):
                            setattr(acc.bet_params, bp_key, bp_value)
                elif hasattr(acc, key):
                    setattr(acc, key, value)
            self._dirty.add(phone)
        return True

    def set_user_state(self, user_id: int, key: str, value: Any):
        if user_id not in self.user_states:
            self.user_states[user_id] = {}
        self.user_states[user_id][key] = value

    def get_user_state(self, user_id: int, key: str, default=None):
        return self.user_states.get(user_id, {}).get(key, default)

    def create_client(self, phone: str) -> Optional[TelegramClient]:
        if phone in self.clients:
            return self.clients[phone]
        try:
            session_name = phone.replace('+', '')
            session_path = Config.SESSIONS_DIR / session_name
            client = TelegramClient(str(session_path), Config.API_ID, Config.API_HASH)
            self.clients[phone] = client
            return client
        except Exception as e:
            logger.log_error(0, f"创建客户端失败 {phone}", e)
            return None

    async def ensure_client_connected(self, phone: str) -> bool:
        client = self.clients.get(phone)
        if not client:
            client = self.create_client(phone)
        if not client:
            return False
        try:
            if not client.is_connected():
                await client.connect()
            return await client.is_user_authorized()
        except Exception as e:
            logger.log_error(0, f"检查客户端连接失败 {phone}", e)
            return False

    async def verify_login_status(self):
        for phone, acc in list(self.accounts.items()):
            if acc.is_logged_in:
                connected = await self.ensure_client_connected(phone)
                if not connected:
                    await self.update_account(phone, is_logged_in=False)
                    logger.log_account(acc.owner_user_id, phone, "登录状态验证失败，已重置")

    def get_cached_balance(self, phone: str) -> Optional[float]:
        cache = self.balance_cache.get(phone)
        if cache:
            if (datetime.now() - cache['time']).seconds < Config.BALANCE_CACHE_SECONDS:
                return cache['balance']
        return None

    def update_balance_cache(self, phone: str, balance: float):
        self.balance_cache[phone] = {'balance': balance, 'time': datetime.now()}

    async def reset_auto_flags_on_start(self):
        for phone, acc in self.accounts.items():
            if acc.auto_betting or acc.prediction_broadcast:
                await self.update_account(phone, auto_betting=False, prediction_broadcast=False)
        logger.log_system("已重置所有账户的自动投注和播报标志")

    async def start_periodic_save(self): self._save_task = asyncio.create_task(self._periodic_save())

    async def stop_periodic_save(self):
        if self._save_task: self._save_task.cancel()
        try: await self._save_task
        except asyncio.CancelledError: pass

# ==================== 金额管理器 ====================
class AmountManager:
    def __init__(self, account_manager):
        self.account_manager = account_manager

    async def set_param(self, phone, param_name, amount, user_id):
        if amount < 0:
            return False, "金额不能为负数"
        acc = self.account_manager.get_account(phone)
        if not acc:
            return False, "账户不存在"

        min_limit, max_limit = acc.get_bet_limits()
        if param_name in ['base_amount', 'max_amount']:
            if amount < min_limit:
                return False, f"金额不能小于 {min_limit}{acc.get_currency_symbol()}"
            if amount > max_limit:
                return False, f"金额不能大于 {max_limit}{acc.get_currency_symbol()}"

        valid_params = ['base_amount', 'max_amount', 'stop_loss', 'stop_win', 'stop_balance', 'resume_balance']
        if param_name not in valid_params:
            return False, f"无效参数，可选: {', '.join(valid_params)}"
        if param_name == 'base_amount' and amount > acc.balance:
            return False, f"基础金额不能超过当前余额 {format_amount(acc.balance, acc.currency)}"
        await self.account_manager.update_account(phone, bet_params={param_name: amount})
        logger.log_betting(user_id, "设置金额参数", f"账户:{phone} {param_name}={amount}{acc.get_currency_symbol()}")
        return True, f"{param_name} 已设置为 {format_amount(amount, acc.currency)}"

# ==================== 策略管理器 ====================
class BettingStrategyManager:
    def __init__(self, account_manager):
        self.account_manager = account_manager
        self.strategies = {
            '保守': {'description': '保守策略', 'base_amount': 10000, 'max_amount': 100000, 'multiplier': 1.5, 'stop_loss': 100000, 'stop_win': 50000, 'stop_balance': 50000, 'resume_balance': 200000},
            '平衡': {'description': '平衡策略', 'base_amount': 50000, 'max_amount': 500000, 'multiplier': 2.0, 'stop_loss': 500000, 'stop_win': 250000, 'stop_balance': 100000, 'resume_balance': 500000},
            '激进': {'description': '激进策略', 'base_amount': 100000, 'max_amount': 1000000, 'multiplier': 2.5, 'stop_loss': 1000000, 'stop_win': 500000, 'stop_balance': 200000, 'resume_balance': 1000000},
        }

    async def set_strategy(self, phone, strategy_name, user_id):
        if strategy_name not in self.strategies: return False, "无效策略"
        cfg = self.strategies[strategy_name]
        await self.account_manager.update_account(phone, betting_strategy=strategy_name, risk_profile=strategy_name, bet_params={'base_amount': cfg['base_amount'], 'max_amount': cfg['max_amount'], 'multiplier': cfg['multiplier'], 'stop_loss': cfg['stop_loss'], 'stop_win': cfg['stop_win'], 'stop_balance': cfg.get('stop_balance', 0), 'resume_balance': cfg.get('resume_balance', 100000)})
        return True, f"已设置为: {strategy_name}"

# ==================== 游戏调度器 ====================
class GameScheduler:
    def __init__(self, account_manager, model, api_client):
        self.account_manager = account_manager
        self.model = model
        self.api = api_client
        self.game_stats = {'total_cycles': 0, 'betting_cycles': 0, 'successful_bets': 0, 'failed_bets': 0, 'total_profit': 0, 'total_loss': 0}
        self.amount_manager = AmountManager(account_manager)

    async def start_auto_betting(self, phone, user_id):
        acc = self.account_manager.get_account(phone)
        if not acc: return False, "账户不存在"
        if not acc.is_logged_in: return False, "请先登录账户"
        if not acc.game_group_id: return False, "请先设置游戏群"
        await self.account_manager.update_account(phone, auto_betting=True, martingale_reset=True, fibonacci_reset=True)
        logger.log_betting(user_id, "自动投注开启", f"账户:{phone}")
        return True, "自动投注已开启"

    async def stop_auto_betting(self, phone, user_id):
        await self.account_manager.update_account(phone, auto_betting=False)
        logger.log_betting(user_id, "自动投注关闭", f"账户:{phone}")
        return True, "自动投注已关闭"

    async def execute_bet(self, phone, kill_target, latest, confidence=0.5):
        acc = self.account_manager.get_account(phone)
        if not acc or not acc.auto_betting: return
        if not await self.account_manager.ensure_client_connected(phone): return
        current_qihao = latest.get('qihao')
        if acc.last_bet_period == current_qihao: return

        # 获取当前余额
        current_balance = await self.get_balance(phone)
        if current_balance is None:
            current_balance = acc.balance

        # 根据置信度调整投注金额
        confidence_multiplier = 0.5 + confidence  # confidence 0-1 -> 0.5-1.5
        confidence_multiplier = max(0.5, min(1.5, confidence_multiplier))
        
        # 使用基础金额
        base_amount = acc.bet_params.base_amount
        # 根据置信度调整
        adjusted_base = base_amount * confidence_multiplier

        # 计算倍投乘数
        current_multiplier = 1.0
        if acc.consecutive_losses > 0:
            current_multiplier = acc.bet_params.multiplier ** acc.consecutive_losses

        # 获取币种限额
        min_limit, max_limit = acc.get_bet_limits()

        # 获取除杀组外的所有组合
        bet_types = [c for c in COMBOS if c != kill_target]
        bet_parts = []
        total_bet_amount = 0

        for t in bet_types:
            calculated_amount = adjusted_base * current_multiplier
            calculated_amount = min(calculated_amount, max_limit)
            calculated_amount = max(calculated_amount, min_limit)
            # 币种精度处理
            if acc.currency != "KKCOIN":
                calculated_amount = round(calculated_amount, 2)
            else:
                calculated_amount = int(calculated_amount)

            bet_parts.append(f"{t}{calculated_amount}")
            total_bet_amount += calculated_amount

        # 拼接最终下注指令文本
        message = " ".join(bet_parts)

        client = self.account_manager.clients.get(phone)
        gid = acc.game_group_id
        try:
            await client.send_message(gid, message)
            self.game_stats['successful_bets'] += 1
            self.game_stats['betting_cycles'] += 1

            await self.account_manager.update_account(
                phone,
                last_bet_time=datetime.now().isoformat(),
                last_bet_amount=total_bet_amount,
                last_bet_types=bet_types,
                total_bets=acc.total_bets + 1,
                last_bet_total=total_bet_amount,
                last_prediction={'kill': kill_target, 'confidence': confidence},
                last_bet_period=current_qihao,
                balance=current_balance,
                last_prediction_confidence=confidence
            )
            logger.log_betting(0, "投注成功", f"账户:{phone} 币种:{acc.currency} 每注:{format_amount(adjusted_base * current_multiplier, acc.currency)} 总金额:{format_amount(total_bet_amount, acc.currency)} 置信度:{confidence:.2f}\n{message}")
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.log_error(0, f"投注失败 {phone}", e)
            self.game_stats['failed_bets'] += 1

    async def get_balance(self, phone: str) -> Optional[float]:
        cached = self.account_manager.get_cached_balance(phone)
        if cached is not None: return cached
        client = self.account_manager.clients.get(phone)
        acc = self.account_manager.get_account(phone)
        if not client or not acc or not await self.account_manager.ensure_client_connected(phone): return None
        try:
            await client.send_message(Config.BALANCE_BOT, "/start")
            await asyncio.sleep(2)
            msgs = await client.get_messages(Config.BALANCE_BOT, limit=5)
            balances = {'KKCOIN': 0.0, 'USDT': 0.0, 'CNY': 0.0}
            for msg in msgs:
                if msg.text:
                    kk_match = re.search(r'KKCOIN\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if kk_match:
                        balances['KKCOIN'] = float(kk_match.group(1).replace(',', ''))
                    usdt_match = re.search(r'USDT\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if usdt_match:
                        balances['USDT'] = float(usdt_match.group(1).replace(',', ''))
                    cny_match = re.search(r'CNY\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if cny_match:
                        balances['CNY'] = float(cny_match.group(1).replace(',', ''))
                    if balances['KKCOIN'] > 0 or balances['USDT'] > 0 or balances['CNY'] > 0:
                        break
            selected_balance = balances.get(acc.currency, 0)
            if selected_balance > 0:
                self.account_manager.update_balance_cache(phone, selected_balance)
                await self.account_manager.update_account(phone, balance=selected_balance)
                return selected_balance
        except Exception as e: logger.log_error(0, f"查询余额失败 {phone}", e)
        return None

    def get_stats(self):
        auto = sum(1 for a in self.account_manager.accounts.values() if a.auto_betting)
        broadcast = sum(1 for a in self.account_manager.accounts.values() if a.prediction_broadcast)
        return {'auto_betting_accounts': auto, 'broadcast_accounts': broadcast, 'game_stats': self.game_stats.copy()}

# ==================== 全局调度器 ====================
class GlobalScheduler:
    def __init__(self, account_manager, model, api_client, game_scheduler):
        self.account_manager = account_manager
        self.model = model
        self.api = api_client
        self.game_scheduler = game_scheduler
        self.task = None
        self.running = False
        self.last_qihao = None
        self.check_interval = Config.SCHEDULER_CHECK_INTERVAL
        self.health_check_interval = Config.HEALTH_CHECK_INTERVAL
        self.last_health_check = 0
        self.bet_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_BETS)
        self.tasks = set()

    async def start(self):
        if self.running: return
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.tasks.add(self.task)
        logger.log_system("全局调度器已启动")

    async def stop(self):
        self.running = False
        self.tasks = {t for t in self.tasks if not t.done()}
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        logger.log_system("全局调度器已停止")

    def _create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _run(self):
        if not await self.api.initialize_history():
            logger.log_error(0, "全局调度器", "无法初始化历史数据")
        while self.running:
            try:
                if (time.time() - self.last_health_check) > self.health_check_interval:
                    await self._health_check()
                    self.last_health_check = time.time()
                latest = await self.api.get_latest_result()
                if latest:
                    qihao = latest.get('qihao')
                    if qihao != self.last_qihao:
                        logger.log_game(f"检测到新期号: {qihao}")
                        await self._on_new_period(qihao, latest)
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.log_error(0, "全局调度器异常", e)
                await asyncio.sleep(10)

    async def _health_check(self):
        now = datetime.now()
        expired_phones = []
        for phone, cache in self.account_manager.balance_cache.items():
            if (now - cache['time']).seconds > Config.BALANCE_CACHE_SECONDS * 2: expired_phones.append(phone)
        for phone in expired_phones: del self.account_manager.balance_cache[phone]

    async def _on_new_period(self, qihao, latest):
        # 优先根据上一期的投注和真实开奖结果,更新连输/回归状态机
        actual_combo = latest.get('combo')
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.last_prediction:
                last_kill = acc.last_prediction.get('kill')
                if last_kill:
                    if actual_combo == last_kill:
                        # 杀组失败（开出了杀组目标），算输
                        new_losses = acc.consecutive_losses + 1
                        new_total_losses = acc.total_loss + acc.last_bet_total
                        await self.account_manager.update_account(phone, consecutive_losses=new_losses, total_loss=new_total_losses)
                        logger.log_game(f"[{phone}] 上期杀【{last_kill}】失败(开出{actual_combo}),连输: {new_losses}, 本期亏损: {acc.last_bet_total}")
                    else:
                        # 杀组成功（开出的不是杀组目标），算赢
                        # PC28杀组赢：投了3注（除杀组外的3个组合），每注赔率约3倍，净盈利 = 赢注金额*3 - 总投注金额
                        bet_per_type = acc.last_bet_total / 3 if acc.last_bet_total > 0 else 0
                        win_amount = bet_per_type * 3  # 赢的那注赔率3倍
                        net_profit = win_amount - acc.last_bet_total  # 净盈利 = 赢回 - 总投注
                        new_total_profit = acc.total_profit + net_profit
                        new_total_wins = acc.total_wins + 1
                        await self.account_manager.update_account(phone, consecutive_losses=0, total_profit=new_total_profit, total_wins=new_total_wins)
                        logger.log_game(f"[{phone}] 上期杀【{last_kill}】成功(开出{actual_combo}),连输清零, 本期净赚: {net_profit}")
        
        # 更新模型表现（在线学习）
        if actual_combo:
            self.model.update_prediction_result(actual_combo)

        # 获取最新历史数据进行新一轮预测
        history = await self.api.get_history(50)
        if len(history) < 10:
            logger.log_game("历史数据不足,跳过预测")
            return

        # 使用集成预测器获取杀组和置信度
        kill_target, confidence = self.model.predict_kill(history)
        logger.log_prediction(0, "集成预测杀组", f"期号:{qihao} 杀:{kill_target} 置信度:{confidence:.2f}")
        
        # 输出各模型权重信息（调试用）
        ensemble_stats = self.model.get_ensemble_stats()
        logger.log_system(f"集成模型权重: {ensemble_stats['weights']}")

        # 投注延迟
        await asyncio.sleep(20)
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.is_logged_in and acc.game_group_id:
                self._create_task(self._execute_bet_with_semaphore(phone, kill_target, latest, confidence))
        self.last_qihao = qihao

    async def _execute_bet_with_semaphore(self, phone, kill_target, latest, confidence):
        async with self.bet_semaphore:
            await self.game_scheduler.execute_bet(phone, kill_target, latest, confidence)

# ==================== 主Bot类 ====================
class PC28Bot:
    def __init__(self):
        self.api = PC28API()
        self.account_manager = AccountManager()
        self.model = ModelManager()
        self.strategy_manager = BettingStrategyManager(self.account_manager)
        self.amount_manager = AmountManager(self.account_manager)
        self.game_scheduler = GameScheduler(self.account_manager, self.model, self.api)
        self.global_scheduler = GlobalScheduler(self.account_manager, self.model, self.api, self.game_scheduler)
        self.application = Application.builder().token(Config.BOT_TOKEN).build()
        self._register_handlers()
        logger.log_system("PC28 Bot初始化完成（已集成优化预测器）")

    def _register_handlers(self):
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("cancel", self.cmd_cancel))
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.login_select, pattern=r'^login_select:')],
            states={
                Config.LOGIN_SELECT: [],
                Config.LOGIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.login_code)],
                Config.LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.login_password)],
            },
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(conv_handler)
        add_account_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_account_start, pattern=r'^add_account$')],
            states={Config.ADD_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(add_account_conv)
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_error_handler(self.error_handler)
        # 新增：查看预测统计的命令
        self.application.add_handler(CommandHandler("predict_stats", self.cmd_predict_stats))

    async def error_handler(self, update, context):
        logger.log_error(0, "Bot错误", str(context.error))

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ 操作已取消")
        return ConversationHandler.END

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("📱 账户管理", callback_data="menu:accounts")],
            [InlineKeyboardButton("🎯 智能预测", callback_data="menu:prediction")],
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:status")],
        ]
        await update.message.reply_text("🎰 *PC28 智能投注系统 v3.0*\n\n✨ 欢迎使用!\n🔄 已集成多模型集成预测", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def cmd_predict_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看预测统计信息"""
        stats = self.model.get_ensemble_stats()
        text = f"📊 *集成预测器统计*\n\n"
        text += f"📈 近期胜率: {stats.get('recent_win_rate', 'N/A')}\n"
        text += f"🎯 预测次数: {stats.get('prediction_count', 0)}\n\n"
        text += f"⚖️ *模型权重:*\n"
        for name, weight in stats.get('weights', {}).items():
            text += f"  • {name}: {weight:.1%}\n"
        text += f"\n💡 权重会根据实际表现自动调整"
        await update.message.reply_text(text, parse_mode='Markdown')

    async def add_account_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("📱 请输入手机号(包含国际区号,如 +861234567890):\n\n点击 /cancel 取消")
        return Config.ADD_ACCOUNT

    async def add_account_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = update.message.text.strip()
        ok, msg = await self.account_manager.add_account(user_id, phone)
        if ok:
            await update.message.reply_text(f"✅ {msg}")
            await self._show_account_detail(update.message, user_id, phone)
        else:
            await update.message.reply_text(f"❌ {msg}")
        return ConversationHandler.END

    async def login_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['login_phone'] = phone
        acc = self.account_manager.get_account(phone)
        if not acc: await query.edit_message_text("账户不存在"); return ConversationHandler.END
        if acc.is_logged_in: await self._show_account_detail(query, query.from_user.id, phone); return ConversationHandler.END
        client = self.account_manager.create_client(phone)
        if not client: await query.edit_message_text("创建客户端失败"); return ConversationHandler.END
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                display = f"{me.first_name or ''} {me.last_name or ''}".strip()
                await self.account_manager.update_account(phone, is_logged_in=True, display_name=display, telegram_user_id=me.id)
                await self._show_account_detail(query, query.from_user.id, phone)
                return ConversationHandler.END
            else:
                await client.send_code_request(phone)
                await query.edit_message_text(f"📱 已向 {phone} 发送验证码，请输入:\n\n点击 /cancel 取消")
                return Config.LOGIN_CODE
        except Exception as e:
            logger.log_error(query.from_user.id, f"登录初始化失败 {phone}", e)
            await query.edit_message_text(f"❌ 登录初始化失败: {e}")
            return ConversationHandler.END

    async def login_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = context.user_data.get('login_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期，请重新登录")
            return ConversationHandler.END
        code = update.message.text.strip()
        client = self.account_manager.clients.get(phone)
        if not client:
            await update.message.reply_text("❌ 客户端未找到")
            return ConversationHandler.END
        try:
            await client.sign_in(phone, code)
            me = await client.get_me()
            display = f"{me.first_name or ''} {me.last_name or ''}".strip()
            await self.account_manager.update_account(phone, is_logged_in=True, display_name=display, telegram_user_id=me.id)
            await update.message.reply_text(f"✅ 登录成功! 欢迎 {display}")
            await self._show_account_detail(update.message, user_id, phone)
            return ConversationHandler.END
        except SessionPasswordNeededError:
            await update.message.reply_text("🔐 需要两步验证密码，请输入:\n\n点击 /cancel 取消")
            return Config.LOGIN_PASSWORD
        except Exception as e:
            logger.log_error(user_id, f"验证码登录失败 {phone}", e)
            await update.message.reply_text(f"❌ 登录失败: {e}")
            return ConversationHandler.END

    async def login_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = context.user_data.get('login_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        password = update.message.text.strip()
        client = self.account_manager.clients.get(phone)
        if not client:
            await update.message.reply_text("❌ 客户端未找到")
            return ConversationHandler.END
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            display = f"{me.first_name or ''} {me.last_name or ''}".strip()
            await self.account_manager.update_account(phone, is_logged_in=True, display_name=display, telegram_user_id=me.id)
            await update.message.reply_text(f"✅ 登录成功! 欢迎 {display}")
            await self._show_account_detail(update.message, user_id, phone)
            return ConversationHandler.END
        except Exception as e:
            logger.log_error(user_id, f"密码登录失败 {phone}", e)
            await update.message.reply_text(f"❌ 登录失败: {e}")
            return ConversationHandler.END

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = query.from_user.id
        data = query.data

        if data == "menu:main":
            await self._show_main_menu(query)
        elif data == "menu:accounts":
            await self._show_accounts_menu(query, user)
        elif data == "menu:prediction":
            await self._show_prediction(query)
        elif data == "menu:status":
            await self._show_status(query)
        elif data.startswith("select_account:"):
            phone = data.split(':')[1]
            await self._show_account_detail(query, user, phone)
        elif data.startswith("action:"):
            parts = data.split(':')
            action, phone = parts[1], parts[2]
            await self._process_action(query, user, action, phone)
        elif data.startswith("set_strategy:"):
            parts = data.split(':')
            phone, strategy = parts[1], parts[2]
            await self._process_set_strategy(query, user, phone, strategy)
        elif data.startswith("set_group:"):
            group_id = int(data.split(':')[1])
            state = self.account_manager.get_user_state(user, 'account_selected')
            if state and 'current_account' in state:
                phone = state['current_account']
                await self.account_manager.update_account(phone, game_group_id=group_id)
                await self._show_account_detail(query, user, phone)
        elif data.startswith("set_currency:"):
            parts = data.split(":")
            if len(parts) == 3:
                phone, currency = parts[1], parts[2]
                await self._set_currency(query, user, phone, currency)
        elif data == "refresh_status":
            await self._show_status(query)

    async def _show_main_menu(self, query):
        kb = [
            [InlineKeyboardButton("📱 账户管理", callback_data="menu:accounts")],
            [InlineKeyboardButton("🎯 智能预测", callback_data="menu:prediction")],
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:status")],
        ]
        await query.edit_message_text("🎰 *PC28 智能投注系统 v3.0*\n\n✨ 请选择操作:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_accounts_menu(self, query, user):
        accounts = self.account_manager.get_user_accounts(user)
        kb = []
        text = "📱 *您的账户列表*\n\n" if accounts else "📭 您还没有添加账户"
        if accounts:
            for acc in accounts:
                status = "✅" if acc.is_logged_in else "❌"
                text += f"{status} {acc.get_display_name()} ({acc.currency})\n"
        kb.append([InlineKeyboardButton("➕ 添加账户", callback_data="add_account")])
        if accounts:
            for acc in accounts:
                kb.append([InlineKeyboardButton(f"{acc.get_display_name()}", callback_data=f"select_account:{acc.phone}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="menu:main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_account_detail(self, query_or_message, user, phone):
        self.account_manager.set_user_state(user, 'account_selected', {'current_account': phone})
        acc = self.account_manager.get_account(phone)
        if not acc:
            try: await query_or_message.edit_message_text("❌ 账户不存在")
            except: await query_or_message.reply_text("❌ 账户不存在")
            return
        display = acc.get_display_name()
        status = "✅ 已登录" if acc.is_logged_in else "❌ 未登录"
        if acc.auto_betting: status += " | 🤖 自动投注"
        bet_button = "🛑 停止自动投注" if acc.auto_betting else "🤖 开启自动投注"
        net_profit = acc.total_profit - acc.total_loss
        win_rate = f"{acc.total_wins / acc.total_bets:.1%}" if acc.total_bets > 0 else "N/A"
        loss_count = acc.total_bets - acc.total_wins

        kb = [
            [InlineKeyboardButton("🔐 登录", callback_data=f"login_select:{phone}"),
             InlineKeyboardButton("🚪 登出", callback_data=f"action:logout:{phone}")],
            [InlineKeyboardButton("💬 游戏群", callback_data=f"action:listgroups:{phone}")],
            [InlineKeyboardButton("📈 金额策略", callback_data=f"action:setstrategy:{phone}"),
             InlineKeyboardButton("💱 投注币种", callback_data=f"action:setcurrency:{phone}")],
            [InlineKeyboardButton(bet_button, callback_data=f"action:toggle_bet:{phone}")],
            [InlineKeyboardButton("💰 查询余额", callback_data=f"action:balance:{phone}"),
             InlineKeyboardButton("📊 账户状态", callback_data=f"action:status:{phone}")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu:accounts")]
        ]
        text = f"📱 *账户: {display}*\n\n状态: {status}\n币种: {acc.currency}\n余额: {format_amount(acc.balance, acc.currency)}\n\n📊 *投注统计*\n• 投注期数: {acc.total_bets}期\n• 赢了: {acc.total_wins}期\n• 输了: {loss_count}期\n• 胜率: {win_rate}\n• 净盈利: {format_amount(net_profit, acc.currency)}\n• 基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}\n\n选择操作:"
        try: await query_or_message.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except: await query_or_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_prediction(self, query):
        history = await self.api.get_history(50)
        if len(history) < 10:
            await query.edit_message_text("❌ 历史数据不足")
            return
        kill_target, confidence = self.model.predict_kill(history)
        latest = history[0] if history else {'qihao': 'N/A', 'combo': 'N/A'}
        
        # 置信度可视化
        confidence_bar = "█" * int(confidence * 10) + "░" * (10 - int(confidence * 10))
        
        text = f"🎯 *当前预测（集成模型）*\n\n"
        text += f"📊 最新期号: {latest.get('qihao')}\n"
        text += f"📌 最新结果: {latest.get('combo')}\n\n"
        text += f"🚫 *杀组推荐:* {kill_target}\n"
        text += f"📈 *置信度:* {confidence_bar} {confidence:.1%}\n\n"
        text += f"💡 投注建议: {' '.join([c for c in COMBOS if c != kill_target])}\n\n"
        text += f"⚙️ 集成模型会随实际结果自动优化"
        
        kb = [[InlineKeyboardButton("🔄 刷新预测", callback_data="menu:prediction")],
              [InlineKeyboardButton("📊 查看模型统计", callback_data="menu:model_stats")],
              [InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_status(self, query):
        api_stats = self.api.get_statistics()
        sched_stats = self.game_scheduler.get_stats()
        ensemble_stats = self.model.get_ensemble_stats()
        total_accounts = len(self.account_manager.accounts)
        logged = sum(1 for a in self.account_manager.accounts.values() if a.is_logged_in)
        auto = sched_stats['auto_betting_accounts']
        text = f"📊 *系统状态 v3.0*\n\n"
        text += f"📈 *API状态*\n• 缓存数据: {api_stats['缓存数据量']}期\n• 最新期号: {api_stats['最新期号']}\n• 成功率: {api_stats['成功率']}\n\n"
        text += f"👥 *账户状态*\n• 总账户: {total_accounts}\n• 已登录: {logged}\n• 自动投注: {auto}\n• 成功投注: {sched_stats['game_stats']['successful_bets']}\n\n"
        text += f"🤖 *集成预测器*\n• 近期胜率: {ensemble_stats.get('recent_win_rate', 'N/A')}\n• 模型权重: {', '.join([f'{k}={v:.0%}' for k,v in ensemble_stats.get('weights', {}).items()])}"
        kb = [[InlineKeyboardButton("🔄 刷新", callback_data="refresh_status")],
              [InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _process_action(self, query, user, action, phone):
        if action == "logout":
            await self.game_scheduler.stop_auto_betting(phone, user)
            client = self.account_manager.clients.get(phone)
            if client:
                try:
                    if client.is_connected(): await client.disconnect()
                except: pass
                self.account_manager.clients.pop(phone, None)
            session_name = phone.replace('+', '')
            for ext in ['.session', '.session-journal']:
                file_path = Config.SESSIONS_DIR / (session_name + ext)
                if file_path.exists(): file_path.unlink()
            await self.account_manager.update_account(phone, is_logged_in=False, auto_betting=False, display_name='')
            await self._show_account_detail(query, user, phone)
        elif action == "toggle_bet":
            acc = self.account_manager.get_account(phone)
            if acc.auto_betting: await self.game_scheduler.stop_auto_betting(phone, user)
            else: await self.game_scheduler.start_auto_betting(phone, user)
            await self._show_account_detail(query, user, phone)
        elif action == "balance":
            acc = self.account_manager.get_account(phone)
            bal = await self.game_scheduler.get_balance(phone)
            if bal is not None:
                text = f"💰 余额: {format_amount(bal, acc.currency if acc else 'KKCOIN')}"
            else:
                text = "❌ 查询失败"
            kb = [[InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "status":
            acc = self.account_manager.get_account(phone)
            net_profit = acc.total_profit - acc.total_loss
            win_rate = f"{acc.total_wins / acc.total_bets:.1%}" if acc.total_bets > 0 else "N/A"
            loss_count = acc.total_bets - acc.total_wins
            text = f"📱 账户状态\n\n• 手机号: {acc.phone}\n• 登录: {'✅' if acc.is_logged_in else '❌'}\n• 自动投注: {'✅' if acc.auto_betting else '❌'}\n• 投注币种: {acc.currency}\n• 游戏群: {acc.game_group_name or '未设置'}\n• 余额: {format_amount(acc.balance, acc.currency)}\n\n📊 *投注统计*\n• 投注期数: {acc.total_bets}期\n• 赢了: {acc.total_wins}期\n• 输了: {loss_count}期\n• 胜率: {win_rate}\n• 净盈利: {format_amount(net_profit, acc.currency)}\n• 基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}"
            kb = [[InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "listgroups":
            client = self.account_manager.clients.get(phone)
            if client:
                try:
                    dialogs = await client.get_dialogs(limit=30)
                    groups = [d for d in dialogs if d.is_group or d.is_channel]
                    kb = []
                    for g in groups[:10]:
                        icon = "📢" if g.is_channel else "👥"
                        kb.append([InlineKeyboardButton(f"{icon} {g.name[:30]}", callback_data=f"set_group:{g.id}")])
                    kb.append([InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")])
                    await query.edit_message_text("📋 选择游戏群:", reply_markup=InlineKeyboardMarkup(kb))
                except: await query.edit_message_text("❌ 获取群组列表失败")
            else: await query.edit_message_text("❌ 客户端未连接")
        elif action == "setstrategy":
            kb = [[InlineKeyboardButton(name, callback_data=f"set_strategy:{phone}:{name}")] for name in self.strategy_manager.strategies.keys()]
            kb.append([InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")])
            await query.edit_message_text("📊 选择投注策略:", reply_markup=InlineKeyboardMarkup(kb))
        elif action == "setcurrency":
            await self._show_currency_menu(query, user, phone)

    async def _process_set_strategy(self, query, user, phone, strategy):
        ok, msg = await self.strategy_manager.set_strategy(phone, strategy, user)
        if ok: await self._show_account_detail(query, user, phone)
        else: await query.edit_message_text(f"❌ {msg}")

    async def _show_currency_menu(self, query, user, phone):
        acc = self.account_manager.get_account(phone)
        if not acc:
            await query.edit_message_text("❌ 账户不存在")
            return
        current = acc.currency
        kb = []
        for currency in Config.AVAILABLE_CURRENCIES:
            mark = "✅ " if currency == current else ""
            kb.append([InlineKeyboardButton(f"{mark}{currency}", callback_data=f"set_currency:{phone}:{currency}")])
        kb.append([InlineKeyboardButton("🔙 返回账户详情", callback_data=f"select_account:{phone}")])
        text = f"""
💱 *投注币种设置*

当前币种: {current}

选择投注时使用的币种：

• KKCOIN - 平台积分，默认币种
• USDT - 稳定币
• CNY - 人民币

余额显示和投注金额都会按您选择的币种计算。

⚠️ 注意：切换币种后，请重新设置投注金额。
        """
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _set_currency(self, query, user, phone, currency):
        if currency not in Config.AVAILABLE_CURRENCIES:
            await query.edit_message_text("❌ 无效币种")
            return
        await self.account_manager.update_account(phone, currency=currency)
        self.account_manager.balance_cache.pop(phone, None)
        await query.edit_message_text(f"✅ 投注币种已切换为 {currency}")
        await self._show_account_detail(query, user, phone)

# ==================== 启动 ====================
async def post_init(application):
    bot = application.bot_data.get('bot')
    if bot:
        await bot.account_manager.reset_auto_flags_on_start()
        await bot.account_manager.verify_login_status()
        await bot.account_manager.start_periodic_save()
        if hasattr(bot, 'global_scheduler'): await bot.global_scheduler.start()

def main():
    def handle_shutdown(signum, frame):
        print("\n🛑 正在关闭...")
        if 'bot' in globals():
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(lambda: asyncio.create_task(bot.global_scheduler.stop()))
                loop.call_soon_threadsafe(lambda: asyncio.create_task(bot.account_manager.stop_periodic_save()))
                loop.call_soon_threadsafe(lambda: asyncio.create_task(bot.api.close()))
                for phone, client in bot.account_manager.clients.items():
                    if client.is_connected(): loop.call_soon_threadsafe(lambda: asyncio.create_task(client.disconnect()))
            except RuntimeError:
                asyncio.run(bot.global_scheduler.stop())
                asyncio.run(bot.account_manager.stop_periodic_save())
                asyncio.run(bot.api.close())
                for phone, client in bot.account_manager.clients.items():
                    if client.is_connected(): asyncio.run(client.disconnect())
        print("✅ 已安全关闭")
        exit(0)
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    print("=" * 40)
    print("PC28 智能预测投注系统 v3.0")
    print("多币种支持: KKCOIN / USDT / CNY")
    print("集成预测器: 趋势 + 概率 + V3 + 原始")
    print("=" * 40)
    try: Config.validate()
    except ValueError as e: print(f"❌ 配置错误: {e}"); return
    bot = PC28Bot()
    bot.application.bot_data['bot'] = bot
    bot.application.post_init = post_init
    print("✅ Bot已启动")
    bot.application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    random.seed(time.time())
    np.random.seed(int(time.time()))
    main()