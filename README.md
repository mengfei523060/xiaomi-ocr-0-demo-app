# Xiaomi-OCR-0 App
官方项目地址：https://github.com/xiaomi-research/Xiaomi-OCR-0
以官方 `demo/` 功能为基础重做的**独立 App 界面**：打开即用——内置模型自动接入，什么都不用填。
`app/` 整个文件夹自包含、可整体拷走，不依赖仓库任何其他目录（后处理代码已内联到 `app/core/vendor/`）。

## 使用要求

| 项目 | 要求 |
| --- | --- |
| Python | ≥ 3.10（启动时只需 Pillow、pypdfium2、numpy 三个包） |
| 模型运行时 | 二选一：**llama.cpp**（macOS 推荐 `brew install llama.cpp`）或 **Ollama** |
| 磁盘 | 模型约 1.7GB（已打包在 `app/model/`）；`out/` 作业缓存若干 |
| 浏览器 | 任意现代浏览器（前端原生 JS + 自带 KaTeX，可离线） |

模型运行时只需装其一：llama.cpp 存在时 App 自动在本机 8190 端口加载内置模型；
只有 Ollama 时自动用 `app/model/Modelfile` 导入为 `xiaomi-ocr-0:app`。全程无需填写任何 API。

## 启动方法（两种任选）

```bash
# 方式一：一键脚本（首次自动创建 app/.venv 并安装 core/requirements.txt）
./app/run.sh                # 默认 http://127.0.0.1:8790 并自动打开浏览器
PORT=8800 ./app/run.sh      # 指定端口

# 方式二：已有 Python 环境
pip install -r app/core/requirements.txt
python3 app/server.py --port 8790 --open
```

Windows 直接 `python app\server.py --port 8790 --open`（`run.sh` 是 bash 脚本）。

打开浏览器 → 状态行显示「使用内置模型 · 正在加载…」→ 绿点后直接拖入图片/选择样例即可。
端口被占用时不抛异常：已有本 App 实例则提示打开现有地址，否则提示换端口。

## 功能

- **文档解析**：整页模式（Markdown + OTSL 表格 → HTML + LaTeX 公式）。界面固定整页识别。
- **字段提取（KIE）**：逗号分隔字段名，输出 JSON 键值表。
- **图像问答（VQA）**。
- **PDF 解析**：逐页渲染（pypdfium2）、并发 OCR、实时进度与缩略图、可取消、导出 `.md`；纯空白页自动跳过。
- 结果视图：预览 / 原文 / JSON；复制、下载（浏览器本地生成 `源文件名.md/.json`，失败自动走服务器兜底）。
- 样例：`app/web/samples/` 内置 7 个（小票、论文公式、书法、手写公式、KIE、VQA 等）。
- 快捷键：`⌘/Ctrl + Enter` 开始解析。

## 高级设置（「高级设置 ▾」下拉）

默认即常用值、改动即时保存：max_tokens 4096（每页）、temperature 0、并发 4（PDF 页）、
PDF DPI 150、PDF 页数不限。

> 服务端接口（`/api/run` 的 `config` 字段、`/api/check`）仍接受自定义模型配置与 `mode:"region"`，
> 供程序化调用等进阶用途；浏览器界面不提供这些选项。

## 内置模型（app/model/）

打包了 **Xiaomi-OCR-0 0.8B BF16**（GGUF + 视觉 mmproj，共约 1.7GB，sha256 见 `MANIFEST.json`），
App 自动探测运行时并零配置接入：

| 运行时（按优先级） | 行为 |
| --- | --- |
| llama.cpp（`llama-server`） | 自动/一键在本机 8190 端口加载内置模型（`POST /api/model/serve`，Ctrl+C 退出时自动停止） |
| Ollama | 一键导入为 `xiaomi-ocr-0:app`（`POST /api/model/import`，与已 pull 的官方模型 blob 去重，秒级完成） |

换机器恢复模型：`ollama pull longwayxu/xiaomi-ocr-0` 后按其 blob 拷贝为 `app/model/` 中同名文件
（sha256 对 `MANIFEST.json`），或从 HuggingFace `SeerRay-Lab/Xiaomi-OCR-0` 下载对应 GGUF。
GGUF 已加入 `.gitignore`，不入库。

## 目录结构

```
app/
  server.py         # 入口：HTTP、SSE 进度、PDF 作业、样例/导出、内置模型接口
  run.sh            # 一键启动（自动建 venv + 装依赖）
  README.md
  core/             # 核心模块（包）
    ocr_pipeline.py    # 模型客户端（OpenAI 兼容 / Ollama）+ OCR/KIE/VQA/PDF 流水线
    model_manager.py   # app/model 内置模型：运行时探测、llama-server 起停、Ollama 导入
    requirements.txt   # Pillow、pypdfium2、numpy（推理走内置模型，无需 SDK）
    vendor/            # 内联依赖（与仓库实现逐行一致：finalize_document、区域拼装、多边形裁剪）
  model/            # 内置模型：GGUF + mmproj + MANIFEST.json + Modelfile
  web/              # 独立前端（原生 JS + 自带 KaTeX，可离线）
    samples/           # 内置样例图片
  out/              # PDF 作业缓存、llama-server 日志（gitignore）
```

根目录只保留 `server.py`、`run.sh`、`README.md` 三个文件，其余按用途归入文件夹。

## 换电脑运行（已实测）

把 `app/` 整个文件夹（**含 `model/` 两个 GGUF**）拷到新电脑，按上面的「使用要求 + 启动方法」操作即可。
已做隔离验证：全新目录、全新进程启动，模型探测、样例、OCR、导出全部正常。
孤儿进程清理依赖 `lsof`，Windows 缺失时自动跳过，不影响使用。
