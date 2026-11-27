"""Built-in model management for the App (app/model/).

The xiaomi-ocr-0 GGUF (+ vision mmproj) ships inside app/model. This module
detects local runtimes and can start/stop them so the App runs with zero
user configuration:

  llama.cpp (llama-server)  — preferred, started by this module on APP_LLAMA_PORT
  ollama                    — imported via app/model/Modelfile (blob dedupe)

Nothing here is required: the UI can still be pointed at any OpenAI-compatible
endpoint. All probes are fast (0.4s timeouts); heavy start/stop only happens
on explicit /api/model/serve|stop calls.
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

APP_ROOT = Path(__file__).resolve().parents[1]   # app/
MODEL_DIR = APP_ROOT / "model"
MANIFEST_PATH = MODEL_DIR / "MANIFEST.json"
MODELFILE = MODEL_DIR / "Modelfile"
LOG_DIR = APP_ROOT / "out"

LLAMA_PORT = int(os.environ.get("APP_LLAMA_PORT", "8190"))
OLLAMA_URL = os.environ.get("APP_OLLAMA_URL", "http://127.0.0.1:11434")
START_TIMEOUT_S = 150

_llama_lock = threading.Lock()
_llama_proc: subprocess.Popen | None = None


def manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def model_files() -> tuple[Path | None, Path | None]:
    m = manifest()
    files = m.get("files", {})
    base = MODEL_DIR / files.get("model", {}).get("path", "xiaomi-ocr-0-bf16.gguf")
    mmproj = MODEL_DIR / files.get("mmproj", {}).get("path", "xiaomi-ocr-0-bf16-mmproj.gguf")
    return (base if base.is_file() else None, mmproj if mmproj.is_file() else None)


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


def status() -> dict:
    """Cheap, non-mutating overview used by the UI on load."""
    m = manifest()
    base, mmproj = model_files()
    llm_exe = shutil.which("llama-server")
    ollama_exe = shutil.which("ollama")
    llm_running = _llama_running()
    names = _ollama_names()
    m_files = m.get("files", {})
    sizes_ok = all(
        (MODEL_DIR / f["path"]).is_file() and (MODEL_DIR / f["path"]).stat().st_size == f["size_bytes"]
        for f in m_files.values()) if m_files else False
    import_name = m.get("runtimes", {}).get("ollama", {}).get("import_name", "xiaomi-ocr-0:app")
    ollama_match = next((n for n in names if n == import_name),
                        next((n for n in names if n in m.get("runtimes", {}).get("ollama", {}).get("also_matches", [])), None))
    preferred = None
    source = ""
    if llm_running:
        preferred, source = {"backend": "openai", **llm_running}, "llama_cpp"
    elif ollama_match:
        preferred, source = {"backend": "ollama", "base_url": OLLAMA_URL, "model": ollama_match}, "ollama"
    return {
        "bundled": bool(base and mmproj and sizes_ok),
        "files": {"model": bool(base), "mmproj": bool(mmproj),
                  "mb": round(sum(p.stat().st_size for p in (base, mmproj) if p) / 1048576)},
        "runtimes": {
            "llama_cpp": {"binary": bool(llm_exe), "running": bool(llm_running), "port": LLAMA_PORT},
            "ollama": {"binary": bool(ollama_exe), "reachable": bool(names), "imported": ollama_match},
        },
        "preferred": preferred,
        "preferred_source": source,
        "model": m.get("display_name", "xiaomi-ocr-0"),
    }


def serve_start() -> dict:
    """Start llama-server on the packaged model; safe to call repeatedly."""
    global _llama_proc
    with _llama_lock:
        running = _llama_running()
        if running:
            return {"ok": True, "config": {"backend": "openai", **running}, "reused": True}
        if not shutil.which("llama-server"):
            return {"ok": False, "error": "未找到 llama-server（brew install llama.cpp），或未运行 Ollama"}
        base, mmproj = model_files()
        if not base or not mmproj:
            return {"ok": False, "error": "app/model 下缺少模型文件（见 app/model/MANIFEST.json）"}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / "llama-server.log", "ab")
        # cwd=MODEL_DIR + relative names keeps the exposed model id short.
        _llama_proc = subprocess.Popen(
            ["llama-server", "--model", base.name, "--mmproj", mmproj.name,
             "--port", str(LLAMA_PORT), "--ctx-size", "32768"],
            cwd=str(MODEL_DIR), stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if _llama_proc.poll() is not None:
                return {"ok": False, "error": f"llama-server 启动失败（退出码 {_llama_proc.returncode}），详见 app/out/llama-server.log"}
            running = _llama_running()
            if running:
                return {"ok": True, "config": {"backend": "openai", **running}, "reused": False}
            time.sleep(1.0)
        serve_stop()
        return {"ok": False, "error": "llama-server 启动超时"}


def serve_stop() -> None:
    global _llama_proc
    with _llama_lock:
        if _llama_proc and _llama_proc.poll() is None:
            _llama_proc.terminate()
            try:
                _llama_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _llama_proc.kill()
        _llama_proc = None
        if _llama_running():
            # Orphan from a previous App run: clear the port we own.
            try:
                out = subprocess.run(["lsof", "-ti", f"tcp:{LLAMA_PORT}"],
                                     capture_output=True, text=True, timeout=5).stdout.split()
                for pid in out:
                    if pid.isdigit():
                        os.kill(int(pid), 15)
            except Exception:  # noqa: BLE001
                pass


def import_ollama() -> dict:
    """Import the packaged GGUF into a running Ollama (blobs dedupe by digest)."""
    if not shutil.which("ollama"):
        return {"ok": False, "error": "未安装 ollama"}
    if not _ollama_names() and _get_json(OLLAMA_URL + "/api/version") is None:
        return {"ok": False, "error": "Ollama 服务未运行（ollama serve）"}
    base, mmproj = model_files()
    if not base or not mmproj:
        return {"ok": False, "error": "app/model 下缺少模型文件"}
    name = manifest().get("runtimes", {}).get("ollama", {}).get("import_name", "xiaomi-ocr-0:app")
    proc = subprocess.run(["ollama", "create", name, "-f", "Modelfile"],
                          cwd=str(MODEL_DIR), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout)[-400:]}
    return {"ok": True, "config": {"backend": "ollama", "base_url": OLLAMA_URL, "model": name}}
