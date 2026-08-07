"""
ComfyUI 助手主程序（后端 HTTP 服务）。

本模块是 ComfyUI 助手应用的后端入口，基于 Python 标准库的
`http.server` 提供多线程 HTTP 服务，负责以下职责：

1. 为前端（web 目录下的静态页面）提供 RESTful API 接口，包括：
   状态查询、词典（提示词字典）、LoRA 列表、工作流管理、
   收藏/历史/素材管理、设置读写、排队生成、文件导入导出等。
2. 作为代理层转发请求到本地 ComfyUI 服务（默认 http://127.0.0.1:8188），
   并封装成更友好的接口供前端调用。
3. 维护一个进程内任务队列 QUEUE，跟踪每次文生图任务的排队、执行、
   成功/失败/取消状态，并在后台线程中轮询 ComfyUI 历史接口以确认生成结果。
4. 启动本地 HTTP 服务器，并尝试以 WebView 窗口打开桌面应用界面；
   若 WebView 不可用（如缺少 pywebview），则退化为纯命令行方式运行。
"""

import json
import os
import random
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import prompt_parser
import queue_engine
import workflow_utils
from storage import AssistantDB

# ---- 全局常量与状态 ----
# 各目录基于当前文件所在位置推算，方便随程序整体迁移
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")          # 前端静态资源目录
DATA_DIR = os.path.join(BASE_DIR, "data")        # 数据存储根目录
GENERATED_DIR = os.path.join(DATA_DIR, "generated")  # 生成的临时工作流文件
WORKFLOW_DIR = os.path.join(DATA_DIR, "workflows")   # 上传保存的工作流目录
IMPORT_DIR = os.path.join(DATA_DIR, "imports")       # 导入的 md 词典文件目录
# ComfyUI 输出图片的绝对路径（本地固定路径）
COMFY_OUTPUT_DIR = r"E:\ComfyUI\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\ComfyUI\output"

# 全局数据库实例（SQLite 封装，负责持久化各类业务数据）
db = AssistantDB(DATA_DIR)
# 进程内文生图任务队列：键为 prompt_id，值为任务信息字典
QUEUE = {}
QUEUE_LOCK = threading.RLock()

MODEL_REQUIREMENTS = {
    "Z-Image Turbo": [
        ("UnetLoaderGGUF", "unet_name", "z_image_turbo-Q4_K_M.gguf"),
        ("CLIPLoaderGGUF", "clip_name", "Qwen3-4B-Q4_K_M.gguf"),
        ("VAELoader", "vae_name", "ae.safetensors"),
    ],
    "Fluxed Up": [
        ("UnetLoaderGGUF", "unet_name", "fluxedup-v10-q4_0.gguf"),
        ("DualCLIPLoaderGGUF", "clip_name1", "clip_l.safetensors"),
        ("DualCLIPLoaderGGUF", "clip_name2", "t5-v1_1-xxl-encoder-Q5_K_M.gguf"),
        ("VAELoader", "vae_name", "ae.safetensors"),
    ],
}


def get_comfyui_url():
    """获取已配置的 ComfyUI 服务地址。

    从数据库读取 comfyui_url 设置，默认 http://127.0.0.1:8188，
    并去掉末尾多余的正斜杠，保证拼接路径时格式统一。

    返回：
        str: ComfyUI 的基础 URL，例如 "http://127.0.0.1:8188"。
    """
    return db.get_setting("comfyui_url", "http://127.0.0.1:8188").rstrip("/")


def comfyui_online(url=None):
    """检测 ComfyUI 服务是否在线。

    通过访问 ComfyUI 的 /system_stats 接口（超时 3 秒）判断连通性。

    参数：
        url (str, 可选): 要检测的 ComfyUI 地址；为空时使用配置的地址。

    返回：
        bool: 返回 HTTP 200 视为在线，否则返回 False。
    """
    url = url or get_comfyui_url()
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def proxy_comfyui(path, method="GET", body=None):
    """代理请求：把请求转发给 ComfyUI 服务。

    这是本程序与 ComfyUI 交互的统一入口，将 JSON 请求体编码后
    发给 ComfyUI，并把响应解析为 JSON 返回。

    参数：
        path (str): ComfyUI 的接口路径，例如 "/prompt" 或 "/history/{id}"。
        method (str, 可选): HTTP 方法，默认 "GET"。
        body (dict, 可选): 请求体，会序列化为 JSON；None 表示无请求体。

    返回：
        tuple: (status_code, json_data)。成功时为 ComfyUI 的状态码与
        解析后的 JSON；发生异常时返回 (502, {"error": 异常信息})。
    """
    url = get_comfyui_url() + path
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        # 网络异常 / 超时 / 解析失败时统一包装为 502
        return 502, {"error": str(exc)}


def get_model_status(model):
    """检查 ComfyUI 是否已识别指定模型工作流所需的全部文件。"""
    requirements = MODEL_REQUIREMENTS.get(model)
    if not requirements:
        return {"ready": False, "error": f"不支持的模型：{model}", "missing": []}
    status, object_info = proxy_comfyui("/object_info")
    if status != 200 or not isinstance(object_info, dict):
        return {
            "ready": False,
            "error": "ComfyUI 未连接或无法读取模型列表",
            "missing": [],
        }
    missing = []
    for node_type, input_name, filename in requirements:
        try:
            choices = object_info[node_type]["input"]["required"][input_name][0]
        except (KeyError, IndexError, TypeError):
            missing.append(filename)
            continue
        if filename not in choices:
            missing.append(filename)
    return {"ready": not missing, "missing": missing, "error": ""}


def list_workflow_files():
    """列出所有可用的工作流文件。

    遍历数据库中配置的工作流目录（workflow_dirs，JSON 数组），
    收集每个目录下以 .json 结尾的文件，并做路径去重。

    返回：
        list: 每个元素为 {"name", "path", "modified"} 的字典列表，
        按目录内文件名排序。
    """
    dirs = json.loads(db.get_setting("workflow_dirs", '["D:\\\\liulanqi"]'))
    results = []
    seen = set()
    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(folder, name)
            if path in seen:
                continue
            seen.add(path)
            results.append({
                "name": name,
                "path": path,
                "modified": os.path.getmtime(path),
            })
    return results


def read_workflow(path):
    """读取指定路径的工作流 JSON 文件。

    参数：
        path (str): 工作流文件的绝对路径。

    返回：
        dict|None: 解析后的工作流字典；文件不存在或解析失败时返回 None。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_uploaded_workflow(payload):
    """保存前端上传的工作流内容到本地文件。

    参数：
        payload (dict): 上传的数据，包含 name（可选）和 workflow（工作流字典）。

    返回：
        str: 保存后的文件绝对路径。
    """
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    name = payload.get("name") or f"workflow_{int(time.time())}.json"
    if not name.lower().endswith(".json"):
        name += ".json"
    # 只取文件名部分，防止路径穿越
    safe_name = os.path.basename(name)
    path = os.path.join(WORKFLOW_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload.get("workflow", {}), f, ensure_ascii=False, indent=2)
    return path


def apply_queue(workflow, params):
    """根据参数构建文生图任务并提交到 ComfyUI 队列。

    支持批量生成（batch_count）：每批次重新生成随机种子（若开启
    random_seed），调用 queue_engine.apply_template 构建模型模板，
    将生成的提示词工作流保存为临时文件，再通过代理提交到 ComfyUI
    的 /prompt 接口，随后启动后台线程监视该任务的状态。

    参数：
        workflow (dict): 前端传来的工作流结构（本函数实际使用 params）。
        params (dict): 生成参数，如 model、prompt、negative、batch_count、
            seed、random_seed、loras 等。

    返回：
        dict: 成功时 {"ok": True, "prompt_ids": [...], "queue_size": n}；
        失败时 {"ok": False, "error": ...}，若已生成临时文件还会附带 saved_path。
    """
    os.makedirs(GENERATED_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    batch_count = max(1, int(params.get("batch_count", 1) or 1))
    random_seed = bool(params.get("random_seed", False))
    base_seed = int(params.get("seed", 0) or 0)
    prompt_ids = []

    # ---- 批量循环：逐张图片构建提示词并提交 ----
    for index in range(batch_count):
        # 是否随机种子：开启则每次随机，否则沿用用户指定种子
        seed = random.randint(0, 4294967295) if random_seed else base_seed
        current_params = dict(params or {})
        current_params["seed"] = seed
        try:
            prompt = queue_engine.apply_template(
                current_params.get("model", "Z-Image Turbo"),
                current_params,
                current_params.get("loras", []),
            )
        except Exception as exc:
            # 模板构建失败（如模型名不支持），直接返回错误
            return {
                "ok": False,
                "error": f"无法构建模型模板：{exc}",
            }

        saved_path = os.path.join(GENERATED_DIR, f"workflow_{timestamp}_{index}.json")
        with open(saved_path, "w", encoding="utf-8") as f:
            json.dump(prompt, f, ensure_ascii=False, indent=2)

        # ---- 代理请求：提交提示词到 ComfyUI 排队 ----
        status, result = proxy_comfyui("/prompt", method="POST", body={"prompt": prompt})
        if status != 200:
            # 提交失败，尝试清理已提交但未开始的任务
            cancelled = []
            uncleaned = []
            for pid in prompt_ids:
                task = QUEUE.get(pid, {})
                if task.get("status") == "queued":
                    # 向 ComfyUI 发送取消请求
                    proxy_comfyui("/queue", method="POST", body={"delete": [pid]})
                    # 检查是否成功取消（仍在排队即为成功取消）
                    _, qdata = proxy_comfyui("/queue")
                    pending = {str(item[1]) for item in (qdata.get("queue_pending", []) if isinstance(qdata, dict) else [])}
                    if pid not in pending:
                        cancelled.append(pid)
                        QUEUE.pop(pid, None)
                    else:
                        uncleaned.append(pid)
                else:
                    # 已在运行中，无法取消
                    uncleaned.append(pid)
            error_msg = f"第 {index + 1}/{batch_count} 张提交失败：{result}"
            if cancelled:
                error_msg += f"；已取消 {len(cancelled)} 个排队任务"
            if uncleaned:
                error_msg += f"；以下任务已提交无法清理：{', '.join(uncleaned)}"
            return {
                "ok": False,
                "saved_path": saved_path,
                "submitted_prompt_ids": prompt_ids,
                "cancelled_prompt_ids": cancelled,
                "uncleaned_prompt_ids": uncleaned,
                "error": error_msg,
            }
        prompt_id = result.get("prompt_id")
        if prompt_id:
            # 登记任务到内存队列，供前端实时查询状态
            with QUEUE_LOCK:
                QUEUE[prompt_id] = {
                "prompt_id": prompt_id,
                "model": current_params.get("model", ""),
                "prompt": current_params.get("prompt", ""),
                "params": current_params,
                "saved_path": saved_path,
                "status": "queued",
                "image_path": "",
                "created_at": time.time(),
                }
            # 启动后台守护线程，轮询确认生成结果
            threading.Thread(
                target=watch_generation,
                args=(prompt_id, current_params, saved_path),
                daemon=True,
            ).start()
            prompt_ids.append(prompt_id)

    return {
        "ok": True,
        "prompt_ids": prompt_ids,
        "queue_size": len(QUEUE),
    }


def watch_generation(prompt_id, params, workflow_path):
    """后台轮询 ComfyUI 历史接口，确认生成是否成功。

    每隔 2 秒查询一次 /history/{prompt_id}，最多轮询 180 次（约 6 分钟）。
    一旦发现状态为 error 则标记失败；状态为 success 时把输出图片写入
    素材库与历史记录，并更新内存队列中的任务状态。

    参数：
        prompt_id (str): ComfyUI 分配的任务 ID。
        params (dict): 生成参数（用于记录素材/历史的元信息）。
        workflow_path (str): 对应临时工作流文件的保存路径。
    """
    url = get_comfyui_url() + f"/history/{prompt_id}"
    # ---- 轮询确认：循环等待 ComfyUI 完成生成 ----
    for _ in range(180):
        time.sleep(2)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                history = json.loads(resp.read().decode("utf-8"))
            item = history.get(prompt_id)
            if not item:
                continue
            status = item.get("status", {})
            status_str = status.get("status_str")
            if status_str == "error":
                # 生成出错，标记任务失败后结束轮询
                if prompt_id in QUEUE:
                    QUEUE[prompt_id]["status"] = "failed"
                return
            if status_str != "success":
                # 仍在排队/执行中，继续下一轮
                continue
            # 生成成功：遍历输出节点收集图片文件
            for output in item.get("outputs", {}).values():
                for image in output.get("images", []):
                    full_path = os.path.join(
                        COMFY_OUTPUT_DIR,
                        image.get("subfolder", ""),
                        image.get("filename", ""),
                    )
                    # 确认图片实际存在后写入素材库与历史记录
                    if os.path.isfile(full_path):
                        params = params or {}
                        db.add_asset(
                            full_path,
                            full_path,
                            params.get("prompt", ""),
                            params.get("model", ""),
                            params,
                        )
                        db.add_history(
                            params.get("prompt", ""),
                            params.get("negative", ""),
                            params.get("model", ""),
                            params,
                            workflow_path,
                            full_path,
                        )
                        if prompt_id in QUEUE:
                            QUEUE[prompt_id].update(status="success", image_path=full_path)
            # 兜底：即使没找到图片也标记为成功，避免任务一直悬挂
            if prompt_id in QUEUE and QUEUE[prompt_id].get("status") != "success":
                QUEUE[prompt_id]["status"] = "success"
            return
        except Exception:
            # 单次查询异常时忽略，继续下一轮重试
            continue


def refresh_queue_status():
    """刷新内存队列中各任务的状态。

    通过代理查询 ComfyUI 的 /queue 接口，获取正在运行与排队的任务 ID，
    据此把 QUEUE 中尚未结束的任务状态更新为 running（运行中）或
    queued（排队中）；已结束的任务（success/cancelled/failed/error）
    保持原样不再改动。
    """
    try:
        status, data = proxy_comfyui("/queue")
        running = set()
        pending = set()
        if status == 200:
            # queue_running / queue_pending 每项形如 [..., prompt_id, ...]
            running = {str(item[1]) for item in data.get("queue_running", [])}
            pending = {str(item[1]) for item in data.get("queue_pending", [])}
    except Exception:
        running = set()
        pending = set()
    with QUEUE_LOCK:
        tasks = list(QUEUE.items())
    for prompt_id, task in tasks:
        if task.get("status") in ("success", "cancelled", "failed", "error"):
            continue
        if prompt_id in running:
            task["status"] = "running"
        elif prompt_id in pending:
            task["status"] = "queued"


def cancel_queue_item(prompt_id):
    """取消指定的排队任务。

    向 ComfyUI 发送删除请求（/queue 的 delete 字段）和中断请求
    （/interrupt），然后从内存队列中移除该任务并标记为 cancelled。

    参数：
        prompt_id (str): 要取消的任务 ID。
    """
    status, data = proxy_comfyui("/queue")
    if status != 200 or not isinstance(data, dict):
        return False
    running = {str(item[1]) for item in data.get("queue_running", []) if len(item) >= 2}
    pending = {str(item[1]) for item in data.get("queue_pending", []) if len(item) >= 2}
    if prompt_id in pending:
        status, _ = proxy_comfyui("/queue", method="POST", body={"delete": [prompt_id]})
    elif prompt_id in running:
        status, _ = proxy_comfyui("/interrupt", method="POST")
    else:
        status = 200
    if status != 200:
        return False
    with QUEUE_LOCK:
        QUEUE.pop(prompt_id, None)
    return True


def sync_and_check(prompt, negative, timeout=2.5, require_negative=True):
    """把提示词同步到 ComfyUI 的 Prompt-Chat 插件并等待确认。

    先向 /api/prompt_chat/sync 提交当前提示词，然后在超时时间内
    反复查询 /api/prompt_chat/sync_ack 的回执，直到回执内容与提交内容
    一致（表示 ComfyUI 端已成功同步）。

    参数：
        prompt (str): 正向提示词。
        negative (str): 反向提示词。
        timeout (float, 可选): 等待确认的最长时间（秒），默认 2.5。

    返回：
        bool: 在超时前收到匹配的回执返回 True，否则返回 False。
    """
    status, _ = proxy_comfyui(
        "/prompt_chat/sync",
        method="POST",
        body={"prompt": prompt, "negative": negative},
    )
    if status != 200:
        return False
    # ---- 轮询确认：循环等待 Prompt-Chat 插件返回匹配回执 ----
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, ack = proxy_comfyui("/prompt_chat/sync_ack")
        if (
            status == 200
            and ack.get("prompt") == prompt
            and (not require_negative or ack.get("negative") == negative)
            and ack.get("positive_applied", True)
            and (not require_negative or ack.get("negative_applied", True))
        ):
            return True
        time.sleep(0.2)
    return False


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。

    继承自 BaseHTTPRequestHandler，通过 do_GET / do_POST / do_DELETE
    实现 RESTful API，同时提供静态文件服务（前端页面）。
    每个请求在独立的线程中执行（配合 ThreadingHTTPServer）。
    """

    def log_message(self, fmt, *args):
        # 关闭默认的请求日志输出，保持控制台干净
        pass

    def _send_response(self, data, status=200, content_type="application/json; charset=utf-8"):
        """发送 HTTP 响应体。

        支持字符串、字典、列表三种数据，自动序列化并附带
        Content-Length 与 Cache-Control: no-store 响应头。

        参数：
            data: 响应内容，可为 str / dict / list。
            status (int, 可选): HTTP 状态码，默认 200。
            content_type (str, 可选): 响应 Content-Type，默认 JSON。
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif isinstance(data, dict) or isinstance(data, list):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data, status=200):
        """发送 JSON 响应（_send_response 的简写封装）。

        参数：
            data: 要序列化的数据（dict / list / str）。
            status (int, 可选): HTTP 状态码，默认 200。
        """
        self._send_response(data, status)

    def _read_body(self):
        """读取并解析请求体 JSON。

        返回：
            dict: 解析后的请求体；无请求体或解析失败时返回空字典 {}。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # 静态文件扩展名 -> Content-Type 映射表
    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".gif": "image/gif",
    }

    def _serve_static(self, path):
        """服务前端静态文件。

        仅允许访问 WEB_DIR 目录内的文件（使用 realpath 校验前缀，
        防止路径穿越），文件不存在或越界时返回 404。

        参数：
            path (str): 请求路径，如 "/" 或 "/index.html"。
        """
        if path in ("", "/"):
            path = "index.html"
        full = os.path.realpath(os.path.join(WEB_DIR, path.lstrip("/")))
        # 路径安全检查：必须位于 WEB_DIR 内部且文件真实存在
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send_response(f.read(), 200, self._CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _send_file(self, path):
        """按绝对路径发送文件（用于浏览本机已生成的图片等）。

        参数：
            path (str): 文件的绝对路径；文件不存在时返回 404。
        """
        if not os.path.isfile(path):
            self.send_error(404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self._send_response(f.read(), 200, self._CONTENT_TYPES.get(ext, "application/octet-stream"))

    # ---- 路由处理：GET 请求 ----
    def do_GET(self):
        """处理所有 GET 请求。

        依据请求路径分发到各 API 端点；若路径不匹配任何 API，
        则按静态文件处理返回前端页面。
        """
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 查询 ComfyUI 连接状态
        if route == "/api/status":
            url = get_comfyui_url()
            self._send_json({
                "connected": comfyui_online(url),
                "comfyui_url": url,
            })
            return

        if route == "/api/model_status":
            model = query.get("model", [""])[0]
            result = get_model_status(model)
            result["model"] = model
            self._send_json(result, 200 if not result.get("error") else 422)
            return

        # 获取提示词词典（支持 nsfw 过滤参数）
        if route == "/api/dictionary":
            include_nsfw = query.get("nsfw", ["0"])[0] == "1"
            files, nsfw_total = prompt_parser.build_dictionary(include_nsfw=include_nsfw)
            self._send_json({"files": files, "nsfw_total": nsfw_total})
            return

        # 代理获取 LoRA 列表
        if route == "/api/loras":
            status, result = proxy_comfyui("/prompt_chat/loras")
            if status == 200:
                self._send_json(result)
            else:
                self._send_json([], 200)
            return

        # 列出工作流文件
        if route == "/api/workflows":
            self._send_json({"workflows": list_workflow_files()})
            return

        # 读取并摘要单个工作流（path 参数指定文件路径）
        if route == "/api/workflow":
            path = query.get("path", [""])[0]
            workflow = read_workflow(path)
            if workflow is None:
                self._send_json({"error": "无法读取工作流文件"}, 404)
            else:
                self._send_json(workflow_utils.summarize_workflow(workflow))
            return

        # 查询收藏列表（可带 type 过滤）
        if route == "/api/favorites":
            ftype = query.get("type", [""])[0] or None
            self._send_json({"favorites": db.list_favorites(ftype=ftype)})
            return

        # 查询任务队列（刷新状态后按创建时间排序返回）
        if route == "/api/queue":
            refresh_queue_status()
            with QUEUE_LOCK:
                tasks = sorted(
                    (dict(item) for item in QUEUE.values()),
                    key=lambda item: item.get("created_at", 0),
                )
            self._send_json({"queue": tasks})
            return

        # 查询 LoRA 笔记列表
        if route == "/api/lora_notes":
            self._send_json({"notes": db.list_lora_notes()})
            return

        # 查询历史记录
        if route == "/api/history":
            self._send_json({"history": db.list_history()})
            return

        # 查询素材库
        if route == "/api/assets":
            self._send_json({"assets": db.list_assets()})
            return

        # 查询设置项
        if route == "/api/settings":
            self._send_json({
                "comfyui_url": db.get_setting("comfyui_url", "http://127.0.0.1:8188"),
                "output_dir": db.get_setting("output_dir", "E:\\AI绘图\\outputs"),
                "workflow_dirs": json.loads(db.get_setting("workflow_dirs", '["D:\\\\liulanqi"]')),
            })
            return

        # 按路径读取本地文件
        if route == "/api/file":
            path = query.get("path", [""])[0]
            self._send_file(path)
            return

        # 兜底：未匹配到 API 则返回前端静态页面
        self._serve_static(parsed.path)

    # ---- 路由处理：POST 请求 ----
    def do_POST(self):
        """处理所有 POST 请求。

        依据请求路径分发到各写入型 API 端点（保存设置、添加收藏、
        保存素材、生成排队等）；未匹配的路径返回 404。
        """
        route = urllib.parse.urlparse(self.path).path
        body = self._read_body()

        # 保存设置（comfyui_url / output_dir / workflow_dirs）
        if route == "/api/settings":
            for key in ("comfyui_url", "output_dir", "workflow_dirs"):
                if key in body:
                    value = body[key]
                    # 列表/字典类型序列化为 JSON 字符串后存储
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, ensure_ascii=False)
                    db.set_setting(key, value)
            self._send_json({"ok": True})
            return

        # 添加收藏
        if route == "/api/favorites":
            favorite_id = db.add_favorite(
                body.get("prompt", ""),
                body.get("negative", ""),
                body.get("category", ""),
                body.get("tags", ""),
                body.get("type", "prompt"),
                body.get("image_path", ""),
                body.get("model", ""),
                body.get("params", {}),
            )
            self._send_json({"ok": True, "id": favorite_id})
            return

        # 保存素材到输出目录
        if route == "/api/assets/save":
            output_dir = db.get_setting("output_dir", "E:\\AI绘图\\outputs")
            os.makedirs(output_dir, exist_ok=True)
            source = body.get("path") or ""
            asset_id = body.get("asset_id")
            # 若只传了 asset_id 而未传路径，则从素材库反查源路径
            if asset_id and not source:
                for asset in db.list_assets():
                    if asset["id"] == int(asset_id):
                        source = asset["path"]
                        break
            if not source or not os.path.isfile(source):
                self._send_json({"ok": False, "error": "图片文件不存在"}, 404)
                return
            # 复制图片到输出目录，并标记素材为已保存
            target = os.path.join(output_dir, os.path.basename(source))
            shutil.copy2(source, target)
            if asset_id:
                db.set_asset_saved(int(asset_id), 1)
            self._send_json({"ok": True, "path": target})
            return

        # 保存 LoRA 笔记
        if route == "/api/lora_notes":
            db.set_lora_note(
                body.get("model", ""),
                body.get("lora_name", ""),
                body.get("note", ""),
            )
            self._send_json({"ok": True})
            return

        # 新增历史记录
        if route == "/api/history":
            history_id = db.add_history(
                body.get("prompt", ""),
                body.get("negative", ""),
                body.get("model", ""),
                body.get("params", {}),
                body.get("workflow", ""),
                body.get("image_path", ""),
            )
            self._send_json({"ok": True, "id": history_id})
            return

        # 新增素材
        if route == "/api/assets":
            asset_id = db.add_asset(
                body.get("path", ""),
                body.get("thumb", ""),
                body.get("prompt", ""),
                body.get("model", ""),
                body.get("params", {}),
            )
            self._send_json({"ok": True, "id": asset_id})
            return

        # 同步提示词到 ComfyUI 的 Prompt-Chat 插件（代理请求）
        if route == "/api/sync_prompt":
            status, result = proxy_comfyui(
                "/prompt_chat/sync",
                method="POST",
                body={
                    "prompt": body.get("prompt", ""),
                    "negative": body.get("negative", ""),
                },
            )
            self._send_json(result, 200 if status == 200 else 502)
            return

        # 同步提示词并轮询等待确认
        if route == "/api/sync_check":
            model = body.get("model", "")
            ok = sync_and_check(
                body.get("prompt", ""),
                body.get("negative", ""),
                require_negative="flux" not in model.lower(),
            )
            self._send_json({"ok": ok})
            return

        # 在本地打开文件（Windows 资源管理器 / 默认程序）
        if route == "/api/open_file":
            path = body.get("path", "")
            if path and os.path.isfile(path):
                os.startfile(path)
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return

        # 保存上传的工作流
        if route == "/api/workflow":
            path = save_uploaded_workflow(body)
            self._send_json({"ok": True, "path": path})
            return

        # 导入 md 词典文件（解析其中的表格为分类条目）
        if route == "/api/import":
            content = body.get("content", "")
            filename = body.get("filename", "imported.md")
            categories = prompt_parser.parse_md_tables(content)
            count = sum(len(c.get("items", [])) for c in categories)
            os.makedirs(IMPORT_DIR, exist_ok=True)
            safe_name = os.path.basename(filename)
            import_path = os.path.join(IMPORT_DIR, safe_name)
            with open(import_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._send_json({
                "ok": True,
                "filename": filename,
                "categories": categories,
                "count": count,
                "path": import_path,
            })
            return

        # 提交文生图任务到队列
        if route == "/api/queue":
            workflow = body.get("workflow") or {}
            result = apply_queue(workflow, body)
            self._send_json(result, 200 if result.get("ok") else 422)
            return

        # 未匹配的路径返回 404
        self._send_json({"error": "not found"}, 404)

    # ---- 路由处理：DELETE 请求 ----
    def do_DELETE(self):
        """处理所有 DELETE 请求。

        支持删除收藏、删除/清空历史记录、取消排队任务三类操作；
        未匹配的路径返回 404。
        """
        route = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # 删除指定收藏（按 id）
        if route == "/api/favorites":
            favorite_id = query.get("id", [""])[0]
            if favorite_id:
                db.delete_favorite(int(favorite_id))
            self._send_json({"ok": True})
            return
        # 删除单条历史记录；未传 id 时清空全部历史
        if route == "/api/history":
            history_id = query.get("id", [""])[0]
            if history_id:
                db.delete_history(int(history_id))
            else:
                db.clear_history()
            self._send_json({"ok": True})
            return
        # 删除指定素材记录
        if route == "/api/assets":
            asset_id = query.get("id", [""])[0]
            if asset_id:
                db.delete_asset(int(asset_id))
            self._send_json({"ok": True})
            return
        # 取消排队任务（按 prompt_id）
        if route == "/api/queue":
            prompt_id = query.get("prompt_id", [""])[0]
            if prompt_id:
                ok = cancel_queue_item(prompt_id)
                self._send_json({"ok": ok}, 200 if ok else 502)
            else:
                self._send_json({"ok": False, "error": "缺少 prompt_id"}, 400)
            return
        self._send_json({"error": "not found"}, 404)


def restore_queue_from_comfyui():
    """从 ComfyUI 的 /queue 和 /history 接口恢复任务状态。

    软件重启后，进程内的 QUEUE 字典会丢失。本函数查询 ComfyUI
    当前队列（运行中 + 排队中）以及最近的历史记录，重建 QUEUE
    中的任务条目，使前端队列页能正确显示 running/queued/success 等状态。
    """
    # 1. 查询当前队列（运行中 + 排队中）
    try:
        _, qdata = proxy_comfyui("/queue")
        if isinstance(qdata, dict):
            running_items = qdata.get("queue_running", [])
            pending_items = qdata.get("queue_pending", [])
            # queue_running/pending 每项形如 [index, prompt_id, ...]
            for item in running_items:
                if len(item) >= 2:
                    pid = str(item[1])
                    QUEUE[pid] = {
                        "prompt_id": pid,
                        "model": "",
                        "prompt": "",
                        "params": {},
                        "saved_path": "",
                        "status": "running",
                        "image_path": "",
                        "created_at": time.time(),
                    }
                    # 启动后台线程继续监视
                    threading.Thread(
                        target=watch_generation,
                        args=(pid, {}, ""),
                        daemon=True,
                    ).start()
            for item in pending_items:
                if len(item) >= 2:
                    pid = str(item[1])
                    QUEUE[pid] = {
                        "prompt_id": pid,
                        "model": "",
                        "prompt": "",
                        "params": {},
                        "saved_path": "",
                        "status": "queued",
                        "image_path": "",
                        "created_at": time.time(),
                    }
                    # 启动后台线程继续监视
                    threading.Thread(
                        target=watch_generation,
                        args=(pid, {}, ""),
                        daemon=True,
                    ).start()
    except Exception:
        pass  # ComfyUI 不可达时静默跳过

    # 2. 查询历史记录，恢复已完成/失败的任务
    try:
        _, hist = proxy_comfyui("/history")
        if isinstance(hist, dict):
            for pid, item in hist.items():
                if pid in QUEUE:
                    continue  # 已在队列中，跳过
                status_info = item.get("status", {})
                status_str = status_info.get("status_str", "")
                completed = status_info.get("completed", False)
                # 只恢复近期任务（24 小时内），避免加载过多历史
                if not completed:
                    continue
                task_status = "success" if status_str == "success" else "failed"
                image_path = ""
                # 尝试提取输出图片路径
                for output in item.get("outputs", {}).values():
                    for image in output.get("images", []):
                        full_path = os.path.join(
                            COMFY_OUTPUT_DIR,
                            image.get("subfolder", ""),
                            image.get("filename", ""),
                        )
                        if os.path.isfile(full_path):
                            image_path = full_path
                            break
                QUEUE[pid] = {
                    "prompt_id": pid,
                    "model": "",
                    "prompt": "",
                    "params": {},
                    "saved_path": "",
                    "status": task_status,
                    "image_path": image_path,
                    "created_at": time.time(),
                }
    except Exception:
        pass  # ComfyUI 不可达时静默跳过


def main():
    """程序入口：启动 HTTP 服务器并打开桌面界面。

    创建数据目录，启动 ThreadingHTTPServer 监听 127.0.0.1 的
    ASSISTANT_PORT（默认 5174）端口，随后尝试用 pywebview 弹出桌面
    窗口；若 pywebview 不可用，则退化为命令行模式直接运行服务。
    程序结束时关闭服务器。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    restore_queue_from_comfyui()
    port = int(os.environ.get("ASSISTANT_PORT", "5174"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 命令行模式：指定 --no-gui 时不弹窗，直接在前台运行服务
    if "--no-gui" in sys.argv:
        print(f"ComfyUI Assistant running at http://127.0.0.1:{port}")
        server.serve_forever()
        return
    try:
        # ---- GUI 模式：使用 pywebview 打开桌面窗口 ----
        import webview
        webview.create_window(
            "ComfyUI Assistant",
            f"http://127.0.0.1:{port}",
            width=1500,
            height=950,
            min_size=(1100, 700),
        )
        webview.start()
    except Exception:
        # pywebview 不可用时退化为命令行模式
        print(f"ComfyUI Assistant running at http://127.0.0.1:{port}")
        server.serve_forever()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
