# -*- coding: utf-8 -*-
# prompt_parser.py
"""
文件级文档字符串：本模块的用途
================================

本模块（prompt_parser.py）是 ComfyUI-Assistant 的「Markdown 提示词词典解析器」。

主要职责：
1. 读取指定目录下的 Markdown 提示词词典文件（.md），
   解析其中的标题（### / ####）以及列表项（- ）和表格（| ... |）格式，
   将其转换为结构化的 Python 字典（categories / items）。
2. 提供「NSFW（不适宜工作场所 / 成人内容）」检测与过滤能力，
   用于在构建提示词词典时过滤掉成人/敏感内容，或单独生成 NSFW 词典。
3. 汇总所有词典文件，生成可供上层（前端 / API）使用的完整词典数据。

核心数据结构：
- category: {"label": 分类标签, "items": [item, ...]}
- item:    {"prompt": 英文提示词, "meaning": 中文含义, ["subcategory": 子分类], ["note": 备注]}
- file:    {"id": 文件ID, "label": 文件名, "target": "positive"/"negative",
            "nsfw": 是否NSFW, "categories": [category, ...], ["imported": 是否来自导入目录]}
"""
import os
import re

# 提示词词典所在的根目录（通过环境变量 PROMPT_DICT_DIR 可覆盖默认路径）
PROMPT_DICTIONARY_DIR = os.environ.get(
    "PROMPT_DICT_DIR",
    r"C:\Users\33570\OneDrive\Apps\remotely-save\obsidian\AI绘图",
)
# 导入目录：位于本文件同级目录下的 data/imports 文件夹，存放用户自定义导入的词典文件
IMPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "imports")

# 需要被强制当作 NSFW（成人内容）处理的词典文件名集合。
# 这些文件即使在 build_dictionary(include_nsfw=True) 时也默认不包含在普通词库中。
NSFW_FILES = {
    "身体细节提示词.md",
    "成人服饰提示词.md",
    "成人姿势提示词.md",
    "动作提示词.md",
}

# 英文 NSFW 关键词正则表达式列表。
# 每个正则使用 \b...\b 表示「词边界」，避免误匹配到普通单词的子串；
# \w* 表示允许部分单词有前后缀变体（如 boob/boobs、fuck/fucking 等）。
# 注意：这些是正则字符串，匹配时使用 re.search（不要求整串匹配）。
NSFW_KEYWORDS_EN = [
    r"\bpussy\b", r"\bvagina\b", r"\bpenis\b", r"\bcock\b", r"\bboobs?\b",
    r"\bbreasts?\b", r"\bnipples?\b", r"\bareola\b", r"\bass\b", r"\bbutt\b",
    r"\banal\b", r"\boral\b", r"\bblowjob\b", r"\bcum\w*\b", r"\bejaculat\w*\b",
    r"\bsperm\b", r"\bsemen\b", r"\borgasm\w*\b", r"\bsex\b", r"\bfuck\w*\b",
    r"\bpenetrat\w*\b", r"\bthrust\w*\b", r"\bmasturbat\w*\b", r"\bdildo\b",
    r"\bbondage\b", r"\bbdsm\b", r"\bnude\b", r"\bnaked\b", r"\btopless\b",
    r"\blingerie\b", r"\bunderwear\b", r"\bpanties\b", r"\bthong\b",
    r"\bcleavage\b", r"\bunderboob\b", r"\bsideboob\b", r"\bcameltoe\b",
    r"\bcrotchless\b", r"\bpasties\b", r"\bswallow\w*\b", r"\bspank\w*\b",
]

# 中文 NSFW 关键词列表（纯文本子串匹配，无需正则）。
NSFW_KEYWORDS_CN = [
    "胸部", "乳房", "乳头", "乳晕", "巨乳", "贫乳", "乳沟", "内衣", "胸罩",
    "丁字裤", "情趣", "裸", "阴道", "阴唇", "阴蒂", "阴茎", "睾丸", "精液",
    "射精", "高潮", "性交", "口交", "肛交", "自慰", "插入", "抽插", "后入",
    "骑乘", "诱惑", "暴露", "色情", "撕衣", "体液", "潮吹", "勃起", "春药",
]


def clean_md_text(value):
    """
    清理 Markdown 文本中的格式标记，返回纯净文本。

    参数：
        value (str): 原始字符串（可能包含 **加粗**、`代码`、链接 [文字](url)、HTML 标签等）。

    返回：
        str：去除 Markdown 标记并去除首尾空白后的纯文本。

    逻辑说明：
        1. 去掉所有 ** 加粗标记 与 ` 行内代码标记，并去掉首尾空白；
        2. 使用正则将 Markdown 链接 [文字](url) 替换为其中的「文字」部分；
        3. 使用正则删除所有 HTML 标签（<...>）；
        4. 再次 strip 后返回。
    """
    value = value.replace("**", "").replace("`", "").strip()
    # 正则 \[([^\]]+)\]\([^)]*\)：匹配 [文字](任意非右括号内容)，替换为文字
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    # 正则 <[^>]+>：匹配任意 HTML 标签（不含 > 的一串字符）
    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()


def clean_md_heading(value):
    """
    清理 Markdown 标题文本，去掉编号前缀和括号注释部分。

    参数：
        value (str): 标题原始文本。

    返回：
        str：清理后的标题文本。

    逻辑说明：
        1. 先调用 clean_md_text 去掉 Markdown 格式标记；
        2. 去掉开头的「数字 + . 或 、」编号前缀（如 "1."、"2、"）；
        3. 去掉中英文括号及其后所有内容（如 "身体（体型）" 会变成 "身体"）。
    """
    value = clean_md_text(value)
    # 正则 ^\d+[\.\、]\s*：匹配行首的数字编号（如 "12. "、"3、"）
    value = re.sub(r"^\d+[\.\、]\s*", "", value)
    # 正则 \s*[（(].*$：匹配从第一个中/英文左括号开始的其余内容并删除
    value = re.sub(r"\s*[（(].*$", "", value)
    return value.strip()


def is_table_separator(cells):
    """
    判断一行表格单元格是否属于 Markdown 表格的「分隔行」（即 --- 那一行）。

    参数：
        cells (list[str]): 表格某行拆分出的单元格列表。

    返回：
        bool：如果每个非空单元格都能匹配形如 --- 、:--- 、---: 、:---: 的分隔符，
              则返回 True（表示这是表格的格式分隔行，应被跳过）。

    正则说明：
        r":?-{2,}:?" 匹配：可选冒号 + 至少 2 个短横线 + 可选冒号（即表格对齐分隔线）。
        这里先用 c.replace(" ", "") 去掉单元格中的空格再匹配，增强容错性。
    """
    return all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c)


def is_nsfw_text(prompt, meaning):
    """
    判断一段提示词（英文 + 中文含义拼接）是否包含 NSFW（成人）内容。

    参数：
        prompt (str): 英文提示词。
        meaning (str): 中文含义/注释。

    返回：
        bool：命中任一英文正则关键词或中文关键词则返回 True（视为 NSFW）。

    逻辑说明：
        1. 将 prompt 与 meaning 拼接并转小写，作为统一检测文本；
        2. 先用英文正则关键词列表逐个 re.search 匹配，命中即返回 True；
        3. 若英文未命中，再检查中文关键词（简单子串包含判断）。
    """
    # 拼接并转小写，保证英文正则的大小写不敏感
    text = f"{prompt} {meaning}".lower()
    # 逐个尝试英文正则；any() 短路，命中即 True
    if any(re.search(pattern, text) for pattern in NSFW_KEYWORDS_EN):
        return True
    # 中文关键词使用子串包含判断
    return any(keyword in text for keyword in NSFW_KEYWORDS_CN)


def is_likely_prompt(prompt):
    """判断清理后的文本是否像真正的提示词条目。

    真正的提示词条目以英文词汇/短语为主；说明性文字通常包含中文字符，
    例如「组合描述：尽量将动作绑定在一起写...」。这里统一过滤掉包含
    中文的文本，避免把 Markdown 中的解释文字错误识别为提示词。
    """
    if not prompt:
        return False
    if re.search(r"[\u4e00-\u9fff]", prompt):
        return False
    return True


def expand_slash_item(prompt, meaning):
    """把形如 "white / black background" 的提示词拆成多个独立条目。"""
    if "/" not in prompt and "／" not in prompt:
        return [(prompt, meaning)]
    prompts = [p.strip() for p in re.split(r"\s*/\s*|／", prompt) if p.strip()]
    meanings = [m.strip() for m in re.split(r"\s*/\s*|／", meaning) if m.strip()]

    if len(prompts) >= 2:
        last_words = prompts[-1].split()
        shared_suffix = last_words[-1] if len(last_words) >= 2 else None
        if shared_suffix and all(not p.endswith(" " + shared_suffix) for p in prompts[:-1]):
            prompts = [
                p if p.endswith(" " + shared_suffix) else (p + " " + shared_suffix).strip()
                for p in prompts
                if p and p != shared_suffix
            ]
        else:
            first_words = prompts[0].split()
            shared_prefix = first_words[0] if len(first_words) >= 2 else None
            if shared_prefix and all(not p.startswith(shared_prefix + " ") for p in prompts[1:]):
                prompts = [
                    p if p.startswith(shared_prefix + " ") else (shared_prefix + " " + p).strip()
                    for p in prompts
                    if p and p != shared_prefix
                ]

    result = []
    for index, item_prompt in enumerate(prompts):
        item_meaning = (
            meanings[index]
            if index < len(meanings)
            else (meanings[-1] if meanings else meaning)
        )
        result.append((item_prompt, item_meaning))
    return result


def parse_md_tables(text):
    """
    解析单个 Markdown 词典文件文本，提取其中的分类与提示词条目。

    参数：
        text (str): 读取到的 Markdown 文件原始内容。

    返回：
        list[dict]：分类列表，每个分类形如
                    {"label": 分类名, "items": [{"prompt":..., "meaning":..., ...}]}

    支持的 Markdown 语法：
        - "### 分类名"            -> 开始一个新的分类（或切换到已有分类）
        - "#### 子分类名"         -> 设置当前子分类（会附加到后续条目上）
        - "- 英文（中文含义）"     -> 列表项：英文 prompt + 括号内中文含义
        - "| 中文 | 英文 | 备注 |" -> 表格：支持 中文/英文 或 英文/中文 两种列顺序

    逻辑细节：
        1. 依次处理每一行，通过 in_table 标志区分「列表/标题模式」与「表格模式」；
        2. 首次遇到 | 开头行时根据表头判断列顺序（chinese_first），
           并跳过表头与分隔行；
        3. 过滤掉 "英文"/"中文" 引导的说明性条目，避免把解释性文字当数据。
    """
    categories = []          # 最终返回的分类列表
    category_by_label = {}   # 分类名 -> 分类对象 的映射，便于去重复用
    current_category = None  # 当前正在填充的分类对象
    current_subcategory = "" # 当前子分类名（#### 标题设置）
    in_table = False         # 是否正处在表格解析模式中
    chinese_first = False    # 表格是否「中文在前、英文在后」的顺序

    # 逐行处理 Markdown 文本
    for raw_line in text.splitlines():
        line = raw_line.strip()

        # 三级标题 "### xxx"：开始/切换一个分类
        if line.startswith("### "):
            # 去掉 "### " 前缀后清理标题文本
            label = clean_md_heading(line[4:])
            current_subcategory = ""  # 新分类重置子分类
            in_table = False          # 退出表格模式
            # 若该分类名已存在则复用，否则新建分类对象
            if label not in category_by_label:
                current_category = {"label": label, "items": []}
                category_by_label[label] = current_category
                categories.append(current_category)
            else:
                current_category = category_by_label[label]
            continue

        # 四级标题 "#### xxx"：设置当前子分类
        if line.startswith("#### "):
            current_subcategory = clean_md_heading(line[5:])
            in_table = False
            continue

        # 列表项 "- xxx（yyy）"：解析单个提示词条目
        if line.startswith("- ") and current_category is not None:
            body = line[2:].strip()  # 去掉 "- " 前缀
            slash_segments = [s.strip() for s in re.split(r"\s*/\s*", body)]
            if len(slash_segments) >= 2:
                parsed_segments = []
                for segment in slash_segments:
                    segment_match = re.match(r"^(.*?)[（(]([^（）()]*)[）)]\s*$", segment)
                    if segment_match:
                        segment_prompt = clean_md_text(segment_match.group(1))
                        segment_meaning = clean_md_text(segment_match.group(2))
                        if (
                            segment_prompt
                            and segment_meaning
                            and "英文" not in segment_prompt
                            and "中文" not in segment_prompt[:6]
                            and is_likely_prompt(segment_prompt)
                        ):
                            parsed_segments.append((segment_prompt, segment_meaning))
                if len(parsed_segments) >= 2:
                    for segment_prompt, segment_meaning in parsed_segments:
                        item = {
                            "prompt": segment_prompt,
                            "meaning": segment_meaning,
                        }
                        if current_subcategory:
                            item["subcategory"] = current_subcategory
                        current_category["items"].append(item)
                    in_table = False
                    continue
            # 正则 ^(.*?)[（(](.*)[）)]\s*$：非贪婪前段 + 括号内内容，
            # 用于分离「英文 prompt」和「括号内的中文含义」
            match = re.match(r"^(.*?)[（(](.*)[）)]\s*$", body)
            if match:
                prompt = clean_md_text(match.group(1))   # 括号前为英文提示词
                meaning = clean_md_text(match.group(2))  # 括号内为中文含义
                # 去掉含义末尾的「（来源：xxx）」标注
                meaning = re.sub(r"（来源：[^）]*）\s*$", "", meaning).strip()
                if prompt and meaning and "英文" not in prompt and "中文" not in prompt[:6]:
                    for expanded_prompt, expanded_meaning in expand_slash_item(prompt, meaning):
                        if is_likely_prompt(expanded_prompt):
                            item = {
                                "prompt": expanded_prompt,
                                "meaning": expanded_meaning,
                            }
                            if current_subcategory:
                                item["subcategory"] = current_subcategory
                            current_category["items"].append(item)
            elif body:  # 没有括号，只有纯文本
                segments = [s.strip() for s in re.split(r"\s*/\s*", body)]
                parsed_segments = []
                for segment in segments:
                    segment_match = re.match(r"^(.*?)[（(]([^（）()]*)[）)]\s*$", segment)
                    if segment_match:
                        segment_prompt = clean_md_text(segment_match.group(1))
                        segment_meaning = clean_md_text(segment_match.group(2))
                        if (
                            segment_prompt
                            and segment_meaning
                            and "英文" not in segment_prompt
                            and "中文" not in segment_prompt[:6]
                            and is_likely_prompt(segment_prompt)
                        ):
                            parsed_segments.append((segment_prompt, segment_meaning))
                if len(parsed_segments) >= 2:
                    for segment_prompt, segment_meaning in parsed_segments:
                        item = {
                            "prompt": segment_prompt,
                            "meaning": segment_meaning,
                        }
                        if current_subcategory:
                            item["subcategory"] = current_subcategory
                        current_category["items"].append(item)
                    in_table = False
                    continue
                prompt = clean_md_text(body)
                if (
                    prompt
                    and "英文" not in prompt
                    and "中文" not in prompt[:6]
                    and is_likely_prompt(prompt)
                ):
                    # 无中文含义时给出默认占位说明
                    item = {"prompt": prompt, "meaning": "未提供中文注释"}
                    if current_subcategory:
                        item["subcategory"] = current_subcategory
                    current_category["items"].append(item)
            in_table = False
            continue

        # 表格行 "| ... | ... |"
        if line.startswith("|"):
            # 去掉首尾 | 后按 | 拆分单元格，并去除每个单元格首尾空白
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                # 表格第一行视为表头：判断列顺序是否为「中文在前、英文在后」
                in_table = True
                chinese_first = bool(
                    len(cells) >= 2
                    and (
                        "中文" in cells[0]
                        or "含义" in cells[0]
                        or "描述" in cells[0]
                    )
                    and (
                        "英文" in cells[1]
                        or "prompt" in cells[1].lower()
                        or "提示词" in cells[1]
                    )
                )
                continue  # 表头行不解析为数据
            # 分隔行（---）直接跳过
            if is_table_separator(cells):
                continue
            # 根据列顺序决定哪一列是 prompt、哪一列是 meaning
            if chinese_first and len(cells) >= 2:
                prompt = clean_md_text(cells[1])  # 第二列是英文 prompt
                meaning = clean_md_text(cells[0]) # 第一列是中文含义
            else:
                prompt = clean_md_text(cells[0]) if cells else ""
                meaning = clean_md_text(cells[1]) if len(cells) >= 2 else ""
            if (
                prompt
                and meaning
                and prompt.lower() != "prompt"     # 防止把表头当数据
                and "英文" not in prompt
                and "negative" not in prompt.lower()
                and current_category is not None
            ):
                note = clean_md_text(cells[2]) if len(cells) > 2 else ""
                for expanded_prompt, expanded_meaning in expand_slash_item(prompt, meaning):
                    if is_likely_prompt(expanded_prompt):
                        item = {
                            "prompt": expanded_prompt,
                            "meaning": expanded_meaning,
                        }
                        # 第三列及之后的内容作为备注 note（可选）
                        if note:
                            item["note"] = note
                        if current_subcategory:
                            item["subcategory"] = current_subcategory
                        current_category["items"].append(item)
            continue

        # 其它行：退出表格模式
        in_table = False

    return categories


def _read_file(path):
    """
    以 UTF-8 编码读取文件内容（内部辅助函数）。

    参数：
        path (str): 文件绝对路径。

    返回：
        str | None：成功时返回文件文本；读取失败（文件不存在/编码错误等）返回 None。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        # 读取失败时静默返回 None，避免中断整个构建流程
        return None


def _nsfw_count():
    """
    统计 NSFW 词典文件（NSFW提示词.md）中的提示词条目总数。

    参数：无。

    返回：
        int：NSFW 词典的总条目数；文件缺失或解析失败时返回 0。
    """
    # NSFW 词典固定放在词典根目录下的 NSFW提示词.md 中
    path = os.path.join(PROMPT_DICTIONARY_DIR, "NSFW提示词.md")
    text = _read_file(path)
    if not text:
        return 0
    # 复用 parse_md_tables 解析后，累加每个分类的条目数
    return sum(len(c["items"]) for c in parse_md_tables(text))


def build_dictionary(include_nsfw=False):
    """
    构建完整的提示词词典数据（主入口函数）。

    参数：
        include_nsfw (bool): 是否包含 NSFW 成人提示词词典。默认 False。

    返回：
        (files, nsfw_total) 元组：
            - files (list[dict])：普通词典文件列表（若 include_nsfw 且存在 NSFW 词典，
              还会追加一个 nsfw=True 的词典文件）；
            - nsfw_total (int)：NSFW 词典的条目总数（用于上层展示）。

    逻辑说明：
        1. 遍历词典根目录下所有 .md 文件，跳过文件名含 "NSFW" 的或属于 NSFW_FILES 的；
        2. 每个文件解析出分类后，逐条过滤 NSFW 内容，并清空无条目的分类；
        3. 生成 file 字典：id 唯一自增，label 取文件名（去掉 .md 后缀），
           target 根据文件名是否含「反向」设为 negative / positive；
        4. 若导入目录存在，同样遍历导入的 .md 文件（追加「（导入）」后缀并标记 imported）；
        5. 最后按需追加 NSFW 词典文件。
    """
    files = []
    # 词典目录不存在时直接返回空结果
    if not os.path.isdir(PROMPT_DICTIONARY_DIR):
        return files, 0

    # 按文件名排序遍历，保证输出顺序稳定
    names = sorted(os.listdir(PROMPT_DICTIONARY_DIR))
    index = 0
    for name in names:
        # 跳过非 .md 文件、NSFW 相关文件以及明确列入 NSFW_FILES 的文件
        if not name.endswith(".md") or "NSFW" in name or name in NSFW_FILES:
            continue
        text = _read_file(os.path.join(PROMPT_DICTIONARY_DIR, name))
        if not text:
            continue
        label = name[:-3]  # 去掉 .md 后缀作为文件标签
        categories = parse_md_tables(text)
        # 逐条过滤 NSFW 内容，保留非 NSFW 条目
        for category in categories:
            category["items"] = [
                item for item in category["items"]
                if not is_nsfw_text(item["prompt"], item["meaning"])
            ]
        # 丢弃所有条目都被过滤掉的空分类
        categories = [c for c in categories if c["items"]]
        if not categories:
            continue
        files.append({
            "id": f"file-{index}",
            "label": label,
            # 文件名含「反向」的词典归入 negative，其余归入 positive
            "target": "negative" if "反向" in label else "positive",
            "nsfw": False,
            "categories": categories,
        })
        index += 1

    # 处理导入目录（data/imports）中的用户自定义词典
    if os.path.isdir(IMPORT_DIR):
        for name in sorted(os.listdir(IMPORT_DIR)):
            if not name.endswith(".md") or "NSFW" in name:
                continue
            text = _read_file(os.path.join(IMPORT_DIR, name))
            if not text:
                continue
            label = name[:-3] + "（导入）"  # 导入词典文件名追加（导入）标记
            categories = parse_md_tables(text)
            for category in categories:
                category["items"] = [
                    item for item in category["items"]
                    if not is_nsfw_text(item["prompt"], item["meaning"])
                ]
            categories = [c for c in categories if c["items"]]
            if not categories:
                continue
            files.append({
                "id": f"file-import-{index}",
                "label": label,
                "target": "positive",   # 导入词典一律归入正向提示词
                "nsfw": False,
                "imported": True,       # 标记为导入文件
                "categories": categories,
            })
            index += 1

    # 统计 NSFW 词典条目总数（无论是否 include_nsfw 都会统计，供上层展示数量）
    nsfw_total = _nsfw_count()
    # 仅当显式要求包含 NSFW 且词典非空时才追加 NSFW 文件
    if include_nsfw and nsfw_total:
        text = _read_file(os.path.join(PROMPT_DICTIONARY_DIR, "NSFW提示词.md"))
        categories = [c for c in parse_md_tables(text) if c["items"]]
        if categories:
            files.append({
                "id": "file-nsfw",
                "label": "NSFW 提示词",
                "target": "positive",
                "nsfw": True,
                "categories": categories,
            })
    return files, nsfw_total
