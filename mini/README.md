# Xiaomi-OCR-0 Mini
官方项目地址：https://github.com/xiaomi-research/Xiaomi-OCR-0 
以官方 demo/ 功能为基础重做的独立 App 界面 的**免模型版**：界面与功能完全一致，但**不打包模型**（整个目录约 3MB）。
模型运行时与 xiaomi-ocr-0 需要先装好、配好，App 打开后自动检测接入，零配置使用。

## 使用要求（先完成这两步）

### 1. 安装 llama.cpp 或 Ollama（二选一）

```bash
brew install llama.cpp     # 方式 A：llama.cpp
# 或
brew install ollama        # 方式 B：Ollama（ollama.com 也有安装包）
```

### 2. 配置 xiaomi-ocr-0 模型

**方式 B（Ollama，推荐，一条命令）：**

```bash
ollama serve                        # 如未运行
ollama pull longwayxu/xiaomi-ocr-0  # Mini 自动识别任何名字含 xiaomi-ocr-0 的模型
```

**方式 A（llama.cpp）：** 从 HuggingFace `SeerRay-Lab/Xiaomi-OCR-0` 下载 GGUF + 视觉 mmproj，
然后二选一：

```bash
# A1：自己启动 llama-server（Mini 会自动接入 :8190 上运行的服务）
llama-server --model xiaomi-ocr-0-bf16.gguf --mmproj xiaomi-ocr-0-bf16-mmproj.gguf \
             --port 8190 --ctx-size 32768

# A2：让 Mini 替你启动（把路径给它即可）
export APP_LLAMA_MODEL=/path/to/xiaomi-ocr-0-bf16.gguf
export APP_LLAMA_MMPROJ=/path/to/xiaomi-ocr-0-bf16-mmproj.gguf
./run.sh
```

> 运行时只能访问本机（loopback），Mini 不会把图像发往任何外部 API。

## 启动

```bash
./run.sh                  # 默认 http://127.0.0.1:8791（与主 App 的 8790 互不冲突）
PORT=9000 ./run.sh        # 指定端口
# 或已有 Python 环境（只需 Pillow、pypdfium2、numpy）：
pip install -r core/requirements.txt
python3 server.py --port 8791 --open
```

打开浏览器：状态行显示「**使用本机模型** · xiaomi-ocr-0 … · llama-server 运行中 :8190」
或「· Ollama · longwayxu/xiaomi-ocr-0:latest」→ 绿点后直接拖图/选样例开跑。
未配置好时，状态行会直接提示缺什么（装运行时 / 拉模型 / 起 llama-server）。

## 接入优先级与探测规则

| 顺序 | 条件 | 结果 |
| --- | --- | --- |
| 1 | 本机 `:8190`（可用 `APP_LLAMA_PORT` 改）有 llama-server 在跑 | 直接用，模型名取其 `/v1/models` |
| 2 | Ollama 运行中且有名字含 `xiaomi-ocr-0` 的模型 | 走 Ollama 原生通道 |
| 3 | 装了 llama.cpp 且设置了 `APP_LLAMA_MODEL` | Mini 自动拉起 llama-server |
| — | 都不满足 | 状态行给出补齐指引，不下载任何东西 |

Ctrl+C 退出时**只停止 Mini 自己启动的** llama-server，你自己启动的不会被杀。

## 功能（与主 App 一致）

文档解析（Markdown + OTSL 表格 + LaTeX）、字段提取（KIE）、图像问答（VQA）、
PDF 整本解析（并发、进度、取消、导出 .md）、预览/原文/JSON 视图、复制/下载、7 个内置样例。
高级设置（Token/Temperature/并发/DPI/页数）在「高级设置 ▾」下拉中即时保存。

## 与主 App（app/）的区别

| | app/ | mini/（本目录） |
| --- | --- | --- |
| 模型文件 | 内置 GGUF（~1.7GB），自动接入 | **无**，需按上文自备 |
| 目录体积 | ~1.7GB | ~3MB |
| 运行时 | 自动探测并拉起 | 探测接入；仅在给定模型路径时拉起 |
| 默认端口 | 8790 | 8791 |

## 目录

```
mini/
  server.py         # 入口（与主 App 同源，去掉模型导入/打包逻辑）
  run.sh            # 一键启动（自动建 venv + 装依赖）
  README.md
  core/             # 核心模块：ocr_pipeline / model_manager(仅本机探测) / requirements / vendor
  web/              # 前端（原生 JS + KaTeX + samples，可离线）
  out/              # 运行时缓存与日志（自动创建）
```

## 快捷键

`⌘/Ctrl + Enter` 开始解析；参数在「高级设置 ▾」中调整。
