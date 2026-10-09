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
    """A球杀号：固定随机杀5码"""
    return random.sample(range(10), 5)


def predict_c_kill(a: int, b: int, c: int) -> Optional[int]:
    """
    C球杀号预测：将a、b、c球拼接成三位数abc,
    计算 abc * π * e / (a+b+c)，取结果的最后一位数字。
    """
    s = a + b + c
    if s == 0:
        return None
    abc = int(f"{a}{b}{c}")
    result = abc * math.pi * math.e / s
    digits = f"{result:.10f}".replace(".", "").rstrip("0")
    return int(digits[-1])


def get_a_kills() -> list:
    return random.sample(range(10), 5)


def get_c_kills(history: list) -> list:
    if history:
        nums = history[0].get("nums", [])
        if len(nums) >= 3:
            predicted = predict_c_kill(nums[0], nums[1], nums[2])
            if predicted is not None:
                return [predicted]
    return random.sample(range(10), 1)


# ==================== 2. 风控管理系统 ====================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
API_KEY = "yu28_0889c78ad74725b7"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DATA_API_URL = "https://yu28.top/api/kj.json?nbr=100"
SESSIONS_DIR = "telegram_sessions"
USER_DATA_DIR = "user_data"

# 北京时间 (UTC+8)，盈亏按北京时间每天 00:00 重置
BEIJING_TZ = timezone(timedelta(hours=8))

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    issue_id: str
    number_str: str
    num_value: int
    combination: str

class RiskManager:
    def __init__(self, daily_stop_loss: float = 500.0, daily_stop_profit: float = 200.0):
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
        """手动清空今日实时盈亏"""
        self._ensure_daily_reset()
        old = self.daily_pnl
        self.daily_pnl = 0.0
        logger.info(f"手动重置每日盈亏: {old:+.2f} -> 0.00")
        return old

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
            daily_stop_loss=data.get("daily_stop_loss", 500.0),
            daily_stop_profit=data.get("daily_stop_profit", 200.0),
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
        self.groups = ["https://t.me/qwly21"]
        self.history = []
        self.risk_mgr = RiskManager()
        self.client = None
        self.temp_phone_code_hash = None
        self.custom_delay = 50.0
        self.custom_suffix = ""  
        self.last_betted_issue = ""

        # 模式配置（仅ABC球模式）
        self.selected_modes = ["ball"]
        self.selected_balls = ["a", "c"]

        # ABC独立设置
        self.ball_bet_amount = 1.0
        self.abc_martingale_steps = [1.0, 3.0, 7.0, 15.0, 31.0, 63.0]  # ABC自定义倍投阶梯
        self.abc_consecutive_losses = 0   # ABC连败次数

        # 上期ABC杀球记录 {b_char: [killed_digits]}
        self.last_ball_kills = {}

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

    def get_abc_multiplier(self) -> float:
        """根据当前连败次数获取ABC倍投倍数"""
        if not self.abc_martingale_steps:
            return 1.0
        idx = min(self.abc_consecutive_losses, len(self.abc_martingale_steps) - 1)
        return float(self.abc_martingale_steps[idx])

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
                        self.custom_delay = data.get("custom_delay", 50.0)
                        self.custom_suffix = data.get("custom_suffix", "")
                        self.selected_modes = ["ball"]
                        loaded_balls = data.get("selected_balls", ["a", "c"])
                        self.selected_balls = [b for b in loaded_balls if b in ("a", "c")]
                        if not self.selected_balls:
                            self.selected_balls = ["a", "c"]
                        self.ball_bet_amount = data.get("ball_bet_amount", 1.0)
                        # 兼容旧版：旧版使用 abc_martingale_multiplier 字段，新版使用 abc_martingale_steps 列表
                        if "abc_martingale_steps" in data:
                            self.abc_martingale_steps = data.get("abc_martingale_steps", [1.0, 3.0, 7.0, 15.0, 31.0, 62.0])
                        elif "abc_martingale_multiplier" in data:
                            old_mul = float(data.get("abc_martingale_multiplier", 2.0))
                            self.abc_martingale_steps = [old_mul ** i for i in range(6)]
                        else:
                            self.abc_martingale_steps = [1.0, 3.0, 7.0, 15.0, 31.0, 62.0]
                        self.abc_consecutive_losses = data.get("abc_consecutive_losses", 0)
                        self.last_ball_kills = data.get("last_ball_kills", {})
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
                        "abc_martingale_steps": self.abc_martingale_steps,
                        "abc_consecutive_losses": self.abc_consecutive_losses,
                        "last_ball_kills": self.last_ball_kills,
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

    def reset_daily_pnl(self):
        """重置今日实时盈亏并保存"""
        old = self.risk_mgr.reset_daily_pnl()
        self.save()
        return old

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
                    combo = item.get("combination", "")
                    if not combo:
                        combo = "大" if total >= 14 else "小" + ("单" if total % 2 else "双")
                    parsed.append({"nums": nums, "sum": total, "type": combo, "issue": str(item.get("nbr", ""))})
            except:
                pass
        return parsed

# ==================== 4.5 播报辅助函数 ====================
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
    """生成播报消息：期号与和值"""
    header = title.strip() if title else "预测播报"
    lines = [header]
    for rec in history_records[-max_records:]:
        q = str(rec.get('qihao', '--'))
        q = q[-4:] if len(q) >= 4 else q
        s = str(rec.get('sum', '') or '')
        lines.append(f"{q}.和{s}")
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
            [Button.inline("🎱 选球设置", data=b"toggle_mode_ball")],
            [Button.inline("📢 报数设置", data=b"broadcast_settings")],
            [Button.inline("💎 附加特码/豹子配置", data=b"extra_config")],
            [Button.inline("💰 独立金额与风控设置", data=b"set_amounts_menu")],
            [Button.inline("➕ 绑定群组", data=b"add_g"), Button.inline("➖ 移除群组", data=b"del_g"), Button.inline("📋 群组列表", data=b"list_g")],
            [Button.inline(f"⏱ 投递延迟: {u_state.custom_delay}s", data=b"set_delay"), Button.inline("📝 设置自定义尾缀", data=b"set_suffix")],
            [Button.inline("🔄 重置今日盈亏", data=b"reset_pnl"), Button.inline("📈 实时收益战报", data=b"stats")]
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
        steps_str = ",".join(str(int(s)) if s == int(s) else str(s) for s in u_state.abc_martingale_steps)
        triggered, reason = u_state.risk_mgr.check_triggered()
        risk_status = f"🔴 {reason}" if triggered else "🟢 正常"
        return [
            [Button.inline(f"ABC杀球单注金额: {u_state.ball_bet_amount}", data=b"set_ball_amount")],
            [Button.inline(f"ABC倍投阶梯: {steps_str}", data=b"set_abc_steps")],
            [Button.inline(f"每日止盈线: {u_state.risk_mgr.daily_stop_profit}", data=b"set_stop_profit")],
            [Button.inline(f"每日止损线: {u_state.risk_mgr.daily_stop_loss}", data=b"set_stop_loss")],
            [Button.inline(f"风控状态: {risk_status}", data=b"noop")],
            [Button.inline("特码与豹子独立金额设置", data=b"set_extra_amounts")],
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
            [mk_btn("a", "A球（第1位）"), mk_btn("c", "C球（第3位）")],
            [Button.inline("💾 保存并返回主菜单", data=b"back_main")]
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
        """执行报数播报：核对上期并发送下期"""
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

            # 生成下一期记录
            next_qihao = get_next_qihao(data.issue_id)
            rec = {'qihao': next_qihao, 'sum': data.num_value}
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

        # ABC杀球模式：A球随机杀5码，C球按预测杀1码
        abc_multiplier = u.get_abc_multiplier()
        if "ball" in u.selected_modes:
            for b_char in u.selected_balls:
                if b_char == "a":
                    killed_digits = get_a_kills()
                    u.last_ball_kills[b_char] = killed_digits
                    active_descriptions.append(f"A球随机杀5码(倍投{abc_multiplier:.1f}x)")
                elif b_char == "c":
                    killed_digits = get_c_kills(u.history)
                    u.last_ball_kills[b_char] = killed_digits
                    active_descriptions.append(f"C球预测杀1码(倍投{abc_multiplier:.1f}x)")

        # 构造实际下注消息
        if u.last_ball_kills:
            single_bet = u.ball_bet_amount * abc_multiplier
            for b_char in u.selected_balls:
                killed_digits = u.last_ball_kills.get(b_char)
                if killed_digits is None:
                    continue
                for d in range(10):
                    if d not in killed_digits:
                        all_bet_lines.append(f"{b_char}{d}/{int(single_bet)}")

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
                mode_label = "+".join(active_descriptions) if active_descriptions else "ABC杀球"
                notify_lines = [
                    f"【自动化下注通知】",
                    f"--------------------",
                    f"期号: `{issue_id}`",
                    f"启用模式: `{mode_label}`",
                    f"下注排版:\n`{bet_msg.replace(chr(10), ' | ')}`",
                    f"--------------------"
                ]
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
            bc_status = "开启" if u.broadcast_enabled else "关闭"
            await event.respond(
                f"欢迎使用 PC28量子智能量化挂机系统\n"
                f"--------------------\n"
                f"运行状态概览:\n"
                f"• 挂机状态: `{status_text}`\n"
                f"• 绑定群组: `{len(u.groups)}` 个\n"
                f"• 选球位次: {','.join([b.upper() for b in u.selected_balls])}\n"
                f"• A球杀码: `随机杀5码`\n"
                f"• C球杀码: `预测杀1码`\n"
                f"• ABC倍投阶梯: `{','.join(str(int(s)) if s == int(s) else str(s) for s in u.abc_martingale_steps)}`\n"
                f"• ABC当前倍投: `{u.get_abc_multiplier()}x`\n"
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

            if data == "toggle_mode_ball":
                await event.edit("请选择需要参与杀球的位次（可多选）", buttons=self.ball_selection_keyboard(u.selected_balls))
                return

            if data.startswith("toggle_ball_"):
                b_char = data.replace("toggle_ball_", "")
                if b_char not in ("a", "c"):
                    await event.answer("仅支持 A球 与 C球", alert=True)
                    return
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
                bc_status = "开启" if u.broadcast_enabled else "关闭"
                await event.edit(
                    f"主控制面板\n"
                    f"--------------------\n"
                    f"• 挂机状态: `{status_text}`\n"
                    f"• 绑定群组: `{len(u.groups)}` 个\n"
                    f"• 选球位次: {','.join([b.upper() for b in u.selected_balls])}\n"
                    f"• A球杀码: `随机杀5码`\n"
                    f"• C球杀码: `预测杀1码`\n"
                    f"• ABC倍投阶梯: `{','.join(str(int(s)) if s == int(s) else str(s) for s in u.abc_martingale_steps)}`\n"
                    f"• ABC当前倍投: `{u.get_abc_multiplier()}x`\n"
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
                        await u.client.disconn