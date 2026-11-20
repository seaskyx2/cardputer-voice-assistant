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
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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
N_THREADS = int(os.getenv("LLM_THREADS", "6"))

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "memory.db")

app = FastAPI(title="AI Legion v2.0 · 端侧小模型集群控制塔")

# ── SQLite 离线家庭第二大脑（记忆与待办） ────────────────────────────────────────
CN_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

def resolve_relative_date(text: str, base_dt: datetime = None) -> Optional[str]:
    """将口语相对日期（今天/明天/这个星期天/下周三/X月X日）锚定为绝对 YYYY-MM-DD"""
    now = base_dt or datetime.now()
    today = now.date()
    # 1. 绝对月日：X月X日/号
    m_md = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
    if m_md:
        mo, dy = int(m_md.group(1)), int(m_md.group(2))
        try:
            cand = date(today.year, mo, dy)
            if cand < today - timedelta(days=30):
                cand = date(today.year + 1, mo, dy)
            return cand.strftime("%Y-%m-%d")
        except Exception:
            pass
    # 2. 今天 / 明天 / 后天 / 大后天
    if "大后天" in text:
        return (today + timedelta(days=3)).strftime("%Y-%m-%d")
    if "后天" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    if "明天" in text or "明日" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "今天" in text or "今晚" in text or "今日" in text:
        return today.strftime("%Y-%m-%d")
    # 3. 本周/这周/这个星期 + X  或  下周/下个星期 + X
    m_wk = re.search(r"(这周|本周|这个星期|这星期|下周|下个星期|下星期)?(?:星期|周|礼拜)([一二三四五六日天])", text)
    if m_wk:
        prefix = m_wk.group(1) or ""
        target_wd = CN_WEEKDAY_MAP[m_wk.group(2)]
        monday = today - timedelta(days=today.weekday())
        if "下" in prefix:
            target = monday + timedelta(days=7 + target_wd)
        elif any(p in prefix for p in ("这", "本")):
            target = monday + timedelta(days=target_wd)
        else:
            # 无前缀（如只说“周日”），取今天起最近的那个周X
            diff = (target_wd - today.weekday()) % 7
            target = today + timedelta(days=diff)
        return target.strftime("%Y-%m-%d")
    # 4. 泛指“这周 / 本周 / 下周”（未指定周几，锚定到该周周日作为截止参考）
    if any(w in text for w in ("下周", "下星期", "下个星期")):
        monday = today - timedelta(days=today.weekday())
        return (monday + timedelta(days=13)).strftime("%Y-%m-%d")
    if any(w in text for w in ("这周", "本周", "这星期", "这个星期")):
        monday = today - timedelta(days=today.weekday())
        return (monday + timedelta(days=6)).strftime("%Y-%m-%d")
    return None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT '随手记',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            event_date TEXT DEFAULT NULL
        )
    """)
    # 兼容旧库自动迁移增加 event_date 列，并回溯补全历史记录的绝对日期
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "event_date" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN event_date TEXT DEFAULT NULL")
    rows = conn.execute("SELECT id, content, created_at, event_date FROM memories WHERE event_date IS NULL").fetchall()
    for mid, content, created_at, _ in rows:
        try:
            base = datetime.strptime(created_at[:16], "%Y-%m-%d %H:%M")
        except Exception:
            base = datetime.now()
        ev = resolve_relative_date(content, base)
        if ev:
            conn.execute("UPDATE memories SET event_date = ?, category = '待办提醒' WHERE id = ?", (ev, mid))
    conn.commit()
    conn.close()

init_db()

def add_memory(content: str, category: str = "随手记", event_date: str = None) -> Dict[str, Any]:
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    ev = event_date or resolve_relative_date(content, now)
    if ev and category == "随手记":
        category = "待办提醒"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO memories (category, content, created_at, event_date) VALUES (?, ?, ?, ?)",
        (category, content.strip(), now_str, ev),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": mid, "category": category, "content": content.strip(), "created_at": now_str, "event_date": ev}

def list_memories(limit: int = 30) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, category, content, created_at, event_date FROM memories ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_memory(mid: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

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
            return f"- [绝对日期: {ev} {wd} ({status}) | 分类: {m['category']} | 记录于: {m['created_at']}] {m['content']}"
        except Exception:
            pass
    return f"- [无指定日期 | 分类: {m['category']} | 记录于: {m['created_at']}] {m['content']}"

def search_memories(query: str, top_k: int = 6) -> Dict[str, Any]:
    """支持时间窗口精确过滤（今天/明天/这周/下周/最近）+ 关键词打分召回"""
    all_mems = list_memories(100)
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    # 判断用户是否在问特定时间窗口的日程
    window_label = None
    win_start, win_end = None, None
    if any(w in query for w in ("今天", "今日", "今晚")):
        window_label = f"今天({today.strftime('%Y-%m-%d')})"
        win_start, win_end = today, today
    elif any(w in query for w in ("明天", "明日")):
        tmr = today + timedelta(days=1)
        window_label = f"明天({tmr.strftime('%Y-%m-%d')})"
        win_start, win_end = tmr, tmr
    elif any(w in query for w in ("这周", "本周", "这星期", "这个星期")):
        window_label = f"本周({monday.strftime('%m-%d')}至{sunday.strftime('%m-%d')})"
        win_start, win_end = monday, sunday
    elif any(w in query for w in ("下周", "下星期", "下个星期")):
        nm = monday + timedelta(days=7)
        ns = nm + timedelta(days=6)
        window_label = f"下周({nm.strftime('%m-%d')}至{ns.strftime('%m-%d')})"
        win_start, win_end = nm, ns
    elif any(w in query for w in ("最近", "近期", "接下来", "待办", "日程", "安排", "要做的事", "什么事")):
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

    # 否则按关键词检索（排除过期日程干扰，保留所有物品位置/随手记）
    clean_q = re.sub(r"[我的你在哪了呢吗呀啊？?！!，,。.请问帮查一下之前记过备忘]", "", query)
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
    items = [x[1] for x in scored[:top_k]] if scored else all_mems[:min(4, len(all_mems))]
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

def get_default_primary_id() -> str:
    global ACTIVE_PRIMARY_ID
    # 优先使用已存在磁盘的 qwen3-4b，否则降级到 phi-4-mini
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
    print(f"[AI-Legion] Loading GGUF model {meta['name']} from {path} ...")
    llm = Llama(
        model_path=path,
        n_ctx=2048,
        n_threads=N_THREADS,
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

# ── 实时端口探活（Whisper / Piper / HA） ───────────────────────────────────────
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

# ── Home Assistant 实体缓存与服务调用 ──────────────────────────────────────────
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

# ── 后台定时推送 nuctutu 算力指标到 HA（供 Cardputer 掌上投屏） + 异常主动守夜 ────
def ha_telemetry_pusher():
    last_alert_ts = 0.0
    while True:
        try:
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=1.0)
            primary = get_default_primary_id()
            mems = list_memories(1)
            latest_memo = mems[0]["content"][:28] if mems else "暂无待办备忘"
            push_ha_sensor("sensor.ai_legion_cpu", round(cpu, 1), "AI Legion CPU", "%")
            push_ha_sensor("sensor.ai_legion_ram", round(mem.percent, 1), "AI Legion RAM", "%")
            push_ha_sensor("sensor.ai_legion_model", MODEL_CATALOG[primary]["name"], "AI Legion Active Model")
            push_ha_sensor("sensor.ai_legion_memo", latest_memo, "AI Legion Latest Memo")

            # 后台异常主动守夜（冷却 10 分钟防止刷屏）
            now = time.time()
            if now - last_alert_ts > 600:
                if mem.percent > 90.0:
                    notify_cardputer_alert("主机内存告警", f"nuctutu 内存占用已达 {mem.percent:.0f}%")
                    last_alert_ts = now
                elif cpu > 92.0:
                    notify_cardputer_alert("主机算力过载", f"nuctutu CPU 持续负载 {cpu:.0f}%")
                    last_alert_ts = now
        except Exception:
            pass
        time.sleep(15)

threading.Thread(target=ha_telemetry_pusher, daemon=True).start()

# ── 统一 LLM 推理封装（带线程锁 + Qwen3 双模 /no_think 支持） ────────────────────
def run_llm_sync(
    system_prompt: str,
    user_prompt: str,
    model_id: str = None,
    enable_thinking: bool = False,
    max_tokens: int = 220,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    target_id = model_id if (model_id in MODEL_CATALOG) else get_default_primary_id()
    start_t = time.time()

    # 若配置了外部 Ollama（Docker 开源部署模式），走 HTTP 调用
    if OLLAMA_URL:
        try:
            user_msg = user_prompt
            if not enable_thinking and "qwen3" in target_id:
                user_msg = user_prompt + " /no_think"
            payload = {
                "model": os.getenv("DEFAULT_MODEL", "qwen3:4b"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
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

    # 本机 llama_cpp 线程安全直推模式
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
                # Qwen3 原生快慢双模指令
                user_msg = f"{user_prompt}\n/think" if enable_thinking else f"{user_prompt}\n/no_think"
                resp = llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                raw = resp["choices"][0]["message"]["content"].strip()
            else:
                resp = llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
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

# ── 异步后台时序调度（彻底不阻塞语音返回） ───────────────────────────────────────
def schedule_async_actions(actions: List[Dict[str, Any]]):
    def worker():
        for act in actions:
            srv = act.get("service", "")
            eid = act.get("entity_id", "")
            if srv == "delay":
                secs = min(float(act.get("seconds", 5)), 300.0)
                time.sleep(secs)
            elif "." in srv and eid:
                d, s = srv.split(".", 1)
                execute_ha_service(d, s, eid)
    threading.Thread(target=worker, daemon=True).start()

# ── 统一智能体路由管线（L0 规则 -> L1 第二大脑 -> L2 家电控制 -> L3 闲聊） ─────────
def handle_voice_query_sync(query: str, source: str = "Voice", preferred_model: str = None, force_think: bool = False) -> Dict[str, Any]:
    start_t = time.time()
    q = query.strip()
    primary = preferred_model if (preferred_model in MODEL_CATALOG) else get_default_primary_id()

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
        reply = f"主机 nuctutu 运行正常，CPU 占用百分之{int(cpu)}，内存使用百分之{int(mem.percent)}，当前主机模型为 {MODEL_CATALOG[primary]['name']}，路由器下载 {spd['down_kb']} KB每秒。"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L0-Ops", "HomeLab 语音查房", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L0-Ops", "latency_ms": lat}

    # ===== L1-A：离线“家庭第二大脑”写入（自动将相对日期锚定为绝对日期） =====
    mem_add_match = re.match(r"^(?:请)?(?:帮我)?(?:记一下|记住|备忘|记录一下|提醒我)[：:，,\s]*(.+)$", q)
    if mem_add_match:
        content = mem_add_match.group(1).strip()
        cat = "物品位置" if any(w in content for w in ("放在", "停在", "抽屉", "柜子", "车位")) else "随手记"
        item = add_memory(content, category=cat)
        if item.get("event_date"):
            ev_d = datetime.strptime(item["event_date"], "%Y-%m-%d").date()
            wd = CN_WEEKDAY_NAMES[ev_d.weekday()]
            reply = f"好的，已记入{ev_d.month}月{ev_d.day}日{wd}待办：{content}"
        else:
            reply = f"好的，已记入本地{item['category']}：{content}"
        lat = (time.time() - start_t) * 1000
        record_telemetry(source, "L1-Memory", f"第二大脑写入 ({item['category']})", q, reply, lat)
        return {"voice_summary": reply, "result": reply, "model_id": "L1-Memory", "latency_ms": lat, "memory": item}

    # ===== L1-B：离线“家庭第二大脑”检索（自然口语全覆盖 + 严格时间窗口过滤） =====
    mem_query_triggers = (
        "放哪了", "在哪放着", "停在哪", "记过什么", "有什么备忘", "有什么待办",
        "查一下备忘", "我的备忘", "什么事情", "什么事", "要做的事", "安排", "日程",
        "计划", "提醒", "待办", "密码", "随手记"
    )
    if any(k in q for k in mem_query_triggers):
        today = datetime.now().date()
        wd_now = CN_WEEKDAY_NAMES[today.weekday()]
        sr = search_memories(q, top_k=6)
        hits = sr["items"]
        win_lbl = sr["window_label"]
        if not hits:
            if win_lbl:
                reply = f"查看了本地日程，{win_lbl}没有安排任何事项。"
            else:
                reply = "本地记忆库里目前还没有相关记录。"
            lat = (time.time() - start_t) * 1000
            record_telemetry(source, "L1-Memory", "第二大脑精准查询", q, reply, lat)
            return {"voice_summary": reply, "result": reply, "model_id": "L1-Memory", "latency_ms": lat}
        ctx = "\n".join([format_memory_for_llm(m, today) for m in hits])
        res = run_llm_sync(
            system_prompt=(
                f"你是家庭离线记忆管家。今天是 {today.strftime('%Y年%m月%d日')} {wd_now}。"
                "请严格依据下方检索到的真实本地记录回答用户，直接说出具体日期和事项，口语简短不超过45字，绝不编造。"
            ),
            user_prompt=f"【本地记忆记录（已按时间窗口筛选）】\n{ctx}\n\n用户提问：{q}",
            model_id=primary,
            enable_thinking=False,
            max_tokens=100,
            temperature=0.1,
        )
        reply = res["text"] or f"找到记录：{hits[0]['content']}"
        record_telemetry(source, primary, f"第二大脑 RAG ({win_lbl or '关键词'})", q, reply, res["latency_ms"])
        return {"voice_summary": reply, "result": reply, "model_id": primary, "latency_ms": res["latency_ms"]}

    # ===== L2：智能家居设备控制（精准判定，杜绝误伤闲聊） =====
    devices = get_controllable_devices()
    matched_by_name = [d for d in devices if d["name"] in q]
    control_phrases = ("开灯", "关灯", "打开", "关闭", "关掉", "熄灭", "吸顶灯", "大灯", "风扇", "空调", "窗帘", "指示灯", "全屋设备", "家里设备")
    is_home_control = bool(matched_by_name) or any(p in q for p in control_phrases)

    if is_home_control and devices:
        # L0 极速单步直通（无延时/无多步词，且明确匹配到设备 + 开/关）
        has_delay = bool(re.search(r"\d+\s*[秒分]", q)) or any(w in q for w in ("再", "然后", "之后", "开着的", "关着的"))
        if not has_delay and matched_by_name and len(matched_by_name) == 1:
            dev = matched_by_name[0]
            domain = dev["id"].split(".")[0]
            if any(w in q for w in ("打开", "开启", "启动")) and not any(w in q for w in ("关", "熄")):
                ok, _ = execute_ha_service(domain, "turn_on", dev["id"])
                reply = f"好的，已为你打开{dev['name']}。" if ok else f"尝试打开{dev['name']}失败，请检查设备在线状态。"
                lat = (time.time() - start_t) * 1000
                record_telemetry(source, "L0-Rule", "极速单步开设备", q, reply, lat)
                return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}
            if any(w in q for w in ("关闭", "关掉", "熄灭")) and not any(w in q for w in ("打开", "开启")):
                ok, _ = execute_ha_service(domain, "turn_off", dev["id"])
                reply = f"好的，已为你关闭{dev['name']}。" if ok else f"尝试关闭{dev['name']}失败，请检查设备在线状态。"
                lat = (time.time() - start_t) * 1000
                record_telemetry(source, "L0-Rule", "极速单步关设备", q, reply, lat)
                return {"voice_summary": reply, "result": reply, "model_id": "L0-Rule", "latency_ms": lat}

        # 复杂/多步/模糊设备指令 -> 交给 Qwen3 真实 JSON 规划 + 异步延时
        dev_lines = "\n".join([f"- {d['id']} | 名称: {d['name']} | 状态: {d['state']}" for d in devices])
        sys_prompt = f"""你是 Home Assistant 本地决策大脑。根据真实设备列表输出严格 JSON（不要输出其他文字）。
【真实设备列表】
{dev_lines}

【输出 JSON 格式】
{{
  "thought": "简短中文分析",
  "voice_summary": "一句简短口语中文答复（不超过30字）",
  "sequence": [
    {{"service": "light.turn_off", "entity_id": "真实ID"}},
    {{"service": "delay", "seconds": 5}},
    {{"service": "light.turn_on", "entity_id": "真实ID"}}
  ]
}}"""
        res = run_llm_sync(sys_prompt, f"用户指令：{q}", model_id=primary, enable_thinking=force_think, max_tokens=300, temperature=0.1)
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
            seq = parsed.get("sequence", [])
        except Exception:
            voice_summary = raw_out[:60] if raw_out else "已处理你的家居指令。"

        # 立即执行 delay 之前的步骤，遇到 delay 则把剩余步骤丢入后台异步线程（0 阻塞！）
        for idx, step in enumerate(seq):
            srv = step.get("service", "")
            eid = step.get("entity_id", "")
            if srv == "delay":
                remaining = seq[idx:]
                schedule_async_actions(remaining)
                exec_logs.append(f"⏱️ 后台异步调度后续 {len(remaining)} 个时序动作（不阻塞语音返回）")
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

    # ===== L3：纯本地自然语言对话（支持快/慢双模） =====
    sys_prompt = "你是运行在家庭边缘主机 nuctutu 上的本地语音助手。请用简短、自然、纯口语的中文回答（不超过45字），严禁输出代码块或 Markdown 符号。"
    res = run_llm_sync(sys_prompt, q, model_id=primary, enable_thinking=force_think, max_tokens=380 if force_think else 110, temperature=0.4)
    reply = res["text"] or "我在，有什么可以帮你的？"
    record_telemetry(source, primary, "深度推理" if force_think else "端侧快速对话", q, reply, res["latency_ms"])
    return {
        "voice_summary": reply,
        "result": res["raw"] if force_think else reply,
        "model_id": primary,
        "latency_ms": res["latency_ms"],
    }

# ── API 端点：系统状态 / 真实模型 / 第二大脑 / HA 设备 / 遥测 ────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "primary_model": get_default_primary_id(), "time": time.time()}

@app.get("/api/system")
def api_system():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    spd = get_router_speeds()
    return {
        "host": "nuctutu (NUC8i5BEK · i5-8259U 4C8T)",
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
        "services": get_real_pipeline_services(),
    }

@app.get("/api/models")
def api_models():
    items = []
    primary = get_default_primary_id()
    for mid, meta in MODEL_CATALOG.items():
        path = os.path.join(MODELS_DIR, meta["filename"])
        exists = os.path.exists(path)
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

@app.post("/api/models/{model_id}/select")
async def select_primary_model(model_id: str):
    global ACTIVE_PRIMARY_ID
    if model_id not in MODEL_CATALOG:
        raise HTTPException(status_code=404, detail="Unknown model")
    path = os.path.join(MODELS_DIR, MODEL_CATALOG[model_id]["filename"])
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="Model file not found on disk")
    ACTIVE_PRIMARY_ID = model_id
    return {"status": "ok", "primary_model": ACTIVE_PRIMARY_ID}

@app.post("/api/models/{model_id}/unload")
async def unload_model_api(model_id: str):
    ok = unload_local_llm(model_id)
    return {"status": "unloaded" if ok else "not_loaded", "model_id": model_id}

@app.get("/api/memories")
def api_get_memories():
    return list_memories(50)

class MemoryCreate(BaseModel):
    content: str
    category: str = "随手记"

@app.post("/api/memories")
def api_add_memory(req: MemoryCreate):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content required")
    return add_memory(req.content, req.category)

@app.delete("/api/memories/{mid}")
def api_delete_memory(mid: int):
    ok = delete_memory(mid)
    return {"deleted": ok, "id": mid}

@app.get("/api/telemetry")
def api_telemetry():
    return TELEMETRY_LOGS

@app.get("/api/ha/devices")
def api_ha_devices():
    return get_controllable_devices(force=True)

class NotifyReq(BaseModel):
    title: str = "集群通知"
    message: str

@app.post("/api/notify_cardputer")
def api_notify_cardputer(req: NotifyReq):
    ok, info = notify_cardputer_alert(req.title, req.message)
    record_telemetry("反向主动唤醒", "Cardputer-Adv", "屏幕弹窗推送", req.title, req.message, 10.0)
    return {"ok": ok, "detail": info}

class ExecuteReq(BaseModel):
    query: str
    model_id: Optional[str] = None
    think: bool = False

@app.post("/api/execute")
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
                "modified_at": datetime.utcnow().isoformat() + "Z",
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
    # 始终以控制塔选定的主力模型为准（在控制塔切模型后全屋立即生效，无需重配 HA）
    target_mid = get_default_primary_id()
    res = await asyncio.to_thread(handle_voice_query_sync, user_query, "Cardputer / HA Assist", target_mid, False)
    reply = res.get("voice_summary", "收到。")
    lat = res.get("latency_ms", 0)
    return {
        "model": req_model,
        "created_at": datetime.utcnow().isoformat() + "Z",
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
        "data": [{"id": f"{mid}:latest", "object": "model", "owned_by": "nuctutu"} for mid in MODEL_CATALOG],
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
    res = await asyncio.to_thread(handle_voice_query_sync, user_query, "小爱音箱 (OpenAI API)", None, False)
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
    return "<h1>AI Legion v2.0 Mission Control</h1>"

if __name__ == "__main__":
    import uvicorn
    print(f"[AI-Legion v2.0] Starting on port {PORT} | Primary: {get_default_primary_id()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
