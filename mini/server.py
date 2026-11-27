#!/usr/bin/env python3
"""Xiaomi-OCR-0 Mini — same UI as the App, but the model is NOT bundled.

The user must install llama.cpp or Ollama beforehand and configure
xiaomi-ocr-0 on the machine (see README). The UI auto-detects a running
llama-server on APP_LLAMA_PORT or an Ollama model matching "xiaomi-ocr-0"
and wires it in with zero configuration. Nothing is downloaded or imported.

Task features: document parsing, KIE, VQA, PDF books with live progress.

Run:
    python3 mini/server.py --port 8790
Then open http://127.0.0.1:8790/
"""
from __future__ import annotations

import argparse
import base64
import errno
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core import model_manager
from core import ocr_pipeline as pipeline

STATIC = ROOT / "web"
BOOK_TMP = ROOT / "out" / "books"
SAMPLES = ROOT / "web" / "samples"

MAX_BODY = 80 * 1024 * 1024  # 80 MB (PDF books)

# Sample documents bundled inside app/ so the folder runs standalone.
SAMPLE_FILES = {
    "receipt":        {"path": SAMPLES / "receipt.jpg",            "task": "document", "title": "购物小票",   "desc": "真实票据 · 文本与金额"},
    "formula":        {"path": SAMPLES / "dense-equations.jpg",    "task": "document", "title": "论文公式",   "desc": "密集公式 · 多行推导"},
    "qianziwen":      {"path": SAMPLES / "qianziwen.jpeg",         "task": "document", "title": "行书千字文", "desc": "古籍书法 · 竖排文字"},
    "handwrite":      {"path": SAMPLES / "handwrite-formula.jpg",  "task": "document", "title": "手写公式",   "desc": "手写笔记 · 公式"},
    "sample_document": {"path": SAMPLES / "sample_document.png",   "task": "document", "title": "文档样例",   "desc": "排版文档 · 整页解析"},
    "sample_kie":     {"path": SAMPLES / "sample_kie.png",         "task": "kie",      "title": "字段提取样例", "desc": "票据信息 · JSON 输出"},
    "sample_vqa":     {"path": SAMPLES / "sample_vqa.png",         "task": "vqa",      "title": "图像问答样例", "desc": "对照图片提问"},
}


def valid_number(req, key, low, high, default=None, allow_none=False):
    if key not in req or (req[key] is None and allow_none):
        return default
    value = req[key]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{key} 必须是 {low}～{high} 之间的整数")
    return value


def validate_request(req):
    if not isinstance(req, dict):
        raise ValueError("请求体必须是 JSON 对象")
    for key in ("task", "prompt", "fields", "question", "mode", "image_base64",
                "pdf_base64", "pdf_name", "sample"):
        if key in req and not isinstance(req[key], str):
            raise ValueError(f"{key} 必须是字符串")
    valid_number(req, "max_tokens", 1, 16384)
    valid_number(req, "concurrency", 1, 16, default=4)
    valid_number(req, "dpi", 36, 300, default=150)
    valid_number(req, "max_pages", 1, 10000, allow_none=True)
    if "task" in req:
        req["task"] = req["task"].strip().lower()
        if req["task"] not in {"document", "kie", "vqa"}:
            raise ValueError("task 必须是 document、kie 或 vqa")
    return req


def parse_fields(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").replace("，", ",").replace("\n", ",").split(",") if x.strip()]


JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def job_get(jid):
    with JOBS_LOCK:
        job = JOBS.get(jid)
        return dict(job) if job else None


def job_update(jid, **values):
    with JOBS_LOCK:
        JOBS.setdefault(jid, {}).update(values)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive + friendlier for browser download managers

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[app] %s - %s\n" % (self.address_string(), fmt % args))

    # ── GET ────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/env":
            env = pipeline.layout_status()
            env["defaults"] = pipeline.DEFAULTS
            env["app"] = "xiaomi-ocr-mini"
            return self._json(200, env)
        if path == "/api/model":
            return self._json(200, model_manager.status())
        if path == "/api/samples":
            items = [{"id": key, "task": item["task"], "title": item["title"], "desc": item["desc"],
                      "image": f"/api/sample/{key}", "available": item["path"].is_file()}
                     for key, item in SAMPLE_FILES.items()]
            return self._json(200, {"items": [i for i in items if i["available"]]})
        if path.startswith("/api/sample/"):
            key = os.path.basename(path.rsplit("/", 1)[-1])
            item = SAMPLE_FILES.get(key)
            if not item or not item["path"].is_file():
                return self._json(404, {"error": "sample not found"})
            return self._send_bytes(item["path"].read_bytes(),
                                    mimetypes.guess_type(str(item["path"]))[0] or "application/octet-stream")
        if path.startswith("/api/book/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 4 and parts[3] == "raw":
                return self._book_raw(parts[2])
            if len(parts) >= 5 and parts[3] == "page":
                return self._book_page(parts[2], parts[4])
            return self._book_status(parts[2] if len(parts) >= 3 else "")
        return super().do_GET()

    # ── POST ───────────────────────────────────────────────────────────────
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/check":
            return self._handle_check()
        if path == "/api/model/serve":
            return self._json(200, model_manager.serve_start())
        if path == "/api/model/stop":
            model_manager.serve_stop()
            return self._json(200, {"ok": True})
        if path == "/api/run":
            return self._handle_run()
        if path == "/api/run/stream":
            return self._handle_run(stream=True)
        if path == "/api/export":
            return self._handle_export()
        if path == "/api/book":
            return self._handle_book_start()
        if path.startswith("/api/book/") and path.endswith("/cancel"):
            jid = path.split("/")[-2]
            if not job_get(jid):
                return self._json(404, {"error": "job not found"})
            job_update(jid, cancel=True)
            return self._json(200, {"ok": True})
        self.send_error(404, "not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── request plumbing ───────────────────────────────────────────────────
    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "缺少或过大的 Content-Length"})
            return None
        try:
            return validate_request(json.loads(self.rfile.read(length)))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return None

    def _config(self, req):
        """Model settings come from the UI with every request."""
        return pipeline.normalize_config(req.get("config") or {})

    def _send_bytes(self, data: bytes, ctype: str, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    # ── model connection check ────────────────────────────────────────────
    def _handle_check(self):
        req = self._read_json()
        if req is None:
            return
        try:
            return self._json(200, pipeline.check_connection(self._config(req)))
        except pipeline.ApiError as exc:
            return self._json(200, {"ok": False, "error": str(exc)})

    # ── image tasks (document / kie / vqa) ─────────────────────────────────
    def _run_image_task(self, req):
        cfg = self._config(req)
        task = req.get("task", "document")
        if not req.get("image_base64"):
            raise pipeline.ApiError("请先选择一张图片")
        max_tokens = req.get("max_tokens") or pipeline.DEFAULTS["max_tokens"]
        started = time.monotonic()
        if task == "kie":
            result = pipeline.kie_image(cfg, image_base64=req["image_base64"],
                                        fields=parse_fields(req.get("fields", "")), max_tokens=max_tokens)
            content = json.dumps(result["json"] if result.get("json") is not None else result,
                                 ensure_ascii=False, indent=2)
        elif task == "vqa":
            result = pipeline.vqa_image(cfg, image_base64=req["image_base64"],
                                        question=req.get("question", ""))
            content = result["answer"]
        else:
            result = pipeline.ocr_image(cfg, image_base64=req["image_base64"],
                                        mode=req.get("mode", "page"), max_tokens=max_tokens,
                                        prompt=req.get("prompt") or None,
                                        concurrency=req.get("concurrency") or 4,
                                        progress=self._stream_progress)
            content = result["markdown"]
        return {"content": content, "result": result, "model": cfg["model"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "usage": result.get("usage", {}), "warning": result.get("warning", ""),
                "mode": result.get("mode", req.get("mode", "page")),
                "regions": result.get("region_results", [])}

    _stream_progress = None  # set per streaming request; callable(event) or None

    def _handle_run(self, stream=False):
        req = self._read_json()
        if req is None:
            return
        if not stream:
            try:
                out = self._run_image_task(req)
            except pipeline.ApiError as exc:
                return self._json(502, {"error": str(exc)})
            return self._json(200, out)

        # SSE: progress events come from pipeline worker threads.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True  # HTTP/1.1: end the stream when this handler returns
        self.connection.settimeout(600)
        write_lock = threading.Lock()
        outcome = {}

        def send(event, data):
            with write_lock:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

        def progress(ev):
            try:
                send("progress", ev)
            except OSError:
                pass

        def run():
            self._stream_progress = progress
            try:
                outcome["ok"] = True
                outcome["value"] = self._run_image_task(req)
            except Exception as exc:  # noqa: BLE001
                outcome["ok"] = False
                outcome["error"] = str(exc)
            finally:
                self._stream_progress = None

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(0.5)
        if outcome.get("ok"):
            send("done", outcome["value"])
        else:
            send("error", {"error": outcome.get("error", "unknown error")})

    # ── PDF book jobs ──────────────────────────────────────────────────────
    def _handle_book_start(self):
        req = self._read_json()
        if req is None:
            return
        try:
            cfg = self._config(req)
        except pipeline.ApiError as exc:
            return self._json(400, {"error": str(exc)})
        mode = (req.get("mode") or "page").strip().lower()
        if mode not in {"page", "region"}:
            return self._json(400, {"error": "PDF mode 必须是 page 或 region"})
        max_tokens = req.get("max_tokens") or pipeline.DEFAULTS["max_tokens"]
        max_pages = req.get("max_pages")
        dpi = req.get("dpi") or 150
        concurrency = min(16, req.get("concurrency") or 2)
        prompt = req.get("prompt") or None

        sample = (req.get("sample") or "").strip()
        pdf_name = (req.get("pdf_name") or "book.pdf").strip()
        try:
            if sample and sample in SAMPLE_FILES and SAMPLE_FILES[sample]["path"].is_file():
                pdf_path = SAMPLE_FILES[sample]["path"]
                pdf_name = pdf_path.name
            elif req.get("pdf_base64"):
                pdf_path = self._save_upload(req["pdf_base64"], pdf_name)
            else:
                return self._json(400, {"error": "请上传 PDF 或选择样例"})
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": f"PDF 保存失败: {exc}"})

        jid = uuid.uuid4().hex[:12]
        job_update(jid, status="queued", total=0, done=0, model=cfg["model"], source=pdf_name,
                   created=int(time.time()), cancel=False, concurrency=concurrency, mode=mode)
        threading.Thread(target=run_book_job, args=(jid, cfg, pdf_path, max_tokens, max_pages,
                                                    dpi, concurrency, prompt, mode), daemon=True).start()
        return self._json(200, {"id": jid, "source": pdf_name, "concurrency": concurrency, "mode": mode})

    def _save_upload(self, b64: str, name: str) -> Path:
        try:
            data = base64.b64decode(b64.split(",", 1)[-1], validate=True)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"base64 无效: {exc}") from exc
        BOOK_TMP.mkdir(parents=True, exist_ok=True)
        safe = os.path.basename(name) or "book.pdf"
        fp = BOOK_TMP / f"upload_{uuid.uuid4().hex[:8]}_{safe}"
        fp.write_bytes(data)
        return fp

    def _book_status(self, jid: str):
        job = job_get(jid)
        if not job:
            return self._json(404, {"error": "job not found"})
        public = {k: v for k, v in job.items() if k not in {"markdown"}}
        public["has_markdown"] = bool(job.get("markdown"))
        return self._json(200, public)

    def _book_raw(self, jid: str):
        job = job_get(jid)
        if not job:
            return self._json(404, {"error": "job not found"})
        md = job.get("markdown") or ""
        return self._json(200, {"id": jid, "status": job.get("status"), "markdown": md,
                                "chars": len(md), "elapsed_ms": job.get("elapsed_ms")})

    def _book_page(self, jid: str, page_s: str):
        job = job_get(jid)
        if not job:
            self.send_error(404, "job not found")
            return
        try:
            n = int(page_s)
        except ValueError:
            self.send_error(400, "bad page")
            return
        pages_dir = job.get("pages_dir")
        fp = Path(pages_dir) / f"p{n:04d}.png" if pages_dir else None
        if not fp or not fp.is_file():
            self.send_error(404, "page not found")
            return
        return self._send_bytes(fp.read_bytes(), "image/png", {"Cache-Control": "max-age=3600"})

    # ── export ─────────────────────────────────────────────────────────────
    def _handle_export(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError("导出大小无效")
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=4)
            content = form.get("content", [""])[0]
            extension = form.get("format", ["md"])[0]
            if not content or extension not in {"md", "json"}:
                raise ValueError("导出需要 content 和 md/json 格式")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._send_bytes(content.encode("utf-8"), "application/octet-stream",
                                {"Content-Disposition": f'attachment; filename="xiaomi-ocr-result.{extension}"'})


def run_book_job(jid, cfg, pdf_path, max_tokens, max_pages, dpi, concurrency, prompt, mode):
    pages_dir = BOOK_TMP / jid / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    job_update(jid, pages_dir=str(pages_dir), status="running", page_log=[], started=time.monotonic())

    def on_render(page, png):
        (pages_dir / f"p{page:04d}.png").write_bytes(png)
        job = job_get(jid) or {}
        job_update(jid, total=max(job.get("total", 0), page))

    def on_page(page, result, total):
        job = job_get(jid) or {}
        log = job.get("page_log", []) + [{"page": page, "ok": result["ok"], "ms": result["ms"],
                                          "chars": len(result["text"]), "error": result["error"],
                                          "img": f"/api/book/{jid}/page/{page}"}]
        usage = pipeline.merge_usage(job.get("usage", {}), result["usage"])
        job_update(jid, page_log=sorted(log, key=lambda row: row["page"]), usage=usage,
                   done=len(log))

    try:
        result = pipeline.ocr_pdf(cfg, pdf_path=str(pdf_path), mode=mode, max_pages=max_pages, dpi=dpi,
                                  max_tokens=max_tokens, concurrency=concurrency, prompt=prompt,
                                  on_render=on_render, on_page=on_page,
                                  cancelled=lambda: (job_get(jid) or {}).get("cancel", False))
        if (job_get(jid) or {}).get("cancel"):
            job_update(jid, status="cancelled")
            return
        out_path = BOOK_TMP / f"{jid}.md"
        out_path.write_text(result["markdown"], encoding="utf-8")
        started_at = (job_get(jid) or {}).get("started", time.monotonic())
        job_update(jid, status="error" if result["warning"] else "done", error=result["warning"],
                   markdown=result["markdown"], result_path=str(out_path), usage=result["usage"],
                   elapsed_ms=int((time.monotonic() - started_at) * 1000))
    except pipeline.ApiError as exc:
        if str(exc) == "cancelled" or (job_get(jid) or {}).get("cancel"):
            job_update(jid, status="cancelled")
        else:
            job_update(jid, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        job_update(jid, status="error", error=str(exc))


def main():
    ap = argparse.ArgumentParser(description="Xiaomi-OCR-0 standalone app server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8791)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open the browser automatically")
    args = ap.parse_args()

    BOOK_TMP.mkdir(parents=True, exist_ok=True)
    url = f"http://{args.host}:{args.port}/"
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 10048}:  # 10048 = WSAEADDRINUSE (Windows)
            instance = _detect_existing(args.host, args.port)
            if instance:
                print(f"App 已在运行：{url}\n"
                      f"直接打开该地址即可，无需重复启动；要再起一个实例请用 --port 8791")
            else:
                print(f"端口 {args.port} 已被其他程序占用：{exc}\n"
                      f"换一个端口启动：python3 app/server.py --port 8791")
            sys.exit(1)
        raise

    print(f"Xiaomi-OCR-0 Mini  →  {url}")
    print("需自备运行时：先安装 llama.cpp 或 Ollama，并配置好 xiaomi-ocr-0（见 README）。模型调用只发往本机。Ctrl+C 退出。")
    # Auto-start llama-server only if the user pointed us at local GGUF files via env.
    auto_base, _ = model_manager.auto_serve_model()
    if shutil.which("llama-server") and auto_base:
        threading.Thread(target=model_manager.serve_start, daemon=True).start()
        print("检测到 APP_LLAMA_MODEL，正在启动 llama-server…")
    if args.open:
        threading.Timer(0.5, lambda: __import__("webbrowser").open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        model_manager.serve_stop()
        print("\nbye")


def _detect_existing(host: str, port: int) -> bool:
    """True when the listener on this port is already this App (GET /api/env marker)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/env", timeout=2) as resp:
            return json.loads(resp.read()).get("app") == "xiaomi-ocr-mini"
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    main()
