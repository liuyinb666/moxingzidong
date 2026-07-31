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
from typing import Optional, List, Dict, Any
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

# ==================== 内置 30 算法矩阵 ====================

class BasePredictor:
    """所有预测算法的基类"""
    name = "base"
    version = "1.0"

    def predict(self, history: list) -> dict:
        """
        输入: history 为列表，元素是 dict，至少包含:
            {"size": "大"/"小", "odd_even": "单"/"双", "dragon_tiger": "龙"/"虎"/"和",
             "total": int, "nums": [int,int,int], "issue": str}
        返回: {"大单": score, "大双": score, "小单": score, "小双": score}
        """
        raise NotImplementedError

    def update(self, actual: dict):
        """反馈: actual 格式同 history 元素"""
        pass

    def _base_scores(self, value=50):
        return {"大单": value, "大双": value, "小单": value, "小双": value}

    def _combo(self, size, odd):
        return f"{size}{odd}"


class Markov3Predictor(BasePredictor):
    """三阶马尔可夫链 - 状态转移记忆"""
    name = "markov_3rd"
    def predict(self, history):
        if len(history) < 4:
            return self._base_scores()
        h = history[-3:]
        key = tuple(r["size"]+r["odd_even"] for r in h)
        model = defaultdict(Counter)
        for i in range(len(history)-3):
            k = tuple(history[j]["size"]+history[j]["odd_even"] for j in range(i,i+3))
            nxt = history[i+3]["size"]+history[i+3]["odd_even"]
            model[k][nxt] += 1
        if not model[key]:
            return self._base_scores()
        total = sum(model[key].values())
        scores = self._base_scores(50)
        for combo, cnt in model[key].items():
            if combo in scores:
                scores[combo] += (cnt/total)*40
        return scores


class Markov4Predictor(BasePredictor):
    """四阶马尔可夫链 - 深层状态记忆"""
    name = "markov_4th"
    def predict(self, history):
        if len(history) < 5:
            return self._base_scores()
        key = tuple(r["size"]+r["odd_even"] for r in history[-4:])
        model = defaultdict(Counter)
        for i in range(len(history)-4):
            k = tuple(history[j]["size"]+history[j]["odd_even"] for j in range(i,i+4))
            nxt = history[i+4]["size"]+history[i+4]["odd_even"]
            model[k][nxt] += 1
        if not model[key]:
            return self._base_scores()
        total = sum(model[key].values())
        scores = self._base_scores(50)
        for combo, cnt in model[key].items():
            if combo in scores:
                scores[combo] += (cnt/total)*40
        return scores


class EWMAMultiPredictor(BasePredictor):
    """EWMA多尺度融合 - 指数加权记忆"""
    name = "ewma_multi"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        scales = [3, 8, 21]
        weights = [0.5, 0.3, 0.2]
        scores = self._base_scores(0)
        for w, span in zip(weights, scales):
            alpha = 2/(span+1)
            sz_w = {"大": 0, "小": 0}
            od_w = {"单": 0, "双": 0}
            for r in history[-span*2:]:
                sz_w[r["size"]] = sz_w[r["size"]]*(1-alpha) + alpha
                od_w[r["odd_even"]] = od_w[r["odd_even"]]*(1-alpha) + alpha
            for combo in scores:
                scores[combo] += sz_w[combo[0]]*od_w[combo[1]]*w*100
        return scores


class HoltWintersPredictor(BasePredictor):
    """Holt-Winters三重平滑 - 趋势+季节"""
    name = "holt_winters"
    def predict(self, history):
        if len(history) < 8:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else -1 for r in history[-20:]]
        od = [1 if r["odd_even"]=="单" else -1 for r in history[-20:]]
        a1, b1 = self._hw(sz)
        a2, b2 = self._hw(od)
        scores = self._base_scores(50)
        if a1+b1 > 0:
            scores["大单"]+=20; scores["大双"]+=20
        else:
            scores["小单"]+=20; scores["小双"]+=20
        if a2+b2 > 0:
            scores["大单"]+=20; scores["小单"]+=20
        else:
            scores["大双"]+=20; scores["小双"]+=20
        return scores
    def _hw(self, seq, alpha=0.3, beta=0.1):
        l, t = seq[0], 0
        for v in seq[1:]:
            l_prev = l
            l = alpha*v + (1-alpha)*(l+t)
            t = beta*(l-l_prev) + (1-beta)*t
        return l, t


class BayesianPredictor(BasePredictor):
    """共轭先验贝叶斯 - 在线频率更新"""
    name = "bayesian_online"
    def __init__(self):
        self.prior = {"大":1,"小":1,"单":1,"双":1}
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        sz = Counter(r["size"] for r in history[-100:])
        od = Counter(r["odd_even"] for r in history[-100:])
        p_big = (sz["大"]+self.prior["大"])/(len(history[-100:])+2)
        p_odd = (od["单"]+self.prior["单"])/(len(history[-100:])+2)
        scores = self._base_scores(50)
        scores["大单"]+=p_big*p_odd*40
        scores["大双"]+=p_big*(1-p_odd)*40
        scores["小单"]+=(1-p_big)*p_odd*40
        scores["小双"]+=(1-p_big)*(1-p_odd)*40
        return scores


class BOCDPredictor(BasePredictor):
    """贝叶斯在线变点检测 - 结构突变识别"""
    name = "bocd"
    def __init__(self):
        self.hazard = 0.01
        self.mu0, self.kappa0, self.alpha0, self.beta0 = 0, 1, 1, 1
    def predict(self, history):
        if len(history) < 20:
            return self._base_scores()
        totals = [r["total"] for r in history[-30:]]
        R = [1.0]
        for x in totals:
            pred = R[-1]*self.hazard + (1-R[-1])*(1-self.hazard)
            R.append(min(pred, 0.99))
        if R[-1] > 0.5:
            scores = self._base_scores(50)
            scores["大单"]+=5; scores["大双"]+=5; scores["小单"]+=5; scores["小双"]+=5
            return scores
        return EWMAMultiPredictor().predict(history) if "EWMAMultiPredictor" in globals() else self._base_scores()


class KNNPredictor(BasePredictor):
    """KNN局部敏感哈希 - 相似历史检索"""
    name = "knn_memory"
    def predict(self, history):
        if len(history) < 20:
            return self._base_scores()
        k = min(30, len(history)//10)
        recent = history[-5:]
        neighbors = []
        for i in range(len(history)-6):
            dist = sum(1 for a,b in zip(recent, history[i:i+5])
                       if a["size"]!=b["size"] or a["odd_even"]!=b["odd_even"])
            neighbors.append((dist, history[i+5]))
        neighbors.sort(key=lambda x:x[0])
        top = [n[1]["size"]+n[1]["odd_even"] for n in neighbors[:k]]
        cnt = Counter(top)
        scores = self._base_scores(50)
        for combo, c in cnt.items():
            if combo in scores:
                scores[combo] += (c/k)*40
        return scores


class RandomForestPredictor(BasePredictor):
    """随机森林 - Bagging集成决策树"""
    name = "random_forest"
    def __init__(self):
        self.clf = None
        self._train_buffer = []
    def predict(self, history):
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            return self._base_scores()
        if len(history) < 30:
            return self._base_scores()
        X, y = self._build_xy(history)
        if len(set(y)) < 2:
            return self._base_scores()
        self.clf = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42)
        self.clf.fit(X, y)
        feat = self._feat(history[-5:])
        proba = self.clf.predict_proba([feat])[0]
        labels = self.clf.classes_
        scores = self._base_scores(50)
        for lab, p in zip(labels, proba):
            if lab in scores:
                scores[lab] += p*40
        return scores
    def _build_xy(self, hist):
        X, y = [], []
        for i in range(len(hist)-6):
            X.append(self._feat(hist[i:i+5]))
            y.append(hist[i+5]["size"]+hist[i+5]["odd_even"])
        return X, y
    def _feat(self, window):
        sz = [1 if r["size"]=="大" else 0 for r in window]
        od = [1 if r["odd_even"]=="单" else 0 for r in window]
        return sz + od + [sum(sz)/5, sum(od)/5]


class GBDTPredictor(BasePredictor):
    """梯度提升决策树 - 残差迭代优化"""
    name = "gbdt"
    def __init__(self):
        self.clf = None
    def predict(self, history):
        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError:
            return self._base_scores()
        if len(history) < 30:
            return self._base_scores()
        X, y = self._build_xy(history)
        if len(set(y)) < 2:
            return self._base_scores()
        self.clf = GradientBoostingClassifier(n_estimators=50, max_depth=3, random_state=42)
        self.clf.fit(X, y)
        feat = self._feat(history[-5:])
        proba = self.clf.predict_proba([feat])[0]
        labels = self.clf.classes_
        scores = self._base_scores(50)
        for lab, p in zip(labels, proba):
            if lab in scores:
                scores[lab] += p*40
        return scores
    def _build_xy(self, hist):
        X, y = [], []
        for i in range(len(hist)-6):
            X.append(self._feat(hist[i:i+5]))
            y.append(hist[i+5]["size"]+hist[i+5]["odd_even"])
        return X, y
    def _feat(self, window):
        sz = [1 if r["size"]=="大" else 0 for r in window]
        od = [1 if r["odd_even"]=="单" else 0 for r in window]
        dt = [1 if r["dragon_tiger"]=="龙" else 0 for r in window]
        return sz + od + dt


class SVMPredictor(BasePredictor):
    """SVM核方法 - 高维超平面分类"""
    name = "svm_kernel"
    def __init__(self):
        self.clf = None
    def predict(self, history):
        try:
            from sklearn.svm import SVC
        except ImportError:
            return self._base_scores()
        if len(history) < 30:
            return self._base_scores()
        X, y = self._build_xy(history)
        if len(set(y)) < 2:
            return self._base_scores()
        self.clf = SVC(kernel="rbf", probability=True, random_state=42)
        self.clf.fit(X, y)
        feat = self._feat(history[-5:])
        proba = self.clf.predict_proba([feat])[0]
        labels = self.clf.classes_
        scores = self._base_scores(50)
        for lab, p in zip(labels, proba):
            if lab in scores:
                scores[lab] += p*40
        return scores
    def _build_xy(self, hist):
        X, y = [], []
        for i in range(len(hist)-6):
            X.append(self._feat(hist[i:i+5]))
            y.append(hist[i+5]["size"]+hist[i+5]["odd_even"])
        return X, y
    def _feat(self, window):
        return [1 if r["size"]=="大" else 0 for r in window] + [1 if r["odd_even"]=="单" else 0 for r in window]


class IsoForestPredictor(BasePredictor):
    """孤立森林 - 异常检测后回归"""
    name = "isolation_forest"
    def predict(self, history):
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            return self._base_scores()
        if len(history) < 20:
            return self._base_scores()
        X = [self._feat(history[i:i+5]) for i in range(len(history)-5)]
        clf = IsolationForest(n_estimators=50, contamination=0.1, random_state=42)
        clf.fit(X)
        recent = self._feat(history[-5:])
        score = clf.score_samples([recent])[0]
        scores = self._base_scores(50)
        if score < -0.3:
            for k in scores:
                scores[k] = 100 - scores[k]
        return scores
    def _feat(self, window):
        return [r["total"] for r in window] + [max(r["nums"])-min(r["nums"]) for r in window]


class FFTPredictor(BasePredictor):
    """FFT频谱分析 - 频域周期提取"""
    name = "fft_spectrum"
    def predict(self, history):
        if len(history) < 16:
            return self._base_scores()
        try:
            import numpy as np
        except ImportError:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else -1 for r in history[-64:]]
        od = [1 if r["odd_even"]=="单" else -1 for r in history[-64:]]
        fsz = np.abs(np.fft.rfft(sz))
        fod = np.abs(np.fft.rfft(od))
        dom_sz = np.argmax(fsz[1:]) + 1 if len(fsz)>1 else 1
        dom_od = np.argmax(fod[1:]) + 1 if len(fod)>1 else 1
        scores = self._base_scores(50)
        phase_sz = math.sin(2*math.pi*dom_sz*len(sz)/len(sz))
        phase_od = math.sin(2*math.pi*dom_od*len(od)/len(od))
        if phase_sz > 0:
            scores["大单"]+=15; scores["大双"]+=15
        else:
            scores["小单"]+=15; scores["小双"]+=15
        if phase_od > 0:
            scores["大单"]+=15; scores["小单"]+=15
        else:
            scores["大双"]+=15; scores["小双"]+=15
        return scores


class KalmanPredictor(BasePredictor):
    """卡尔曼滤波 - 最优线性估计"""
    name = "kalman_filter"
    def __init__(self):
        self.x = 0.0
        self.P = 1.0
        self.Q = 0.01
        self.R = 0.1
    def predict(self, history):
        if len(history) < 5:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else -1 for r in history]
        od = [1 if r["odd_even"]=="单" else -1 for r in history]
        x_sz = self._kalman(sz)
        x_od = self._kalman(od)
        scores = self._base_scores(50)
        if x_sz > 0:
            scores["大单"]+=20; scores["大双"]+=20
        else:
            scores["小单"]+=20; scores["小双"]+=20
        if x_od > 0:
            scores["大单"]+=20; scores["小单"]+=20
        else:
            scores["大双"]+=20; scores["小双"]+=20
        return scores
    def _kalman(self, z_seq):
        x, P = self.x, self.P
        for z in z_seq:
            x = x
            P = P + self.Q
            K = P / (P + self.R)
            x = x + K*(z - x)
            P = (1 - K)*P
        return x


class ParticleFilterPredictor(BasePredictor):
    """粒子滤波 - 蒙特卡洛递推估计"""
    name = "particle_filter"
    def __init__(self, n_particles=100):
        self.particles = [random.uniform(-1,1) for _ in range(n_particles)]
    def predict(self, history):
        if len(history) < 5:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else -1 for r in history[-10:]]
        weights = [1.0]*len(self.particles)
        for z in sz:
            for i,p in enumerate(self.particles):
                weights[i] *= max(0.01, 1-abs(z-p))
        s = sum(weights)
        weights = [w/s for w in weights]
        estimate = sum(p*w for p,w in zip(self.particles, weights))
        scores = self._base_scores(50)
        if estimate > 0:
            scores["大单"]+=20; scores["大双"]+=20
        else:
            scores["小单"]+=20; scores["小双"]+=20
        self.particles = random.choices(self.particles, weights=weights, k=len(self.particles))
        return scores


class NashPredictor(BasePredictor):
    """纳什均衡 - 博弈论策略推断"""
    name = "nash_equilibrium"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        recent = history[-10:]
        combos = [r["size"]+r["odd_even"] for r in recent]
        cnt = Counter(combos)
        total = len(combos)
        payoff = {c: cnt[c]/total for c in ["大单","大双","小单","小双"]}
        best = max(payoff, key=payoff.get)
        scores = self._base_scores(50)
        scores[best] += 30
        return scores


class KellyPredictor(BasePredictor):
    """凯利公式 - 最优下注比例"""
    name = "kelly_criterion"
    def predict(self, history):
        if len(history) < 20:
            return self._base_scores()
        sz = Counter(r["size"] for r in history[-50:])
        od = Counter(r["odd_even"] for r in history[-50:])
        n = 50
        p_big = sz["大"]/n
        p_odd = od["单"]/n
        scores = self._base_scores(50)
        scores["大单"] += p_big*p_odd*50
        scores["大双"] += p_big*(1-p_odd)*50
        scores["小单"] += (1-p_big)*p_odd*50
        scores["小双"] += (1-p_big)*(1-p_odd)*50
        return scores


class ReflexivityPredictor(BasePredictor):
    """反身性模型 - 偏见与基本面互馈"""
    name = "soros_reflexivity"
    def __init__(self):
        self.bias = 0
    def predict(self, history):
        if len(history) < 6:
            return self._base_scores()
        last3 = history[-3:]
        hit = sum(1 for i in range(1,4) if i<len(last3) and
                  last3[i]["size"]==last3[i-1]["size"])
        scores = self._base_scores(50)
        if hit >= 2:
            self.bias = min(self.bias+0.1, 1)
        else:
            self.bias = max(self.bias-0.1, -1)
        if self.bias > 0:
            last = history[-1]["size"]
            for combo in scores:
                if combo[0]==last:
                    scores[combo]+=25
        else:
            last = history[-1]["size"]
            flip = "小" if last=="大" else "大"
            for combo in scores:
                if combo[0]==flip:
                    scores[combo]+=25
        return scores


class MarketDepthPredictor(BasePredictor):
    """市场深度模拟 - 盘口压力推断"""
    name = "market_depth"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        sz = Counter(r["size"] for r in history[-20:])
        od = Counter(r["odd_even"] for r in history[-20:])
        big_pressure = sz["大"]/(sz["大"]+sz["小"])
        odd_pressure = od["单"]/(od["单"]+od["双"])
        scores = self._base_scores(50)
        if big_pressure > 0.6:
            scores["大单"]+=15; scores["大双"]+=15
        elif big_pressure < 0.4:
            scores["小单"]+=15; scores["小双"]+=15
        if odd_pressure > 0.6:
            scores["大单"]+=15; scores["小单"]+=15
        elif odd_pressure < 0.4:
            scores["大双"]+=15; scores["小双"]+=15
        return scores


class EvoGamePredictor(BasePredictor):
    """演化博弈 - 复制动态收敛"""
    name = "evolutionary_game"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        recent = history[-10:]
        combos = [r["size"]+r["odd_even"] for r in recent]
        cnt = Counter(combos)
        total = sum(cnt.values())
        fitness = {c: (cnt.get(c,0)/total)**2 for c in ["大单","大双","小单","小双"]}
        avg_fit = sum(fitness.values())/4
        scores = self._base_scores(50)
        for c in fitness:
            if fitness[c] > avg_fit:
                scores[c] += (fitness[c]-avg_fit)*100
        return scores


class PiCyclePredictor(BasePredictor):
    """π周期探测 - 无理数混沌周期"""
    name = "pi_chaos_cycle"
    def predict(self, history):
        if len(history) < 30:
            return self._base_scores()
        pi = "14159265358979323846"
        periods = [int(pi[i])+2 for i in range(10)]
        h = history
        best_period, best_score, best_pred = None, 0, None
        for period in set(periods):
            if len(h) < period*3:
                continue
            template = [r["size"]+r["odd_even"] for r in h[-period:]]
            matches, nexts = 0, []
            for i in range(len(h)-period*3, len(h)-period, period):
                if i < 0: continue
                if all(h[i+j]["size"]+h[i+j]["odd_even"]==template[j] for j in range(period)):
                    matches += 1
                    nexts.append(h[i+period]["size"]+h[i+period]["odd_even"])
            rate = matches/max(1, len(h)//period-3)
            if rate > best_score and rate > 0.15:
                best_score, best_period = rate, period
                if nexts:
                    best_pred = Counter(nexts).most_common(1)[0][0]
        scores = self._base_scores(50)
        if best_pred:
            scores[best_pred] += 30 + best_score*40
        return scores


class LyapunovPredictor(BasePredictor):
    """李雅普诺夫指数 - 混沌可预测性度量"""
    name = "lyapunov_exp"
    def predict(self, history):
        if len(history) < 20:
            return self._base_scores()
        totals = [r["total"] for r in history[-20:]]
        diverge = []
        for i in range(10):
            for j in range(i+1, 10):
                d0 = abs(totals[i]-totals[j])
                d1 = abs(totals[i+1]-totals[j+1]) if i+1<len(totals) and j+1<len(totals) else 0
                if d0 > 0:
                    diverge.append(math.log(d1/d0) if d1>0 else -5)
        le = sum(diverge)/len(diverge) if diverge else 0
        scores = self._base_scores(50)
        if le > 0:
            last = history[-1]["size"]+history[-1]["odd_even"]
            scores[last] += 20
        else:
            flip = {"大单":"小双","大双":"小单","小单":"大双","小双":"大单"}
            last = history[-1]["size"]+history[-1]["odd_even"]
            if last in flip:
                scores[flip[last]] += 20
        return scores


class HurstPredictor(BasePredictor):
    """Hurst指数 - 长期记忆性检测"""
    name = "hurst_exponent"
    def predict(self, history):
        if len(history) < 20:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else 0 for r in history[-20:]]
        h = self._rs(sz)
        scores = self._base_scores(50)
        if h > 0.55:
            last = history[-1]["size"]
            for combo in scores:
                if combo[0]==last:
                    scores[combo]+=25
        elif h < 0.45:
            last = history[-1]["size"]
            flip = "小" if last=="大" else "大"
            for combo in scores:
                if combo[0]==flip:
                    scores[combo]+=25
        return scores
    def _rs(self, series):
        n = len(series)
        mean = sum(series)/n
        z = [s-mean for s in series]
        cum = [sum(z[:i+1]) for i in range(n)]
        r = max(cum)-min(cum)
        s = math.sqrt(sum(x*x for x in z)/n) or 1
        return math.log(r/s)/math.log(n) if n>1 else 0.5


class RecurrencePredictor(BasePredictor):
    """递归图分析 - 相空间轨迹结构"""
    name = "recurrence_plot"
    def predict(self, history):
        if len(history) < 15:
            return self._base_scores()
        totals = [r["total"] for r in history[-15:]]
        m, eps = 3, 5
        rec = 0
        for i in range(len(totals)-m):
            for j in range(i+1, len(totals)-m):
                d = sum((totals[i+k]-totals[j+k])**2 for k in range(m))
                if d < eps*eps:
                    rec += 1
        det = rec/max(1, (len(totals)-m)*(len(totals)-m-1)//2)
        scores = self._base_scores(50)
        if det > 0.3:
            last = history[-1]["size"]+history[-1]["odd_even"]
            scores[last]+=25
        return scores


class PoincarePredictor(BasePredictor):
    """庞加莱截面映射 - 吸引子降维"""
    name = "poincare_map"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        totals = [r["total"] for r in history[-10:]]
        spans = [max(r["nums"])-min(r["nums"]) for r in history[-10:]]
        clusters = {}
        for t, s in zip(totals, spans):
            k = (t//5, s)
            clusters[k] = clusters.get(k, 0)+1
        best = max(clusters, key=clusters.get)
        scores = self._base_scores(50)
        if best[0] >= 3:
            scores["大单"]+=20; scores["大双"]+=20
        else:
            scores["小单"]+=20; scores["小双"]+=20
        return scores


class StackingPredictor(BasePredictor):
    """Stacking元学习 - 两层模型融合"""
    name = "stacking_meta"
    def __init__(self):
        self.base_learners = []
        self.meta_weights = [0.25, 0.25, 0.25, 0.25]
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        scores = self._base_scores(0)
        methods = [
            lambda h: {"大单":60,"大双":50,"小单":40,"小双":50} if h[-1]["size"]=="大" else {"大单":40,"大双":50,"小单":60,"小双":50},
            lambda h: {"大单":50,"大双":60,"小单":50,"小双":40} if h[-1]["odd_even"]=="双" else {"大单":50,"大双":40,"小单":50,"小双":60},
            lambda h: {"大单":55,"大双":45,"小单":45,"小双":55},
            lambda h: {"大单":45,"大双":55,"小单":55,"小双":45},
        ]
        for w, m in zip(self.meta_weights, methods):
            pred = m(history)
            for k in scores:
                scores[k] += pred[k]*w
        return scores


class MoEPredictor(BasePredictor):
    """混合专家门控 - 动态路由选择"""
    name = "moe_gate"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        recent = history[-5:]
        streak = sum(1 for i in range(1,5) if i<len(recent) and recent[i]["size"]==recent[i-1]["size"])
        scores = self._base_scores(50)
        if streak >= 3:
            last = recent[-1]["size"]
            for combo in scores:
                if combo[0]==last:
                    scores[combo]+=30
        else:
            sz = sum(1 if r["size"]=="大" else -1 for r in history[-10:])
            if sz > 0:
                scores["小单"]+=20; scores["小双"]+=20
            else:
                scores["大单"]+=20; scores["大双"]+=20
        return scores


class BMAPredictor(BasePredictor):
    """贝叶斯模型平均 - 后验概率加权"""
    name = "bma_weight"
    def __init__(self):
        self.models = {"动量":0, "反转":0}
        self.samples = 0
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        self.samples += 1
        recent = history[-5:]
        streak = sum(1 for i in range(1,5) if i<len(recent) and recent[i]["size"]==recent[i-1]["size"])
        if streak >= 3:
            self.models["动量"] += 1
        else:
            self.models["反转"] += 1
        total = self.models["动量"]+self.models["反转"]
        p_mom = self.models["动量"]/total if total else 0.5
        scores = self._base_scores(50)
        last = history[-1]["size"]
        if p_mom > 0.5:
            for combo in scores:
                if combo[0]==last:
                    scores[combo]+=p_mom*30
        else:
            flip = "小" if last=="大" else "大"
            for combo in scores:
                if combo[0]==flip:
                    scores[combo]+=(1-p_mom)*30
        return scores


class ARCHPredictor(BasePredictor):
    """ARCH效应检测 - 波动率聚类"""
    name = "arch_detector"
    def predict(self, history):
        if len(history) < 15:
            return self._base_scores()
        totals = [r["total"] for r in history[-15:]]
        mean = sum(totals)/len(totals)
        resid = [t-mean for t in totals]
        arch = sum(r*r for r in resid)/len(resid)
        scores = self._base_scores(50)
        if arch > 50:
            for k in scores:
                scores[k] = 100-scores[k]
        return scores


class ARIMAPredictor(BasePredictor):
    """ARIMA简化 - 自回归差分"""
    name = "arima_simplified"
    def predict(self, history):
        if len(history) < 10:
            return self._base_scores()
        totals = [r["total"] for r in history[-10:]]
        diff = [totals[i]-totals[i-1] for i in range(1, len(totals))]
        if not diff:
            return self._base_scores()
        ar1 = sum(diff[i]*diff[i-1] for i in range(1, len(diff)))/sum(d*d for d in diff[:-1]) if sum(d*d for d in diff[:-1]) else 0
        pred_diff = ar1*diff[-1]
        pred_total = totals[-1]+pred_diff
        scores = self._base_scores(50)
        if pred_total >= 14:
            scores["大单"]+=20; scores["大双"]+=20
        else:
            scores["小单"]+=20; scores["小双"]+=20
        if int(pred_total)%2==1:
            scores["大单"]+=15; scores["小单"]+=15
        else:
            scores["大双"]+=15; scores["小双"]+=15
        return scores


class WaveletPredictor(BasePredictor):
    """Haar小波分解 - 多分辨率趋势"""
    name = "wavelet_haar"
    def predict(self, history):
        if len(history) < 8:
            return self._base_scores()
        sz = [1 if r["size"]=="大" else 0 for r in history[-8:]]
        cA = [(sz[i]+sz[i+1])/2 for i in range(0, 8, 2)]
        trend = sum(cA)/len(cA)
        scores = self._base_scores(50)
        if trend > 0.5:
            scores["大单"]+=25; scores["大双"]+=25
        else:
            scores["小单"]+=25; scores["小双"]+=25
        return scores


class KillGroupPredictor:
    """基于 30 算法集成投票的杀组预测器"""
    def __init__(self):
        self.predictors = [
            Markov3Predictor(),
            Markov4Predictor(),
            EWMAMultiPredictor(),
            HoltWintersPredictor(),
            BayesianPredictor(),
            BOCDPredictor(),
            KNNPredictor(),
            RandomForestPredictor(),
            GBDTPredictor(),
            SVMPredictor(),
            IsoForestPredictor(),
            FFTPredictor(),
            KalmanPredictor(),
            ParticleFilterPredictor(),
            NashPredictor(),
            KellyPredictor(),
            ReflexivityPredictor(),
            MarketDepthPredictor(),
            EvoGamePredictor(),
            PiCyclePredictor(),
            LyapunovPredictor(),
            HurstPredictor(),
            RecurrencePredictor(),
            PoincarePredictor(),
            StackingPredictor(),
            MoEPredictor(),
            BMAPredictor(),
            ARCHPredictor(),
            ARIMAPredictor(),
            WaveletPredictor(),
        ]
        logger.info(f"杀组预测器已加载 {len(self.predictors)} 个算法引擎")

    def predict_kill(self, history: list) -> tuple[str, float]:
        """
        history: list of dict with keys: size, odd_even, total, nums, issue, dragon_tiger
                时序须为 最旧 → 最新
        返回: (建议杀的组合, 置信度)
        逻辑: 回测最近10期，选择胜率最高的算法，并用其预测当前期；每期都重新回测并选择
        """
        if not self.predictors:
            return "小单", 0.5

        n_backtest = 10
        min_hist = max(2, n_backtest)

        # 历史不足时退化到全体投票
        if len(history) < min_hist:
            scores = {"大单": 0.0, "大双": 0.0, "小单": 0.0, "小双": 0.0}
            valid_count = 0
            for p in self.predictors:
                try:
                    pred = p.predict(history)
                    for k in scores:
                        scores[k] += pred.get(k, 50)
                    valid_count += 1
                except Exception:
                    continue
            if valid_count == 0:
                return "小单", 0.5
            for k in scores:
                scores[k] /= valid_count
            best = max(scores, key=scores.get)
            return best, 0.5

        # 回测窗口：最近 n_backtest 期
        test_start = len(history) - n_backtest
        win_rates = {}

        for p in self.predictors:
            wins = 0
            valid = 0
            for i in range(test_start, len(history)):
                train_hist = history[:i]
                actual = history[i]["size"] + history[i]["odd_even"]
                try:
                    pred_scores = p.predict(train_hist)
                    predicted = max(pred_scores, key=pred_scores.get)
                    # 当前杀组逻辑：predicted 为预测最可能开出的组合，我们杀掉它；
                    # 若实际不等于 predicted，则下注的另外3组中奖，视为该算法策略赢。
                    if predicted != actual:
                        wins += 1
                    valid += 1
                except Exception:
                    continue
            win_rates[p.name] = wins / valid if valid > 0 else 0.0

        if not win_rates:
            return "小单", 0.5

        best_name = max(win_rates, key=win_rates.get)
        best_rate = win_rates[best_name]
        best_predictor = next((p for p in self.predictors if p.name == best_name), None)

        if best_predictor is None:
            return "小单", 0.5

        final_scores = best_predictor.predict(history)
        kill_target = max(final_scores, key=final_scores.get)

        confidence = min(0.99, max(0.25, best_rate))
        logger.info(f"[杀组动态选择] 近{n_backtest}期回测最高胜率: {best_name} ({best_rate:.1%}) -> 杀 {kill_target}")
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

        # 30算法杀组模式
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
                active_descriptions.append(f"30算法杀组(杀{kill_target},置信{confidence:.0%},倍投{multiplier:.1f}x)")
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
                await event.answer("ABC球模式：针对开奖号码的前三位进行定位杀号。用户可多选A、B、C球，自定义杀码数量后系统自动随机杀掉对应数量的数字，并按独立金额与倍投设置自动投递剩余数字。中奖倍率9.99。", alert=True)
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
