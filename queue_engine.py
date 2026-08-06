"""
queue_engine.py — 生成队列引擎（工作流模板应用模块）
=====================================================

本模块负责在把参数提交给 ComfyUI 之前，将用户设定的参数（提示词、种子、
步数、CFG、分辨率、LoRA 等）注入到标准的工作流模板 JSON 中，生成可直接
执行的最终工作流。

设计思路：
    - 每种模型（当前支持 Z-Image 与 Flux 两类）对应一份 JSON 模板，
      存放在 `data/model_templates` 目录下；
    - 模块按"模板名 + 参数 + LoRA 列表"动态改写模板节点，返回新的模板字典；
    - 提供通用工具函数（`_find_nodes`、`_set_*` 系列）供具体模型适配函数复用。

主要入口：
    - `apply_template(model, params, loras)` —— 根据模型自动分派到
      `apply_flux` 或 `apply_zimage`。

说明:
    尽管文件名带有 "queue"（队列）字眼，本文件当前实际承担的是
    "模板参数注入"（template application）职责，是生成任务预处理的一环。
"""

import copy  # 深拷贝工具（当前代码中未直接使用，保留导入）
import json  # 读写 JSON 模板文件
import os    # 拼接模板目录路径

# 模型模板存放目录：位于本文件同级 data/model_templates 文件夹下
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "model_templates")


def load_template(model):
    """
    根据模型名称加载对应的工作流模板。

    参数:
        model (str): 模型名称（用于判断模板类型，含 "flux" 即视为 Flux）。

    返回:
        dict: 解析后的模板 JSON，格式为 {节点id: 节点定义} 的字典。

    说明:
        - 模板文件以 key 命名（"fluxed_up.json" / "zimage.json"），
          通过 `key + ".json"` 拼出文件路径；
        - 命名存在隐含约定：Flux 模板对应文件为 `fluxed_up.json`，
          其他模型一律使用 `zimage.json` 模板。
    """
    # 模型名包含 "flux"（不区分大小写）→ 使用 Flux 模板，否则使用 Z-Image 模板
    key = "fluxed_up" if "flux" in model.lower() else "zimage"
    # 拼接模板文件完整路径
    path = os.path.join(TEMPLATE_DIR, key + ".json")
    # 以 UTF-8 编码读取 JSON 模板文件
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_nodes(template, class_type=None, title_part=None):
    """
    在模板中查找符合指定条件的节点。

    参数:
        template (dict): 工作流模板字典 {节点id: 节点定义}。
        class_type (str | None): 需要匹配的节点类型（如 "KSamplerAdvanced"）；
                                 为 None 时不按类型过滤。
        title_part (str | None): 节点标题中需包含的子串（不区分大小写）；
                                 为 None 时不按标题过滤。

    返回:
        list[tuple[str, dict]]: 匹配的 (节点id, 节点定义) 列表。

    说明:
        两种过滤条件均可选，可组合使用；都不传时返回模板全部节点。
    """
    result = []  # 保存匹配结果
    # 遍历模板中的每一个节点
    for node_id, node in template.items():
        # 若指定了 class_type，且节点类型不匹配则跳过
        if class_type and node.get("class_type") != class_type:
            continue
        # 读取节点标题（存于 _meta.title 中，可能为空）
        title = node.get("_meta", {}).get("title", "")
        # 若指定了标题关键字，且标题不包含该关键字（忽略大小写）则跳过
        if title_part and title_part.lower() not in title.lower():
            continue
        # 同时满足条件则加入结果
        result.append((node_id, node))
    return result


def _set_prompt_text(template, sampler_input, prompt, negative):
    """
    将提示词写入采样器引用的 CLIPTextEncode 节点。

    参数:
        template (dict): 工作流模板字典。
        sampler_input (dict): 采样器节点的 inputs 字典，
                              其中 positive/negative 为指向文本节点的引用。
        prompt (str): 正向提示词文本。
        negative (str): 反向提示词文本。

    说明:
        ComfyUI 中节点间的连接以 `[被引用节点id, 输出槽位]` 的列表形式表示。
        采样器的 `positive` / `negative` 输入即指向 CLIPTextEncode 节点，
        本函数沿引用找到目标节点并改写其 `text` 输入字段。
    """
    # 同时处理正向与反向两组 (输入名, 文本)
    for input_name, text in (("positive", prompt), ("negative", negative)):
        # 取出引用，格式形如 ["12", 0]；引用无效（缺目标节点）则跳过
        ref = sampler_input.get(input_name)
        if not ref or ref[0] not in template:
            continue
        # 根据引用 id 找到实际节点
        node = template[ref[0]]
        # 仅当节点是 CLIPTextEncode 且含 text 输入字段时才写入
        if node.get("class_type") == "CLIPTextEncode" and "text" in node.get("inputs", {}):
            node["inputs"]["text"] = text  # 覆盖提示词文本


def _set_seed(template, sampler_input, seed):
    """
    设置采样器使用的随机种子。

    参数:
        template (dict): 工作流模板字典。
        sampler_input (dict): 采样器节点的 inputs 字典。
        seed (int): 目标种子值。

    说明:
        不同模板中控制种子的字段名可能是 `value` 或 `noise_seed`，
        这里做兼容处理：优先使用已有的字段名写入。
    """
    # 取出噪声种子输入引用（如 ["12", 0]）
    ref = sampler_input.get("noise_seed")
    if not ref or ref[0] not in template:
        return  # 引用无效则直接返回
    node = template[ref[0]]
    inputs = node.get("inputs", {})
    # 兼容两种字段名：有 value 用 value，否则用 noise_seed
    key = "value" if "value" in inputs else "noise_seed"
    # 写入种子，并强制转为整数
    if key in inputs:
        inputs[key] = int(seed)


def _set_resolution(template, sampler_input, width, height):
    """
    设置生成图像的分辨率（宽/高）。

    参数:
        template (dict): 工作流模板字典。
        sampler_input (dict): 采样器节点的 inputs 字典。
        width (int | None): 目标宽度；为 None 时不修改。
        height (int | None): 目标高度；为 None 时不修改。

    说明:
        采样器的 `latent_image` 输入指向潜空间图源节点
        （EmptyLatentImage 或 EmptySD3LatentImage），仅对这两种节点改写宽高。
    """
    # 取出潜空间图像输入引用
    ref = sampler_input.get("latent_image")
    if not ref or ref[0] not in template:
        return  # 引用无效则返回
    node = template[ref[0]]
    # 仅处理"空潜空间图"类节点，其他类型（如图像输入）不改分辨率
    if node.get("class_type") not in ("EmptyLatentImage", "EmptySD3LatentImage"):
        return
    # 分别写入宽高（只在传入非 None 时才覆盖）
    if width is not None:
        node["inputs"]["width"] = int(width)
    if height is not None:
        node["inputs"]["height"] = int(height)


def _inject_loras(template, loras, model_node_ids, clip_node_ids):
    """在模型/CLIP 来源节点与其下游消费者之间注入 LoraLoader 节点。

    参数:
        template (dict): 工作流模板字典（会被就地修改）。
        loras (list[dict]): LoRA 配置列表，每项含 name、strength_model 等。
        model_node_ids (list[str]): 模型来源节点 id 列表（如 UnetLoaderGGUF）。
        clip_node_ids (list[str]): CLIP 来源节点 id 列表（如 DualCLIPLoaderGGUF）。

    说明:
        该函数用于 Flux 类模板的 LoRA 注入，采用"串接"策略：
        - 为每个 LoRA 新建一个 id 以 9000 起始递增的 LoraLoader 节点；
        - 第 1 个 LoraLoader 从原始 model/clip 节点取输入，
          之后的 LoraLoader 以上一个 LoraLoader 的输出作为输入，
          从而形成 chain（链式串接）；
        - 最后把所有原本直接连到 model/clip 来源节点的下游输入，
          改接到链尾 LoraLoader 的输出上。
    """
    # 任一条件不满足则无需注入
    if not loras or not model_node_ids or not clip_node_ids:
        return
    # 初始引用：模型来源 [首个模型节点id, 输出槽位0]、CLIP 来源 [首个CLIP节点id, 输出槽位1]
    model_ref = [model_node_ids[0], 0]
    clip_ref = [clip_node_ids[0], 1]
    base_id = 9000  # 注入节点 id 的起始基数，避开模板原有 id 区间
    # 逐个 LoRA 创建 LoraLoader 节点并串接
    for index, lora in enumerate(loras, start=1):
        node_id = str(base_id + index)  # 生成新节点 id（"9001"、"9002"…）
        template[node_id] = {  # 向模板中插入新节点
            "class_type": "LoraLoader",
            "inputs": {
                "model": model_ref,          # 模型输入：接上一个节点输出
                "clip": clip_ref,            # CLIP 输入：接上一个节点输出
                "lora_name": lora["name"],   # LoRA 文件名
                # 模型强度：优先 strength_model，回退到 strength，再默认 0.7
                "strength_model": float(lora.get("strength_model", lora.get("strength", 0.7))),
                # CLIP 强度：优先 strength_clip，回退到 strength，再默认 0.7
                "strength_clip": float(lora.get("strength_clip", lora.get("strength", 0.7))),
            },
        }
        # 更新引用，使下一个 LoRA 接在当前 LoRA 之后（链式串接）
        model_ref = [node_id, 0]
        clip_ref = [node_id, 1]
    # 重新接线：将下游消费者原本指向来源节点的连接改为指向链尾 LoRA
    # 注意：跳过本次新建的 9001+ 节点，只重连原始下游消费者，避免自环
    injected_ids = {str(base_id + i) for i in range(1, len(loras) + 1)}
    for node_id, node in template.items():
        if node_id in injected_ids:
            continue  # 跳过新建的 LoraLoader 节点，避免自环
        for key, value in node.get("inputs", {}).items():
            # 节点输入中形如 [id, 槽位] 的列表即节点间连接引用
            if isinstance(value, list) and len(value) == 2:
                # 若该输入的"模型"连接到模型来源节点 → 改接到链尾
                if value[0] in model_node_ids and key == "model":
                    node["inputs"][key] = model_ref
                # 若该输入的"CLIP"连接到 CLIP 来源节点 → 改接到链尾
                elif value[0] in clip_node_ids and key == "clip":
                    node["inputs"][key] = clip_ref


def apply_zimage(template, params, loras):
    """
    将参数应用到 Z-Image 类型模板。

    参数:
        template (dict): 已加载的模板字典（会被就地修改）。
        params (dict): 生成参数（prompt、negative、seed、steps、cfg、宽高等）。
        loras (list[dict]): 启用的 LoRA 配置列表。

    返回:
        dict: 注入参数后的模板字典。

    说明:
        - 先通过 `_find_nodes` 找到 KSamplerAdvanced 采样器节点；
        - 复用 `_set_prompt_text` / `_set_seed` / `_set_resolution`
          完成提示词、种子、分辨率的注入；
        - 再直接改写采样器的 steps / cfg / sampler_name / scheduler；
        - Z-Image 模板使用 rgthree 的 "Power Lora Loader" 节点，
          LoRA 以 `lora_N` 命名字段内联注入，而不是新建 LoraLoader 节点。
    """
    # 查找采样器节点（KSamplerAdvanced 类型）
    samplers = _find_nodes(template, class_type="KSamplerAdvanced")
    if not samplers:
        return template  # 找不到采样器则原样返回模板
    # 只处理第一个采样器
    _, sampler = samplers[0]
    inputs = sampler["inputs"]  # 采样器输入字典（后续多处直接修改）

    # 注入提示词（正/反向）、种子与分辨率
    _set_prompt_text(template, inputs, params.get("prompt", ""), params.get("negative", ""))
    _set_seed(template, inputs, params.get("seed", 0))
    _set_resolution(template, inputs, params.get("width"), params.get("height"))

    # 采样步数与 CFG：参数非空时写入，并做类型转换
    for key, cast in (("steps", int), ("cfg", float)):
        if params.get(key) is not None:
            inputs[key] = cast(params[key])  # steps 转 int，cfg 转 float
    # 采样器名称与调度器：参数名可能是 sampler_name / scheduler
    for key in ("sampler_name", "scheduler"):
        # 若参数里给出 sampler（对应 sampler_name）或 scheduler 则写入
        if params.get(key.replace("_name", "")):
            inputs[key] = params[key.replace("_name", "")]

    # Z-Image 使用 rgthree Power Lora Loader 节点 —— 采用内联注入方式
    for node_id, node in template.items():
        # 只处理 Power Lora Loader 节点
        if "Power Lora Loader" not in node.get("class_type", ""):
            continue
        # 清空所有 lora_ 开头的旧字段，避免残留
        cleaned = {k: v for k, v in node.get("inputs", {}).items() if not k.lower().startswith("lora_")}
        # 按序号将每个 LoRA 注入为 lora_1、lora_2、… 字段
        for index, lora in enumerate(loras, start=1):
            # 模型强度：优先 strength_model，回退到 strength，默认 0.7
            strength = float(lora.get("strength_model", lora.get("strength", 0.7)))
            cleaned[f"lora_{index}"] = {
                "on": True,                     # 启用该 LoRA
                "lora": lora["name"],           # LoRA 文件名
                "strength": strength,           # 主强度
                # 次强度：优先 strength_clip，回退到 strength
                "strengthTwo": float(lora.get("strength_clip", strength)),
            }
        # 用清洗 + 注入后的字典替换原 inputs
        node["inputs"] = cleaned
    return template  # 返回处理后的模板


def apply_flux(template, params, loras):
    """
    将参数应用到 Flux 类型模板。

    参数:
        template (dict): 已加载的模板字典（会被就地修改）。
        params (dict): 生成参数（prompt、seed、steps、scheduler、sampler、宽高等）。
        loras (list[dict]): 启用的 LoRA 配置列表。

    返回:
        dict: 注入参数后的模板字典。

    说明:
        Flux 模板的节点结构与 Z-Image 不同，需要逐类节点处理：
        - RandomNoise        —— 注入种子；
        - BasicScheduler     —— 注入步数与调度器；
        - KSamplerSelect     —— 注入采样器名称；
        - EmptyLatentImage / ModelSamplingFlux —— 注入分辨率；
        - CLIPTextEncode     —— 注入提示词（文本节点在 inputs.value）；
        - UnetLoaderGGUF / DualCLIPLoaderGGUF  —— 通过 `_inject_loras`
          以链式 LoraLoader 节点方式注入 LoRA。
    """
    # Seed（种子）：修改 RandomNoise 节点的 noise_seed 输入
    for cls, key in (("RandomNoise", "noise_seed"),):
        nodes = _find_nodes(template, class_type=cls)
        if nodes:
            # 写入种子并转为整数
            nodes[0][1]["inputs"][key] = int(params.get("seed", 0))

    # Steps / Scheduler（步数与调度器）：修改 BasicScheduler 节点
    scheduler_nodes = _find_nodes(template, class_type="BasicScheduler")
    if scheduler_nodes:
        si = scheduler_nodes[0][1]["inputs"]
        # 步数非空则写入
        if params.get("steps") is not None:
            si["steps"] = int(params["steps"])
        # 调度器非空则写入
        if params.get("scheduler"):
            si["scheduler"] = params["scheduler"]

    # Sampler（采样器）：修改 KSamplerSelect 节点的 sampler_name
    sampler_nodes = _find_nodes(template, class_type="KSamplerSelect")
    if sampler_nodes and params.get("sampler"):
        sampler_nodes[0][1]["inputs"]["sampler_name"] = params["sampler"]

    # Resolution（分辨率）：处理空潜图与 Flux 采样节点
    for cls in ("EmptyLatentImage", "ModelSamplingFlux"):
        nodes = _find_nodes(template, class_type=cls)
        if nodes:
            inp = nodes[0][1]["inputs"]
            # 宽高非空才覆盖
            if params.get("width") is not None:
                inp["width"] = int(params["width"])
            if params.get("height") is not None:
                inp["height"] = int(params["height"])

    # Prompt text（提示词）：Flux 的 CLIPTextEncode 文本输入在 inputs.value
    clip_encode = _find_nodes(template, class_type="CLIPTextEncode")
    if clip_encode:
        # 文本输入是引用（指向另一个文本节点），形如 ["id", 0]
        text_ref = clip_encode[0][1].get("inputs", {}).get("text")
        if isinstance(text_ref, list) and text_ref[0] in template:
            text_node = template[text_ref[0]]
            # 目标节点以 value 字段保存文本 → 覆盖之
            if "value" in text_node.get("inputs", {}):
                text_node["inputs"]["value"] = params.get("prompt", "")

    # LoRA injection（LoRA 注入，与 Z-Image 共用链式逻辑）
    # 找到模型来源（UnetLoaderGGUF）与 CLIP 来源（DualCLIPLoaderGGUF）节点
    unet = _find_nodes(template, class_type="UnetLoaderGGUF")
    clip = _find_nodes(template, class_type="DualCLIPLoaderGGUF")
    # 将来源节点 id 列表传给 _inject_loras，在其下游插入 LoraLoader 链
    _inject_loras(template, loras,
                  [n[0] for n in unet],
                  [n[0] for n in clip])
    return template  # 返回处理后的模板


def apply_template(model, params, loras):
    """
    根据模型类型，把参数与 LoRA 注入到对应的工作流模板中（模块主入口）。

    参数:
        model (str): 模型名称，用于选择模板并判断模型族（含 "flux" 为 Flux）。
        params (dict | None): 生成参数；为 None 时按空字典处理。
        loras (list[dict] | None): LoRA 配置列表；为 None 时按空列表处理。
                                   仅保留 enabled 为 True 的项。

    返回:
        dict: 注入参数后的最终工作流模板。

    说明:
        - 加载的模板会在应用参数时被就地修改；
        - 通过模型名是否包含 "flux" 自动分派到 `apply_flux` 或 `apply_zimage`。
    """
    # 加载模型对应的工作流模板
    template = load_template(model)
    # 归一化参数：None → 空字典
    params = dict(params or {})
    # 过滤 LoRA：只保留显式启用（enabled 为 True）的项
    loras = [lora for lora in (loras or []) if lora.get("enabled", True)]
    # 按模型族分派：Flux 走 apply_flux，其余走 apply_zimage
    if "flux" in model.lower():
        return apply_flux(template, params, loras)
    return apply_zimage(template, params, loras)
