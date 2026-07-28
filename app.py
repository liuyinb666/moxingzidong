import os
import re
import json
import asyncio
import logging
import threading
import random
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple
import aiohttp
import gradio as gr
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError

# ==================== 1. 核心常量与工具函数 ====================
def get_type(s: int) -> str:
    return ('大' if s >= 14 else '小') + ('单' if s % 2 else '双')

# ==================== 2. 风控管理系统（已移除熔断与复杂倍投） ====================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DATA_API_URL = "https://pc28.help/api/kj.json?nbr=100"
SESSIONS_DIR = "telegram_sessions"
USER_DATA_DIR = "user_data"

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
    def __init__(self, daily_stop_loss: float = 3000.0, daily_stop_profit: float = 5000.0):
        self.daily_stop_loss = daily_stop_loss
        self.daily_stop_profit = daily_stop_profit
        self.daily_pnl = 0.0

    def can_bet(self) -> tuple[bool, str]:
        if self.daily_pnl <= -self.daily_stop_loss: return False, "已触及每日止损线"
        if self.daily_pnl >= self.daily_stop_profit: return False, "已触及每日止盈线"
        return True, "运行正常"

    def add_pnl(self, amount: float):
        self.daily_pnl += amount

    def to_dict(self):
        return {
            "daily_stop_loss": self.daily_stop_loss,
            "daily_stop_profit": self.daily_stop_profit,
            "daily_pnl": self.daily_pnl
        }

    @classmethod
    def from_dict(cls, data):
        rm = cls(
            daily_stop_loss=data.get("daily_stop_loss", 3000.0),
            daily_stop_profit=data.get("daily_stop_profit", 5000.0),
        )
        rm.daily_pnl = data.get("daily_pnl", 0.0)
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

        # 模式配置（仅保留ABC球）
        self.selected_modes = ["ball"]
        self.selected_balls = ["a"] 

        # ABC独立设置
        self.ball_bet_amount = 100.0
        self.abc_kill_count = 1           # 每个球杀码数量（1-9）
        self.abc_martingale_multiplier = 2.0  # ABC倍投倍数
        self.abc_consecutive_losses = 0   # ABC连败次数

        # 上期ABC杀球记录 {b_char: [killed_digits]}
        self.last_ball_kills = {}

        # 附加下注特码与豹子配置
        self.extra_special_numbers = []  
        self.extra_bauzi = False         
        self.extra_bet_amounts = {
            "0_27": 100.0,
            "1_26": 100.0,
            "baozi": 100.0
        }

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
                        # 兼容旧数据：过滤掉已删除的模式
                        loaded_modes = data.get("selected_modes", ["ball"])
                        self.selected_modes = [m for m in loaded_modes if m == "ball"]
                        if not self.selected_modes:
                            self.selected_modes = ["ball"]
                        self.selected_balls = data.get("selected_balls", ["a"])
                        self.ball_bet_amount = data.get("ball_bet_amount", 100.0)
                        self.abc_kill_count = data.get("abc_kill_count", 1)
                        self.abc_martingale_multiplier = data.get("abc_martingale_multiplier", 2.0)
                        self.abc_consecutive_losses = data.get("abc_consecutive_losses", 0)
                        self.last_ball_kills = data.get("last_ball_kills", {})
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
                if await self.client.is_user_authorized(): return True
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
            except: pass
        return parsed

# ==================== 5. 系统中控与自动化调度中心 ====================
class SystemOrchestrator:
    def __init__(self):
        self.bot = TelegramClient("telegram_sessions/bot_master", API_ID, API_HASH)
        self.users = {}
        self.user_login_states = {}
        self.last_issue_id = None

    def get_user_state(self, uid):
        if uid not in self.users: self.users[uid] = UserState(uid)
        return self.users[uid]

    def main_keyboard(self, u_state: UserState):
        status = "🟢 运行中" if u_state.is_active else "🔴 已暂停"
        login = "🚪 登出账号" if u_state.is_logged_in else "🔑 登录协议号"
        return [
            [Button.inline(f"状态: {status}", data=b"noop"), Button.inline(login, data=b"login")],
            [Button.inline("🚀 启动挂机", data=b"start"), Button.inline("⏹ 暂停挂机", data=b"stop")],
            [Button.inline(f"⚙️ ABC杀球设置", data=b"select_mode")],
            [Button.inline("📖 模式介绍与说明", data=b"mode_intro_menu")],
            [Button.inline("💎 附加特码/豹子配置", data=b"extra_config")],
            [Button.inline("💰 独立金额与倍投设置", data=b"set_amounts_menu")],
            [Button.inline("➕ 绑定群组", data=b"add_g"), Button.inline("➖ 移除群组", data=b"del_g"), Button.inline("📋 群组列表", data=b"list_g")],
            [Button.inline(f"⏱ 投递延迟: {u_state.custom_delay}s", data=b"set_delay"), Button.inline("📝 设置自定义尾缀", data=b"set_suffix")],
            [Button.inline("📈 实时收益战报", data=b"stats")]
        ]

    def mode_selection_keyboard(self, u_state: UserState):
        def chk(m): return "✅ " if m in u_state.selected_modes else "⬜ "
        return [
            [Button.inline(f"{chk('ball')}启用 ABC杀球模式", data=b"toggle_mode_ball")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def mode_intro_keyboard(self):
        return [
            [Button.inline("ABC球模式介绍", data=b"intro_ball")],
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
        return [
            [Button.inline(f"ABC杀球单注金额: {u_state.ball_bet_amount}", data=b"set_ball_amount")],
            [Button.inline(f"ABC倍投倍数: {u_state.abc_martingale_multiplier}x", data=b"set_abc_multiplier")],
            [Button.inline(f"ABC杀码数量: {u_state.abc_kill_count}个", data=b"set_abc_kill_count")],
            [Button.inline(f"特码与豹子独立金额设置", data=b"set_extra_amounts")],
            [Button.inline("⬅️ 返回主菜单", data=b"back_main")]
        ]

    def ball_selection_keyboard(self, current_balls: list):
        def mk_btn(b_char, name):
            checked = "✅ " if b_char in current_balls else "⬜ "
            return Button.inline(f"{checked}{name}", data=f"toggle_ball_{b_char}")
        return [
            [mk_btn("a", "A球（第1位）"), mk_btn("b", "B球（第2位）"), mk_btn("c", "C球（第3位）")],
            [Button.inline("💾 保存并返回主菜单", data=b"back_main")]
        ]

    async def load_existing_users(self):
        if os.path.exists(USER_DATA_DIR):
            for file in os.listdir(USER_DATA_DIR):
                if file.endswith(".json"):
                    try:
                        uid = int(file.replace(".json", ""))
                        await self.get_user_state(uid).try_reconnect()
                    except: pass

    async def handle_new_issue_bet(self, u: UserState, issue_id: str, latest_market_data: MarketData = None):
        if u.last_betted_issue == issue_id: return
        u.last_betted_issue = issue_id

        can_bet, reason = u.risk_mgr.can_bet()
        if not can_bet or not u.groups: return

        all_bet_lines = []
        active_descriptions = []

        # 重置上期杀球记录，准备生成本期
        u.last_ball_kills = {}

        # ABC杀球模式（支持自定义杀码数量与倍投）
        if "ball" in u.selected_modes:
            multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
            single_bet = u.ball_bet_amount * multiplier
            buy_count = 10 - u.abc_kill_count

            for b_char in u.selected_balls:
                # 随机杀 abc_kill_count 个不同数字
                killed_digits = random.sample(range(10), u.abc_kill_count)
                u.last_ball_kills[b_char] = killed_digits
                for d in range(10):
                    if d not in killed_digits:
                        all_bet_lines.append(f"{b_char}{d}/{int(single_bet)}")
            active_descriptions.append(f"ABC杀球(杀{u.abc_kill_count}码,倍投{multiplier:.1f}x)")

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

        if u.custom_suffix: all_bet_lines.append(u.custom_suffix)
        if not all_bet_lines: return

        bet_msg = "\n".join(all_bet_lines)

        if u.custom_delay > 0: await asyncio.sleep(u.custom_delay)
        if not u.is_active or not u.client: return

        for group in u.groups:
            try:
                await u.client.send_message(group, bet_msg)
                logger.info(f"[用户 {u.user_id}] 成功向群组 [{group}] 发送下注 (第 {issue_id} 期)")
            except Exception as e:
                logger.error(f"发送群组下注失败: {e}")

        try:
            mode_label = "+".join(active_descriptions)
            await self.bot.send_message(
                u.user_id,
                f"【自动化下注通知】\n"
                f"--------------------\n"
                f"期号: `{issue_id}`\n"
                f"启用模式: `{mode_label}`\n"
                f"下注排版:\n`{bet_msg.replace(chr(10), ' | ')}`\n"
                f"--------------------"
            )
        except: pass

    async def register_handlers(self):
        @self.bot.on(events.NewMessage(pattern="/start"))
        async def handler_start(event):
            u = self.get_user_state(event.sender_id)
            await event.respond(
                f"欢迎使用 PC28量子智能量化挂机系统\n"
                f"--------------------\n"
                f"运行状态概览:\n"
                f"• 挂机状态: `{'运行中' if u.is_active else '已停止'}`\n"
                f"• 绑定群组: `{len(u.groups)}` 个\n"
                f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
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
                await event.answer(); return

            if data == "select_mode":
                await event.edit("请选择ABC杀球设置", buttons=self.mode_selection_keyboard(u))
                return

            if data == "toggle_mode_ball":
                if "ball" in u.selected_modes:
                    if len(u.selected_modes) > 1: u.selected_modes.remove("ball")
                else:
                    u.selected_modes.append("ball")
                u.save()
                await event.edit("请选择需要参与杀球的位次（可多选）", buttons=self.ball_selection_keyboard(u.selected_balls))
                return

            if data.startswith("toggle_ball_"):
                b_char = data.replace("toggle_ball_", "")
                if b_char in u.selected_balls:
                    if len(u.selected_balls) > 1: u.selected_balls.remove(b_char)
                else:
                    u.selected_balls.append(b_char)
                u.save()
                await event.edit("请选择需要参与杀球的位次（可多选）", buttons=self.ball_selection_keyboard(u.selected_balls))
                return

            if data == "set_amounts_menu":
                await event.edit("请选择需要修改的金额或倍投参数", buttons=self.amounts_menu_keyboard(u))
                return

            if data == "mode_intro_menu":
                await event.edit("请选择要查看的模式介绍说明", buttons=self.mode_intro_keyboard())
                return

            if data == "intro_ball":
                await event.answer("ABC球模式：针对开奖号码的前三位进行定位杀号。用户可多选A、B、C球，自定义杀码数量后系统自动随机杀掉对应数量的数字，并按独立金额与倍投设置自动投递剩余数字。中奖倍率9.99。", alert=True)
                return
            if data == "intro_extra":
                await event.answer("特码与豹子：支持独立设置金额并附加下注特码（0/27、1/26）以及豹子。", alert=True)
                return

            if data == "extra_config":
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_027":
                if "0_27" in u.extra_special_numbers: u.extra_special_numbers.remove("0_27")
                else: u.extra_special_numbers.append("0_27")
                u.save()
                await event.edit("请勾选您需要附加下注的特码与豹子", buttons=self.extra_config_keyboard(u))
                return

            if data == "toggle_extra_126":
                if "1_26" in u.extra_special_numbers: u.extra_special_numbers.remove("1_26")
                else: u.extra_special_numbers.append("1_26")
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

            if data == "back_main":
                await event.edit(
                    f"主控制面板\n"
                    f"--------------------\n"
                    f"• 挂机状态: `{'运行中' if u.is_active else '已停止'}`\n"
                    f"• 绑定群组: `{len(u.groups)}` 个\n"
                    f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                    f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
                    f"• 今日盈亏: `{u.risk_mgr.daily_pnl:+.2f}`\n"
                    f"--------------------",
                    buttons=self.main_keyboard(u)
                )
                return
            elif data == "start":
                if not u.is_logged_in or not u.groups:
                    await event.answer("请先登录账号并绑定至少一个目标群组!", alert=True)
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
                    if u.client: await u.client.disconnect()
                    u.save()
                    await event.edit("协议号已安全登出。", buttons=self.main_keyboard(u))
                else:
                    self.user_login_states[sid] = "WAIT_PHONE"
                    await event.respond("请发送您的 Telegram 手机号:")
            elif data == "add_g":
                self.user_login_states[sid] = "WAIT_GROUP"
                await event.respond("请发送目标群组的 Username 或 ID:")
            elif data == "del_g":
                if not u.groups: await event.respond("当前没有绑定任何群组。")
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
            elif data == "stats":
                rm = u.risk_mgr
                current_multiplier = u.abc_martingale_multiplier ** u.abc_consecutive_losses
                await event.respond(
                    f"详细收益战报与风控统计\n"
                    f"--------------------\n"
                    f"• 今日总盈亏: `{rm.daily_pnl:+.2f}`\n"
                    f"• ABC杀码数量: `{u.abc_kill_count}` 个\n"
                    f"• ABC倍投倍数: `{u.abc_martingale_multiplier}x`\n"
                    f"• ABC当前连败: `{u.abc_consecutive_losses}` 次\n"
                    f"• ABC当前计算单注: `{u.ball_bet_amount * current_multiplier:.2f}`\n"
                    f"--------------------"
                )

        @self.bot.on(events.NewMessage)
        async def handler_text(event):
            if event.text.startswith("/"): return
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
                if grp not in u.groups: u.groups.append(grp); u.save()
                await event.respond(f"成功绑定群组: `{grp}`", buttons=self.main_keyboard(u))
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_DEL_GROUP":
                val = event.text.strip()
                if val.isdigit() and 0 <= int(val)-1 < len(u.groups):
                    rmv = u.groups.pop(int(val)-1)
                    u.save()
                    await event.respond(f"已成功移除群组: `{rmv}`", buttons=self.main_keyboard(u))
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_DELAY":
                try:
                    u.custom_delay = max(0.0, float(event.text.strip()))
                    u.save()
                    await event.respond(f"投递延迟更新为: `{u.custom_delay}s`", buttons=self.main_keyboard(u))
                except: await event.respond("请输入有效的秒数数字")
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
                except: await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_ABC_MULTIPLIER":
                try:
                    val = float(event.text.strip())
                    u.abc_martingale_multiplier = max(1.0, val)
                    u.save()
                    await event.respond(f"ABC倍投倍数更新为: `{u.abc_martingale_multiplier}x`", buttons=self.main_keyboard(u))
                except: await event.respond("请输入有效数字")
                self.user_login_states.pop(sid, None)
            elif state == "WAIT_ABC_KILL_COUNT":
                try:
                    val = int(event.text.strip())
                    u.abc_kill_count = max(1, min(9, val))
                    u.save()
                    await event.respond(f"ABC杀码数量更新为: `{u.abc_kill_count}`个", buttons=self.main_keyboard(u))
                except: await event.respond("请输入1-9之间的整数")
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

                            u.history.insert(0, {"nums": [int(d) for d in data.number_str if d.isdigit()], "sum": data.num_value, "type": data.combination, "issue": data.issue_id})
                            if len(u.history) > 120: u.history = u.history[:120]
                            u.save()

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
                            except: pass

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
    try: loop.run_until_complete(orchestrator.start())
    except Exception as e: logger.error(f"Bot 运行异常: {e}")

threading.Thread(target=start_bot_thread, daemon=True).start()

with gr.Blocks(title="PC28量化智能挂机系统", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚀 PC28量化智能挂机系统 - 24小时永动中控")
    gr.Markdown("已移除杀组与中边玩法，删除熔断机制。ABC杀球模式支持自定义杀码数量、可配置倍投倍数（中奖倍率9.99），盈亏实时独立结算。保留特码与豹子附加下注。")
    gr.Markdown("---")
    gr.Markdown("<div style='text-align: center; color: gray;'>PC28量化挂机中控台 © 2026</div>")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port)
