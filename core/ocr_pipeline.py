#!/usr/bin/env python3
"""Standalone OCR pipeline for the App — same task semantics as the Demo,
but the model endpoint (base URL / model id / API key) comes from the UI
on every request instead of environment variables.

Backends:
  openai   — any OpenAI-compatible /v1/chat/completions server
             (vLLM, SGLang, Ollama /v1, OpenAI, DashScope, OpenRouter, ...)
  ollama   — Ollama native /api/generate with the checkpoint's raw template
             (matches pipeline/ollama_backend.py used by ollama.sh)

The App is standalone: postprocessing/cropping/layout helpers live in core/vendor
(copied from the repository, logic unchanged) so the output quality matches
demo/MCP while app/ can run on its own with just Pillow/pypdfium2/numpy.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

from .vendor.cropping import crop_region, fit_image
from .vendor.document import finalize_document
from .vendor.layout_regions import serialize_regions
from .vendor.otsl import convert_otsl_to_html
from .vendor.region import merge_page_to_markdown

PROMPTS = {
    "document": (
        "Extract all information from the main body of the document image and represent it "
        "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
        "format, formulas in the document should be represented using LATEX format, and the "
        "parsing should be organized according to the reading order."
    ),
    "text": "Extract the text in the image.",
    "table": "Parse the table in the image into OTSL.",
    "formula": "Identify the formula in the image and represent it using LATEX format.",
}

DEFAULTS = {"max_tokens": 4096, "temperature": 0.0, "concurrency": 4, "dpi": 150}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ApiError(RuntimeError):
    pass


# ── model configuration (filled in the UI, sent with every request) ──────────

def normalize_config(cfg: dict) -> dict:
    """Validate and normalize the per-request model settings from the UI."""
    if not isinstance(cfg, dict):
        raise ApiError("缺少模型配置")
    backend = str(cfg.get("backend") or "openai").strip().lower()
    if backend not in {"openai", "ollama"}:
        raise ApiError("backend 必须是 openai 或 ollama")
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not base:
        raise ApiError("请填写模型 API 地址（Base URL）")
    if not model:
        raise ApiError("请填写模型名称")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError("API 地址需要以 http:// 或 https:// 开头")

    base = base.removesuffix("/chat/completions").rstrip("/")
    if backend == "ollama":
        base = base.removesuffix("/v1").removesuffix("/api").rstrip("/")
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK:
            raise ApiError("Ollama 原生接口需要本机 http 地址（如 http://127.0.0.1:11434）")
    else:
        path = parsed.path.rstrip("/")
        if path in ("", "/") or not path.endswith("/v1"):
            base += "/v1"

    temperature = cfg.get("temperature", DEFAULTS["temperature"])
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ApiError("temperature 需在 0～2 之间")
    return {"backend": backend, "base_url": base, "model": model, "api_key": api_key,
            "temperature": float(temperature)}


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def check_connection(cfg: dict) -> dict:
    """Probe the configured endpoint and report whether the model is listed."""
    base, backend = cfg["base_url"], cfg["backend"]
    if backend == "ollama":
        data = _get_json(base + "/api/tags", cfg["api_key"], timeout=8)
        models = [m.get("name", "") for m in data.get("models", [])]
    else:
        data = _get_json(base + "/models", cfg["api_key"], timeout=8)
        models = [m.get("id", "") for m in data.get("data", data.get("models", []))]
    models = [m for m in models if m]
    found = cfg["model"] in models
    return {"ok": True, "models": models[:50], "total": len(models), "model_found": found,
            "error": "" if found or not models else "服务端未列出该模型，请检查模型名称"}


def _get_json(url: str, api_key: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, headers=_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ApiError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接 {url}: {exc.reason}") from exc


# ── inference ─────────────────────────────────────────────────────────────────

def _image_png(raw: bytes) -> bytes:
    try:
        from PIL import Image, ImageOps
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        fit_image(image)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"无法解码图像: {exc}") from exc


def _chat(cfg: dict, image: bytes, prompt: str, max_tokens: int, slots: threading.Semaphore) -> dict:
    if cfg["backend"] == "ollama":
        if not slots.acquire(blocking=False):
            slots.acquire()
        try:
            from .vendor.ollama_backend import complete
            return complete(image, prompt, max_tokens, base_url=cfg["base_url"],
                            model=cfg["model"], temperature=cfg["temperature"])
        except RuntimeError as exc:
            raise ApiError(str(exc)) from exc
        finally:
            slots.release()

    url = cfg["base_url"] + "/chat/completions"
    encoded = base64.b64encode(_image_png(image)).decode()
    payload = {"model": cfg["model"], "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        {"type": "text", "text": prompt},
    ]}], "max_tokens": max_tokens, "temperature": cfg["temperature"], "top_p": 0.8}
    hostname = urllib.parse.urlparse(cfg["base_url"]).hostname
    if hostname in LOOPBACK:
        # Local vLLM / SGLang extensions; cloud APIs reject unknown fields.
        payload["presence_penalty"] = 0.0
        payload["repetition_penalty"] = 1.0
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if not slots.acquire(blocking=False):
        slots.acquire()
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=_headers(cfg["api_key"]), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400 and "maximum context length" in body.lower():
                raise ApiError("图像/提示词加输出超出模型上下文，请调小 max_tokens 或输入尺寸") from exc
            raise ApiError(f"模型服务返回 HTTP {exc.code}: {body[:400]}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"模型服务不可用（{url}）: {exc.reason}") from exc
    finally:
        slots.release()

    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ApiError(f"模型返回格式异常: {json.dumps(data)[:300]}") from exc
    if isinstance(message, list):
        message = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in message)
    text = str(message or "").strip()
    if not text:
        raise ApiError("模型返回了空结果")
    if data["choices"][0].get("finish_reason") == "length":
        raise ApiError(f"输出被截断（max_tokens={max_tokens}），请在模型上下文允许范围内调大 max_tokens")
    return {"text": text, "usage": data.get("usage") or {}}


# ── layout regions (optional PaddleX, same as demo) ──────────────────────────

_LAYOUT_MODEL = None
_LAYOUT_LOCK = threading.Lock()


def _layout_regions(png: bytes) -> list[dict]:
    global _LAYOUT_MODEL
    try:
        import numpy as np
        import paddlex as pdx
        from PIL import Image
    except ImportError as exc:
        raise ImportError(str(exc)) from exc
    img = Image.open(io.BytesIO(png)).convert("RGB")
    with _LAYOUT_LOCK:
        if _LAYOUT_MODEL is None:
            _LAYOUT_MODEL = pdx.create_model("PP-DocLayoutV3", threshold=0.3)
        result = list(_LAYOUT_MODEL.predict(np.asarray(img)))[0]
    return serialize_regions(result, *img.size)


def _crop(image, box) -> bytes:
    out = io.BytesIO()
    crop_region(image, box).save(out, format="PNG")
    return out.getvalue()


def assemble_regions(regions: list[dict], texts: list[str]) -> str:
    rows = []
    for region, text in zip(regions, texts):
        if region.get("task_type") == "table":
            try:
                text = convert_otsl_to_html(text)
            except Exception:
                pass
        rows.append({**region, "merged": text})
    return merge_page_to_markdown(rows)


def merge_usage(a, b):
    result = dict(a)
    for key, value in b.items():
        if isinstance(value, dict):
            result[key] = merge_usage(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        elif isinstance(value, (int, float)):
            previous = result.get(key)
            result[key] = (previous if isinstance(previous, (int, float)) else 0) + value
    return result


# ── tasks ─────────────────────────────────────────────────────────────────────

def ocr_image(cfg: dict, *, image_base64: str, mode: str = "page", max_tokens: int = DEFAULTS["max_tokens"],
              prompt: str | None = None, concurrency: int = DEFAULTS["concurrency"],
              slots: threading.Semaphore | None = None, progress=None) -> dict:
    if mode not in {"page", "region"}:
        raise ApiError("mode 必须是 page 或 region")
    slots = slots or threading.Semaphore(1)
    png = _image_png(_decode_b64(image_base64, "图像"))
    if mode == "page":
        result = _chat(cfg, png, prompt or PROMPTS["document"], max_tokens, slots)
        return {"mode": "page", "markdown": finalize_document(result["text"]), "usage": result["usage"]}

    warning = ""
    try:
        regions = [r for r in _layout_regions(png) if r.get("task_type") not in {"skip", "abandon"}]
    except ImportError:
        regions = []
        warning = "未安装 PaddleX 版面检测依赖，已自动回退到整页模式。"
    if not regions:
        if not warning:
            warning = "版面检测没有可用区域，已回退到整页模式。"
        result = _chat(cfg, png, prompt or PROMPTS["document"], max_tokens, slots)
        return {"mode": "page_fallback", "markdown": finalize_document(result["text"]),
                "usage": result["usage"], "region_results": [], "warning": warning}

    from PIL import Image
    image = Image.open(io.BytesIO(png)).convert("RGB")
    results, details = [""] * len(regions), [None] * len(regions)
    usage, done_count = {}, [0]
    lock = threading.Lock()

    def run(i: int):
        region = regions[i]
        started = time.monotonic()
        result = _chat(cfg, _crop(image, region["bbox_2d"]),
                       PROMPTS.get(region.get("task_type", "text"), PROMPTS["text"]), max_tokens, slots)
        return i, result, int((time.monotonic() - started) * 1000)

    workers = min(max(1, concurrency), len(regions))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run, i) for i in range(len(regions))]):
            i, result, ms = future.result()
            results[i] = result["text"]
            usage = merge_usage(usage, result["usage"])
            details[i] = {"bbox": regions[i]["bbox_2d"], "label": regions[i]["label"],
                          "bucket": regions[i].get("task_type", "text"), "ok": True, "ms": ms,
                          "chars": len(result["text"])}
            with lock:
                done_count[0] += 1
                if progress:
                    progress({"kind": "regions", "done": done_count[0], "total": len(regions)})
    return {"mode": "region", "regions": len(regions), "markdown": assemble_regions(regions, results),
            "region_results": details, "usage": usage, "concurrency": workers, "warning": warning}


def _decode_b64(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value.split(",", 1)[-1], validate=True)
    except Exception as exc:
        raise ApiError(f"{label} 的 base64 无效或被截断") from exc


# Optional live progress hook: each task accepts a callable(event: dict).


def _render_pdf(raw: bytes, max_pages: int | None, dpi: int, on_render=None, cancelled=None) -> list[bytes | None]:
    if dpi <= 0 or (max_pages is not None and max_pages < 1):
        raise ApiError("dpi 和 max_pages 必须为正数")
    if not raw.startswith(b"%PDF"):
        raise ApiError("上传的文件不是 PDF")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ApiError("PDF 解析需要 pypdfium2，请先安装 requirements.txt") from exc
    with pdfium.PdfDocument(raw) as doc:
        count = len(doc) if max_pages is None else min(len(doc), max_pages)
        pages = []
        for i in range(count):
            if cancelled and cancelled():
                break
            with closing(doc[i]) as page:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            blank = all(low == high and low >= 240 for low, high in image.getextrema())
            if image.width > 2200:
                from PIL import Image
                scale = 2200 / image.width
                image = image.resize((2200, int(image.height * scale)), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, "PNG")
            png = out.getvalue()
            pages.append(None if blank else png)
            if on_render:
                on_render(i + 1, png)
        return pages


def ocr_pdf(cfg: dict, *, pdf_path: str | None = None, pdf_base64: str | None = None, mode="page",
            max_pages=None, dpi=DEFAULTS["dpi"], max_tokens=DEFAULTS["max_tokens"],
            concurrency=DEFAULTS["concurrency"], prompt=None, on_render=None, on_page=None,
            cancelled=None, progress=None) -> dict:
    if mode not in {"page", "region"}:
        raise ApiError("mode 必须是 page 或 region")
    raw = open(pdf_path, "rb").read() if pdf_path else _decode_b64(pdf_base64, "PDF")
    pages = _render_pdf(raw, max_pages, dpi, on_render=on_render, cancelled=cancelled)
    slots = threading.Semaphore(max(1, concurrency))

    def run(i: int):
        started = time.monotonic()
        try:
            if cancelled and cancelled():
                raise ApiError("cancelled")
            if pages[i] is None:
                return i, {"ok": True, "text": "", "usage": {}, "ms": 0, "error": ""}
            page_progress = (lambda ev, _p=i + 1: progress({**ev, "page": _p})) if progress else None
            result = ocr_image(cfg, image_base64=base64.b64encode(pages[i]).decode(), mode=mode,
                               max_tokens=max_tokens, prompt=prompt, concurrency=concurrency,
                               slots=slots, progress=page_progress)
            return i, {"ok": True, "text": result["markdown"], "usage": result.get("usage", {}),
                       "ms": int((time.monotonic() - started) * 1000), "error": ""}
        except ApiError as exc:
            if str(exc) == "cancelled":
                raise
            return i, {"ok": False, "text": f"<!-- 第 {i + 1} 页解析失败: {exc} -->", "usage": {},
                       "ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
        except Exception as exc:  # keep_partial behaviour: one bad page does not kill the book
            return i, {"ok": False, "text": f"<!-- 第 {i + 1} 页解析失败: {exc} -->", "usage": {},
                       "ms": int((time.monotonic() - started) * 1000), "error": str(exc)}

    outputs, usage = [None] * len(pages), {}
    # Blank pages carry no model work: record them up front so progress is honest.
    live = []
    for i, page_img in enumerate(pages):
        if page_img is None:
            outputs[i] = {"ok": True, "text": "", "usage": {}, "ms": 0, "error": ""}
            if on_page:
                on_page(i + 1, outputs[i], len(pages))
            if progress:
                progress({"kind": "pages", "done": sum(o is not None for o in outputs), "total": len(pages)})
        else:
            live.append(i)
    workers = max(1, concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, i) for i in live]
        for future in as_completed(futures):
            i, result = future.result()  # a cancelled/failed task raises here
            outputs[i] = result
            usage = merge_usage(usage, result["usage"])
            if on_page:
                on_page(i + 1, result, len(pages))
            if progress:
                progress({"kind": "pages", "done": sum(o is not None for o in outputs), "total": len(pages)})
    body = "\n\n---\n\n".join(f"<!-- 第 {i + 1} 页 -->\n\n{result['text']}"
                              for i, result in enumerate(outputs) if result is not None)
    failed = sum(not result["ok"] for result in outputs if result)
    return {"pages": len(pages), "mode": mode, "markdown": body, "usage": usage,
            "blank_pages": sum(page is None for page in pages),
            "failed_pages": failed,
            "warning": "部分页面解析失败，已保留可恢复的输出。" if failed else ""}


def kie_image(cfg: dict, *, image_base64: str, fields: list[str] | None = None,
              max_tokens: int = DEFAULTS["max_tokens"], slots: threading.Semaphore | None = None) -> dict:
    slots = slots or threading.Semaphore(1)
    png = _image_png(_decode_b64(image_base64, "图像"))
    if fields:
        schema = {str(f): "" for f in fields}
        prompt = ("Extract key information in the image\n\nPlease output the key information in JSON format "
                  "according to the following schema:\n" + json.dumps(schema, ensure_ascii=False, indent=4))
    else:
        prompt = ("Extract the key information from the document image as a JSON object. Use concise field names. "
                  "Return JSON only; use an empty string for missing values.")
    raw = _chat(cfg, png, prompt, max_tokens, slots)["text"]
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return {"json": None, "raw": raw, "warning": "模型输出中没有 JSON 对象"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"json": None, "raw": raw, "warning": "模型输出中的 JSON 无法解析"}
    if fields:
        data = {f: data.get(f, "") for f in fields}
    return {"json": data}


def vqa_image(cfg: dict, *, image_base64: str, question: str, slots: threading.Semaphore | None = None) -> dict:
    if not question.strip():
        raise ApiError("请输入关于图像内容的问题")
    slots = slots or threading.Semaphore(1)
    png = _image_png(_decode_b64(image_base64, "图像"))
    prompt = question.strip() + "\nAnswer the question using a single word or phrase."
    return {"answer": _chat(cfg, png, prompt, 1024, slots)["text"]}


def layout_status() -> dict:
    """Report whether PaddleX region detection is usable in this environment."""
    dependencies = {"paddle": "PaddlePaddle", "paddlex": "PaddleX", "numpy": "NumPy",
                    "PIL": "Pillow", "pypdfium2": "pypdfium2"}
    import importlib.util
    missing = [name for module, name in dependencies.items()
               if importlib.util.find_spec(module) is None]
    return {"region_available": not missing, "missing": missing}
