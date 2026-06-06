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
    DEFAULT_BASE_AMOUNT = 2.00
    DEFAULT_MULTIPLIER = 2.0
    DEFAULT_STOP_BALANCE = 0
    MIN_BET_AMOUNT = 0.1
    MAX_BET_AMOUNT = 10000
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
    SET_BASE_AMOUNT = 11
    SET_CHASE_NUMBERS = 12
    SET_CHASE_AMOUNT = 13
    SET_CHASE_PERIODS = 14
    SET_STOP_BALANCE = 15
    MAX_ACCOUNTS_PER_USER = 5
    PREDICTION_HISTORY_SIZE = 20
    AVAILABLE_CURRENCIES = ["KKCOIN", "USDT", "CNY"]
    DEFAULT_CURRENCY = "CNY"
    CURRENCY_BET_LIMITS = {
        "KKCOIN": {"min": 1, "max": 10000000},
        "USDT": {"min": 0.1, "max": 100},
        "CNY": {"min": 0.1, "max": 10000}
    }
    CURRENCY_SYMBOLS = {
        "KKCOIN": "KK",
        "USDT": "USDT",
        "CNY": "¥"
    }
    MIN_PREDICTION_CONFIDENCE = 0.35
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
    def _mask_phone(self, phone: str) -> str:
        if len(phone) >= 8: return phone[:5] + "****" + phone[-3:]
        return phone

logger = BotLogger()

COMBOS = ["小单", "小双", "大单", "大双"]

# ==================== 预测算法 ====================

def trend_based_prediction(history: List[Dict]) -> List[str]:
    if len(history) < 15:
        return ["小单"]
    combos = [h.get("combo", h.get("combination", "小单")) for h in history[:20]]
    last_3 = combos[:3]
    if len(set(last_3)) == 1:
        opposite = {"大单":"小双", "小双":"大单", "大双":"小单", "小单":"大双"}
        return [opposite.get(last_3[0], "小单")]
    size_counter = Counter([c[0] for c in combos])
    parity_counter = Counter([c[1] for c in combos])
    recent_size = [c[0] for c in combos[:10]]
    recent_parity = [c[1] for c in combos[:10]]
    if recent_size.count(recent_size[0]) >= 7:
        predicted_size = "小" if recent_size[0] == "大" else "大"
    else:
        predicted_size = min(size_counter, key=size_counter.get)
    if recent_parity.count(recent_parity[0]) >= 7:
        predicted_parity = "双" if recent_parity[0] == "单" else "单"
    else:
        predicted_parity = min(parity_counter, key=parity_counter.get)
    return [predicted_size + predicted_parity]

def probability_distribution_prediction(history: List[Dict]) -> List[str]:
    if len(history) < 20:
        return ["小单"]
    combos = [h.get("combo", h.get("combination", "小单")) for h in history[:30]]
    transitions = {}
    for i in range(len(combos) - 1):
        curr, next_c = combos[i], combos[i+1]
        if curr not in transitions:
            transitions[curr] = {}
        transitions[curr][next_c] = transitions[curr].get(next_c, 0) + 1
    for curr in transitions:
        total = sum(transitions[curr].values())
        if total > 0:
            for next_c in transitions[curr]:
                transitions[curr][next_c] /= total
    current = combos[0]
    if current in transitions and transitions[current]:
        probs = transitions[current]
        predicted = min(probs, key=probs.get)
        return [predicted]
    return ["小单"]

def v3_enhanced_prediction(history: List[Dict]) -> List[str]:
    if len(history) < 10:
        return ["小单"]
    forms = ["大单", "小单", "大双", "小双"]
    h = [x.get("combo", x.get("combination", "小单")) for x in history[:30]]
    if not h:
        return ["小单"]
    weighted_counts = {f: 0 for f in forms}
    for i, combo in enumerate(h):
        weight = 1.0 / (i + 1)
        weighted_counts[combo] = weighted_counts.get(combo, 0) + weight
    unique_recent = len(set(h[:5]))
    if unique_recent == 1:
        opposite = {"大单":"小双", "小双":"大单", "大双":"小单", "小单":"大双"}
        return [opposite.get(h[0], "小单")]
    elif unique_recent <= 2:
        return [min(weighted_counts, key=weighted_counts.get)]
    else:
        return [min(weighted_counts, key=weighted_counts.get)]

def original_armor_prediction(history: List[Dict]) -> List[str]:
    if len(history) < 15:
        return ["小单"]
    try:
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
        return [s]
    except: return ["小单"]

# ==================== 集成预测器 ====================

class EnsemblePredictor:
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
        
    def predict(self, history: List[Dict]) -> Tuple[str, float]:
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
        vote_count = {}
        for name, pred in predictions.items():
            weight = self.weights.get(name, 1.0)
            perf = self._get_recent_performance(name)
            adjusted_weight = weight * (1 + perf)
            vote_count[pred] = vote_count.get(pred, 0) + adjusted_weight
        kill_target = min(vote_count, key=vote_count.get)
        total_weight = sum(vote_count.values())
        if total_weight > 0:
            confidence = 1 - (vote_count.get(kill_target, 0) / total_weight)
        else:
            confidence = 0.5
        self.prediction_history.append({
            'time': datetime.now(),
            'kill': kill_target,
            'confidence': confidence,
            'predictions': predictions.copy()
        })
        return kill_target, confidence
    
    def _get_recent_performance(self, model_name: str) -> float:
        history = self.performance_history.get(model_name, [])
        if len(history) < 5:
            return 0
        recent = list(history)[-20:]
        weights = [1.0 / (i + 1) for i in range(len(recent))]
        total_weight = sum(weights)
        if total_weight == 0:
            return 0
        weighted_acc = sum(w * acc for w, acc in zip(weights, recent)) / total_weight
        return (weighted_acc - 0.5) * 1.0
    
    def update_performance(self, actual_combo: str):
        if not self.prediction_history:
            return
        last_pred = self.prediction_history[-1]
        predicted_kill = last_pred['kill']
        predictions = last_pred.get('predictions', {})
        is_win = (actual_combo != predicted_kill)
        for name, pred in predictions.items():
            model_win = (actual_combo != pred)
            self.performance_history[name].append(1.0 if model_win else 0.0)
        for name in self.models:
            perf = self._get_recent_performance(name)
            adjustment = perf * 0.05
            self.weights[name] = max(0.05, min(0.5, self.weights.get(name, 0.1) + adjustment))
        total = sum(self.weights.values())
        if total > 0:
            for name in self.weights:
                self.weights[name] /= total
        logger.log_prediction(0, "预测结果反馈", 
                             f"杀:{predicted_kill} 开:{actual_combo} {'✓赢' if is_win else '✗输'} 置信度:{last_pred['confidence']:.2f}")
    
    def get_stats(self) -> Dict:
        recent_perf = list(self.performance_history.get("trend", []))[-20:]
        win_rate = sum(recent_perf) / len(recent_perf) if recent_perf else 0
        return {
            'weights': self.weights.copy(),
            'recent_win_rate': f"{win_rate:.1%}",
            'prediction_count': len(self.prediction_history)
        }

class ModelManager:
    def __init__(self):
        self.ensemble = EnsemblePredictor()

    def predict_kill(self, history: List[Dict]) -> Tuple[str, float]:
        if len(history) < 10:
            return "小单", 0.5
        return self.ensemble.predict(history)
    
    def update_prediction_result(self, actual_combo: str):
        self.ensemble.update_performance(actual_combo)
    
    def get_ensemble_stats(self) -> Dict:
        return self.ensemble.get_stats()

# ==================== API模块 ====================
class PC28API:
    def __init__(self):
        self.base_url = "https://pc28.help/api"
        self.session = None
        self.call_stats = {'total_calls': 0, 'successful_calls': 0, 'failed_calls': 0}
        self.cache_file = Config.CACHE_DIR / "history_cache.pkl"
        self.history_cache = deque(maxlen=Config.CACHE_SIZE)
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
        except Exception as e: logger.log_error(0, "加载缓存失败", e)

    def save_cache(self):
        try:
            with open(self.cache_file, 'wb') as f: pickle.dump(list(self.history_cache), f)
        except Exception as e: logger.log_error(0, "保存缓存失败", e)

    async def fetch_kj(self, nbr=1):
        await self.ensure_session()
        try:
            url = f"{self.base_url}/kj.json?nbr={nbr}"
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if data.get('message') != 'success':
                    return []
                result = []
                for item in data.get('data', []):
                    try:
                        qihao = str(item.get('nbr', ''))
                        number = item.get('number')
                        if not number:
                            continue
                        if isinstance(number, str) and '+' in number:
                            parts = number.split('+')
                            total = sum(int(p) for p in parts)
                        else:
                            total = int(number)
                        combo = item.get('combination', '')
                        if not combo:
                            size = "大" if total >= 14 else "小"
                            parity = "单" if total % 2 else "双"
                            combo = size + parity
                        result.append({'qihao': qihao, 'combo': combo, 'sum': total})
                    except:
                        continue
                return result
        except Exception as e:
            logger.log_error(0, "获取开奖数据失败", e)
            return []

    async def get_history(self, count=50):
        return list(self.history_cache)[:count]

    async def get_latest_result(self):
        latest_api = await self.fetch_kj(nbr=1)
        if not latest_api:
            return None
        latest = latest_api[0]
        if not any(x.get('qihao') == latest['qihao'] for x in self.history_cache):
            self.history_cache.appendleft(latest)
            self.save_cache()
        return latest

    async def initialize_history(self, count=100):
        kj_data = await self.fetch_kj(nbr=count)
        if kj_data:
            self.history_cache.clear()
            for item in kj_data:
                self.history_cache.append(item)
            self.save_cache()
            return len(self.history_cache) >= 30
        return False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def get_statistics(self):
        return {'缓存数据量': len(self.history_cache), '最新期号': self.history_cache[0].get('qihao') if self.history_cache else '无'}

# ==================== 数据模型 ====================
@dataclass
class BetParams:
    base_amount: float = Config.DEFAULT_BASE_AMOUNT
    stop_balance: float = Config.DEFAULT_STOP_BALANCE

@dataclass
class ChaseConfig:
    enabled: bool = False
    numbers: List[int] = field(default_factory=list)
    amount: float = 0.0
    total_periods: int = 0
    current_period: int = 0
    hit: bool = False

@dataclass
class Account:
    phone: str
    owner_user_id: int
    created_time: str = field(default_factory=lambda: datetime.now().isoformat())
    is_logged_in: bool = False
    auto_betting: bool = False
    display_name: str = ""
    telegram_user_id: int = 0
    game_group_id: int = 0
    game_group_name: str = ""
    bet_params: BetParams = field(default_factory=BetParams)
    balance: float = 0
    initial_balance: float = 0
    net_profit: float = 0
    consecutive_losses: int = 0
    total_bets: int = 0
    total_wins: int = 0
    last_bet_time: Optional[str] = None
    last_bet_period: Optional[str] = None
    last_bet_amount: float = 0
    last_prediction: Dict = field(default_factory=dict)
    chase: ChaseConfig = field(default_factory=ChaseConfig)
    currency: str = Config.DEFAULT_CURRENCY
    last_prediction_confidence: float = 0.0
    stop_reason: Optional[str] = None

    def get_display_name(self) -> str:
        return self.display_name if self.display_name else self.phone

    def get_currency_symbol(self) -> str:
        return Config.CURRENCY_SYMBOLS.get(self.currency, "")

    def get_bet_limits(self) -> Tuple[float, float]:
        limits = Config.CURRENCY_BET_LIMITS.get(self.currency, {"min": 0.1, "max": 10000})
        return limits["min"], limits["max"]

# ==================== 账户管理器 ====================
class AccountManager:
    def __init__(self):
        self.accounts_file = Config.DATA_DIR / "accounts.json"
        self.user_states_file = Config.DATA_DIR / "user_states.json"
        self.accounts: Dict[str, Account] = {}
        self.user_states: Dict[int, Dict] = {}
        self.clients: Dict[str, TelegramClient] = {}
        self.account_locks: Dict[str, asyncio.Lock] = {}
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
                    chase_data = acc_data.pop('chase', {})
                    chase = ChaseConfig(**chase_data) if chase_data else ChaseConfig()
                    acc_data['chase'] = chase
                    if 'currency' not in acc_data:
                        acc_data['currency'] = Config.DEFAULT_CURRENCY
                    if 'net_profit' not in acc_data:
                        acc_data['net_profit'] = 0.0
                    if 'total_wins' not in acc_data:
                        acc_data['total_wins'] = 0
                    if 'stop_reason' not in acc_data:
                        acc_data['stop_reason'] = None
                    self.accounts[phone] = Account(**acc_data)
            if self.user_states_file.exists():
                with open(self.user_states_file, 'r', encoding='utf-8') as f:
                    self.user_states = {int(k): v for k, v in json.load(f).items()}
        except Exception as e:
            logger.log_error(0, "加载账户数据失败", e)

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
        try:
            data = {}
            for phone, acc in self.accounts.items():
                acc_dict = asdict(acc)
                acc_dict['bet_params'] = asdict(acc.bet_params)
                acc_dict['chase'] = asdict(acc.chase)
                data[phone] = acc_dict
            with open(self.accounts_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            with open(self.user_states_file, 'w', encoding='utf-8') as f:
                json.dump(self.user_states, f, ensure_ascii=False, indent=2)
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
                elif key == 'chase' and isinstance(value, dict):
                    for c_key, c_value in value.items():
                        if hasattr(acc.chase, c_key):
                            setattr(acc.chase, c_key, c_value)
                elif hasattr(acc, key):
                    setattr(acc, key, value)
            acc.net_profit = acc.balance - acc.initial_balance
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

    async def start_periodic_save(self):
        self._save_task = asyncio.create_task(self._periodic_save())

    async def stop_periodic_save(self):
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass

# ==================== 游戏调度器 ====================
class GameScheduler:
    def __init__(self, account_manager, model, api_client):
        self.account_manager = account_manager
        self.model = model
        self.api = api_client
        self.game_stats = {'successful_bets': 0, 'failed_bets': 0}

    async def start_auto_betting(self, phone, user_id):
        acc = self.account_manager.get_account(phone)
        if not acc:
            return False, "账户不存在"
        if not acc.is_logged_in:
            return False, "请先登录账户"
        if not acc.game_group_id:
            return False, "请先设置游戏群"
        await self.account_manager.update_account(phone, auto_betting=True, stop_reason=None)
        logger.log_betting(user_id, "自动投注开启", f"账户:{phone}")
        return True, "自动投注已开启"

    async def stop_auto_betting(self, phone, user_id, reason=None):
        await self.account_manager.update_account(phone, auto_betting=False, stop_reason=reason)
        logger.log_betting(user_id, "自动投注关闭", f"账户:{phone} 原因:{reason}")
        return True, "自动投注已关闭"

    async def get_balance(self, phone: str) -> Optional[float]:
        client = self.account_manager.clients.get(phone)
        acc = self.account_manager.get_account(phone)
        if not client or not acc or not await self.account_manager.ensure_client_connected(phone):
            return None
        try:
            await client.send_message(Config.BALANCE_BOT, "/start")
            await asyncio.sleep(2)
            msgs = await client.get_messages(Config.BALANCE_BOT, limit=5)
            balances = {'KKCOIN': 0.0, 'USDT': 0.0, 'CNY': 0.0}
            for msg in msgs:
                if msg.text:
                    cny_match = re.search(r'CNY\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if cny_match:
                        balances['CNY'] = float(cny_match.group(1).replace(',', ''))
                    usdt_match = re.search(r'USDT\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if usdt_match:
                        balances['USDT'] = float(usdt_match.group(1).replace(',', ''))
                    kk_match = re.search(r'KKCOIN\s*[:：]\s*([\d,]+\.?\d*)', msg.text, re.IGNORECASE)
                    if kk_match:
                        balances['KKCOIN'] = float(kk_match.group(1).replace(',', ''))
            selected_balance = balances.get(acc.currency, 0)
            if selected_balance > 0:
                # 如果是第一次查询余额（initial_balance为0），设置初始余额
                if acc.initial_balance == 0:
                    await self.account_manager.update_account(phone, initial_balance=selected_balance, balance=selected_balance)
                else:
                    await self.account_manager.update_account(phone, balance=selected_balance)
                return selected_balance
        except Exception as e:
            logger.log_error(0, f"查询余额失败 {phone}", e)
        return None

    async def check_stop_balance(self, phone: str, current_balance: float) -> bool:
        """检查余额是否达到停止线"""
        acc = self.account_manager.get_account(phone)
        if not acc:
            return False
        stop_balance = acc.bet_params.stop_balance
        if stop_balance > 0 and current_balance >= stop_balance:
            await self.stop_auto_betting(phone, acc.owner_user_id, f"余额达到{format_amount(stop_balance, acc.currency)}")
            logger.log_betting(acc.owner_user_id, "余额达标停止", f"账户:{phone} 余额:{format_amount(current_balance, acc.currency)} >= {format_amount(stop_balance, acc.currency)}")
            return True
        return False

    async def execute_bet(self, phone, kill_target, latest, confidence=0.5):
        acc = self.account_manager.get_account(phone)
        if not acc or not acc.auto_betting:
            return
        if not await self.account_manager.ensure_client_connected(phone):
            return
        current_qihao = latest.get('qihao')
        if acc.last_bet_period == current_qihao:
            return

        current_balance = await self.get_balance(phone)
        if current_balance is None:
            current_balance = acc.balance
        
        # 检查余额是否达到停止线
        if await self.check_stop_balance(phone, current_balance):
            return

        # 检查追号是否启用且未中奖且未达到期数
        chase = acc.chase
        if chase.enabled and not chase.hit and chase.current_period < chase.total_periods:
            await self.execute_chase_bet(phone, latest, current_balance)
            return

        # 正常杀组投注
        await self.execute_kill_bet(phone, kill_target, latest, confidence, current_balance)

    async def execute_chase_bet(self, phone, latest, current_balance):
        acc = self.account_manager.get_account(phone)
        chase = acc.chase
        current_qihao = latest.get('qihao')
        
        if chase.current_period >= chase.total_periods:
            await self.account_manager.update_account(phone, chase={'enabled': False})
            logger.log_betting(0, "追号结束", f"账户:{phone} 已追{chase.total_periods}期")
            return

        min_limit, max_limit = acc.get_bet_limits()
        bet_amount = chase.amount
        bet_amount = min(bet_amount, max_limit)
        bet_amount = max(bet_amount, min_limit)
        
        if acc.currency != "KKCOIN":
            bet_amount = round(bet_amount, 2)
        else:
            bet_amount = int(bet_amount)

        bet_parts = []
        total_bet_amount = 0
        for num in chase.numbers:
            bet_parts.append(f"{num}/{bet_amount}")
            total_bet_amount += bet_amount

        if total_bet_amount > current_balance:
            logger.log_betting(0, "追号失败-余额不足", f"账户:{phone}")
            await self.account_manager.update_account(phone, auto_betting=False, chase={'enabled': False})
            return

        message = " ".join(bet_parts)
        client = self.account_manager.clients.get(phone)
        gid = acc.game_group_id
        
        try:
            await client.send_message(gid, message)
            new_period = chase.current_period + 1
            await self.account_manager.update_account(
                phone,
                chase={'current_period': new_period},
                last_bet_time=datetime.now().isoformat(),
                last_bet_period=current_qihao,
                last_bet_amount=total_bet_amount,
                total_bets=acc.total_bets + 1
            )
            logger.log_betting(0, "追号投注", f"账户:{phone} 追号:{chase.numbers} 金额:{format_amount(bet_amount, acc.currency)}/个 期数:{new_period}/{chase.total_periods}")
            self.game_stats['successful_bets'] += 1
        except Exception as e:
            logger.log_error(0, f"追号投注失败 {phone}", e)
            self.game_stats['failed_bets'] += 1

    async def execute_kill_bet(self, phone, kill_target, latest, confidence, current_balance):
        acc = self.account_manager.get_account(phone)
        current_qihao = latest.get('qihao')
        base_amount = acc.bet_params.base_amount
        
        confidence_multiplier = 0.8 + confidence * 0.4
        confidence_multiplier = max(0.8, min(1.2, confidence_multiplier))
        adjusted_base = base_amount * confidence_multiplier

        current_multiplier = 1.0
        if acc.consecutive_losses > 0:
            current_multiplier = Config.DEFAULT_MULTIPLIER ** acc.consecutive_losses

        max_multiplier = 64
        if current_multiplier > max_multiplier:
            current_multiplier = max_multiplier

        min_limit, max_limit = acc.get_bet_limits()

        bet_types = [c for c in COMBOS if c != kill_target]
        bet_parts = []
        total_bet_amount = 0

        for t in bet_types:
            calculated_amount = adjusted_base * current_multiplier
            calculated_amount = min(calculated_amount, max_limit)
            calculated_amount = max(calculated_amount, min_limit)
            
            if acc.currency != "KKCOIN":
                calculated_amount = round(calculated_amount, 2)
            else:
                calculated_amount = int(calculated_amount)

            bet_parts.append(f"{t}{calculated_amount}")
            total_bet_amount += calculated_amount

        if total_bet_amount > current_balance:
            logger.log_betting(0, "投注失败-余额不足", f"账户:{phone}")
            await self.stop_auto_betting(phone, acc.owner_user_id, "余额不足")
            return

        message = " ".join(bet_parts)
        client = self.account_manager.clients.get(phone)
        gid = acc.game_group_id
        
        try:
            await client.send_message(gid, message)
            await self.account_manager.update_account(
                phone,
                last_bet_time=datetime.now().isoformat(),
                last_bet_period=current_qihao,
                last_bet_amount=total_bet_amount,
                last_prediction={'kill': kill_target, 'confidence': confidence},
                total_bets=acc.total_bets + 1,
                last_prediction_confidence=confidence
            )
            logger.log_betting(0, "杀组投注", f"账户:{phone} 杀:{kill_target} 金额:{format_amount(adjusted_base * current_multiplier, acc.currency)}/个 总:{format_amount(total_bet_amount, acc.currency)} 倍投:{current_multiplier:.0f}倍")
            self.game_stats['successful_bets'] += 1
        except Exception as e:
            logger.log_error(0, f"投注失败 {phone}", e)
            self.game_stats['failed_bets'] += 1

    def get_stats(self):
        auto = sum(1 for a in self.account_manager.accounts.values() if a.auto_betting)
        return {'auto_betting_accounts': auto, 'game_stats': self.game_stats.copy()}

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
        self.tasks = set()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.tasks.add(self.task)
        logger.log_system("全局调度器已启动")

    async def stop(self):
        self.running = False
        self.tasks = {t for t in self.tasks if not t.done()}
        for task in self.tasks:
            task.cancel()
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
                latest = await self.api.get_latest_result()
                if latest:
                    qihao = latest.get('qihao')
                    if qihao != self.last_qihao:
                        logger.log_game(f"检测到新期号: {qihao}")
                        await self._on_new_period(qihao, latest)
                await asyncio.sleep(Config.SCHEDULER_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.log_error(0, "全局调度器异常", e)
                await asyncio.sleep(10)

    async def _on_new_period(self, qihao, latest):
        actual_combo = latest.get('combo')
        actual_sum = latest.get('sum', 0)
        
        # 更新追号结果
        for phone, acc in self.account_manager.accounts.items():
            chase = acc.chase
            if chase.enabled and not chase.hit and chase.current_period > 0:
                if actual_sum in chase.numbers:
                    await self.account_manager.update_account(phone, chase={'hit': True, 'enabled': False})
                    logger.log_game(f"[{phone}] 🎉 追号中奖! 号码:{actual_sum} 已停止追号")
                elif chase.current_period >= chase.total_periods:
                    await self.account_manager.update_account(phone, chase={'enabled': False})
                    logger.log_game(f"[{phone}] 追号结束,未中奖")

        # 更新杀组结果和余额
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.last_prediction:
                last_kill = acc.last_prediction.get('kill')
                last_bet_amount = acc.last_bet_amount
                if last_kill:
                    if actual_combo == last_kill:
                        new_losses = acc.consecutive_losses + 1
                        await self.account_manager.update_account(phone, consecutive_losses=new_losses)
                        logger.log_game(f"[{phone}] ❌ 杀【{last_kill}】失败(开出{actual_combo}),连输:{new_losses}")
                    else:
                        profit = last_bet_amount * 0.5
                        new_total_wins = acc.total_wins + 1
                        # 更新余额
                        new_balance = acc.balance + profit
                        await self.account_manager.update_account(
                            phone,
                            consecutive_losses=0,
                            total_wins=new_total_wins,
                            balance=new_balance
                        )
                        logger.log_game(f"[{phone}] ✅ 杀【{last_kill}】成功(开出{actual_combo}),盈利:+{format_amount(profit, acc.currency)}")

        # 更新预测模型
        if actual_combo:
            self.model.update_prediction_result(actual_combo)

        # 获取预测
        history = await self.api.get_history(50)
        if len(history) < 10:
            return

        kill_target, confidence = self.model.predict_kill(history)
        logger.log_prediction(0, "预测", f"期号:{qihao} 杀:{kill_target} 置信度:{confidence:.2f}")

        await asyncio.sleep(20)
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.is_logged_in and acc.game_group_id:
                self._create_task(self.game_scheduler.execute_bet(phone, kill_target, latest, confidence))
        self.last_qihao = qihao

# ==================== 主Bot类 ====================
class PC28Bot:
    def __init__(self):
        self.api = PC28API()
        self.account_manager = AccountManager()
        self.model = ModelManager()
        self.game_scheduler = GameScheduler(self.account_manager, self.model, self.api)
        self.global_scheduler = GlobalScheduler(self.account_manager, self.model, self.api, self.game_scheduler)
        self.application = Application.builder().token(Config.BOT_TOKEN).build()
        self._register_handlers()
        logger.log_system("PC28 Bot初始化完成")

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
        
        set_base_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_base_start, pattern=r'^set_base:')],
            states={Config.SET_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_base_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(set_base_conv)
        
        set_chase_numbers_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_chase_numbers_start, pattern=r'^set_chase_numbers:')],
            states={Config.SET_CHASE_NUMBERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_chase_numbers_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(set_chase_numbers_conv)
        
        set_chase_amount_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_chase_amount_start, pattern=r'^set_chase_amount:')],
            states={Config.SET_CHASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_chase_amount_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(set_chase_amount_conv)
        
        set_chase_periods_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_chase_periods_start, pattern=r'^set_chase_periods:')],
            states={Config.SET_CHASE_PERIODS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_chase_periods_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(set_chase_periods_conv)
        
        set_stop_balance_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_stop_balance_start, pattern=r'^set_stop_balance:')],
            states={Config.SET_STOP_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_stop_balance_input)]},
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )
        self.application.add_handler(set_stop_balance_conv)
        
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_error_handler(self.error_handler)
        self.application.add_handler(CommandHandler("bet_stats", self.cmd_bet_stats))

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
        await update.message.reply_text("🎰 *PC28 智能投注系统 v3.0*\n\n✨ 欢迎使用!\n💰 默认基础金额: 2元 | 倍投: 固定2倍", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def cmd_bet_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        accounts = self.account_manager.get_user_accounts(user_id)
        if not accounts:
            await update.message.reply_text("📭 您还没有添加账户")
            return
        
        text = f"📊 *投注统计汇总*\n\n"
        for acc in accounts:
            net = acc.balance - acc.initial_balance
            net_str = f"+{format_amount(net, acc.currency)}" if net >= 0 else format_amount(net, acc.currency)
            net_emoji = "📈" if net >= 0 else "📉"
            win_rate = (acc.total_wins / acc.total_bets * 100) if acc.total_bets > 0 else 0
            lose_count = acc.total_bets - acc.total_wins
            
            text += f"*{acc.get_display_name()}*\n"
            text += f"  • 投注期数: {acc.total_bets}期\n"
            text += f"  • ✅ 赢了: {acc.total_wins}期\n"
            text += f"  • ❌ 输了: {lose_count}期\n"
            text += f"  • 📊 胜率: {win_rate:.1f}%\n"
            text += f"  • {net_emoji} 净盈利: {net_str}\n"
            text += f"  • 基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}\n"
            if acc.bet_params.stop_balance > 0:
                text += f"  • 🛑 停止余额: {format_amount(acc.bet_params.stop_balance, acc.currency)}\n"
            if acc.chase.enabled:
                text += f"  • 🎯 追号中: {acc.chase.numbers} | 金额:{acc.chase.amount}元 | 期数:{acc.chase.current_period}/{acc.chase.total_periods}\n"
            text += "\n"
        
        keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def set_base_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['setting_phone'] = phone
        acc = self.account_manager.get_account(phone)
        await query.edit_message_text(f"💰 请输入基础金额 (当前: {format_amount(acc.bet_params.base_amount, acc.currency)}):\n\n点击 /cancel 取消")
        return Config.SET_BASE_AMOUNT

    async def set_base_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = context.user_data.get('setting_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        try:
            amount = float(update.message.text.strip())
            if amount <= 0:
                await update.message.reply_text("❌ 金额必须大于0")
                return Config.SET_BASE_AMOUNT
            acc = self.account_manager.get_account(phone)
            min_limit, max_limit = acc.get_bet_limits()
            if amount < min_limit:
                await update.message.reply_text(f"❌ 金额不能小于 {min_limit}{acc.get_currency_symbol()}")
                return Config.SET_BASE_AMOUNT
            if amount > max_limit:
                await update.message.reply_text(f"❌ 金额不能大于 {max_limit}{acc.get_currency_symbol()}")
                return Config.SET_BASE_AMOUNT
            await self.account_manager.update_account(phone, bet_params={'base_amount': amount})
            await update.message.reply_text(f"✅ 基础金额已设置为 {format_amount(amount, acc.currency)}")
            await self._show_account_detail(update.message, user_id, phone)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
            return Config.SET_BASE_AMOUNT
        return ConversationHandler.END

    async def set_stop_balance_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['setting_phone'] = phone
        acc = self.account_manager.get_account(phone)
        current = format_amount(acc.bet_params.stop_balance, acc.currency) if acc.bet_params.stop_balance > 0 else "未设置"
        await query.edit_message_text(f"🛑 请输入停止投注的余额目标 (当前: {current})\n\n当余额达到或超过此金额时自动停止投注\n输入0表示不限制\n\n点击 /cancel 取消")
        return Config.SET_STOP_BALANCE

    async def set_stop_balance_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = context.user_data.get('setting_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        try:
            amount = float(update.message.text.strip())
            if amount < 0:
                await update.message.reply_text("❌ 金额不能为负数")
                return Config.SET_STOP_BALANCE
            acc = self.account_manager.get_account(phone)
            await self.account_manager.update_account(phone, bet_params={'stop_balance': amount})
            if amount > 0:
                await update.message.reply_text(f"✅ 停止余额已设置为 {format_amount(amount, acc.currency)}\n当余额达到此金额时自动停止投注")
            else:
                await update.message.reply_text(f"✅ 已取消余额限制")
            await self._show_account_detail(update.message, user_id, phone)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
            return Config.SET_STOP_BALANCE
        return ConversationHandler.END

    async def set_chase_numbers_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['chase_phone'] = phone
        await query.edit_message_text("🎯 请输入要追的号码(多个号码用逗号隔开，如: 27,15,8)\n\n号码范围: 0-27\n\n点击 /cancel 取消")
        return Config.SET_CHASE_NUMBERS

    async def set_chase_numbers_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        phone = context.user_data.get('chase_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        try:
            text = update.message.text.strip()
            numbers = []
            for part in text.replace('，', ',').split(','):
                num = int(part.strip())
                if 0 <= num <= 27:
                    numbers.append(num)
                else:
                    await update.message.reply_text(f"❌ 号码 {num} 无效，范围0-27")
                    return Config.SET_CHASE_NUMBERS
            if not numbers:
                await update.message.reply_text("❌ 请至少输入一个有效号码")
                return Config.SET_CHASE_NUMBERS
            context.user_data['chase_numbers'] = numbers
            await update.message.reply_text(f"✅ 已设置追号号码: {numbers}\n\n📝 请输入每个号码的投注金额:")
            return Config.SET_CHASE_AMOUNT
        except ValueError:
            await update.message.reply_text("❌ 请输入正确的数字格式，如: 27,15,8")
            return Config.SET_CHASE_NUMBERS

    async def set_chase_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['chase_phone'] = phone
        await query.edit_message_text("💰 请输入每个号码的追号金额:\n\n点击 /cancel 取消")
        return Config.SET_CHASE_AMOUNT

    async def set_chase_amount_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        phone = context.user_data.get('chase_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        try:
            amount = float(update.message.text.strip())
            if amount <= 0:
                await update.message.reply_text("❌ 金额必须大于0")
                return Config.SET_CHASE_AMOUNT
            acc = self.account_manager.get_account(phone)
            min_limit, max_limit = acc.get_bet_limits()
            if amount < min_limit:
                await update.message.reply_text(f"❌ 金额不能小于 {min_limit}{acc.get_currency_symbol()}")
                return Config.SET_CHASE_AMOUNT
            if amount > max_limit:
                await update.message.reply_text(f"❌ 金额不能大于 {max_limit}{acc.get_currency_symbol()}")
                return Config.SET_CHASE_AMOUNT
            context.user_data['chase_amount'] = amount
            await update.message.reply_text(f"✅ 已设置追号金额: {amount}元\n\n📝 请输入追号期数(1-100期):")
            return Config.SET_CHASE_PERIODS
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
            return Config.SET_CHASE_AMOUNT

    async def set_chase_periods_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        phone = query.data.split(':')[1]
        context.user_data['chase_phone'] = phone
        await query.edit_message_text("📝 请输入追号期数(1-100期):\n\n点击 /cancel 取消")
        return Config.SET_CHASE_PERIODS

    async def set_chase_periods_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = context.user_data.get('chase_phone')
        if not phone:
            await update.message.reply_text("❌ 会话已过期")
            return ConversationHandler.END
        try:
            periods = int(update.message.text.strip())
            if periods < 1 or periods > 100:
                await update.message.reply_text("❌ 期数范围1-100")
                return Config.SET_CHASE_PERIODS
            
            numbers = context.user_data.get('chase_numbers', [])
            amount = context.user_data.get('chase_amount', 0)
            
            chase_config = {
                'enabled': True,
                'numbers': numbers,
                'amount': amount,
                'total_periods': periods,
                'current_period': 0,
                'hit': False
            }
            await self.account_manager.update_account(phone, chase=chase_config)
            
            await update.message.reply_text(
                f"✅ 追号设置完成!\n\n"
                f"🎯 追号号码: {numbers}\n"
                f"💰 每个金额: {amount}元\n"
                f"📝 追号期数: {periods}期\n"
                f"📊 总投注: {len(numbers) * amount}元/期\n\n"
                f"⚠️ 中奖后自动停止追号\n"
                f"⚠️ 达到期数后自动停止追号"
            )
            await self._show_account_detail(update.message, user_id, phone)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
            return Config.SET_CHASE_PERIODS
        return ConversationHandler.END

    async def stop_chase(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        await self.account_manager.update_account(phone, chase={'enabled': False})
        await update.callback_query.answer("已停止追号")
        await self._show_account_detail(update.callback_query, update.effective_user.id, phone)

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
        if not acc:
            await query.edit_message_text("账户不存在")
            return ConversationHandler.END
        if acc.is_logged_in:
            await self._show_account_detail(query, query.from_user.id, phone)
            return ConversationHandler.END
        client = self.account_manager.create_client(phone)
        if not client:
            await query.edit_message_text("创建客户端失败")
            return ConversationHandler.END
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                display = f"{me.first_name or ''} {me.last_name or ''}".strip()
                await self.account_manager.update_account(phone, is_logged_in=True, display_name=display, telegram_user_id=me.id)
                balance = await self.game_scheduler.get_balance(phone)
                if balance:
                    await self.account_manager.update_account(phone, initial_balance=balance, balance=balance)
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
            balance = await self.game_scheduler.get_balance(phone)
            if balance:
                await self.account_manager.update_account(phone, initial_balance=balance, balance=balance)
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
            balance = await self.game_scheduler.get_balance(phone)
            if balance:
                await self.account_manager.update_account(phone, initial_balance=balance, balance=balance)
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
        elif data == "menu:bet_stats":
            await self._show_bet_stats(query, user)
        elif data.startswith("select_account:"):
            phone = data.split(':')[1]
            await self._show_account_detail(query, user, phone)
        elif data.startswith("action:"):
            parts = data.split(':')
            action, phone = parts[1], parts[2]
            await self._process_action(query, user, action, phone)
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
        elif data.startswith("stop_chase:"):
            phone = data.split(':')[1]
            await self.stop_chase(update, context, phone)

    async def _show_bet_stats(self, query, user):
        accounts = self.account_manager.get_user_accounts(user)
        if not accounts:
            await query.edit_message_text("📭 您还没有添加账户")
            return
        
        text = f"📊 *投注统计汇总*\n\n"
        for acc in accounts:
            net = acc.balance - acc.initial_balance
            net_str = f"+{format_amount(net, acc.currency)}" if net >= 0 else format_amount(net, acc.currency)
            net_emoji = "📈" if net >= 0 else "📉"
            win_rate = (acc.total_wins / acc.total_bets * 100) if acc.total_bets > 0 else 0
            lose_count = acc.total_bets - acc.total_wins
            
            text += f"*{acc.get_display_name()}*\n"
            text += f"  • 投注期数: {acc.total_bets}期\n"
            text += f"  • ✅ 赢了: {acc.total_wins}期\n"
            text += f"  • ❌ 输了: {lose_count}期\n"
            text += f"  • 📊 胜率: {win_rate:.1f}%\n"
            text += f"  • {net_emoji} 净盈利: {net_str}\n"
            text += f"  • 基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def _show_main_menu(self, query):
        kb = [
            [InlineKeyboardButton("📱 账户管理", callback_data="menu:accounts")],
            [InlineKeyboardButton("🎯 智能预测", callback_data="menu:prediction")],
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:status")],
            [InlineKeyboardButton("📈 投注统计", callback_data="menu:bet_stats")],
        ]
        await query.edit_message_text("🎰 *PC28 智能投注系统 v3.0*\n\n✨ 请选择操作:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_accounts_menu(self, query, user):
        accounts = self.account_manager.get_user_accounts(user)
        kb = []
        text = "📱 *您的账户列表*\n\n" if accounts else "📭 您还没有添加账户"
        if accounts:
            for acc in accounts:
                status = "✅" if acc.is_logged_in else "❌"
                net = acc.balance - acc.initial_balance
                net_emoji = "📈" if net >= 0 else "📉"
                win_rate = (acc.total_wins / acc.total_bets * 100) if acc.total_bets > 0 else 0
                chase_icon = "🎯 " if acc.chase.enabled else ""
                stop_icon = "🛑 " if acc.bet_params.stop_balance > 0 and acc.auto_betting == False and acc.stop_reason and "余额" in str(acc.stop_reason) else ""
                text += f"{status} {chase_icon}{stop_icon}{acc.get_display_name()} | {net_emoji} {format_amount(net, acc.currency)} | 胜率: {win_rate:.0f}%\n"
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
            try:
                await query_or_message.edit_message_text("❌ 账户不存在")
            except:
                await query_or_message.reply_text("❌ 账户不存在")
            return
        
        display = acc.get_display_name()
        status = "✅ 已登录" if acc.is_logged_in else "❌ 未登录"
        if acc.auto_betting:
            status += " | 🤖 自动投注"
        if acc.stop_reason:
            status += f" | ⚠️ 已停止: {acc.stop_reason}"
        bet_button = "🛑 停止自动投注" if acc.auto_betting else "🤖 开启自动投注"
        
        net = acc.balance - acc.initial_balance
        net_display = f"+{format_amount(net, acc.currency)}" if net >= 0 else format_amount(net, acc.currency)
        net_emoji = "📈" if net >= 0 else "📉"
        
        win_rate = (acc.total_wins / acc.total_bets * 100) if acc.total_bets > 0 else 0
        lose_count = acc.total_bets - acc.total_wins

        stop_balance_text = f"🛑 停止余额: {format_amount(acc.bet_params.stop_balance, acc.currency)}" if acc.bet_params.stop_balance > 0 else "🛑 停止余额: 未设置"

        kb = [
            [InlineKeyboardButton("🔐 登录", callback_data=f"login_select:{phone}"),
             InlineKeyboardButton("🚪 登出", callback_data=f"action:logout:{phone}")],
            [InlineKeyboardButton("💬 游戏群", callback_data=f"action:listgroups:{phone}")],
            [InlineKeyboardButton("💰 设置基础金额", callback_data=f"set_base:{phone}")],
            [InlineKeyboardButton("🛑 设置停止余额", callback_data=f"set_stop_balance:{phone}")],
            [InlineKeyboardButton("🎯 设置追号", callback_data=f"set_chase_numbers:{phone}")],
            [InlineKeyboardButton("💱 切换币种", callback_data=f"action:setcurrency:{phone}")],
            [InlineKeyboardButton(bet_button, callback_data=f"action:toggle_bet:{phone}")],
            [InlineKeyboardButton("💰 查询余额", callback_data=f"action:balance:{phone}"),
             InlineKeyboardButton("📊 账户状态", callback_data=f"action:status:{phone}")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu:accounts")]
        ]
        
        text = f"📱 *账户: {display}*\n\n"
        text += f"状态: {status}\n"
        text += f"币种: {acc.currency}\n"
        text += f"余额: {format_amount(acc.balance, acc.currency)}\n"
        text += f"{net_emoji} 净盈利: {net_display}\n"
        text += f"基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}\n"
        text += f"倍投倍数: 固定2倍\n"
        text += f"{stop_balance_text}\n"
        text += f"投注统计: {acc.total_bets}期 | ✅ 赢了: {acc.total_wins} | ❌ 输了: {lose_count} | 胜率: {win_rate:.1f}%\n"
        
        if acc.chase.enabled:
            text += f"\n🎯 *追号中*\n"
            text += f"  号码: {acc.chase.numbers}\n"
            text += f"  金额: {acc.chase.amount}元/个\n"
            text += f"  期数: {acc.chase.current_period}/{acc.chase.total_periods}\n"
            if not acc.chase.hit:
                kb.insert(5, [InlineKeyboardButton("🛑 停止追号", callback_data=f"stop_chase:{phone}")])
        
        text += f"\n选择操作:"
        try:
            await query_or_message.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except:
            await query_or_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_prediction(self, query):
        history = await self.api.get_history(50)
        if len(history) < 10:
            await query.edit_message_text("❌ 历史数据不足")
            return
        kill_target, confidence = self.model.predict_kill(history)
        latest = history[0] if history else {'qihao': 'N/A', 'combo': 'N/A'}
        
        confidence_bar = "█" * int(confidence * 10) + "░" * (10 - int(confidence * 10))
        
        text = f"🎯 *当前预测（集成模型）*\n\n"
        text += f"📊 最新期号: {latest.get('qihao')}\n"
        text += f"📌 最新结果: {latest.get('combo')}\n\n"
        text += f"🚫 *杀组推荐:* {kill_target}\n"
        text += f"📈 *置信度:* {confidence_bar} {confidence:.1%}\n\n"
        text += f"💡 投注建议: {' '.join([c for c in COMBOS if c != kill_target])}\n\n"
        text += f"⚙️ 集成模型会随实际结果自动优化"
        
        kb = [[InlineKeyboardButton("🔄 刷新预测", callback_data="menu:prediction")],
              [InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_status(self, query):
        api_stats = self.api.get_statistics()
        sched_stats = self.game_scheduler.get_stats()
        ensemble_stats = self.model.get_ensemble_stats()
        total_accounts = len(self.account_manager.accounts)
        logged = sum(1 for a in self.account_manager.accounts.values() if a.is_logged_in)
        auto = sched_stats['auto_betting_accounts']
        chase_count = sum(1 for a in self.account_manager.accounts.values() if a.chase.enabled)
        total_bets = sum(a.total_bets for a in self.account_manager.accounts.values())
        total_wins = sum(a.total_wins for a in self.account_manager.accounts.values())
        overall_win_rate = (total_wins / total_bets * 100) if total_bets > 0 else 0
        total_net = sum(a.balance - a.initial_balance for a in self.account_manager.accounts.values())
        net_display = f"+{format_amount(total_net, 'CNY')}" if total_net >= 0 else format_amount(total_net, 'CNY')
        
        text = f"📊 *系统状态 v3.0*\n\n"
        text += f"📈 *API状态*\n• 缓存数据: {api_stats['缓存数据量']}期\n• 最新期号: {api_stats['最新期号']}\n\n"
        text += f"👥 *账户状态*\n• 总账户: {total_accounts}\n• 已登录: {logged}\n• 自动投注: {auto}\n• 追号中: {chase_count}\n"
        text += f"• 总投注: {total_bets}期 | 总胜场: {total_wins} | 胜率: {overall_win_rate:.1f}%\n"
        text += f"• 总净盈利: {net_display}\n\n"
        text += f"🤖 *集成预测器*\n• 近期胜率: {ensemble_stats.get('recent_win_rate', 'N/A')}"
        
        kb = [[InlineKeyboardButton("🔄 刷新", callback_data="refresh_status")],
              [InlineKeyboardButton("📈 投注统计", callback_data="menu:bet_stats")],
              [InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _process_action(self, query, user, action, phone):
        if action == "logout":
            await self.game_scheduler.stop_auto_betting(phone, user, "用户登出")
            client = self.account_manager.clients.get(phone)
            if client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except:
                    pass
                self.account_manager.clients.pop(phone, None)
            session_name = phone.replace('+', '')
            for ext in ['.session', '.session-journal']:
                file_path = Config.SESSIONS_DIR / (session_name + ext)
                if file_path.exists():
                    file_path.unlink()
            await self.account_manager.update_account(phone, is_logged_in=False, auto_betting=False, display_name='', stop_reason=None)
            await self._show_account_detail(query, user, phone)
        elif action == "toggle_bet":
            acc = self.account_manager.get_account(phone)
            if acc.auto_betting:
                await self.game_scheduler.stop_auto_betting(phone, user, "用户手动停止")
            else:
                # 如果之前是因为余额停止的，清除停止原因
                if acc.stop_reason and "余额" in str(acc.stop_reason):
                    await self.account_manager.update_account(phone, stop_reason=None)
                await self.game_scheduler.start_auto_betting(phone, user)
            await self._show_account_detail(query, user, phone)
        elif action == "balance":
            acc = self.account_manager.get_account(phone)
            bal = await self.game_scheduler.get_balance(phone)
            if bal is not None:
                text = f"💰 余额: {format_amount(bal, acc.currency if acc else 'CNY')}\n"
                text += f"📈 初始余额: {format_amount(acc.initial_balance, acc.currency)}\n"
                text += f"{'📈' if bal - acc.initial_balance >= 0 else '📉'} 净盈利: {format_amount(bal - acc.initial_balance, acc.currency)}"
            else:
                text = "❌ 查询失败"
            kb = [[InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "status":
            acc = self.account_manager.get_account(phone)
            net = acc.balance - acc.initial_balance
            net_display = f"+{format_amount(net, acc.currency)}" if net >= 0 else format_amount(net, acc.currency)
            net_emoji = "📈" if net >= 0 else "📉"
            win_rate = (acc.total_wins / acc.total_bets * 100) if acc.total_bets > 0 else 0
            lose_count = acc.total_bets - acc.total_wins
            text = f"📱 账户状态\n\n"
            text += f"• 手机号: {acc.phone}\n"
            text += f"• 登录: {'✅' if acc.is_logged_in else '❌'}\n"
            text += f"• 自动投注: {'✅' if acc.auto_betting else '❌'}\n"
            if acc.stop_reason:
                text += f"• ⚠️ 停止原因: {acc.stop_reason}\n"
            text += f"• 投注币种: {acc.currency}\n"
            text += f"• 游戏群: {acc.game_group_name or '未设置'}\n"
            text += f"• 余额: {format_amount(acc.balance, acc.currency)}\n"
            text += f"• 初始余额: {format_amount(acc.initial_balance, acc.currency)}\n"
            text += f"• 基础金额: {format_amount(acc.bet_params.base_amount, acc.currency)}\n"
            text += f"• 倍投倍数: 固定2倍\n"
            text += f"• 停止余额: {format_amount(acc.bet_params.stop_balance, acc.currency) if acc.bet_params.stop_balance > 0 else '未设置'}\n"
            text += f"• 总投注: {acc.total_bets}次\n"
            text += f"• ✅ 赢了: {acc.total_wins}次\n"
            text += f"• ❌ 输了: {lose_count}次\n"
            text += f"• 📊 胜率: {win_rate:.1f}%\n"
            text += f"• {net_emoji} 净盈利: {net_display}\n"
            if acc.chase.enabled:
                text += f"\n🎯 追号中: {acc.chase.numbers} | {acc.chase.amount}元/个 | {acc.chase.current_period}/{acc.chase.total_periods}期"
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
                except:
                    await query.edit_message_text("❌ 获取群组列表失败")
            else:
                await query.edit_message_text("❌ 客户端未连接")
        elif action == "setcurrency":
            await self._show_currency_menu(query, user, phone)

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
        text = f"💱 *投注币种设置*\n\n当前币种: {current}\n\n选择投注时使用的币种：\n• KKCOIN - 平台积分\n• USDT - 稳定币\n• CNY - 人民币（默认）"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _set_currency(self, query, user, phone, currency):
        if currency not in Config.AVAILABLE_CURRENCIES:
            await query.edit_message_text("❌ 无效币种")
            return
        await self.account_manager.update_account(phone, currency=currency)
        await query.edit_message_text(f"✅ 投注币种已切换为 {currency}")
        await self._show_account_detail(query, user, phone)

# ==================== 启动 ====================
async def post_init(application):
    bot = application.bot_data.get('bot')
    if bot:
        await bot.account_manager.start_periodic_save()
        if hasattr(bot, 'global_scheduler'):
            await bot.global_scheduler.start()

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
                    if client.is_connected():
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(client.disconnect()))
            except RuntimeError:
                asyncio.run(bot.global_scheduler.stop())
                asyncio.run(bot.account_manager.stop_periodic_save())
                asyncio.run(bot.api.close())
                for phone, client in bot.account_manager.clients.items():
                    if client.is_connected():
                        asyncio.run(client.disconnect())
        print("✅ 已安全关闭")
        exit(0)
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    print("=" * 40)
    print("PC28 智能预测投注系统 v3.0")
    print("多币种支持: KKCOIN / USDT / CNY")
    print("默认基础金额: 2元 | 倍投: 固定2倍")
    print("新增功能: 追号系统 | 余额停止线")
    print("=" * 40)
    
    try:
        Config.validate()
    except ValueError as e:
        print(f"❌ 配置错误: {e}")
        return
    
    bot = PC28Bot()
    bot.application.bot_data['bot'] = bot
    bot.application.post_init = post_init
    print("✅ Bot已启动")
    bot.application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    random.seed(time.time())
    np.random.seed(int(time.time()))
    main()