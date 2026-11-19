import os
import sys
import time
import json
import re
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Any, Dict

app = FastAPI(title="AI Legion Voice Gateway")

HA_URL = os.getenv("HA_URL", "http://127.0.0.1:8123").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "phi4-mini")
PORT = int(os.getenv("PORT", "8096"))

def get_real_ha_devices() -> List[Dict[str, str]]:
    """Fetch all controllable entities from Home Assistant."""
    if not HA_TOKEN:
        return []
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}"}
        resp = httpx.get(f"{HA_URL}/api/states", headers=headers, timeout=3.0)
        if resp.status_code == 200:
            states = resp.json()
            devices = []
            for s in states:
                eid = s.get("entity_id", "")
                if any(eid.startswith(prefix) for prefix in ["light.", "fan.", "switch.", "cover.", "climate."]):
                    name = s.get("attributes", {}).get("friendly_name", eid)
                    state = s.get("state", "unknown")
                    devices.append({"id": eid, "name": name, "state": state})
            return devices
    except Exception as e:
        print(f"[Gateway] Error fetching HA devices: {e}")
    return []

def execute_ha_service(domain: str, service: str, entity_id: str, extra_data: dict = None):
    """Execute a real Home Assistant service call."""
    if not HA_TOKEN:
        return False, "HA_TOKEN not configured"
    try:
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"entity_id": entity_id}
        if extra_data:
            payload.update(extra_data)
        url = f"{HA_URL}/api/services/{domain}/{service}"
        resp = httpx.post(url, headers=headers, json=payload, timeout=4.0)
        return resp.status_code == 200, resp.json() if resp.status_code == 200 else resp.text
    except Exception as e:
        return False, str(e)

def run_smart_agent_pipeline(user_query: str) -> str:
    """Analyze user intent, execute physical actions, and return conversational spoken Chinese."""
    devices = get_real_ha_devices()
    device_names = [d["name"] for d in devices]
    
    # 1. Device match
    matched_device = None
    for d in devices:
        if d["name"] in user_query or any(k in user_query for k in d["name"].split()):
            matched_device = d
            break
            
    # Default to first light if user just says "turn on light"
    if not matched_device and ("灯" in user_query or "开关" in user_query):
        for d in devices:
            if d["id"].startswith("light."):
                matched_device = d
                break

    # 2. Check for multi-step sequence (e.g., "开再关，过5秒再开")
    delay_match = re.search(r"(\d+)\s*秒", user_query)
    delay_sec = int(delay_match.group(1)) if delay_match else 0
    has_toggle_sequence = any(w in user_query for w in ["再", "又", "过"]) and delay_sec > 0

    if matched_device:
        domain = matched_device["id"].split(".")[0]
        dev_name = matched_device["name"]
        dev_id = matched_device["id"]
        
        if has_toggle_sequence:
            # Multi-step action execution
            execute_ha_service(domain, "turn_on", dev_id)
            time.sleep(0.5)
            execute_ha_service(domain, "turn_off", dev_id)
            
            # Non-blocking background resumption
            def delayed_turn_on():
                time.sleep(delay_sec)
                execute_ha_service(domain, "turn_on", dev_id)
            
            import threading
            threading.Thread(target=delayed_turn_on, daemon=True).start()
            return f"已为你开启{dev_name}并关闭，{delay_sec}秒后重新开启。"

        elif any(k in user_query for k in ["开", "开启", "打开", "启动"]):
            execute_ha_service(domain, "turn_on", dev_id)
            return f"好的，已为你开启{dev_name}。"
            
        elif any(k in user_query for k in ["关", "关闭", "关掉", "熄灭"]):
            execute_ha_service(domain, "turn_off", dev_id)
            return f"好的，已为你关闭{dev_name}。"

    # 3. Fallback to Local LLM (Ollama) for general conversation
    try:
        sys_prompt = "你是本地智能家居语音助手。请用简短、亲切、纯口语的中文直接回答（不超过30字），绝不要输出任何代码、脚本或Markdown代码块。"
        payload = {
            "model": DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_query}
            ],
            "stream": False,
            "options": {"temperature": 0.5, "max_tokens": 60}
        }
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=10.0)
        if resp.status_code == 200:
            res_content = resp.json().get("message", {}).get("content", "").strip()
            # Clean up code blocks if model slips
            res_content = re.sub(r"```.*?```", "", res_content, flags=re.DOTALL)
            res_content = re.sub(r"`.*?`", "", res_content).strip()
            if res_content:
                return res_content
    except Exception as e:
        print(f"[Gateway] Ollama call error: {e}")

    return "收到，正在为你处理。"

@app.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama API compatible endpoint for Home Assistant."""
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", DEFAULT_MODEL)
    
    user_prompt = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_prompt = m.get("content", "")
            break
            
    # Intent dispatch
    reply = run_smart_agent_pipeline(user_prompt)
    
    return {
        "model": model,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "message": {"role": "assistant", "content": reply},
        "done": True
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}

if __name__ == "__main__":
    import uvicorn
    print(f"Starting AI Legion Voice Gateway on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
