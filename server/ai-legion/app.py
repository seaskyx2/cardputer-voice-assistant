import os
import re
import time
import json
import socket
import sqlite3
import asyncio
import threading
import psutil
import httpx
from datetime import datetime, timedelta, date, timezone
from typing import List, Optional, Dict, Any, Tuple
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── 加载本地 .env（无第三方依赖） ──────────────────────────────────────────────
def load_dotenv_file(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv_file(os.path.join(BASE_DIR, ".env"))

HA_URL = os.getenv("HA_URL", "http://127.0.0.1:8123").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
MODELS_DIR = os.getenv("MODELS_DIR", os.path.join(BASE_DIR, "models"))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", os.path.join(BASE_DIR, "dashboard"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "").rstrip("/")  # 若配置则走外部 Ollama；留空则使用本机 llama_cpp 直载 GGUF
PORT = int(os.getenv("PORT", "8096"))
N_THREADS = int(os.getenv("LLM_THREADS", "4"))
HOST_NAME = os.getenv("HOST_NAME", "").strip() or socket.gethostname()
HOST_LABEL = os.getenv("HOST_LABEL", "").strip() or f"{HOST_NAME} ({psutil.cpu_count(logical=False) or 4}C{psutil.cpu_count(logical=True) or 8}T)"
LEGION_API_TOKEN = os.getenv("LEGION_API_TOKEN", "").strip()  # 可选：配置后对控制塔敏感写接口启用 Bearer/Header 校验

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "memory.db")

app = FastAPI(title="AI Legion v2.6 · 端侧小模型集群控制塔")

def verify_optional_token(request: Request):
    """可选局域网写接口防护：若 .env 配置了 LEGION_API_TOKEN 则强制校验，未配置则默认放行"""
    if not LEGION_API_TOKEN:
        return
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return
    auth = request.headers.get("Authorization", "")
    x_tok = request.headers.get("X-Legion-Token", "")
    if auth == f"Bearer {LEGION_API_TOKEN}" or x_tok == LEGION_API_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing LEGION_API_TOKEN")

# ── 语音识别领域同音热词纠偏 ──────────────────────────────────────────────────
HOMOPHONE_FIXES = (
    ("随手机", "随手记"),
    ("随手计", "随手记"),
    ("备忘路", "备忘录"),
    ("被忘录", "备忘录"),
    ("代办", "待办"),
    ("吸顶等", "吸顶灯"),
)

def normalize_voice_text(text: str) -> str:
    for wrong, right in HOMOPHONE_FIXES:
        if wrong in text:
            text = text.replace(wrong, right)
    return text

# ── SQLite 离线家庭第二大脑（记忆与待办 · WAL 并发安全） ─────────────────────────
CN_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

def parse_cn_int(s: str) -> Optional[int]:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2 and s[1] in digits:
        return 10 + digits[s[1]]
    if s.endswith("十") and len(s) == 2 and s[0] in digits:
        return digits[s[0]] * 10
    if "十" in s and len(s) == 3:
        p = s.split("十")
        if p[0] in digits and p[1] in digits:
            return digits[p[0]] * 10 + digits[p[1]]
    if len(s) == 1 and s in digits:
        return digits[s]
    return None

def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def resolve_relative_date(text: str, base_dt: datetime = None) -> Optional[str]:
    """将口语相对日期（今天/明天/周末/三天后/下周三/下个月五号/X月X日）锚定为绝对 YYYY-MM-DD"""
    now = base_dt or datetime.now()
    today = now.date()

    # 1. 绝对月日：X月X日/号（支持中文数字或阿拉伯数字）
    m_md = re.search(r"([0-9一二三四五六七八九十]{1,3})月([0-9一二三四五六七八九十]{1,3})[日号]?", text)
    if m_md:
        mo = parse_cn_int(m_md.group(1))
        dy = parse_cn_int(m_md.group(2))
        if mo and dy:
            try:
                cand = date(today.year, mo, dy)
                if cand < today - timedelta(days=30):
                    cand = date(today.year + 1, mo, dy)
                return cand.strftime("%Y-%m-%d")
            except Exception:
                pass

    # 2. 下个月X号 / 本月X号
    m_nmd = re.search(r"(这个月|本月|下个月|下月)([0-9一二三四五六七八九十]{1,3})[日号]?", text)
    if m_nmd:
        dy = parse_cn_int(m_nmd.group(2))
        if dy:
            yr, mo = today.year, today.month
            if "下" in m_nmd.group(1):
                mo += 1
                if mo > 12:
                    mo = 1
                    yr += 1
            try:
                return date(yr, mo, dy).strftime("%Y-%m-%d")
            except Exception:
                pass

    # 3. X天后 / X周后
    m_after_d = re.search(r"([0-9一二两三四五六七八九十]{1,3})\s*天(?:后|之后|以后)", text)
    if m_after_d:
        n = parse_cn_int(m_after_d.group(1))
        if n is not None:
            return (today + timedelta(days=n)).strftime("%Y-%m-%d")

    m_after_w = re.search(r"([0-9一二两三四五六七八九十]{1,2})\s*(?:周|个?星期|个?礼拜)(?:后|之后|以后)", text)
    if m_after_w:
        n = parse_cn_int(m_after_w.group(1))
        if n is not None:
            return (today + timedelta(days=n * 7)).strftime("%Y-%m-%d")

    # 4. 大后天 / 后天 / 明天 / 今天
    if "大后天" in text:
        return (today + timedelta(days=3)).strftime("%Y-%m-%d")
    if "后天" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    if "明天" in text or "明日" in text or "明早" in text or "明晚" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "今天" in text or "今晚" in text or "今日" in text or "今早" in text:
        return today.strftime("%Y-%m-%d")

    # 5. 周末 / 这周末 / 下周末（锚定至对应周六；若今天已是周日且说“这周末”，则锚定今天周日）
    monday = today - timedelta(days=today.weekday())
    if "下周末" in text or "下个周末" in text:
        return (monday + timedelta(days=12)).strftime("%Y-%m-%d")
    if "周末" in text:
        sat = monday + timedelta(days=5)
        if today.weekday() == 6:
            return today.strftime("%Y-%m-%d")
        return sat.strftime("%Y-%m-%d")

    # 6. 本周/这周/下周 + 星期X
    m_wk = re.search(r"(这周|本周|这个星期|这星期|下周|下个星期|下星期)?(?:星期|周|礼拜)([一二三四五六日天])", text)
    if m_wk:
        prefix = m_wk.group(1) or ""
        target_wd = CN_WEEKDAY_MAP[m_wk.group(2)]
        if "下" in prefix:
            target = monday + timedelta(days=7 + target_wd)
        elif any(p in prefix for p in ("这", "本")):
            target = monday + timedelta(days=target_wd)
        else:
            diff = (target_wd - today.weekday()) % 7
            target = today + timedelta(days=diff)
        return target.strftime("%Y-%m-%d")

    # 7. 泛指“这周 / 本周 / 下周”
    if any(w in text for w in ("下周", "下星期", "下个星期")):
        return (monday + timedelta(days=13)).strftime("%Y-%m-%d")
    if any(w in text for w in ("这周", "本周", "这星期", "这个星期")):
        return (monday + timedelta(days=6)).strftime("%Y-%m-%d")
    return None

def resolve_reminder_time(text: str, base_dt: datetime = None) -> Optional[str]:
    """解析口语精确提醒时刻（如 '15分钟后' / '半小时后' / '明天上午9点半' / '今晚8点20'），返回 YYYY-MM-DD HH:MM"""
    now = base_dt or datetime.now()

    # 1. 相对分钟/小时后
    if "半小时后" in text or "半个小时后" in text or "半小时之后" in text:
        return (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")

    m_min = re.search(r"([0-9一二两三四五六七八九十]{1,3})\s*分钟?(?:后|之后|以后)", text)
    if m_min:
        mins = parse_cn_int(m_min.group(1))
        if mins and 1 <= mins <= 1440:
            return (now + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")

    m_hr = re.search(r"([0-9一二两三四五六七八九十]{1,2})\s*个?小时(?:后|之后|以后)", text)
    if m_hr:
        hrs = parse_cn_int(m_hr.group(1))
        if hrs and 1 <= hrs <= 72:
            return (now + timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M")

    # 2. 具体时刻（如 下午3点半 / 明天上午9点 / 20点15分 / 今晚8点）
    m_tm = re.search(
        r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明早|明晚)?\s*"
        r"([0-9一二两三四五六七八九十]{1,3})\s*(?:点|:|：)\s*"
        r"(半|一刻|三刻|[0-9一二三四五六七八九十]{1,3})?分?",
        text,
    )
    if m_tm:
        period = m_tm.group(1) or ""
        hr = parse_cn_int(m_tm.group(2))
        mn_raw = m_tm.group(3)
        if hr is not None and 0 <= hr <= 23:
            if mn_raw == "半":
                mn = 30
            elif mn_raw == "一刻":
                mn = 15
            elif mn_raw == "三刻":
                mn = 45
            elif mn_raw:
                mn = parse_cn_int(mn_raw) or 0
            else:
                mn = 0
            if mn > 59:
                mn = 0
            if period in ("下午", "傍晚", "晚上", "今晚", "明晚") and 1 <= hr <= 11:
                hr += 12
            elif period == "中午" and 1 <= hr <= 5:
                hr += 12
            ev_date_str = resolve_relative_date(text, now)
            if ev_date_str:
                base_d = datetime.strptime(ev_date_str, "%Y-%m-%d").date()
            else:
                base_d = now.date()
            cand = datetime(base_d.year, base_d.month, base_d.day, hr, mn)
            if not ev_date_str and cand <= now:
                cand += timedelta(days=1)
            return cand.strftime("%Y-%m-%d %H:%M")
    return None

def init_db():
    with get_db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL DEFAULT '随手记',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_date TEXT DEFAULT NULL,
                remind_at TEXT DEFAULT NULL,
                reminded INTEGER NOT NULL DEFAULT 0
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "event_date" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN event_date TEXT DEFAULT NULL")
        if "remind_at" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN remind_at TEXT DEFAULT NULL")
        if "reminded" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0")
        rows = conn.execute("SELECT id, content, created_at, event_date FROM memories WHERE event_date IS NULL").fetchall()
        for r in rows:
            try:
                base = datetime.strptime(r["created_at"][:16], "%Y-%m-%d %H:%M")
            except Exception:
                base = datetime.now()
            ev = resolve_relative_date(r["content"], base)
            if ev:
                conn.execute("UPDATE memories SET event_date = ?, category = '待办提醒' WHERE id = ?", (ev, r["id"]))
        conn.commit()

init_db()

def list_memories(limit: int = 30) -> List[Dict[str, Any]]:
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT id, category, content, created_at, event_date, remind_at, reminded FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

def get_best_memo_for_screen() -> str:
    """为 Cardputer 桌面副屏挑选最高价值的一条备忘：优先展示即将到点的定时提醒，其次最近未过期日程"""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today = now.date()
    today_str = today.strftime("%Y-%m-%d")
    with get_db_conn() as conn:
        rem = conn.execute(
            "SELECT content, remind_at FROM memories WHERE remind_at IS NOT NULL AND reminded = 0 AND remind_at >= ? ORDER BY remind_at ASC LIMIT 1",
            (now_str,),
        ).fetchone()
        if rem:
            hm = rem["remind_at"][11:16]
            return f"⏰{hm} {rem['content']}"[:30]

        upcoming = conn.execute(
            "SELECT content, event_date FROM memories WHERE event_date IS NOT NULL AND event_date >= ? ORDER BY event_date ASC, id DESC LIMIT 1",
            (today_str,),
        ).fetchone()
        if upcoming:
            ev = upcoming["event_date"]
            txt = upcoming["content"]
            try:
                ev_d = datetime.strptime(ev, "%Y-%m-%d").date()
                delta = (ev_d - today).days
                if delta == 0:
                    prefix = "今日:"
                elif delta == 1:
                    prefix = "明日:"
                else:
                    prefix = f"{ev_d.month}/{ev_d.day}:"
                return f"{prefix}{txt}"[:30]
            except Exception:
                return txt[:28]

        latest_note = conn.execute(
            "SELECT content FROM memories WHERE event_date IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest_note:
            return latest_note["content"][:28]
    return "暂无待办备忘"

def sync_memo_sensor_async():
    def _push():
        push_ha_sensor("sensor.ai_legion_memo", get_best_memo_for_screen(), "AI Legion Latest Memo")
    threading.Thread(target=_push, daemon=True).start()

def add_memory(content: str, category: str = "随手记", event_date: str = None, remind_at: str = None) -> Dict[str, Any]:
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    rem = remind_at or resolve_reminder_time(content, now)
    ev = event_date or (rem[:10] if rem else resolve_relative_date(content, now))
    if rem:
        category = "定时提醒"
    elif ev and category == "随手记":
        category = "待办提醒"
    with get_db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories (category, content, created_at, event_date, remind_at, reminded) VALUES (?, ?, ?, ?, ?, 0)",
            (category, content.strip(), now_str, ev, rem),
        )
        mid = cur.lastrowid
        conn.commit()
    sync_memo_sensor_async()
    return {"id": mid, "category": category, "content": content.strip(), "created_at": now_str, "event_date": ev, "remind_at": rem, "reminded": 0}

def delete_memory(mid: int) -> bool:
    with get_db_conn() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        sync_memo_sensor_async()
    return deleted

def delete_memory_by_voice_keyword(query: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """根据语音口令中的关键词匹配并删除对应随手记/待办"""
    all_mems = list_memories(100)
    if not all_mems:
        return False, None
    # 剥离删除操作套话，提取核心事项词
    core = re.sub(
        r"(请|帮我|把|那个|那条|关于|的|备忘录?|随手记|待办|提醒|记录|日程|事项|事儿?|给?删掉|删除|移除|取消|清掉|清除|销掉|已经|办完了?|做完了?|干完了?|搞定了?|完成了?|装好了?|弄好了?|吧|了|啊|呢|[，,。！!？?\s])",
        "",
        query,
    )
    if not core:
        return False, None
    tokens = [t for t in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}", core) if t]
    best_score = 0
    best_item = None
    for m in all_mems:
        score = 0
        txt = m["content"]
        if core in txt:
            score += 12
        for tk in tokens:
            if tk in txt:
                score += len(tk) * 2
        if score > best_score:
            best_score = score
            best_item = m
    if best_item and best_score >= 2:
        ok = delete_memory(best_item["id"])
        return ok, best_item
    return False, None

def format_memory_for_llm(m: Dict[str, Any], today: date) -> str:
    ev = m.get("event_date")
    if ev:
        try:
            ev_d = datetime.strptime(ev, "%Y-%m-%d").date()
            delta = (ev_d - today).days
            wd = CN_WEEKDAY_NAMES[ev_d.weekday()]
            if delta < 0:
                status = f"已过期{-delta}天"
            elif delta == 0:
                status = "就是今天"
            elif delta == 1:
                status = "明天"
            else:
                status = f"还有{delta}天"
            return f"- [日程日期: {ev} {wd} ({status}) | 分类: {m['category']}] {m['content']}"
        except Exception:
            pass
    return f"- [长期随手记/无特定日期 | 分类: {m['category']}] {m['content']}"

def build_grounding_memories_snippet(limit: int = 8) -> str:
    """为所有对话提供精简、真实的本地随手记锚点，彻底杜绝小模型凭空瞎编用户日程"""
    mems = list_memories(limit)
    if not mems:
        return "（当前本地随手记为空，用户没有任何已登记的待办、活动或备忘）"
    today = datetime.now().date()
    return "\n".join([format_memory_for_llm(m, today) for m in mems])

def search_memories(query: str, top_k: int = 6) -> Dict[str, Any]:
    """支持时间窗口精确过滤（今天/明天/后天/周末/这周/下周/近期）+ 关键词核验"""
    all_mems = list_memories(100)
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    # 若用户在核验特定事实（如“我有说过今天要跟妈妈吃饭吗？”、“看下我随手记的内容”），直接返回全量有效记录供精确比对
    is_fact_check = any(w in query for w in ("有说过", "说过", "记过", "随手记的内容", "所有备忘", "全部备忘", "有哪些备忘"))
    if is_fact_check:
        return {"mode": "fact_check", "window_label": "全部本地随手记", "items": all_mems[:max(top_k, 10)]}

    window_label = None
    win_start, win_end = None, None
    if any(w in query for w in ("今天", "今日", "今晚", "今早")):
        window_label = f"今天({today.strftime('%Y-%m-%d')})"
        win_start, win_end = today, today
    elif "大后天" in query:
        d3 = today + timedelta(days=3)
        window_label = f"大后天({d3.strftime('%Y-%m-%d')})"
        win_start, win_end = d3, d3
    elif "后天" in query:
        d2 = today + timedelta(days=2)
        window_label = f"后天({d2.strftime('%Y-%m-%d')})"
        win_start, win_end = d2, d2
    elif any(w in query for w in ("明天", "明日", "明早", "明晚")):
        tmr = today + timedelta(days=1)
        window_label = f"明天({tmr.strftime('%Y-%m-%d')})"
        win_start, win_end = tmr, tmr
    elif "下周末" in query or "下个周末" in query:
        nsat = monday + timedelta(days=12)
        nsun = monday + timedelta(days=13)
        window_label = f"下周末({nsat.strftime('%m-%d')}至{nsun.strftime('%m-%d')})"
        win_start, win_end = nsat, nsun
    elif "周末" in query:
        sat = monday + timedelta(days=5)
        window_label = f"本周末({sat.strftime('%m-%d')}至{sunday.strftime('%m-%d')})"
        win_start, win_end = sat, sunday
    elif any(w in query for w in ("这周", "本周", "这星期", "这个星期")):
        window_label = f"本周({monday.strftime('%m-%d')}至{sunday.strftime('%m-%d')})"
        win_start, win_end = monday, sunday
    elif any(w in query for w in ("下周", "下星期", "下个星期")):
        nm = monday + timedelta(days=7)
        ns = nm + timedelta(days=6)
        window_label = f"下周({nm.strftime('%m-%d')}至{ns.strftime('%m-%d')})"
        win_start, win_end = nm, ns
    elif any(w in query for w in ("最近", "近期", "接下来", "待办", "日程", "安排", "要做的事", "什么事", "什么活", "活动")):
        window_label = f"近期({today.strftime('%m-%d')}起14天内)"
        win_start, win_end = today, today + timedelta(days=14)

    if win_start and win_end:
        in_window = []
        for m in all_mems:
            ev = m.get("event_date")
            if ev:
                try:
                    ev_d = datetime.strptime(ev, "%Y-%m-%d").date()
                    if win_start <= ev_d <= win_end:
                        in_window.append(m)
                except Exception:
                    pass
        return {"mode": "time_window", "window_label": window_label, "items": in_window[:top_k]}

    clean_q = re.sub(r"[我的你在哪了呢吗呀啊？?！!，,。.请问帮查一下之前记过备忘随手记]", "", query)
    tokens = [t for t in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}", clean_q) if t]
    scored = []
    for m in all_mems:
        score = 0
        txt = m["content"] + " " + m["category"]
        if clean_q and clean_q in txt:
            score += 10
        for tk in tokens:
            if tk in txt:
                score += len(tk)
        if score > 0:
            scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    items = [x[1] for x in scored[:top_k]] if scored else all_mems[:min(5, len(all_mems))]
    return {"mode": "keyword", "window_label": None, "items": items}

# ── 真实模型注册表（只扫描磁盘真实存在的 GGUF） ─────────────────────────────────
MODEL_CATALOG = {
    "qwen3-4b": {
        "id": "qwen3-4b",
        "filename": "Qwen3-4B-Q4_K_M.gguf",
        "name": "Qwen3-4B (Q4_K_M)",
        "role": "主力双模大脑 · /no_think 极速控家 + think 深度推理",
        "family": "qwen3",
        "params": "4.0B · 32K Ctx · Q4_K_M",
        "supports_think": True,
    },
    "phi-4-mini": {
        "id": "phi-4-mini",
        "filename": "Phi-4-mini-instruct-Q4_K_M.gguf",
        "name": "Phi-4-mini-instruct (3.8B)",
        "role": "备选逻辑节点 · 微软轻量化指令模型",
        "family": "phi3",
        "params": "3.8B · 128K Ctx · Q4_K_M",
        "supports_think": False,
    },
    "deepseek-r1-1.5b": {
        "id": "deepseek-r1-1.5b",
        "filename": "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
        "name": "DeepSeek-R1-Distill-1.5B",
        "role": "按需推理节点 · 轻量 CoT 推演",
        "family": "qwen2",
        "params": "1.5B · CoT · Q4_K_M",
        "supports_think": True,
    },
}

LOADED_LLMS: Dict[str, Any] = {}
MODEL_STATS: Dict[str, Dict[str, Any]] = {}
LLM_LOCK = threading.Lock()
ACTIVE_PRIMARY_ID = "qwen3-4b"
TELEMETRY_LOGS: List[Dict[str, Any]] = []
PENDING_TIMERS: List[Dict[str, Any]] = []
TIMERS_LOCK = threading.Lock()

def get_default_primary_id() -> str:
    global ACTIVE_PRIMARY_ID
    if os.path.exists(os.path.join(MODELS_DIR, MODEL_CATALOG[ACTIVE_PRIMARY_ID]["filename"])):
        return ACTIVE_PRIMARY_ID
    for mid, meta in MODEL_CATALOG.items():
        if os.path.exists(os.path.join(MODELS_DIR, meta["filename"])):
            ACTIVE_PRIMARY_ID = mid
            return mid
    return "qwen3-4b"

def ensure_model_stats(mid: str):
    if mid not in MODEL_STATS:
        MODEL_STATS[mid] = {
            "call_count": 0,
            "last_active": None,
            "last_latency_ms": 0.0,
            "state": "READY",
        }

def load_local_llm(model_id: str):
    """线程安全懒加载本地 GGUF 模型"""
    if model_id not in MODEL_CATALOG:
        model_id = get_default_primary_id()
    meta = MODEL_CATALOG[model_id]
    path = os.path.join(MODELS_DIR, meta["filename"])
    if not os.path.exists(path):
        return None
    if model_id in LOADED_LLMS:
        return LOADED_LLMS[model_id]
    from llama_cpp import Llama
    print(f"[AI-Legion] Loading GGUF model {meta['name']} from {path} (CPU Fast n_ctx=1024 + Q8_0 KV) ...")
    try:
        llm = Llama(
            model_path=path,
            n_ctx=1024,
            n_threads=N_THREADS,
            n_threads_batch=8,
            n_batch=256,
            flash_attn=False,
            type_k=8,
            type_v=8,
            use_mmap=True,
            use_mlock=False,
            verbose=False,
        )
    except Exception as e:
        print(f"[AI-Legion] Q8 KV fallback ({e}), loading default...")
        llm = Llama(
            model_path=path,
            n_ctx=1024,
            n_threads=N_THREADS,
            n_threads_batch=8,
            n_batch=256,
            verbose=False,
        )
    LOADED_LLMS[model_id] = llm
    ensure_model_stats(model_id)
    return llm

def unload_local_llm(model_id: str) -> bool:
    with LLM_LOCK:
        if model_id in LOADED_LLMS:
            del LOADED_LLMS[model_id]
            import gc
            gc.collect()
            return True
    return False

def record_telemetry(source: str, model_id: str, task_name: str, input_text: str, output_text: str, latency_ms: float):
    entry = {
        "id": int(time.time() * 1000) % 1000000,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "source": source,
        "model_id": model_id,
        "model_name": MODEL_CATALOG.get(model_id, {}).get("name", model_id),
        "task_name": task_name,
        "input_preview": (input_text[:60] + "...") if len(input_text) > 60 else input_text,
        "output_preview": (output_text[:160] + "...") if len(output_text) > 160 else output_text,
        "full_output": output_text,
        "latency_ms": round(latency_ms, 1),
    }
    TELEMETRY_LOGS.insert(0, entry)
    if len(TELEMETRY_LOGS) > 40:
        TELEMETRY_LOGS.pop()
    ensure_model_stats(model_id)
    MODEL_STATS[model_id]["call_count"] += 1
    MODEL_STATS[model_id]["last_active"] = entry["timestamp"]
    MODEL_STATS[model_id]["last_latency_ms"] = entry["latency_ms"]

# ── 实时端口探活（SenseVoice / Whisper / Piper / HA） ──────────────────────────
def check_tcp_port(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def get_real_pipeline_services() -> List[Dict[str, Any]]:
    services = [
        {"id": "wyoming-asr", "name": "SenseVoice / Whisper ASR", "role": "离线中文语音转写 (ASR)", "port": 10300, "match": "wyoming_sensevoice|wyoming_faster_whisper"},
        {"id": "wyoming-piper", "name": "Piper TTS (zh_CN-huayan)", "role": "离线中文语音合成 (TTS)", "port": 10200, "match": "wyoming_piper"},
        {"id": "homeassistant", "name": "Home Assistant Core", "role": "全屋设备总线与语音中枢", "port": 8123, "match": "homeassistant"},
    ]
    proc_mem = {}
    proc_engine = {}
    for p in psutil.process_iter(["cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            for s in services:
                for kw in s["match"].split("|"):
                    if kw in cmd:
                        proc_mem[s["id"]] = round(p.info["memory_info"].rss / (1024 * 1024), 1)
                        if kw == "wyoming_sensevoice":
                            proc_engine[s["id"]] = "SenseVoiceSmall (INT8 · 100ms)"
                        elif kw == "wyoming_faster_whisper":
                            proc_engine[s["id"]] = "Faster-Whisper (small-int8)"
        except Exception:
            pass
    out = []
    for s in services:
        alive = check_tcp_port("127.0.0.1", s["port"])
        out.append({
            "id": s["id"],
            "name": proc_engine.get(s["id"], s["name"]),
            "role": s["role"],
            "port": s["port"],
            "online": alive,
            "ram_mb": proc_mem.get(s["id"], 0.0),
        })
    return out

# ── Home Assistant 实体缓存、设备别名清洗与极速匹配 ────────────────────────────
HA_CACHE = {"ts": 0.0, "states": []}

def get_ha_states_cached(force: bool = False) -> List[Dict[str, Any]]:
    if not HA_TOKEN:
        return []
    now = time.time()
    if not force and (now - HA_CACHE["ts"] < 5.0) and HA_CACHE["states"]:
        return HA_CACHE["states"]
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}"}
        resp = httpx.get(f"{HA_URL}/api/states", headers=headers, timeout=3.0)
        if resp.status_code == 200:
            HA_CACHE["states"] = resp.json()
            HA_CACHE["ts"] = now
            return HA_CACHE["states"]
    except Exception as e:
        print(f"[HA] Fetch error: {e}")
    return HA_CACHE["states"]

def get_controllable_devices(force: bool = False) -> List[Dict[str, str]]:
    states = get_ha_states_cached(force=force)
    devices = []
    for s in states:
        eid = s.get("entity_id", "")
        if any(eid.startswith(p) for p in ("light.", "fan.", "switch.", "climate.", "cover.")):
            if "cardputer_screen_backlight" in eid:
                continue
            devices.append({
                "id": eid,
                "name": s.get("attributes", {}).get("friendly_name", eid),
                "state": s.get("state", "unknown"),
            })
    return devices

AUXILIARY_SWITCH_WORDS = ("指示灯", "凌动开关", "物理控制锁", "童锁", "提示音", "自定义服务", "开始向左转", "开始向右转", "麦克风", "闹钟")

def extract_device_aliases(dev: Dict[str, str]) -> List[str]:
    """将米家/HA 带双空格后缀的设备名（如 '大灯  主灯'、'小米be6500  指示灯'）拆解为自然口语可命中的别名集合"""
    raw_name = dev["name"].strip()
    is_indicator = "指示灯" in raw_name
    aliases = {raw_name, re.sub(r"\s+", "", raw_name)}
    parts = [p.strip() for p in re.split(r"\s{2,}|\*", raw_name) if p.strip()]
    if parts and not is_indicator:
        head = parts[0]
        if len(head) >= 2:
            aliases.add(head)
        no_brand = re.sub(r"^[A-Za-z0-9_\-\s]+", "", head).strip()
        if len(no_brand) >= 2:
            aliases.add(no_brand)
    if is_indicator and ("小米" in raw_name or "be6500" in raw_name.lower()):
        aliases.update(["小米指示灯", "路由器指示灯", "指示灯"])
    if ("落地扇" in raw_name and not is_indicator) or dev["id"].startswith("fan."):
        aliases.update(["落地扇", "风扇", "电风扇"])
    if "吸顶灯" in raw_name and dev["id"].startswith("light.") and not is_indicator:
        aliases.add("吸顶灯")
    return [a for a in aliases if len(a) >= 2]

def match_devices_in_query(query: str, devices: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """匹配口语中的目标设备：默认排除童锁/凌动开关等附属子开关，且在线主设备优先"""
    want_aux = any(w in query for w in AUXILIARY_SWITCH_WORDS)
    exact_candidates = []
    generic_candidates = []
    q_compact = re.sub(r"\s+", "", query)

    for d in devices:
        is_aux = any(w in d["name"] for w in AUXILIARY_SWITCH_WORDS)
        if is_aux and not want_aux:
            continue
        aliases = extract_device_aliases(d)
        best_len = 0
        for al in aliases:
            al_c = re.sub(r"\s+", "", al)
            if al_c and al_c in q_compact:
                best_len = max(best_len, len(al_c))
        if best_len >= 3:
            exact_candidates.append((best_len, d))
        elif best_len == 2:
            generic_candidates.append((best_len, d))

    pool = exact_candidates if exact_candidates else generic_candidates
    if not pool:
        return []
    # 如果池中既有高特异性匹配又有泛化词匹配，取最高匹配长度；并在同分下优先选择在线（非 unavailable）设备
    max_l = max(x[0] for x in pool)
    top_devs = [x[1] for x in pool if x[0] >= max_l]
    online_devs = [d for d in top_devs if d.get("state") not in ("unavailable", "unknown")]
    return online_devs if online_devs else top_devs

def get_router_speeds() -> Dict[str, float]:
    states = get_ha_states_cached()
    down_bps, up_bps = 0.0, 0.0
    for s in states:
        eid = s.get("entity_id", "")
        if "download_speed" in eid:
            try:
                down_bps = float(s.get("state", 0))
            except Exception:
                pass
        elif "upload_speed" in eid:
            try:
                up_bps = float(s.get("state", 0))
            except Exception:
                pass
    return {"down_kb": round(down_bps / 1024.0, 1), "up_kb": round(up_bps / 1024.0, 1)}

def execute_ha_service(domain: str, service: str, entity_id: str, extra_data: dict = None):
    if not HA_TOKEN:
        return False, "HA_TOKEN not configured"
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        payload = {"entity_id": entity_id}
        if extra_data:
            payload.update(extra_data)
        resp = httpx.post(f"{HA_URL}/api/services/{domain}/{service}", headers=headers, json=payload, timeout=4.0)
        return resp.status_code == 200, resp.text[:120]
    except Exception as e:
        return False, str(e)

def push_ha_sensor(entity_id: str, state: Any, friendly_name: str, unit: str = None):
    if not HA_TOKEN:
        return
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        attrs = {"friendly_name": friendly_name}
        if unit:
            attrs["unit_of_measurement"] = unit
        httpx.post(
            f"{HA_URL}/api/states/{entity_id}",
            headers=headers,
            json={"state": str(state), "attributes": attrs},
            timeout=2.5,
        )
    except Exception:
        pass

def notify_cardputer_alert(title: str, message: str):
    """反向主动唤醒 Cardputer 屏幕并弹出警报卡片"""
    if not HA_TOKEN:
        return False, "HA_TOKEN not configured"
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        resp = httpx.post(
            f"{HA_URL}/api/services/esphome/m5cardputer_notify_alert",
            headers=headers,
            json={"title": title[:24], "message": message[:60]},
            timeout=3.0,
        )
        return resp.status_code == 200, resp.text[:120]
    except Exception as e:
        return False, str(e)

def announce_cardputer_voice(message: str):
    """通过 HA assist_satellite.announce 主动唤醒 Cardputer 开口语音播报提醒"""
    if not HA_TOKEN:
        return False, "HA_TOKEN not configured"
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        resp = httpx.post(
            f"{HA_URL}/api/services/assist_satellite/announce",
            headers=headers,
            json={
                "entity_id": os.getenv("CARDPUTER_SATELLITE_ID", "assist_satellite.m5cardputer_assist_satellite"),
                "message": message[:120],
            },
            timeout=6.0,
        )
        return resp.status_code == 200, resp.text[:120]
    except Exception as e:
        return False, str(e)

def reminder_watcher():
    """后台定时提醒守夜线程（每 5 秒检查一次 SQLite，到点自动亮屏弹窗 + 主动语音播报）"""
    while True:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            due_rows = []
            with get_db_conn() as conn:
                rows = conn.execute(
                    "SELECT id, content, remind_at FROM memories WHERE remind_at IS NOT NULL AND reminded = 0 AND remind_at <= ?",
                    (now_str,),
                ).fetchall()
                for r in rows:
                    due_rows.append(dict(r))
                    conn.execute("UPDATE memories SET reminded = 1 WHERE id = ?", (r["id"],))
                if due_rows:
                    conn.commit()
            for item in due_rows:
                msg = item["content"]
                notify_cardputer_alert("到点提醒", msg)
                announce_cardputer_voice(f"提醒时间到了，{msg}")
                record_telemetry("定时提醒引擎", "Cardputer-Adv", "到点亮屏+语音提醒", item["remind_at"], msg, 15.0)
                sync_memo_sensor_async()
        except Exception as e:
            print(f"[Reminder] watcher error: {e}")
        time.sleep(5)

threading.Thread(target=reminder_watcher, daemon=True).start()

# ── 后台定时推送边缘主机算力指标与最优待办到 HA + 异常主动守夜 ───────────────────
def ha_telemetry_pusher():
    last_alert_ts = 0.0
    while True:
        try:
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=1.0)
            primary = get_default_primary_id()
            best_memo = get_best_memo_for_screen()
            push_ha_sensor("sensor.ai_legion_cpu", round(cpu, 1), "AI Legion CPU", "%")
            push_ha_sensor("sensor.ai_legion_ram", round(mem.percent, 1), "AI Legion RAM", "%")
            push_ha_sensor("sensor.ai_legion_model", MODEL_CATALOG[primary]["name"], "AI Legion Active Model")
            push_ha_sensor("sensor.ai_legion_memo", best_memo, "AI Legion Latest Memo")

            now = time.time()
            if now - last_alert_ts > 600:
                if mem.percent > 90.0:
                    notify_cardputer_alert("主机内存告警", f"{HOST_NAME} 内存占用已达 {mem.percent:.0f}%")
                    last_alert_ts = now
                elif cpu > 92.0:
                    notify_cardputer_alert("主机算力过载", f"{HOST_NAME} CPU 持续负载 {cpu:.0f}%")
                    last_alert_ts = now
        except Exception:
            pass
        time.sleep(15)

threading.Thread(target=ha_telemetry_pusher, daemon=True).start()

# ── 按终端来源隔离的多轮对话滑动窗口缓存（3 分钟有效，互不串台） ─────────────────
RECENT_DIALOG_BUCKETS: Dict[str, List[Dict[str, Any]]] = {}

def get_recent_history(source: str, incoming_msgs: Optional[List[Dict[str, str]]] = None, max_turns: int = 6) -> List[Dict[str, str]]:
    now = time.time()
    bucket = RECENT_DIALOG_BUCKETS.setdefault(source or "default", [])
    while bucket and (now - bucket[0]["ts"] > 180):
        bucket.pop(0)
    if incoming_msgs:
        filtered = [
            {"role": m["role"], "content": m["content"]}
            for m in incoming_msgs[:-1]
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if filtered:
            return filtered[-max_turns:]
    return [{"role": x["role"], "content": x["content"]} for x in bucket[-max_turns:]]

def append_recent_turn(source: str, user_q: str, assistant_a: str):
    now = time.time()
    bucket = RECENT_DIALOG_BUCKETS.setdefault(source or "default", [])
    if user_q:
        bucket.append({"role": "user", "content": user_q, "ts": now})
    if assistant_a:
        bucket.append({"role": "assistant", "content": assistant_a, "ts": now})
    while len(bucket) > 10:
        bucket.pop(0)

def clear_recent_history(source: str = None):
    if source and source in RECENT_DIALOG_BUCKETS:
        RECENT_DIALOG_BUCKETS[source].clear()
    else:
        RECENT_DIALOG_BUCKETS.clear()

def warmup_primary_model():
    if OLLAMA_URL:
        return
    time.sleep(1.0)
    try:
        pid = get_default_primary_id()
        with LLM_LOCK:
            llm = load_local_llm(pid)
            if llm is not None:
                llm.create_chat_completion(
                    messages=[{"role": "user", "content": "你好 /no_think"}],
                    max_tokens=4,
                    temperature=0.0,
                )
                print(f"[AI-Legion] Warmup completed for {pid}, model resident in RAM!")
    except Exception as e:
        print(f"[AI-Legion] Warmup warning: {e}")

threading.Thread(target=warmup_primary_model, daemon=True).start()

# ── 统一 LLM 推理封装（带线程锁 + Qwen3 双模 /no_think 支持） ────────────────────
def run_llm_sync(
    system_prompt: str,
    user_prompt: str,
    model_id: str = None,
    enable_thinking: bool = False,
    max_tokens: int = 220,
    temperature: float = 0.2,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    target_id = model_id if (model_id in MODEL_CATALOG) else get_default_primary_id()
    start_t = time.time()
    hist = history or []

    if OLLAMA_URL:
        try:
            user_msg = user_prompt
            if not enable_thinking and "qwen3" in target_id:
                user_msg = user_prompt + " /no_think"
            payload = {
                "model": os.getenv("DEFAULT_MODEL", "qwen3:4b"),
                "messages": [{"role": "system", "content": system_prompt}] + hist + [{"role": "user", "content": user_msg}],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
            resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=25.0)
            if resp.status_code == 200:
                raw = resp.json().get("message", {}).get("content", "").strip()
                clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip() if not enable_thinking else raw
                return {"text": clean, "raw": raw, "model_id": target_id, "latency_ms": (time.time() - start_t) * 1000}
        except Exception as e:
            return {"text": f"Ollama 服务调用异常: {e}", "raw": "", "model_id": target_id, "latency_ms": 0}

    with LLM_LOCK:
        ensure_model_stats(target_id)
        MODEL_STATS[target_id]["state"] = "THINKING" if enable_thinking else "ACTIVE"
        try:
            llm = load_local_llm(target_id)
            if llm is None:
                return {"text": "本地模型权重尚未就绪，请稍候。", "raw": "", "model_id": target_id, "latency_ms": 0}

            meta = MODEL_CATALOG[target_id]
            user_msg = user_prompt
            if meta["family"] == "qwen3":
                user_msg = f"{user_prompt}\n/think" if enable_thinking else f"{user_prompt}\n/no_think"
            full_msgs = [{"role": "system", "content": system_prompt}] + hist + [{"role": "user", "content": user_msg}]
            resp = llm.create_chat_completion(
                messages=full_msgs,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            raw = resp["choices"][0]["message"]["content"].strip()

            clean = raw
            if not enable_thinking:
                clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL).strip()
            clean = re.sub(r"```.*?```", "", clean, flags=re.DOTALL).strip()
            return {
                "text": clean,
                "raw": raw,
                "model_id": target_id,
                "latency_ms": (time.time() - start_t) * 1000,
            }
        finally:
            MODEL_STATS[target_id]["state"] = "READY"

# ── 可取消的异步后台时序调度（接入 PENDING_TIMERS，支持随时语音喊停） ────────────
def schedule_async_actions(actions: List[Dict[str, Any]], desc: str = "延时家居任务") -> int:
    cancel_ev = threading.Event()
    tid = int(time.time() * 1000) % 1000000
    entry = {
        "id": tid,
        "desc": desc,
        "created_at": datetime.now().strftime("%H:%M:%S"),
        "cancel_event": cancel_ev,
    }
    with TIMERS_LOCK:
        PENDING_TIMERS.append(entry)

    def worker():
        try:
            for act in actions:
                if cancel_ev.is_set():
                    break
                srv = act.get("service", "")
                eid = act.get("entity_id", "")
                if srv == "delay":
                    secs = min(float(act.get("seconds", 5)), 3600.0)
                    if cancel_ev.wait(timeout=secs):
                        break
                elif "." in srv and eid:
                    d, s = srv.split(".", 1)
                    execute_ha_service(d, s, eid)
        finally:
            with TIMERS_LOCK:
                if entry in PENDING_TIMERS:
                    PENDING_TIMERS.remove(entry)

    threading.Thread(target=worker, daemon=True).start()
    return tid

def cancel_all_pending_timers() -> int:
    with TIMERS_LOCK:
        cnt = len(PENDING_TIMERS)
        for t in PENDING_TIMERS:
            t["cancel_event"].set()
        PENDING_TIMERS.clear()
        return cnt

# ── 统一智能体路由管线（L0 规则 -> L1 第二大脑读写销 -> L2 家电控制 -> L3 锚定对话） ─
def get_real_ha_weather() -> Optional[str]:
    for s in get_ha_states_cached():
        eid = s.get("entity_id", "")
        if eid.startswith("weather.") and s.get("state") not in ("unknown", "unavailable", ""):
            attrs = s.get("attributes", {})
            temp = attrs.get("temperature")
            hum = attrs.get("humidity")
            cond = s.get("state")
            parts = [f"天气状态 {cond}"]
            if temp is not None:
                parts.append(f"气温 {temp} 度")
            if hum is not None:
                parts.append(f"湿度百分之 {hum}")
            return "，".join(parts)
    return None

def is_memory_delete_intent(q: str) -> bool:
    has_del_verb = any(w in q for w in ("删掉", "删除", "移除", "清掉", "清除", "销掉", "取消"))
    has_mem_noun = any(w in q for w in ("备忘", "随手记", "待办", "提醒", "记录", "日程", "事项"))
    if has_del_verb and has_mem_noun:
        return True
    if any(w in q for w in ("办完了", "做完了", "干完了", "搞定了", "装好了")) and any(w in q for w in ("删", "销", "备忘", "随手记", "待办", "事")):
        return True
    return False

def is_memory_query_intent(q: str) -> bool:
    explicit_triggers = (
        "放哪", "在哪放", "停在哪", "记过", "记了什么", "有什么记录", "备忘", "待办",
        "随手记", "笔记", "清单", "什么事情", "什么事", "啥事", "要做的事",
        "什么活", "啥活", "活要干", "有活吗", "有什么活动", "啥活动",
        "安排", "日程", "计划", "提醒", "任务", "密码", "账号", "有说过"
    )
    if any(k in q for k in explicit_triggers):
        return True
    # 时间词 + 动作/事项询问句式（如“今天我要干啥”、“周末有啥要忙的”）
    has_time = any(t in q for t in ("今天", "今日", "今晚", "明天", "明日", "后天", "大后天", "周末", "这周", "本周", "下周", "最近", "近期", "接下来"))
    has_act = any(a in q for a in ("干啥", "干什么", "做啥", "做什么", "忙啥", "忙什么", "有什么要", "有啥要", "有课吗", "有会吗"))
    return has_time and has_act

def handle_voice_query_sync(
    query: str,
    source: str = "Voice",
    preferred_model: str = None,
    force_think: bool = False,
    incoming_messages: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    start_t = time.time()
    q = normalize_voice_text(query or "")
    q = re.sub(r"^[^\w\u4e00-\u9fff]+|[^\w\u4e00-\u9fff？?！!。，,]+$", "", q.strip())
    if not re.sub(r"[^\w\u4e00-\u9fff]+", "", q):
        return {"voice_summary": "没听清，请再说一遍。", "result": "空语音已忽略", "model_id": "L0-Guard", "latency_ms": 0.1}
    primary = preferred_model if (preferred_model in MODEL_CATALOG) else get_default_primary_id()

    # ===== L0-Reset：重置对话上下文 / 取消后台延时任务 =====
    if any(k in q for k in ("清空上下文", "重置对话", "清空对话记忆", "重新开始对话")):
        clear_recent_history(source)
        reply = "好的，已清空之前的对话上下文。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L0-Rule", "清空会话上下文", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}

    if any(k in q for k in ("取消定时", "取消延时", "停止倒计时", "取消倒计时")):
        cnt = cancel_all_pending_timers()
        reply = f"好的，已为你取消 {cnt} 个正在倒计时的家居任务。" if cnt > 0 else "当前没有正在倒计时的延时任务。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L0-Rule", "取消异步延时任务", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}

    # ===== L0-Weather：室外天气与气温诚实查询 =====
    if any(k in q for k in ("天气", "气温", "下雨", "下雪", "刮风", "多少度")) and not any(d in q for d in ("空调", "调到", "设为")):
        w_info = get_real_ha_weather()
        if w_info:
            reply = f"根据家庭气象实体数据，当前{w_info}。"
        else:
            reply = "当前处于纯离线模式，Home Assistant 尚未接入室外气象或温度传感器，无法查询实时天气。"
        lat = (time.time() - start_t) * 1000
        append_recent_turn(source, q, reply)
        record_telemetry(source, "L0-Weather", "离线气象诚实核验", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Weather", "latency_ms": lat}

    # ===== L0-A：极速时间查询 (<1ms) =====
    if any(k in q for k in ("现在几点", "几点了", "几号", "星期几", "周几")) and len(q) <= 14:
        now = datetime.now()
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        reply = f"现在是{now.month}月{now.day}日星期{weekdays[now.weekday()]}，{now.hour}点{now.minute:02d}分。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L0-Rule", "极速时间查询", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}

    # ===== L0-B：HomeLab 服务器与网络查房 (<15ms) =====
    if any(k in q for k in ("服务器状态", "服务器怎么样", "主机状态", "内存占用", "网速", "集群状态", "算力状态")):
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.1)
        spd = get_router_speeds()
        reply = f"主机 {HOST_NAME} 运行正常，CPU 占用百分之{int(cpu)}，内存使用百分之{int(mem.percent)}，当前主力模型为 {MODEL_CATALOG[primary]['name']}，路由器下载 {spd['down_kb']} KB每秒。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L0-Ops", "HomeLab 语音查房", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Ops", "latency_ms": lat}

    # ===== L1-A：离线“家庭第二大脑”写入 & 到点主动亮屏/语音提醒 =====
    # 1) 精确秒级倒计时提醒（如“30秒后提醒我关火”）
    sec_rem_m = re.search(r"([0-9一二两三四五六七八九十]{1,3})\s*秒钟?(?:后|之后|以后)?.*?(?:提醒我|叫我|喊我)[：:，,\s]*(.+)$", q)
    if sec_rem_m:
        secs = parse_cn_int(sec_rem_m.group(1)) or 10
        rem_content = sec_rem_m.group(2).strip("。！!？? ")
        def _sec_alert(s=secs, msg=rem_content):
            ev = threading.Event()
            tid = int(time.time() * 1000) % 1000000
            entry = {"id": tid, "desc": f"定时提醒:{msg[:18]}", "created_at": datetime.now().strftime("%H:%M:%S"), "cancel_event": ev}
            with TIMERS_LOCK:
                PENDING_TIMERS.append(entry)
            try:
                if not ev.wait(timeout=min(float(s), 3600.0)):
                    notify_cardputer_alert("到点提醒", msg)
                    announce_cardputer_voice(f"提醒时间到了，{msg}")
                    record_telemetry("定时提醒引擎", "Cardputer-Adv", "秒级倒计时提醒", f"{s}秒后", msg, 15.0)
            finally:
                with TIMERS_LOCK:
                    if entry in PENDING_TIMERS:
                        PENDING_TIMERS.remove(entry)
        threading.Thread(target=_sec_alert, daemon=True).start()
        reply = f"好的，已设定 {secs} 秒后主动亮屏并语音提醒你：{rem_content}"
        lat = (time.time() - start_t) * 1000
        append_recent_turn(source, q, reply)
        record_telemetry(source, "L1-Reminder", "秒级定时提醒设定", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L1-Reminder", "latency_ms": lat}

    # 2) 前置时间定时提醒（如“15分钟后提醒我关火” / “明天上午9点提醒我开会”）
    pre_rem_m = re.match(r"^(?:请)?(?:帮我)?(.+?)(?:提醒我|叫我|通知我)[：:，,\s]*(.+)$", q)
    if pre_rem_m and not any(w in q for w in ("有什么提醒", "查一下提醒", "删掉", "删除", "取消")):
        rem_at = resolve_reminder_time(q)
        if rem_at:
            rem_content = pre_rem_m.group(2).strip("。！!？? ")
            item = add_memory(rem_content, category="定时提醒", remind_at=rem_at)
            dt_obj = datetime.strptime(rem_at, "%Y-%m-%d %H:%M")
            day_lbl = "今天" if dt_obj.date() == datetime.now().date() else ("明天" if dt_obj.date() == (datetime.now().date() + timedelta(days=1)) else f"{dt_obj.month}月{dt_obj.day}日")
            reply = f"好的，已设定{day_lbl} {dt_obj.hour}点{dt_obj.minute:02d}分 主动亮屏并语音提醒你：{rem_content}"
            lat = (time.time() - start_t) * 1000
            append_recent_turn(source, q, reply)
            record_telemetry(source, "L1-Reminder", f"持久定时提醒 ({rem_at})", q, reply, lat)
            return {"voice_summary": reply, "result": reply, "model_id": "L1-Reminder", "latency_ms": lat, "memory": item}

    # 3) 标准随手记写入（支持附带日期或精确时刻）
    mem_add_match = re.match(r"^(?:请)?(?:帮我)?(?:记一下|记住|备忘|记录一下|记入随手记|提醒我)[：:，,\s]*(.+)$", q)
    if mem_add_match:
        content = mem_add_match.group(1).strip()
        cat = "物品位置" if any(w in content for w in ("放在", "停在", "抽屉", "柜子", "车位")) else "随手记"
        item = add_memory(content, category=cat)
        if item.get("remind_at"):
            dt_obj = datetime.strptime(item["remind_at"], "%Y-%m-%d %H:%M")
            day_lbl = "今天" if dt_obj.date() == datetime.now().date() else f"{dt_obj.month}月{dt_obj.day}日"
            reply = f"好的，已设定{day_lbl} {dt_obj.hour}点{dt_obj.minute:02d}分 主动提醒：{content}"
        elif item.get("event_date"):
            ev_d = datetime.strptime(item["event_date"], "%Y-%m-%d").date()
            wd = CN_WEEKDAY_NAMES[ev_d.weekday()]
            reply = f"好的，已记入{ev_d.month}月{ev_d.day}日{wd}待办：{content}"
        else:
            reply = f"好的，已记入本地{item['category']}：{content}"
        lat = (time.time() - start_t) * 1000
        append_recent_turn(source, q, reply)
        record_telemetry(source, "L1-Memory", f"第二大脑写入 ({item['category']})", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L1-Memory", "latency_ms": lat, "memory": item}

    # ===== L1-Del：离线“家庭第二大脑”语音销账/删除备忘 =====
    if is_memory_delete_intent(q):
        ok, deleted_item = delete_memory_by_voice_keyword(q)
        if ok and deleted_item:
            reply = f"好的，已从随手记中移除：{deleted_item['content']}"
        else:
            reply = "在随手记里没有找到匹配的条目，请说出具体事项关键词。"
        lat = (time.time() - start_t) * 1000
        append_recent_turn(source, q, reply)
        record_telemetry(source, "L1-Memory", "第二大脑语音销账", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L1-Memory", "latency_ms": lat}

    # ===== L1-B：离线“家庭第二大脑”检索（全方位口语覆盖 + 严格事实核验） =====
    if is_memory_query_intent(q):
        today = datetime.now().date()
        wd_now = CN_WEEKDAY_NAMES[today.weekday()]
        sr = search_memories(q, top_k=8)
        hits = sr["items"]
        win_lbl = sr["window_label"]
        if not hits:
            if win_lbl:
                reply = f"查看了你的本地随手记，{win_lbl}没有安排任何事项。"
            else:
                reply = "查看了你的本地随手记，目前没有相关记录。"
            lat = (time.time() - start_t) * 1000
            append_recent_turn(source, q, reply)
            record_telemetry(source, "L1-Memory", "第二大脑精准查询", q, reply, lat)
            return {"voice_summary": reply, "result": reply, "model_id": "L1-Memory", "latency_ms": lat}

        ctx = "\n".join([format_memory_for_llm(m, today) for m in hits])
        res = run_llm_sync(
            system_prompt=(
                f"你是家庭离线记忆管家。今天是 {today.strftime('%Y年%m月%d日')} {wd_now}。\n"
                "【最高铁律】请严格、唯一地依据下方检索到的真实本地记录回答用户！"
                "记录里有什么就答什么；记录里没有的内容必须明确回答“随手记里没有这项记录”，严禁推测或编造任何事项！口语简短不超过45字。"
            ),
            user_prompt=f"【真实本地随手记记录（{win_lbl or '关键词检索'}）】\n{ctx}\n\n用户提问：{q}",
            model_id=primary,
            enable_thinking=False,
            max_tokens=100,
            temperature=0.05,
        )
        reply = res["text"] or f"随手记里有：{hits[0]['content']}"
        append_recent_turn(source, q, reply)
        record_telemetry(source, primary, f"第二大脑 RAG ({win_lbl or '关键词'})", q, reply, res["latency_ms"])
        return {"voice_summary": reply, "result": reply, "model_id": primary, "latency_ms": res["latency_ms"]}

    # ===== L2：智能家居设备控制（支持双空格米家设备名清洗 + 在线主设备毫秒直通） =====
    devices = get_controllable_devices()
    matched_devs = match_devices_in_query(q, devices)
    control_phrases = ("开灯", "关灯", "打开", "关闭", "关掉", "熄灭", "吸顶灯", "大灯", "风扇", "落地扇", "空调", "窗帘", "指示灯", "全屋设备", "家里设备")
    is_home_control = bool(matched_devs) or any(p in q for p in control_phrases)

    if is_home_control and devices:
        has_multi_target = any(w in q for w in ("所有", "全部", "全屋", "开着的", "关着的"))
        delay_m = re.search(r"([0-9一二两三四五六七八九十]{1,3})\s*(秒钟?|分钟?)", q)
        has_delay = bool(delay_m) or any(w in q for w in ("再", "然后", "之后"))

        if not has_multi_target and len(matched_devs) == 1:
            dev = matched_devs[0]
            clean_dev_name = re.sub(r"\s{2,}(?:主灯|风扇)$", "", dev["name"]).strip()
            clean_dev_name = re.sub(r"\s{2,}", "", clean_dev_name) or dev["name"]
            if dev.get("state") == "unavailable":
                reply = f"{clean_dev_name}当前处于离线断电状态，请检查物理开关。"
                lat = (time.time() - start_t) * 1000
                record_telemetry(source, "L0-Rule", "离线设备快速拦截", q, reply, lat)
                return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}

            domain = dev["id"].split(".")[0]
            if not has_delay:
                if any(w in q for w in ("打开", "开启", "启动", "开一下")) and not any(w in q for w in ("关", "熄")):
                    ok, _ = execute_ha_service(domain, "turn_on", dev["id"])
                    reply = f"好的，已为你打开{clean_dev_name}。" if ok else f"尝试打开{clean_dev_name}失败，请检查设备状态。"
                    lat = (time.time() - start_t) * 1000
                    record_telemetry(source, "L0-Rule", "极速单步开设备", q, reply, lat)
                    return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}
                if any(w in q for w in ("关闭", "关掉", "熄灭", "关一下", "关了")) and not any(w in q for w in ("打开", "开启")):
                    ok, _ = execute_ha_service(domain, "turn_off", dev["id"])
                    reply = f"好的，已为你关闭{clean_dev_name}。" if ok else f"尝试关闭{clean_dev_name}失败，请检查设备状态。"
                    lat = (time.time() - start_t) * 1000
                    record_telemetry(source, "L0-Rule", "极速单步关设备", q, reply, lat)
                    return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}
            elif delay_m:
                # L0 极速延时调度（支持“过30秒关闭XX”以及“把XX关闭，过3秒再打开”）
                num_val = parse_cn_int(delay_m.group(1)) or 5
                unit_str = delay_m.group(2)
                secs = num_val * 60 if "分" in unit_str else num_val
                before_delay, after_delay = q[:delay_m.start()], q[delay_m.end():]
                act1 = "turn_on" if any(w in before_delay for w in ("打开", "开启", "启动")) else ("turn_off" if any(w in before_delay for w in ("关闭", "关掉", "熄灭", "关了")) else None)
                act2 = "turn_on" if any(w in after_delay for w in ("打开", "开启", "启动")) else ("turn_off" if any(w in after_delay for w in ("关闭", "关掉", "熄灭", "关了")) else None)
                if act1 and act2:
                    execute_ha_service(domain, act1, dev["id"])
                    tid = schedule_async_actions([{"service": "delay", "seconds": secs}, {"service": f"{domain}.{act2}", "entity_id": dev["id"]}], desc=q[:30])
                    a1_cn = "打开" if act1 == "turn_on" else "关闭"
                    a2_cn = "打开" if act2 == "turn_on" else "关闭"
                    reply = f"好的，已先{a1_cn}{clean_dev_name}，将在{num_val}{unit_str[0]}后自动{a2_cn}。"
                    lat = (time.time() - start_t) * 1000
                    record_telemetry(source, "L0-Timer", f"极速往返延时 #{tid}", q, reply, lat)
                    return {"voice_summary": reply, "result": reply, "model_id": "L0-Timer", "latency_ms": lat}
                elif act2 and not act1:
                    tid = schedule_async_actions([{"service": "delay", "seconds": secs}, {"service": f"{domain}.{act2}", "entity_id": dev["id"]}], desc=q[:30])
                    a2_cn = "打开" if act2 == "turn_on" else "关闭"
                    reply = f"好的，已设定倒计时，将在{num_val}{unit_str[0]}后为你{a2_cn}{clean_dev_name}。"
                    lat = (time.time() - start_t) * 1000
                    record_telemetry(source, "L0-Timer", f"极速单步延时 #{tid}", q, reply, lat)
                    return {"voice_summary": reply, "result": reply, "model_id": "L0-Timer", "latency_ms": lat}

        # 复杂/多步/多设备指令 -> 用短编号 D1..Dn 映射替代超长米家实体 ID，提速 5 倍且零拼写错误
        want_aux = any(w in q for w in AUXILIARY_SWITCH_WORDS)
        filtered_devs = [d for d in devices if want_aux or not any(w in d["name"] for w in ("凌动开关", "物理控制锁", "童锁", "提示音", "自定义服务", "开始向左转", "开始向右转"))]
        if not filtered_devs:
            filtered_devs = devices
        short_map = {}
        dev_lines = []
        for i, d in enumerate(filtered_devs, 1):
            code = f"D{i}"
            short_map[code] = d["id"]
            short_map[d["id"]] = d["id"]
            dom = d["id"].split(".")[0]
            dev_lines.append(f"- {code} ({dom}) | 名称: {d['name']} | 状态: {d['state']}")
        dev_block = "\n".join(dev_lines)
        sys_prompt = f"""你是智能家居决策大脑。请根据设备表输出严格JSON，不要解释。
若目标设备为 unavailable，在 voice_summary 告知离线，sequence 留空 []。
【设备表】
{dev_block}
【JSON格式】
{{"voice_summary":"简短口语答复","sequence":[{{"service":"light.turn_off","entity_id":"D1"}},{{"service":"delay","seconds":3}},{{"service":"light.turn_on","entity_id":"D1"}}]}}"""
        res = run_llm_sync(sys_prompt, f"指令：{q}", model_id=primary, enable_thinking=force_think, max_tokens=160, temperature=0.05)
        raw_out = res["text"]
        voice_summary = ""
        thought = ""
        seq = []
        exec_logs = []
        try:
            m = re.search(r"\{.*\}", raw_out, re.DOTALL)
            parsed = json.loads(m.group(0) if m else raw_out)
            thought = parsed.get("thought", "")
            voice_summary = parsed.get("voice_summary", "")
            raw_seq = parsed.get("sequence", [])
            for st_item in raw_seq:
                if "entity_id" in st_item and st_item["entity_id"] in short_map:
                    st_item["entity_id"] = short_map[st_item["entity_id"]]
                seq.append(st_item)
        except Exception:
            voice_summary = raw_out[:60] if raw_out else "已处理你的家居指令。"

        for idx, step in enumerate(seq):
            srv = step.get("service", "")
            eid = step.get("entity_id", "")
            if srv == "delay":
                remaining = seq[idx:]
                tid = schedule_async_actions(remaining, desc=q[:30])
                exec_logs.append(f"⏱️ 后台任务 #{tid} 调度后续 {len(remaining)} 步（可随时说“取消定时”中止）")
                break
            elif "." in srv and eid:
                d, s = srv.split(".", 1)
                ok, info = execute_ha_service(d, s, eid)
                exec_logs.append(f"🔌 {d}.{s} -> {eid} [{'OK' if ok else info}]")

        if not voice_summary:
            voice_summary = "好的，已为你调度执行。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, primary, "Qwen3 智能家居调度", q, f"{voice_summary} | {' ; '.join(exec_logs)}", lat)
        return {
            "voice_summary": voice_summary,
            "thought": thought,
            "sequence": seq,
            "execution_logs": exec_logs,
            "result": voice_summary + ("\n" + "\n".join(exec_logs) if exec_logs else ""),
            "model_id": primary,
            "latency_ms": lat,
        }

    # ===== L3：事实锚定型多轮自然语言对话（按需挂载随手记，兼顾 3s 极速响应与零个人幻觉） =====
    today = datetime.now().date()
    wd_now = CN_WEEKDAY_NAMES[today.weekday()]
    hist = get_recent_history(source, incoming_messages, max_turns=4)
    need_mem_grounding = any(w in q for w in ("我", "咱", "今天", "明天", "后天", "周", "最近", "记", "说过", "忘", "哪", "事", "活", "忙"))
    if need_mem_grounding:
        mem_snapshot = build_grounding_memories_snippet(limit=5)
        sys_prompt = (
            f"你是纯离线中文语音管家，今天{today.strftime('%Y-%m-%d')} {wd_now}。"
            "涉及个人日程/活计/说过的话时严格依据下方随手记，无记录就直说“随手记里没有”，绝不编造；"
            f"其他问题简短口语答复(40字内)。\n【随手记】\n{mem_snapshot}"
        )
    else:
        sys_prompt = (
            f"你是纯离线中文语音管家，今天{today.strftime('%Y-%m-%d')} {wd_now}。"
            "若要求扫描网络或查实时联网信息，直说纯离线模式无法扫描或联网；否则用简短自然口语回答(40字内)，不编造事实。"
        )
    res = run_llm_sync(
        sys_prompt, q, model_id=primary, enable_thinking=force_think,
        max_tokens=380 if force_think else 90, temperature=0.2, history=hist
    )
    reply = res["text"] or "我在，有什么可以帮你的？"
    append_recent_turn(source, q, reply)
    record_telemetry(source, primary, "深度推理" if force_think else "端侧多轮对话", q, reply, res["latency_ms"])
    return {
        "voice_summary": reply,
        "result": res["raw"] if force_think else reply,
        "model_id": primary,
        "latency_ms": res["latency_ms"],
    }

# ── API 端点：系统状态 / 真实模型 / 第二大脑 / HA 设备 / 遥测 ────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "host": HOST_NAME, "primary_model": get_default_primary_id(), "time": time.time()}

@app.get("/api/system")
def api_system():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    spd = get_router_speeds()
    with TIMERS_LOCK:
        timers_view = [{"id": t["id"], "desc": t["desc"], "created_at": t["created_at"]} for t in PENDING_TIMERS]
    return {
        "host": HOST_LABEL,
        "host_name": HOST_NAME,
        "total_ram_gb": round(mem.total / (1024**3), 1),
        "used_ram_gb": round(mem.used / (1024**3), 1),
        "free_ram_gb": round(mem.available / (1024**3), 1),
        "ram_percent": round(mem.percent, 1),
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "router_down_kb": spd["down_kb"],
        "router_up_kb": spd["up_kb"],
        "primary_model": get_default_primary_id(),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600.0, 1),
        "pending_timers": timers_view,
        "services": get_real_pipeline_services(),
    }

@app.get("/api/models")
def api_models():
    items = []
    primary = get_default_primary_id()
    for mid, meta in MODEL_CATALOG.items():
        path = os.path.join(MODELS_DIR, meta["filename"])
        exists = os.path.exists(path)
        if not exists and not OLLAMA_URL:
            continue
        size_gb = round(os.path.getsize(path) / (1024**3), 2) if exists else 0.0
        ensure_model_stats(mid)
        st = MODEL_STATS[mid]
        items.append({
            "id": mid,
            "name": meta["name"],
            "role": meta["role"],
            "params": meta["params"],
            "supports_think": meta["supports_think"],
            "on_disk": exists,
            "size_gb": size_gb,
            "loaded": mid in LOADED_LLMS,
            "is_primary": mid == primary,
            "state": st["state"] if (mid in LOADED_LLMS) else ("ON_DISK" if exists else "MISSING"),
            "call_count": st["call_count"],
            "last_active": st["last_active"],
            "last_latency_ms": st["last_latency_ms"],
        })
    return items

@app.post("/api/models/{model_id}/select", dependencies=[Depends(verify_optional_token)])
async def select_primary_model(model_id: str):
    global ACTIVE_PRIMARY_ID
    if model_id not in MODEL_CATALOG:
        raise HTTPException(status_code=404, detail="Unknown model")
    path = os.path.join(MODELS_DIR, MODEL_CATALOG[model_id]["filename"])
    if not os.path.exists(path) and not OLLAMA_URL:
        raise HTTPException(status_code=400, detail="Model file not found on disk")
    ACTIVE_PRIMARY_ID = model_id
    return {"status": "ok", "primary_model": ACTIVE_PRIMARY_ID}

@app.post("/api/models/{model_id}/unload", dependencies=[Depends(verify_optional_token)])
async def unload_model_api(model_id: str):
    ok = unload_local_llm(model_id)
    return {"status": "unloaded" if ok else "not_loaded", "model_id": model_id}

@app.get("/api/memories", dependencies=[Depends(verify_optional_token)])
def api_get_memories():
    return list_memories(50)

class MemoryCreate(BaseModel):
    content: str
    category: str = "随手记"

@app.post("/api/memories", dependencies=[Depends(verify_optional_token)])
def api_add_memory(req: MemoryCreate):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content required")
    return add_memory(req.content, req.category)

@app.delete("/api/memories/{mid}", dependencies=[Depends(verify_optional_token)])
def api_delete_memory(mid: int):
    ok = delete_memory(mid)
    return {"deleted": ok, "id": mid}

@app.post("/api/timers/cancel", dependencies=[Depends(verify_optional_token)])
def api_cancel_timers():
    cnt = cancel_all_pending_timers()
    return {"cancelled": cnt}

@app.get("/api/telemetry")
def api_telemetry():
    return TELEMETRY_LOGS

@app.get("/api/ha/devices")
def api_ha_devices():
    return get_controllable_devices(force=True)

class NotifyReq(BaseModel):
    title: str = "集群通知"
    message: str

@app.post("/api/notify_cardputer", dependencies=[Depends(verify_optional_token)])
def api_notify_cardputer(req: NotifyReq):
    ok, info = notify_cardputer_alert(req.title, req.message)
    record_telemetry("反向主动唤醒", "Cardputer-Adv", "屏幕弹窗推送", req.title, req.message, 10.0)
    return {"ok": ok, "detail": info}

class ExecuteReq(BaseModel):
    query: str
    model_id: Optional[str] = None
    think: bool = False

@app.post("/api/execute", dependencies=[Depends(verify_optional_token)])
async def api_execute(req: ExecuteReq):
    return await asyncio.to_thread(
        handle_voice_query_sync,
        req.query,
        "控制塔实战台",
        req.model_id,
        req.think,
    )

# ── 完整 Ollama 兼容端点（Home Assistant 官方 Ollama 集成 100% 握手） ────────────
@app.get("/api/version")
@app.get("/v1/api/version")
def ollama_version():
    return {"version": "0.6.2"}

@app.get("/api/tags")
@app.get("/v1/api/tags")
def ollama_tags():
    models = []
    for mid, meta in MODEL_CATALOG.items():
        path = os.path.join(MODELS_DIR, meta["filename"])
        if os.path.exists(path) or OLLAMA_URL:
            models.append({
                "name": f"{mid}:latest",
                "model": f"{mid}:latest",
                "modified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "size": os.path.getsize(path) if os.path.exists(path) else 2500000000,
                "digest": f"sha256:{mid}",
                "details": {
                    "format": "gguf",
                    "family": meta["family"],
                    "parameter_size": meta["params"].split("·")[0].strip(),
                    "quantization_level": "Q4_K_M",
                },
            })
    return {"models": models}

@app.post("/api/show")
@app.post("/v1/api/show")
async def ollama_show(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name", f"{get_default_primary_id()}:latest")
    return {
        "license": "Apache-2.0",
        "modelfile": f"FROM {name}",
        "details": {"format": "gguf", "family": "qwen3", "parameter_size": "4B", "quantization_level": "Q4_K_M"},
    }

@app.post("/api/chat")
@app.post("/v1/api/chat")
async def ollama_chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    req_model = body.get("model", get_default_primary_id())
    user_query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_query = m.get("content", "")
            break
    target_mid = get_default_primary_id()
    res = await asyncio.to_thread(handle_voice_query_sync, user_query, "Cardputer / HA Assist", target_mid, False, messages)
    reply = res.get("voice_summary", "收到。")
    lat = res.get("latency_ms", 0)
    return {
        "model": req_model,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant", "content": reply},
        "done": True,
        "total_duration": int(lat * 1_000_000),
    }

# ── OpenAI 兼容端点（供小爱音箱 xiaogpt 等客户端直连） ─────────────────────────
@app.get("/v1/models")
@app.get("/models")
def openai_models():
    return {
        "object": "list",
        "data": [{"id": f"{mid}:latest", "object": "model", "owned_by": HOST_NAME} for mid in MODEL_CATALOG],
    }

@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    body = await request.json()
    req_model = body.get("model", f"{get_default_primary_id()}:latest")
    messages = body.get("messages", [])
    user_query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_query = m.get("content", "")
            break
    res = await asyncio.to_thread(handle_voice_query_sync, user_query, "小爱音箱 (OpenAI API)", None, False, messages)
    reply = res.get("voice_summary", "收到。")
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
    }

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    html_file = os.path.join(DASHBOARD_DIR, "index.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>AI Legion v2.6 Mission Control</h1>"

if __name__ == "__main__":
    import uvicorn
    print(f"[AI-Legion v2.6] Starting on port {PORT} | Host: {HOST_NAME} | Primary: {get_default_primary_id()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
