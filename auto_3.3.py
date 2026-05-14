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
    API_HASH = os.getenv("API_HASH")"
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
    DEFAULT_MULTIPLIER = 2.0
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
    RECOMMEND_BASE_RISK = 0.01
    RECOMMEND_RISK_RANGE = 0.02

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
    
    def predict_kill(self, history):
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
    base_amount: int = Config.DEFAULT_BASE_AMOUNT
    max_amount: int = Config.DEFAULT_MAX_AMOUNT
    multiplier: float = Config.DEFAULT_MULTIPLIER
    stop_loss: int = Config.DEFAULT_STOP_LOSS
    stop_win: int = Config.DEFAULT_STOP_WIN
    stop_balance: int = Config.DEFAULT_STOP_BALANCE
    resume_balance: int = Config.DEFAULT_RESUME_BALANCE
    dynamic_base_ratio: float = 0.0  # 新增：动态投注比例

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
    betting_scheme: str = "杀主"
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
    last_bet_amount: int = 0
    last_bet_total: int = 0
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
    recommend_mode: bool = False
    risk_profile: str = "稳定"
    last_message_id: Optional[int] = None
    prediction_content: str = "kill"
    broadcast_stop_requested: bool = False

    def get_display_name(self) -> str:
        return self.display_name if self.display_name else self.phone

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
        self.load_accounts()
        self.load_user_states()
        logger.log_system(f"账户管理器初始化完成,已加载 {len(self.accounts)} 个账户")

    def load_accounts(self):
        if self.accounts_file.exists():
            try:
                with open(self.accounts_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for phone, acc_dict in data.items():
                    bet_params_dict = acc_dict.get('bet_params', {})
                    bet_params_filtered = {k: v for k, v in bet_params_dict.items() if k in BetParams.__dataclass_fields__}
                    # 确保 dynamic_base_ratio 字段存在
                    if 'dynamic_base_ratio' not in bet_params_filtered:
                        bet_params_filtered['dynamic_base_ratio'] = 0.0
                    bet_params = BetParams(**bet_params_filtered)
                    acc_dict['bet_params'] = bet_params
                    for key in ['is_listening', 'needs_2fa', 'login_temp_data', 'chase_enabled', 'chase_numbers', 'chase_periods', 'chase_current', 'chase_amount', 'chase_stop_reason', 'recommend_mode', 'risk_profile', 'streak_records_double', 'streak_records_kill', 'current_streak_type_double', 'current_streak_count_double', 'current_streak_type_kill', 'current_streak_count_kill', 'last_message_id', 'prediction_content', 'broadcast_stop_requested']:
                        if key not in acc_dict: acc_dict[key] = False if 'enabled' in key or 'mode' in key else ([] if 'records' in key or 'numbers' in key else (0 if 'count' in key or 'current' in key else ("kill" if key == 'prediction_content' else None)))
                    self.accounts[phone] = Account(**{k: v for k, v in acc_dict.items() if k in Account.__dataclass_fields__})
            except Exception as e: logger.log_error(0, "加载账户数据失败", e)

    async def save_accounts(self):
        data = {}
        for phone, acc in self.accounts.items():
            acc_dict = asdict(acc)
            data[phone] = acc_dict
        try:
            async with aiofiles.open(self.accounts_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e: logger.log_error(0, "保存账户数据失败", e)

    async def _periodic_save(self):
        while True:
            await asyncio.sleep(Config.ACCOUNT_SAVE_INTERVAL)
            dirty = None
            async with self.update_lock:
                if self._dirty: dirty = self._dirty.copy(); self._dirty.clear()
            if dirty: await self.save_accounts()

    def load_user_states(self):
        if self.user_states_file.exists():
            try:
                with open(self.user_states_file, 'r', encoding='utf-8') as f: self.user_states = json.load(f)
            except: pass

    def save_user_states(self):
        try:
            with open(self.user_states_file, 'w', encoding='utf-8') as f: json.dump(self.user_states, f, ensure_ascii=False, indent=2)
        except: pass

    async def add_account(self, user_id, phone) -> Tuple[bool, str]:
        async with self.update_lock:
            if user_id not in Config.ADMIN_USER_IDS:
                user_accounts = [acc for acc in self.accounts.values() if acc.owner_user_id == user_id]
                if len(user_accounts) >= Config.MAX_ACCOUNTS_PER_USER: return False, f"最多只能添加 {Config.MAX_ACCOUNTS_PER_USER} 个账户"
            if phone in self.accounts: return False, "账户已存在"
            if not re.match(r'^\+\d{10,15}$', phone): return False, "手机号格式不正确,需包含国际区号"
            self.accounts[phone] = Account(phone=phone, owner_user_id=user_id)
            self._dirty.add(phone)
            return True, f"账户 {phone} 添加成功"

    def get_account(self, phone) -> Optional[Account]: return self.accounts.get(phone)

    async def update_account(self, phone, **kwargs):
        async with self.update_lock:
            if phone not in self.account_locks: self.account_locks[phone] = asyncio.Lock()
        async with self.account_locks[phone]:
            if phone in self.accounts:
                acc = self.accounts[phone]
                for k, v in kwargs.items():
                    if k == 'bet_params' and isinstance(v, dict):
                        for pk, pv in v.items(): setattr(acc.bet_params, pk, pv)
                    else: setattr(acc, k, v)
                async with self.update_lock: self._dirty.add(phone)
                return True
            return False

    def get_user_accounts(self, user_id): return [acc for acc in self.accounts.values() if acc.owner_user_id == user_id]

    def set_user_state(self, user_id, state, data=None):
        self.user_states.setdefault(user_id, {})
        self.user_states[user_id]['state'] = state
        if data: self.user_states[user_id].update(data)
        self.user_states[user_id]['last_update'] = datetime.now().isoformat()
        self.save_user_states()

    def get_user_state(self, user_id): return self.user_states.get(user_id, {})

    def set_login_session(self, phone, session_data): self.login_sessions[phone] = session_data
    def get_login_session(self, phone): return self.login_sessions.get(phone)

    def create_client(self, phone):
        try:
            session_name = phone.replace('+', '')
            session_path = Config.SESSIONS_DIR / session_name
            client = TelegramClient(str(session_path), Config.API_ID, Config.API_HASH)
            self.clients[phone] = client
            return client
        except Exception as e: logger.log_error(0, f"创建客户端失败 {phone}", e); return None

    async def ensure_client_connected(self, phone):
        client = self.clients.get(phone)
        if not client:
            await self.update_account(phone, is_logged_in=False, auto_betting=False, prediction_broadcast=False)
            return False
        if not client.is_connected():
            try: await client.connect()
            except: await self.update_account(phone, is_logged_in=False, auto_betting=False, prediction_broadcast=False); return False
        try:
            if not await client.is_user_authorized():
                await self.update_account(phone, is_logged_in=False, auto_betting=False, prediction_broadcast=False)
                return False
        except: await self.update_account(phone, is_logged_in=False, auto_betting=False, prediction_broadcast=False); return False
        return True

    def get_cached_balance(self, phone):
        cache = self.balance_cache.get(phone)
        if cache and (datetime.now() - cache['time']).seconds < Config.BALANCE_CACHE_SECONDS: return cache['balance']
        return None

    def update_balance_cache(self, phone, balance): self.balance_cache[phone] = {'balance': balance, 'time': datetime.now()}

    async def verify_login_status(self):
        for phone, acc in self.accounts.items():
            if acc.is_logged_in:
                if not await self.ensure_client_connected(phone):
                    logger.log_system(f"账户 {phone} 连接失效")

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

# ==================== 金额管理器（新增） ====================
class AmountManager:
    def __init__(self, account_manager):
        self.account_manager = account_manager

    async def set_param(self, phone, param_name, amount, user_id):
        if amount < 0: 
            return False, "金额不能为负数"
        acc = self.account_manager.get_account(phone)
        if not acc: 
            return False, "账户不存在"
        
        if param_name in ['base_amount', 'max_amount']:
            if amount < Config.MIN_BET_AMOUNT:
                return False, f"金额不能小于 {Config.MIN_BET_AMOUNT} KK"
            if amount > Config.MAX_BET_AMOUNT:
                return False, f"金额不能大于 {Config.MAX_BET_AMOUNT} KK"
        
        valid_params = ['base_amount', 'max_amount', 'stop_loss', 'stop_win', 'stop_balance', 'resume_balance', 'dynamic_base_ratio']
        if param_name not in valid_params: 
            return False, f"无效参数，可选: {', '.join(valid_params)}"
        if param_name == 'base_amount' and amount > acc.balance:
            return False, f"基础金额不能超过当前余额 {acc.balance:.0f} KK"
        await self.account_manager.update_account(phone, bet_params={param_name: amount})
        logger.log_betting(user_id, "设置金额参数", f"账户:{phone} {param_name}={amount} KK")
        return True, f"{param_name} 已设置为 {amount} KK"

# ==================== 策略管理器 ====================
class BettingStrategyManager:
    def __init__(self, account_manager):
        self.account_manager = account_manager
        self.strategies = {
            '保守': {'description': '保守策略', 'base_amount': 10000, 'max_amount': 100000, 'multiplier': 1.5, 'stop_loss': 100000, 'stop_win': 50000, 'stop_balance': 50000, 'resume_balance': 200000},
            '平衡': {'description': '平衡策略', 'base_amount': 50000, 'max_amount': 500000, 'multiplier': 2.0, 'stop_loss': 500000, 'stop_win': 250000, 'stop_balance': 100000, 'resume_balance': 500000},
            '激进': {'description': '激进策略', 'base_amount': 100000, 'max_amount': 1000000, 'multiplier': 2.5, 'stop_loss': 1000000, 'stop_win': 500000, 'stop_balance': 200000, 'resume_balance': 1000000},
        }
        self.schemes = {'组合1': '投注第1推荐', '组合2': '投注第2推荐', '组合1+2': '同时投注1、2', '杀主': '投注除最不可能外的所有组合'}

    async def set_strategy(self, phone, strategy_name, user_id):
        if strategy_name not in self.strategies: return False, "无效策略"
        cfg = self.strategies[strategy_name]
        await self.account_manager.update_account(phone, betting_strategy=strategy_name, risk_profile=strategy_name, bet_params={'base_amount': cfg['base_amount'], 'max_amount': cfg['max_amount'], 'multiplier': cfg['multiplier'], 'stop_loss': cfg['stop_loss'], 'stop_win': cfg['stop_win'], 'stop_balance': cfg.get('stop_balance', 0), 'resume_balance': cfg.get('resume_balance', 100000)})
        return True, f"已设置为: {strategy_name}"

    async def set_scheme(self, phone, scheme_name, user_id):
        if scheme_name not in self.schemes: return False, "无效方案"
        await self.account_manager.update_account(phone, betting_scheme=scheme_name)
        return True, f"投注方案已设置为: {scheme_name}"

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

    async def execute_bet(self, phone, kill_target, latest):
        acc = self.account_manager.get_account(phone)
        if not acc or not acc.auto_betting: return
        if not await self.account_manager.ensure_client_connected(phone): return
        current_qihao = latest.get('qihao')
        if acc.last_bet_period == current_qihao: return

        # 计算基础金额（支持动态比例）
        current_balance = await self.get_balance(phone)
        if current_balance is None:
            current_balance = acc.balance
        
        base_amount = acc.bet_params.base_amount
        if acc.bet_params.dynamic_base_ratio > 0 and current_balance > 0:
            base_amount = int(current_balance * acc.bet_params.dynamic_base_ratio)
            base_amount = max(base_amount, Config.MIN_BET_AMOUNT)
            base_amount = min(base_amount, acc.bet_params.max_amount)
            logger.log_betting(0, "动态基础金额", f"账户:{phone} 余额:{current_balance:.0f} 比例:{acc.bet_params.dynamic_base_ratio*100:.0f}% 基础金额:{base_amount}")

        # 计算倍投乘数:根据连输次数进行乘机
        current_multiplier = 1.0
        if acc.consecutive_losses > 0:
            current_multiplier = acc.bet_params.multiplier ** acc.consecutive_losses

        # 获取除杀组外的所有组合
        bet_types = [c for c in COMBOS if c != kill_target]
        bet_parts = []
        total_bet_amount = 0
        
        for t in bet_types:
            # 使用统一的基础金额
            calculated_amount = int(base_amount * current_multiplier)
            calculated_amount = min(calculated_amount, acc.bet_params.max_amount)
            calculated_amount = max(calculated_amount, Config.MIN_BET_AMOUNT)
            
            # 格式: 大双63000
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
            
            # 记录数据供下期结算使用
            await self.account_manager.update_account(
                phone, 
                last_bet_time=datetime.now().isoformat(), 
                last_bet_amount=total_bet_amount, 
                last_bet_types=bet_types, 
                total_bets=acc.total_bets + 1, 
                last_bet_total=total_bet_amount, 
                last_prediction={'kill': kill_target}, 
                last_bet_period=current_qihao,
                balance=current_balance
            )
            logger.log_betting(0, "投注成功", f"账户:{phone} 连输:{acc.consecutive_losses} 倍率:{current_multiplier:.1f}\n{message}")
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.log_error(0, f"投注失败 {phone}", e)
            self.game_stats['failed_bets'] += 1

    async def get_balance(self, phone: str) -> Optional[float]:
        cached = self.account_manager.get_cached_balance(phone)
        if cached is not None: return cached
        client = self.account_manager.clients.get(phone)
        if not client or not await self.account_manager.ensure_client_connected(phone): return None
        try:
            await client.send_message(Config.BALANCE_BOT, "/start")
            await asyncio.sleep(2)
            msgs = await client.get_messages(Config.BALANCE_BOT, limit=5)
            for msg in msgs:
                if msg.text and 'KKCOIN' in msg.text.upper():
                    for pat in [r'💰\s*KKCOIN\s*[::]\s*([\d,]+\.?\d*)', r'([\d,]+\.?\d*)\s*KKCOIN']:
                        m = re.search(pat, msg.text, re.IGNORECASE)
                        if m:
                            try:
                                balance = float(m.group(1).replace(',', ''))
                                self.account_manager.update_balance_cache(phone, balance)
                                await self.account_manager.update_account(phone, balance=balance)
                                return balance
                            except: continue
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
        # 在新期号出现时,优先根据上一期的投注和真实开奖结果,更新连输/回归状态机
        actual_combo = latest.get('combo')
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.last_prediction:
                last_kill = acc.last_prediction.get('kill')
                if last_kill:
                    # 如果这期开奖的结果恰好是你上一期杀掉的组合(即下注的组合里没有它),说明杀组失败(输了)
                    if actual_combo == last_kill:
                        new_losses = acc.consecutive_losses + 1
                        await self.account_manager.update_account(phone, consecutive_losses=new_losses)
                        logger.log_game(f"[{phone}] 上期杀【{last_kill}】失败(开出{actual_combo}),连输: {new_losses},下期倍投触发")
                    else:
                        # 否则杀组成功(赢了),连输清零,下期金额回归正常
                        if acc.consecutive_losses > 0:
                            await self.account_manager.update_account(phone, consecutive_losses=0)
                            logger.log_game(f"[{phone}] 上期杀【{last_kill}】成功(开出{actual_combo}),连输清零回归正常")

        # 原有逻辑:获取最新历史数据进行新一轮杀组预测
        history = await self.api.get_history(50)
        if len(history) < 10:
            logger.log_game("历史数据不足,跳过预测")
            return
        
        kill_target = self.model.predict_kill(history)
        logger.log_prediction(0, "预测杀组", f"期号:{qihao} 杀:{kill_target}")
        
        # 投注延迟
        await asyncio.sleep(20)
        for phone, acc in self.account_manager.accounts.items():
            if acc.auto_betting and acc.is_logged_in and acc.game_group_id:
                self._create_task(self._execute_bet_with_semaphore(phone, kill_target, latest))
        self.last_qihao = qihao

    async def _execute_bet_with_semaphore(self, phone, kill_target, latest):
        async with self.bet_semaphore:
            await self.game_scheduler.execute_bet(phone, kill_target, latest)

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
        logger.log_system("PC28 Bot（含推荐金额功能）初始化完成")

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
            [InlineKeyboardButton("💡 推荐金额", callback_data="menu:recommend")],
        ]
        await update.message.reply_text("🎰 *PC28 智能投注系统*\n\n✨ 欢迎使用!\n💡 新增功能：智能推荐金额", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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
                res = await client.send_code_request(phone)
                self.account_manager.set_login_session(phone, {'phone_code_hash': res.phone_code_hash})
                await query.edit_message_text(f"📨 验证码已发送到 `{phone}`\n\n请输入验证码:", parse_mode='Markdown')
                return Config.LOGIN_CODE
        except Exception as e:
            await query.edit_message_text(f"❌ 登录失败:{str(e)[:200]}")
            return ConversationHandler.END

    async def login_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user.id
        phone = context.user_data.get('login_phone')
        code = update.message.text.strip()
        client = self.account_manager.clients.get(phone)
        sess = self.account_manager.get_login_session(phone)
        try:
            await client.sign_in(phone, code, phone_code_hash=sess['phone_code_hash'])
            me = await client.get_me()
            display = f"{me.first_name or ''} {me.last_name or ''}".strip()
            await self.account_manager.update_account(phone, is_logged_in=True, needs_2fa=False, display_name=display, telegram_user_id=me.id)
            self.account_manager.login_sessions.pop(phone, None)
            await self._show_account_detail(update.message, user, phone)
            return ConversationHandler.END
        except SessionPasswordNeededError:
            await update.message.reply_text("🔒 此账户启用了两步验证,请输入密码:")
            return Config.LOGIN_PASSWORD
        except Exception as e:
            await update.message.reply_text(f"❌ 验证失败:{str(e)}")
            return Config.LOGIN_CODE

    async def login_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user.id
        phone = context.user_data.get('login_phone')
        pwd = update.message.text.strip()
        client = self.account_manager.clients.get(phone)
        try:
            await client.sign_in(password=pwd)
            me = await client.get_me()
            display = f"{me.first_name or ''} {me.last_name or ''}".strip()
            await self.account_manager.update_account(phone, is_logged_in=True, needs_2fa=False, display_name=display, telegram_user_id=me.id)
            await self._show_account_detail(update.message, user, phone)
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ 密码验证失败:{str(e)}")
            return Config.LOGIN_PASSWORD

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        user = query.from_user.id

        if data == "menu:main": await self._show_main_menu(query)
        elif data == "menu:prediction": await self._show_prediction(query)
        elif data == "menu:status": await self._show_status(query)
        elif data == "menu:accounts": await self._show_accounts_menu(query, user)
        elif data == "menu:recommend": await self._show_recommend_accounts(query, user)
        elif data == "add_account": await self.add_account_start(update, context)
        elif data == "refresh_status": await self._show_status(query)
        elif data.startswith("select_account:"):
            phone = data.split(":")[1]
            await self._show_account_detail(query, user, phone)
        elif data.startswith("recommend_account:"):
            phone = data.split(":")[1]
            await self._show_recommend_menu(query, user, phone)
        elif data.startswith("recommend_mode:"):
            parts = data.split(":")
            mode = parts[1]
            phone = parts[2]
            await self._calculate_and_show_recommendation(query, user, phone, mode)
        elif data.startswith("set_recommend:"):
            parts = data.split(":")
            answer = parts[1]
            phone = parts[2]
            amount = float(parts[3])
            if answer == 'yes':
                ok, msg = await self.amount_manager.set_param(phone, 'base_amount', amount, user)
                if ok:
                    await query.edit_message_text(f"✅ 基础金额已设置为 {amount:.0f} KK")
                else:
                    await query.edit_message_text(f"❌ 设置失败: {msg}")
            await self._show_account_detail(query, user, phone)
        elif data.startswith("action:"):
            parts = data.split(":")
            action = parts[1]
            phone = parts[2] if len(parts) > 2 else None
            await self._process_action(query, user, action, phone)
        elif data.startswith("set_strategy:"):
            parts = data.split(":")
            if len(parts) == 3: await self._process_set_strategy(query, user, parts[1], parts[2])
        elif data.startswith("set_scheme:"):
            parts = data.split(":")
            if len(parts) == 3: await self._process_set_scheme(query, user, parts[1], parts[2])
        elif data.startswith("set_group:"):
            group_id = int(data.split(":")[1])
            phone = self.account_manager.get_user_state(user).get('current_account')
            if phone:
                client = self.account_manager.clients.get(phone)
                group_name = str(group_id)
                if client:
                    try:
                        entity = await client.get_entity(group_id)
                        group_name = getattr(entity, 'title', str(group_id))
                    except: pass
                await self.account_manager.update_account(phone, game_group_id=group_id, game_group_name=group_name)
                await self._show_account_detail(query, user, phone)
        elif data.startswith("amount_menu:"):
            phone = data.split(":")[1]
            await self._show_amount_menu(query, user, phone, context)
        elif data.startswith("amount_set:"):
            parts = data.split(":")
            param_name = parts[1]
            phone = parts[2]
            await self._amount_set_callback(query, user, phone, param_name, context)

    async def _show_main_menu(self, query):
        kb = [
            [InlineKeyboardButton("📱 账户管理", callback_data="menu:accounts")],
            [InlineKeyboardButton("🎯 智能预测", callback_data="menu:prediction")],
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:status")],
            [InlineKeyboardButton("💡 推荐金额", callback_data="menu:recommend")],
        ]
        await query.edit_message_text("🎮 *PC28 智能投注系统*\n\n✨ 请选择操作:\n💡 推荐金额：根据余额智能计算建议每注金额", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_accounts_menu(self, query, user):
        accounts = self.account_manager.get_user_accounts(user)
        kb = []
        text = "📱 *您的账户列表*\n\n" if accounts else "📭 您还没有添加账户"
        if accounts:
            for acc in accounts:
                status = "✅" if acc.is_logged_in else "❌"
                text += f"{status} {acc.get_display_name()}\n"
        kb.append([InlineKeyboardButton("➕ 添加账户", callback_data="add_account")])
        if accounts:
            for acc in accounts:
                kb.append([InlineKeyboardButton(f"{acc.get_display_name()}", callback_data=f"select_account:{acc.phone}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="menu:main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_recommend_accounts(self, query, user):
        accounts = self.account_manager.get_user_accounts(user)
        if not accounts:
            await query.edit_message_text("📭 您还没有添加账户，请先添加账户", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ 添加账户", callback_data="add_account")]]))
            return
        
        kb = []
        text = "💡 *选择要计算推荐金额的账户*\n\n"
        for acc in accounts:
            if acc.is_logged_in:
                text += f"✅ {acc.get_display_name()}\n"
                kb.append([InlineKeyboardButton(f"{acc.get_display_name()}", callback_data=f"recommend_account:{acc.phone}")])
            else:
                text += f"❌ {acc.get_display_name()} (未登录)\n"
        
        kb.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_recommend_menu(self, query, user, phone):
        acc = self.account_manager.get_account(phone)
        if not acc:
            await query.edit_message_text("❌ 账户不存在")
            return
        
        kb = [
            [InlineKeyboardButton("🛡️ 保守型 (1%)", callback_data=f"recommend_mode:保守:{phone}"),
             InlineKeyboardButton("📊 稳健型 (0.8%)", callback_data=f"recommend_mode:稳健:{phone}")],
            [InlineKeyboardButton("⚖️ 平衡型 (1.5%)", callback_data=f"recommend_mode:平衡:{phone}"),
             InlineKeyboardButton("🚀 进取型 (3%)", callback_data=f"recommend_mode:进取:{phone}")],
            [InlineKeyboardButton("⚡ 激进型 (5%)", callback_data=f"recommend_mode:激进:{phone}")],
            [InlineKeyboardButton("🔙 返回账户列表", callback_data="menu:accounts")]
        ]
        
        text = f"💡 *推荐金额计算 - {acc.get_display_name()}*\n\n"
        text += "请选择风险偏好，系统将根据当前余额计算建议每注金额：\n\n"
        text += "• 保守型：余额 × 1% (风险最低)\n"
        text += "• 稳健型：余额 × 0.8% (低风险)\n"
        text += "• 平衡型：余额 × 1.5% (中低风险)\n"
        text += "• 进取型：余额 × 3% (中高风险)\n"
        text += "• 激进型：余额 × 5% (高风险)\n"
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _calculate_and_show_recommendation(self, query, user, phone, mode):
        acc = self.account_manager.get_account(phone)
        if not acc:
            await query.edit_message_text("❌ 账户不存在")
            return
        
        # 获取当前余额
        balance = self.account_manager.get_cached_balance(phone)
        if balance is None:
            balance = await self.game_scheduler.get_balance(phone)
        if balance is None:
            await query.edit_message_text("❌ 无法获取余额，请稍后重试")
            return

        # 风险比例映射
        risk_ratios = {
            '保守': 0.01,   # 1%
            '稳健': 0.008,  # 0.8%
            '平衡': 0.015,  # 1.5%
            '进取': 0.03,   # 3%
            '激进': 0.05    # 5%
        }
        
        ratio = risk_ratios.get(mode, 0.01)
        recommend = balance * ratio
        
        # 限制在合理范围内
        recommend = max(Config.MIN_BET_AMOUNT, min(recommend, Config.MAX_BET_AMOUNT))
        # 取整到最近的1000
        recommend = int(recommend / 1000) * 1000
        if recommend < Config.MIN_BET_AMOUNT:
            recommend = Config.MIN_BET_AMOUNT
        
        text = (
            f"💡 *推荐金额计算结果*\n\n"
            f"📱 账户: {acc.get_display_name()}\n"
            f"💰 当前余额: {balance:.0f} KK\n"
            f"📊 选择模式: {mode}\n"
            f"🎯 推荐每注金额: {recommend:.0f} KK\n\n"
            f"是否将此金额设置为当前账户的基础金额？\n\n"
            f"💡 提示：推荐金额仅供参考，请根据实际情况调整。"
        )
        kb = [
            [InlineKeyboardButton("✅ 是，设置为基础金额", callback_data=f"set_recommend:yes:{phone}:{recommend}"),
             InlineKeyboardButton("❌ 否，返回", callback_data=f"select_account:{phone}")],
            [InlineKeyboardButton("🔄 重新选择模式", callback_data=f"recommend_account:{phone}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_amount_menu(self, query, user, phone, context):
        acc = self.account_manager.get_account(phone)
        if not acc:
            await query.edit_message_text("❌ 账户不存在")
            return
        
        text = f"""
💰 *金额设置*

📱 账户: {acc.get_display_name()}

当前设置:
• 基础金额: {acc.bet_params.base_amount} KK
• 最大金额: {acc.bet_params.max_amount} KK
• 止损金额: {acc.bet_params.stop_loss} KK
• 止盈金额: {acc.bet_params.stop_win} KK
• 停止余额: {acc.bet_params.stop_balance} KK
• 恢复余额: {acc.bet_params.resume_balance} KK
• 动态比例: {acc.bet_params.dynamic_base_ratio*100:.0f}% ({'开启' if acc.bet_params.dynamic_base_ratio>0 else '关闭'})

请选择需要修改的金额类型：
        """
        kb = [
            [InlineKeyboardButton("💰 基础金额", callback_data=f"amount_set:base_amount:{phone}"),
             InlineKeyboardButton("💎 最大金额", callback_data=f"amount_set:max_amount:{phone}")],
            [InlineKeyboardButton("🛑 停止余额", callback_data=f"amount_set:stop_balance:{phone}"),
             InlineKeyboardButton("⛔ 止损金额", callback_data=f"amount_set:stop_loss:{phone}")],
            [InlineKeyboardButton("✅ 止盈金额", callback_data=f"amount_set:stop_win:{phone}"),
             InlineKeyboardButton("🔄 恢复余额", callback_data=f"amount_set:resume_balance:{phone}")],
            [InlineKeyboardButton("📈 动态投注比例", callback_data=f"amount_set:dynamic_base_ratio:{phone}")],
            [InlineKeyboardButton("🔙 返回账户详情", callback_data=f"select_account:{phone}")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _amount_set_callback(self, query, user, phone, param_name, context):
        param_names = {
            'base_amount': '基础金额',
            'max_amount': '最大金额',
            'stop_balance': '停止余额',
            'stop_loss': '止损金额',
            'stop_win': '止盈金额',
            'resume_balance': '恢复余额',
            'dynamic_base_ratio': '动态投注比例'
        }
        display_name = param_names.get(param_name, param_name)
        self.account_manager.set_user_state(user, 'account_selected', {'current_account': phone})
        await self.account_manager.update_account(phone, input_mode=param_name, input_buffer='')
        
        if param_name == 'dynamic_base_ratio':
            text = f"🔢 请输入动态投注比例（如0.03表示3%）：\n\n范围0~1，输入0关闭动态。"
        else:
            text = f"🔢 请输入新的 {display_name}（单位：KK）："
        
        kb = [[InlineKeyboardButton("🔙 返回", callback_data=f"amount_menu:{phone}")]]
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
        
        # 动态比例状态
        dynamic_status = f"动态:{acc.bet_params.dynamic_base_ratio*100:.0f}%" if acc.bet_params.dynamic_base_ratio > 0 else "动态:关闭"
        
        kb = [
            [InlineKeyboardButton("🔐 登录", callback_data=f"login_select:{phone}"),
             InlineKeyboardButton("🚪 登出", callback_data=f"action:logout:{phone}")],
            [InlineKeyboardButton("💬 游戏群", callback_data=f"action:listgroups:{phone}")],
            [InlineKeyboardButton("🎯 投注方案", callback_data=f"action:setscheme:{phone}"),
             InlineKeyboardButton("📈 金额策略", callback_data=f"action:setstrategy:{phone}")],
            [InlineKeyboardButton("💰 金额设置", callback_data=f"amount_menu:{phone}"),
             InlineKeyboardButton("💡 推荐金额", callback_data=f"recommend_account:{phone}")],
            [InlineKeyboardButton(bet_button, callback_data=f"action:toggle_bet:{phone}")],
            [InlineKeyboardButton("💰 查询余额", callback_data=f"action:balance:{phone}"),
             InlineKeyboardButton("📊 账户状态", callback_data=f"action:status:{phone}")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu:accounts")]
        ]
        text = f"📱 *账户: {display}*\n\n状态: {status}\n净盈利: {net_profit:.0f}KK\n基础金额: {acc.bet_params.base_amount} KK\n{dynamic_status}\n\n选择操作:"
        try: await query_or_message.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except: await query_or_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_prediction(self, query):
        history = await self.api.get_history(50)
        if len(history) < 10: await query.edit_message_text("❌ 历史数据不足"); return
        kill_target = self.model.predict_kill(history)
        latest = history[0] if history else {'qihao': 'N/A', 'combo': 'N/A'}
        text = f"🎯 *当前预测*\n\n📊 最新期号: {latest.get('qihao')}\n📌 最新结果: {latest.get('combo')}\n\n🚫 杀组推荐: {kill_target}\n💡 投注: {' '.join([c for c in COMBOS if c != kill_target])}"
        kb = [[InlineKeyboardButton("🔄 刷新预测", callback_data="menu:prediction")],
              [InlineKeyboardButton("🔙 返回", callback_data="menu:main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    async def _show_status(self, query):
        api_stats = self.api.get_statistics()
        sched_stats = self.game_scheduler.get_stats()
        total_accounts = len(self.account_manager.accounts)
        logged = sum(1 for a in self.account_manager.accounts.values() if a.is_logged_in)
        auto = sched_stats['auto_betting_accounts']
        text = f"📊 *系统状态*\n\n• 缓存数据: {api_stats['缓存数据量']}期\n• 最新期号: {api_stats['最新期号']}\n• 总账户: {total_accounts}\n• 已登录: {logged}\n• 自动投注: {auto}\n• 成功投注: {sched_stats['game_stats']['successful_bets']}"
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
            bal = await self.game_scheduler.get_balance(phone)
            text = f"💰 余额: {bal:.2f} KK" if bal is not None else "❌ 查询失败"
            kb = [[InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "status":
            acc = self.account_manager.get_account(phone)
            dynamic_status = f"动态比例: {acc.bet_params.dynamic_base_ratio*100:.0f}%" if acc.bet_params.dynamic_base_ratio > 0 else "动态比例: 关闭"
            text = f"📱 账户状态\n\n• 手机号: {acc.phone}\n• 登录: {'✅' if acc.is_logged_in else '❌'}\n• 自动投注: {'✅' if acc.auto_betting else '❌'}\n• 游戏群: {acc.game_group_name or '未设置'}\n• 余额: {acc.balance:.2f}KK\n• 基础金额: {acc.bet_params.base_amount} KK\n• {dynamic_status}\n• 总投注: {acc.total_bets}次\n• 净盈利: {acc.total_profit - acc.total_loss:.2f}KK"
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
        elif action == "setscheme":
            kb = [[InlineKeyboardButton(name, callback_data=f"set_scheme:{phone}:{name}")] for name in self.strategy_manager.schemes.keys()]
            kb.append([InlineKeyboardButton("🔙 返回", callback_data=f"select_account:{phone}")])
            await query.edit_message_text("🎯 选择投注方案:", reply_markup=InlineKeyboardMarkup(kb))

    async def _process_set_strategy(self, query, user, phone, strategy):
        ok, msg = await self.strategy_manager.set_strategy(phone, strategy, user)
        if ok: await self._show_account_detail(query, user, phone)
        else: await query.edit_message_text(f"❌ {msg}")

    async def _process_set_scheme(self, query, user, phone, scheme):
        ok, msg = await self.strategy_manager.set_scheme(phone, scheme, user)
        if ok: await self._show_account_detail(query, user, phone)
        else: await query.edit_message_text(f"❌ {msg}")

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
    print("PC28 智能预测投注系统")
    print("含推荐金额功能 | 支持动态投注比例")
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
