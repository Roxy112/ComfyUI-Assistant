# -*- coding: utf-8 -*-
# workflow_utils.py
"""
文件级文档字符串：本模块的用途
================================

本模块（workflow_utils.py）提供 ComfyUI 工作流（workflow）相关的实用工具函数。

工作流（workflow）是 ComfyUI 前端导出的 JSON 格式图数据，结构大致为：
    {
        "name": "工作流名称",
        "nodes": [ { "id", "type", "title", "inputs", "widgets_values", ... }, ... ],
        "links": [ [链接ID, 源节点ID, 源输出索引, 目标节点ID, 目标输入索引, 类型], ... ],
    }

主要职责：
1. 在工作流节点中按类型/标题查找节点（find_nodes）；
2. 修改节点小部件（widget）的值（set_widget）；
3. 生成工作流的精简摘要（summarize_workflow）；
4. 从 ComfyUI 服务端拉取节点对象信息（load_object_info）；
5. 将前端 workflow 转换为后端 API 所需的 prompt 格式（convert_workflow）；
6. 将常用生成参数（正向/反向提示词、种子、步数、CFG、采样器等）批量写入工作流
   的对应节点（apply_common_params）。
"""
import json
import urllib.request


def find_nodes(workflow, node_type=None, title=None):
    """
    在工作流中查找符合条件的节点。

    参数：
        workflow (dict): 工作流数据（含 "nodes" 列表）。
        node_type (str | None): 需要的节点类型（如 "PrimitiveNode"、"KSampler"）。
                                为 None 时不按类型过滤。
        title (str | None): 需要的节点标题（忽略大小写，按去除首尾空白后比较）。
                            为 None 时不按标题过滤。

    返回：
        list[dict]：所有同时满足 node_type 和 title 条件的节点列表。

    逻辑说明：
        当同时指定 node_type 和 title 时，两者是「与」的关系（都要满足）。
        标题比较时对标题做 strip() 并统一转大写，实现大小写不敏感的模糊匹配。
    """
    result = []
    for node in workflow.get("nodes", []):
        # 指定了类型但节点类型不匹配则跳过
        if node_type and node.get("type") != node_type:
            continue
        # 指定了标题但标题（忽略大小写）不匹配则跳过
        if title and (node.get("title") or "").strip().upper() != title.upper():
            continue
        result.append(node)
    return result


def set_widget(node, index, value):
    """
    设置节点第 index 个小部件（widget）的值为 value。

    参数：
        node (dict): 节点对象（需包含 "widgets_values" 列表）。
        index (int): 要修改的小部件下标。
        value: 新值（类型视具体 widget 而定）。

    返回：
        bool：设置成功返回 True；节点为空、缺少 widgets_values、
              或 index 越界时返回 False。

    逻辑说明：
        通过读取并修改 node["widgets_values"] 列表的指定下标实现。
        修改后写回原节点，保证引用同一对象。
    """
    # 节点不存在或没有小部件值列表则无法设置
    if not node or "widgets_values" not in node:
        return False
    values = node["widgets_values"]
    # 不是列表或下标越界则返回失败
    if not isinstance(values, list) or index >= len(values):
        return False
    values[index] = value
    node["widgets_values"] = values  # 写回，保持引用一致
    return True


def summarize_workflow(workflow):
    """
    生成工作流的精简摘要信息。

    参数：
        workflow (dict): 工作流数据。

    返回：
        dict：形如 {"name": 工作流名称, "nodes": [{"id", "type", "title",
              "widgets_values"}, ...]} 的摘要结构。
              名称缺失时使用默认值「未命名工作流」。

    逻辑说明：
        仅提取每个节点的 id / type / title / widgets_values 关键字段，
        丢弃 inputs、links 等无关信息，方便上层序列化或展示。
    """
    nodes = []
    for node in workflow.get("nodes", []):
        nodes.append({
            "id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title", ""),
            "widgets_values": node.get("widgets_values", []),
        })
    return {
        "name": workflow.get("name", "未命名工作流"),
        "nodes": nodes,
    }


def load_object_info(comfyui_url):
    """
    从 ComfyUI 服务端获取节点对象信息（object_info）。

    参数：
        comfyui_url (str): ComfyUI 服务的基础地址（如 "http://127.0.0.1:8188"）。

    返回：
        dict：object_info 数据（描述每个节点类型的输入/输出定义）。
              请求失败或解析异常时返回空字典 {}。

    逻辑说明：
        调用 ComfyUI 的 GET /object_info 接口，设置 8 秒超时，
        将返回的 JSON 文本解析为字典。任何异常都会被捕获并返回空字典，
        避免影响上层流程。
    """
    try:
        # 请求 {comfyui_url}/object_info，超时 8 秒
        with urllib.request.urlopen(f"{comfyui_url}/object_info", timeout=8) as resp:
            # 读取响应字节流并解码为 UTF-8 文本，再解析为 JSON 对象
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        # 网络异常 / 超时 / JSON 解析失败时统一返回空字典
        return {}


def convert_workflow(workflow, object_info):
    """
    将前端工作流（workflow）转换为后端 API 所需的 prompt 格式。

    参数：
        workflow (dict): 前端工作流数据，需包含 "nodes" 与 "links"。
        object_info (dict): 节点对象信息（用于获取每个节点类型的 required 输入定义）。

    返回：
        dict：标准 prompt 格式，形如：
              { "<节点ID>": {"class_type": 节点类型, "inputs": {输入名: 值或[节点ID, 输出索引]}} }

    逻辑说明：
        1. 遍历 links，建立「链接ID -> 链接数组」的映射，便于通过 input 的 link 值
           快速找到上游连接；
        2. 对每个节点，从 object_info 中取出其 required 输入列表，按定义顺序处理：
            - 若该输入有上游链接（link 不为 None），则转换为 [源节点ID, 源输出索引]；
            - 否则若还有未消费的 widget 值，则按顺序取出一个作为该输入的值
              （widget_values 与 required 顺序对齐的约定）；
        3. 以节点 id 的字符串形式作为 prompt 的键。
    """
    links = workflow.get("links", [])
    link_map = {}
    # 构建 链接ID -> 链接 的映射表
    for link in links:
        # 链接数组至少包含 6 个元素：[id, 源节点, 源输出, 目标节点, 目标输入, 类型]
        if isinstance(link, list) and len(link) >= 6:
            link_map[link[0]] = link

    prompt = {}
    for node in workflow.get("nodes", []):
        node_id = node.get("id")
        if node_id is None:
            continue
        class_type = node.get("type")
        # 建立 输入名 -> 输入定义 的映射，便于按 required 查找
        node_inputs = {inp.get("name"): inp for inp in node.get("inputs", [])}
        info = object_info.get(class_type, {})
        # required 输入定义：{输入名: 输入约束}
        required = info.get("input", {}).get("required", {})
        # 复制 widget 值列表（后续 pop 会消费，copy 避免影响原节点）
        widget_values = list(node.get("widgets_values") or [])
        inputs = {}
        for key in required:
            inp = node_inputs.get(key)
            # 若该输入存在且连接了上游链路
            if inp and inp.get("link") is not None:
                link = link_map.get(inp["link"])
                if link:
                    # 链接格式：[id, 源节点id, 源输出索引, ...]，取其前两个字段
                    inputs[key] = [link[1], link[2]]
            # 否则使用 widget 值（按序弹出一个）
            elif widget_values:
                inputs[key] = widget_values.pop(0)
        prompt[str(node_id)] = {
            "class_type": class_type,
            "inputs": inputs,
        }
    return prompt


def apply_common_params(workflow, params):
    """
    将常用生成参数批量写入工作流的对应节点。

    参数：
        workflow (dict): 工作流数据（会被就地修改）。
        params (dict): 参数字典，可包含：
            - prompt: 正向提示词文本
            - negative: 反向提示词文本
            - seed: 随机种子
            - steps: 采样步数
            - cfg: CFG 引导强度
            - sampler: 采样器名称
            - scheduler: 调度器名称
            - width / height: 生成图像宽高

    返回：
        dict：修改后的 workflow（与入参为同一对象）。

    逻辑说明：
        1. 遍历标题为 PROMPT 的 PrimitiveNode，将正向提示词写入其第 0 个 widget；
        2. 遍历所有 CLIPTextEncode 节点，将其第 0 个 widget（text）设为反向提示词；
        3. 遍历标题为 SEED 的 PrimitiveInt 节点，写入种子；
        4. 遍历 KSampler / KSamplerAdvanced 节点，按各自的 widget 下标写入
           steps / cfg / sampler / scheduler；
        5. 遍历 EmptyLatentImage / EmptySD3LatentImage 节点，写入宽高。
        所有参数仅在对应值非 None 时才写入（None 表示「不修改」）。
    """
    # 提取参数（缺省为 None）
    prompt_text = params.get("prompt", "")
    negative = params.get("negative", "")
    seed = params.get("seed")
    steps = params.get("steps")
    cfg = params.get("cfg")
    sampler = params.get("sampler")
    scheduler = params.get("scheduler")
    width = params.get("width")
    height = params.get("height")

    # 正向提示词：写入标题为 PROMPT 的 PrimitiveNode 的第 0 个 widget
    for node in find_nodes(workflow, node_type="PrimitiveNode", title="PROMPT"):
        set_widget(node, 0, prompt_text)

    # 反向提示词：写入所有 CLIPTextEncode 节点的第 0 个 widget（text 输入）
    for node in workflow.get("nodes", []):
        if node.get("type") == "CLIPTextEncode" and node.get("widgets_values"):
            node["widgets_values"][0] = negative

    # 随机种子：写入标题为 SEED 的 PrimitiveInt 节点的第 0 个 widget
    for node in find_nodes(workflow, node_type="PrimitiveInt", title="SEED"):
        if seed is not None:
            set_widget(node, 0, int(seed))

    # 采样参数 / 尺寸：遍历所有节点按类型分别处理
    for node in workflow.get("nodes", []):
        ntype = node.get("type")
        values = node.get("widgets_values")
        if not isinstance(values, list):
            continue  # 无 widget 列表的节点跳过
        if ntype == "KSamplerAdvanced" and len(values) >= 10:
            # KSamplerAdvanced 的 widget 顺序：
            # [add_noise, noise_seed, ...] -> steps/cfg/sampler/scheduler 位于下标 3~6
            if steps is not None:
                values[3] = int(steps)
            if cfg is not None:
                values[4] = float(cfg)
            if sampler:
                values[5] = sampler
            if scheduler:
                values[6] = scheduler
        elif ntype == "KSampler" and len(values) >= 7:
            # KSampler 的 widget 顺序：seed/steps/cfg/sampler/scheduler 位于下标 2~5
            if steps is not None:
                values[2] = int(steps)
            if cfg is not None:
                values[3] = float(cfg)
            if sampler:
                values[4] = sampler
            if scheduler:
                values[5] = scheduler
        if ntype in ("EmptyLatentImage", "EmptySD3LatentImage") and len(values) >= 2:
            # 空潜空间图像节点：[width, height, batch_size]，下标 0 和 1 为宽高
            if width is not None:
                values[0] = int(width)
            if height is not None:
                values[1] = int(height)
        # 写回修改后的 widget 列表
        node["widgets_values"] = values

    return workflow
