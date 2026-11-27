"""Local runtime detection for the Mini App — the model is NOT bundled.

Requirements (the user sets these up beforehand, see README):
  1. llama.cpp installed (llama-server on PATH)  —or—  Ollama installed and running
  2. xiaomi-ocr-0 configured on the machine:
       - Ollama: any model whose name contains "xiaomi-ocr-0" (e.g. `ollama pull longwayxu/xiaomi-ocr-0`)
       - llama.cpp: the user runs llama-server themselves on APP_LLAMA_PORT (default 8190)
         with the GGUF + mmproj downloaded from HuggingFace SeerRay-Lab/Xiaomi-OCR-0.
         Optionally set APP_LLAMA_MODEL (and APP_LLAMA_MMPROJ) so this module can start
         llama-server automatically.

Preference order: a running llama-server > an Ollama model matching "xiaomi-ocr-0".
Nothing is downloaded or imported by the App itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]   # mini/
LOG_DIR = APP_ROOT / "out"

LLAMA_PORT = int(os.environ.get("APP_LLAMA_PORT", "8190"))
OLLAMA_URL = os.environ.get("APP_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL_KEY = "xiaomi-ocr-0"          # Ollama 模型名匹配关键字
START_TIMEOUT_S = 150

_llama_lock = threading.Lock()
_llama_proc: subprocess.Popen | None = None
_owns_llama = False                 # llama-server 由本进程启动时才允许停它

HINT_NO_RUNTIME = "需要先安装 llama.cpp（brew install llama.cpp）或 Ollama（ollama.com/download）"
HINT_NO_MODEL = (f"运行时已安装，但未发现 {MODEL_KEY} 模型："
                 "Ollama 请执行 ollama pull longwayxu/xiaomi-ocr-0；"
                 "llama.cpp 请用 llama-server 加载 GGUF 监听 "
                 f":{LLAMA_PORT}（详见 README）")


def _get_json(url: str, timeout: float = 0.4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None


def _llama_running() -> dict | None:
    data = _get_json(f"http://127.0.0.1:{LLAMA_PORT}/v1/models")
    if not data or not data.get("models"):
        return None
    name = data["models"][0].get("name") or data["models"][0].get("model")
    return {"base_url": f"http://127.0.0.1:{LLAMA_PORT}", "model": name}


def _ollama_names() -> list[str]:
    data = _get_json(OLLAMA_URL + "/api/tags")
    return [m.get("name", "") for m in (data or {}).get("models", [])]


def _ollama_match() -> str | None:
    return next((n for n in _ollama_names() if MODEL_KEY in n.lower()), None)


def auto_serve_model() -> tuple[Path | None, Path | None]:
    """Optional auto-start: APP_LLAMA_MODEL (+ APP_LLAMA_MMPROJ) point to local GGUFs."""
    model = os.environ.get("APP_LLAMA_MODEL", "").strip()
    if not model:
        return None, None
    mmproj = os.environ.get("APP_LLAMA_MMPROJ", "").strip()
    base = Path(model).expanduser()
    return (base if base.is_file() else None,
            Path(mmproj).expanduser() if mmproj and Path(mmproj).expanduser().is_file() else None)


def status() -> dict:
    """Cheap, non-mutating overview used by the UI on load."""
    llm_exe = shutil.which("llama-server")
    ollama_exe = shutil.which("ollama")
    llm_running = _llama_running()
    ollama_names = _ollama_names()
    matched = next((n for n in ollama_names if MODEL_KEY in n.lower()), None)
    auto_base, _ = auto_serve_model()
    preferred, source, model_name = None, "", MODEL_KEY
    if llm_running:
        preferred, source = {"backend": "openai", "base_url": llm_running["base_url"],
                             "model": llm_running["model"]}, "llama_cpp"
        model_name = llm_running["model"] or MODEL_KEY
    elif matched:
        preferred, source, model_name = {"backend": "ollama", "base_url": OLLAMA_URL,
                                         "model": matched}, "ollama", matched
    hint = ""
    if not preferred:
        if not llm_exe and not ollama_exe:
            hint = HINT_NO_RUNTIME
        elif not llm_running and not matched:
            hint = HINT_NO_MODEL
    return {
        "bundled": False,
        "files": {},
        "model": model_name,
        "runtimes": {
            "llama_cpp": {"binary": bool(llm_exe), "running": bool(llm_running),
                          "port": LLAMA_PORT, "auto_serve": bool(auto_base)},
            "ollama": {"binary": bool(ollama_exe), "reachable": bool(ollama_names),
                       "matched": matched, "models": ollama_names[:20]},
        },
        "preferred": preferred,
        "preferred_source": source,
        "hint": hint,
    }


def serve_start() -> dict:
    """Reuse a running llama-server; optionally start one via APP_LLAMA_MODEL. Never imports into Ollama."""
    global _llama_proc, _owns_llama
    with _llama_lock:
        running = _llama_running()
        if running:
            return {"ok": True, "config": {"backend": "openai", **running}, "reused": True}
        base, mmproj = auto_serve_model()
        if not shutil.which("llama-server") or not base:
            return {"ok": False, "error": HINT_NO_MODEL if shutil.which("llama-server") else HINT_NO_RUNTIME}
        exe = shutil.which("llama-server")
        args = [exe, "--model", str(base)]
        if mmproj:
            args += ["--mmproj", str(mmproj)]
        args += ["--port", str(LLAMA_PORT), "--ctx-size", "32768"]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / "llama-server.log", "ab")
        _llama_proc = subprocess.Popen(args, cwd=str(base.parent),
                                       stdout=log, stderr=subprocess.STDOUT)
        _owns_llama = True
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if _llama_proc.poll() is not None:
                return {"ok": False, "error": f"llama-server 启动失败（退出码 {_llama_proc.returncode}），详见 mini/out/llama-server.log"}
            running = _llama_running()
            if running:
                return {"ok": True, "config": {"backend": "openai", **running}, "reused": False}
            time.sleep(1.0)
        serve_stop()
        return {"ok": False, "error": "llama-server 启动超时"}


def serve_stop() -> None:
    """Stop only the llama-server this App started — a user-managed one is left alone."""
    global _llama_proc, _owns_llama
    with _llama_lock:
        if _llama_proc and _llama_proc.poll() is None and _owns_llama:
            _llama_proc.terminate()
            try:
                _llama_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _llama_proc.kill()
        if _llama_proc and _owns_llama and _llama_running():
            # Orphan of a previous Mini run (we started it before): clear our own port.
            try:
                out = subprocess.run(["lsof", "-ti", f"tcp:{LLAMA_PORT}"],
                                     capture_output=True, text=True, timeout=5).stdout.split()
                for pid in out:
                    if pid.isdigit():
                        os.kill(int(pid), 15)
            except Exception:  # noqa: BLE001
                pass
        _llama_proc = None
        _owns_llama = False
