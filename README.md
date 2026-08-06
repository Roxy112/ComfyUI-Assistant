# ComfyUI Assistant

本地 AI 绘图桌面控制台，通过 pywebview 提供原生桌面窗口，后端使用 Python 标准库 HTTP 服务，前端为原生 HTML/CSS/JS，数据存储在本地 SQLite。

## 系统架构

```
┌─────────────────────────────┐     ┌──────────────────────────────┐
│   ComfyUI Assistant (:5174) │────▶│     ComfyUI Server (:8188)    │
│   pywebview 桌面窗口         │     │   模型推理 + 工作流执行        │
│   Python HTTP 后端           │     │   提示词同步插件               │
│   SQLite 本地数据库          │     │   prompt_chat_server.py       │
└─────────────────────────────┘     └──────────────────────────────┘
```

- **主后端**：`app.py` — HTTP 路由、同步检查、队列管理、代理请求
- **数据库**：`storage.py` — SQLite 表（设置/收藏/历史/资产/工作流/LoRA备注）
- **生成引擎**：`queue_engine.py` — 模型模板注入、LoRA 装配、参数替换
- **提示词解析**：`prompt_parser.py` — Markdown 表格解析、NSFW 检测、字典构建
- **工作流工具**：`workflow_utils.py` — 工作流转换、参数应用
- **ComfyUI 插件前端**：`prompt_chat.js` — 面板 UI、LoRA 管理、双向同步
- **ComfyUI 插件后端**：`prompt_chat_server.py` — 同步文件读写、WebSocket 推送

## 快速启动

### 1. 启动 ComfyUI
```batch
cd ComfyUI_windows_portable
run_nvidia_gpu_stable.bat
```

### 2. 启动助手
```batch
cd ComfyUI-Assistant
run.bat
```
然后浏览器访问 `http://127.0.0.1:5174`（桌面窗口自动打开）。

### 3. 加载模型模板
- **Z-Image Turbo**：`http://127.0.0.1:8188/?launch=z-image-workflow`
- **Fluxed Up**：`http://127.0.0.1:8188/?launch=fluxed-up-workflow`

## 功能特性

| 模块 | 功能 |
|------|------|
| 生成 | 双模型支持（Z-Image / Fluxed Up）、批量生图、随机种子、队列管理 |
| 提示词库 | Markdown 词典导入、搜索筛选、拖拽填充、NSFW 开关 |
| LoRA | 自动检测兼容 LoRA、强度调节、备注持久化、推荐参数 |
| 收藏 | 提示词/图片双类型收藏、一键回填参数 |
| 历史 | 生成记录回溯、参数恢复、快速收藏 |
| 资产 | 图片预览（缩放/拖拽）、保存到本地、缩略图 |
| 设置 | ComfyUI 地址、输出目录、工作流目录配置 |
| 同步 | 提示词实时同步到 ComfyUI 画布，双端确认机制 |

## API 路由

### GET
| 路由 | 说明 |
|------|------|
| `/api/status` | 检查 ComfyUI 连接状态 |
| `/api/dictionary` | 获取提示词分类列表，支持 `?nsfw=1` |
| `/api/loras` | 代理获取 ComfyUI LoRA 列表 |
| `/api/workflows` | 列出已配置目录中的工作流文件 |
| `/api/workflow?path=` | 读取并解析工作流详情 |
| `/api/favorites` | 列出收藏，支持 `?type=prompt\|image` |
| `/api/queue` | 获取当前生成队列状态 |
| `/api/lora_notes` | 获取所有 LoRA 备注 |
| `/api/history` | 获取生成历史 |
| `/api/assets` | 获取资产（生成图片）列表 |
| `/api/settings` | 获取当前配置 |
| `/api/file?path=` | 返回本地文件（图片等） |

### POST
| 路由 | 说明 |
|------|------|
| `/api/settings` | 更新配置项 |
| `/api/favorites` | 添加收藏 |
| `/api/assets/save` | 保存图片到输出目录 |
| `/api/lora_notes` | 设置 LoRA 备注 |
| `/api/history` | 记录生成历史 |
| `/api/assets` | 添加资产记录 |
| `/api/sync_prompt` | 推送到 ComfyUI（不等待确认） |
| `/api/sync_check` | 推送并等待 ComfyUI 前端确认 |
| `/api/open_file` | 系统默认程序打开文件 |
| `/api/workflow` | 保存上传的工作流 |
| `/api/import` | 导入 Markdown 提示词文件 |
| `/api/queue` | 提交生成任务 |

### DELETE
| 路由 | 说明 |
|------|------|
| `/api/favorites?id=` | 删除收藏 |
| `/api/history?id=` | 删除单条/清空历史 |
| `/api/queue?prompt_id=` | 取消生成任务 |

## 目录结构

```
ComfyUI-Assistant/
├── app.py                # HTTP 服务主入口（路由、同步、队列）
├── storage.py            # SQLite 数据库封装
├── queue_engine.py       # 模型模板注入与参数替换
├── prompt_parser.py      # Markdown 提示词解析
├── workflow_utils.py     # 工作流读写工具
├── run.bat               # 启动脚本（pythonw）
├── web/
│   ├── index.html        # 前端页面
│   ├── app.js            # 前端逻辑
│   └── styles.css        # 样式
├── data/
│   ├── assistant.db      # SQLite 数据库
│   ├── generated/        # 生成的 workflow JSON（带时间戳）
│   ├── imports/          # 导入的 Markdown 文件
│   ├── workflows/        # 用户上传的工作流
│   └── model_templates/  # 模型模板（zimage.json / fluxed_up.json）
└── README.md
```

> `data/` 目录仅用于本地数据，默认不提交到 Git，避免个人记录与敏感提示词外泄。

## 提示词同步机制

```
用户修改提示词
   │
   ▼
app.js: syncPromptToComfyUI()  ──debounce 150ms──▶  POST /api/sync_prompt
                                                          │
                                                          ▼
                                              app.py: proxy_comfyui()
                                              POST /api/prompt_chat/sync
                                                          │
                                                          ▼
                                              prompt_chat_server.py
                                              写入 prompt_chat_sync.json
                                              发送 WebSocket push
                                                          │
                                                          ▼
                                              prompt_chat.js: pollPromptSync()
                                              (800ms 轮询 + WebSocket 监听)
                                                          │
                                                          ▼
                                              applyPromptSync()
                                              - 找到所有 CLIPTextEncode
                                              - 追踪到 PrimitiveStringMultiline 源
                                              - 写入 widget.value
                                              - setDirtyCanvas 触发重绘
                                              - POST sync_ack 确认
                                                          │
                                                          ▼
                                              app.py: sync_and_check()
                                              轮询 sync_ack 直到匹配
                                              返回 {ok: true}
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ASSISTANT_PORT` | 助手服务端口 | `5174` |
| `PROMPT_DICT_DIR` | 提示词词典目录 | `C:\Users\...\AI绘图` |

## 部署

- `--no-gui`：以无桌面窗口模式运行（仅 HTTP 服务）
- ComfyUI 需先启动并监听 `127.0.0.1:8188`
- 提示词同步依赖 ComfyUI-Prompt-Chat 插件

## 依赖

- Python 3.11+
- pywebview（桌面窗口，可选）
- ComfyUI + ComfyUI-Prompt-Chat 自定义节点
