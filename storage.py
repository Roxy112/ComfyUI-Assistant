"""
storage.py — SQLite 数据库层（数据持久化模块）
==================================================

本模块是 ComfyUI-Assistant 的数据持久化层，负责所有本地数据的存储与读取。
它使用 Python 内置的 `sqlite3` 模块，将数据保存在 SQLite 数据库文件中，
无需额外安装数据库服务，轻量且可靠。

模块核心功能：
    - 提供 `AssistantDB` 类，封装了对数据库的所有常见操作；
    - 自动建库建表，兼容旧库结构（通过 `_ensure_columns` 增量补齐缺失字段）；
    - 管理以下业务数据：
        1. settings      —— 键值对形式的全局设置（如 API Key、默认参数等）；
        2. favorites     —— 用户收藏的提示词 / 参数组合；
        3. history       —— 生成历史记录；
        4. assets        —— 生成的图片资源（含缩略图）；
        5. workflows     —— 保存的 ComfyUI 工作流模板；
        6. lora_notes    —— 针对特定模型 + LoRA 组合的备注笔记。

设计说明：
    - 所有 SQL 均使用参数化查询（`?` 占位符），避免 SQL 注入风险；
    - `connect()` 通过上下文管理器（`with`）使用连接，保证事务自动提交、
      连接自动关闭；
    - `sqlite3.Row` 行工厂让查询结果可以像字典一样通过列名访问。
"""

import json  # 用于将参数对象序列化为 JSON 字符串存入数据库
import os    # 用于拼接数据库文件路径、创建目录
import sqlite3  # SQLite 数据库驱动
import time  # 用于生成时间戳（created_at 字段）
from contextlib import contextmanager


class AssistantDB:
    """
    AssistantDB —— 数据库访问类（门面模式）

    封装了本应用对 SQLite 数据库的全部读写操作。调用方无需关心 SQL 细节，
    只需调用对应的方法即可完成数据的增删改查。

    典型用法::

        db = AssistantDB(data_dir="data")      # 创建/打开数据库
        db.add_favorite(prompt="a cat", ...)   # 写入一条收藏
        rows = db.list_favorites()             # 读取所有收藏

    内部维护一个数据库文件 `assistant.db`，所有数据表在初始化时自动创建。
    """

    def __init__(self, data_dir):
        """
        初始化数据库连接。

        参数:
            data_dir (str): 数据库文件所在目录。若目录不存在会自动创建，
                             数据库文件最终位于 `data_dir/assistant.db`。

        说明:
            构造后立即调用 `init()` 完成建表与字段兼容工作，
            因此首次创建实例时即可保证表结构完整。
        """
        # 确保数据目录存在；exist_ok=True 表示目录已存在时也不报错
        os.makedirs(data_dir, exist_ok=True)
        # 拼接出数据库文件的完整路径
        self.path = os.path.join(data_dir, "assistant.db")
        # 初始化表结构
        self.init()

    @contextmanager
    def connect(self):
        """
        建立一个新的数据库连接。

        返回:
            sqlite3.Connection: 配置好的连接对象。

        说明:
            - 设置 `row_factory = sqlite3.Row`，使查询结果行支持按列名访问
              （`row["name"]`），代码可读性更好；
            - 调用方应使用 `with self.connect() as conn:` 形式，
              这样连接关闭、事务提交都由上下文管理器自动处理。
        """
        # 打开数据库文件，建立连接
        conn = sqlite3.connect(self.path)
        # 将行工厂设为 Row，允许通过列名（而非数字下标）读取字段
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self):
        """
        初始化数据库表结构。

        职责：
            1. 使用 `CREATE TABLE IF NOT EXISTS` 创建全部业务表（已存在则跳过）；
            2. 调用 `_ensure_columns` 为历史遗留的旧表补上缺失的新字段，
               保证升级后旧数据库仍可正常使用。

        说明:
            各表之间没有外键约束，表间关系由应用层逻辑维护，简单直观。
        """
        # 在同一连接中批量执行建表脚本，保证原子性
        with self.connect() as conn:
            # executescript 可一次执行多条 SQL 语句（用分号分隔）
            conn.executescript("""
            -- 全局设置表：以 key 为主键，存储键值对
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,        -- 设置项名称（唯一）
                value TEXT                   -- 设置项值
            );
            -- 收藏表：保存用户收藏的提示词及配套参数
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键
                prompt TEXT NOT NULL,                  -- 正向提示词（必填）
                negative TEXT DEFAULT '',              -- 反向提示词
                category TEXT DEFAULT '',              -- 分类
                tags TEXT DEFAULT '',                  -- 标签
                note TEXT DEFAULT '',                  -- 用户备注
                type TEXT DEFAULT 'prompt',            -- 类型（如 prompt / workflow 等）
                image_path TEXT DEFAULT '',            -- 关联的示例图片路径
                model TEXT DEFAULT '',                 -- 关联的模型名称
                params_json TEXT DEFAULT '{}',         -- 其他参数（JSON 字符串）
                created_at REAL                        -- 创建时间戳（Unix 秒）
            );
            -- 历史记录表：记录每次生成请求的内容
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,                  -- 正向提示词
                negative TEXT DEFAULT '',              -- 反向提示词
                model TEXT DEFAULT '',                 -- 使用的模型
                params_json TEXT DEFAULT '{}',         -- 参数快照（JSON）
                workflow TEXT DEFAULT '',              -- 工作流内容/引用
                image_path TEXT DEFAULT '',            -- 生成结果图片路径
                created_at REAL                        -- 记录时间戳
            );
            -- 素材资源表：管理生成的图片及其缩略图
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT,                             -- 原图路径
                thumb TEXT,                            -- 缩略图路径
                prompt TEXT DEFAULT '',                -- 生成所用提示词
                model TEXT DEFAULT '',                 -- 生成所用模型
                params_json TEXT DEFAULT '{}',         -- 生成参数（JSON）
                saved INTEGER DEFAULT 0,               -- 是否已保存（1/0）
                created_at REAL                        -- 生成时间戳
            );
            -- 生成任务表：在提交时保存参数快照，进程重启后仍可正确归档图片。
            CREATE TABLE IF NOT EXISTS generation_tasks (
                prompt_id TEXT PRIMARY KEY,
                params_json TEXT DEFAULT '{}',
                workflow_path TEXT DEFAULT '',
                created_at REAL
            );
            -- 工作流表：保存用户保存的工作流模板
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,                             -- 工作流名称
                path TEXT,                             -- 工作流文件路径
                model TEXT DEFAULT '',                 -- 关联模型
                config_json TEXT,                      -- 工作流配置（JSON）
                created_at REAL                        -- 保存时间戳
            );
            -- LoRA 备注表：记录某个模型下某个 LoRA 的使用笔记
            CREATE TABLE IF NOT EXISTS lora_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT DEFAULT '',                 -- 模型名称
                lora_name TEXT DEFAULT '',             -- LoRA 名称
                note TEXT DEFAULT '',                  -- 备注内容
                updated_at REAL,                       -- 最后更新时间
                UNIQUE(model, lora_name)               -- 联合唯一：同一组合只保留一条
            );
            """)
            # 兼容旧版 favorites 表：若缺少新字段则动态补列
            self._ensure_columns(conn, "favorites", {
                "type": "TEXT DEFAULT 'prompt'",       # 收藏类型字段
                "image_path": "TEXT DEFAULT ''",       # 图片路径字段
                "model": "TEXT DEFAULT ''",            # 模型字段
                "params_json": "TEXT DEFAULT '{}'",    # 参数 JSON 字段
                "note": "TEXT DEFAULT ''",             # 用户备注字段
            })
            # 兼容旧版 assets 表：补上 saved 字段
            self._ensure_columns(conn, "assets", {"saved": "INTEGER DEFAULT 0"})

    def _ensure_columns(self, conn, table, additions):
        """
        为指定表补加缺失的字段（轻量级数据库迁移）。

        参数:
            conn (sqlite3.Connection): 当前数据库连接。
            table (str): 需要检查/修改的表名。
            additions (dict): 需要确保存在的字段映射，格式为
                              {列名: 列定义 SQL 片段}。

        说明:
            SQLite 的 `ALTER TABLE ... ADD COLUMN` 每次只能加一列，
            因此这里逐个字段判断后再执行 ADD COLUMN。
            通过 `PRAGMA table_info` 读取现有列名集合来判断缺失项。
        """
        # PRAGMA table_info 返回该表的全部列信息，提取现有列名集合
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        # 遍历需要补加的字段定义
        for col, defn in additions.items():
            # 若该列尚不存在，则执行 ALTER TABLE 动态添加
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")

    def get_setting(self, key, default=None):
        """
        读取一项全局设置。

        参数:
            key (str): 设置项名称。
            default (object): 设置项不存在时返回的默认值。

        返回:
            设置项的值；若不存在则返回 `default`。

        说明:
            使用 `?` 占位符参数化查询，key 作为参数传入，杜绝 SQL 注入。
        """
        with self.connect() as conn:
            # 按主键精确查询单项设置
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        # 查到则返回值，否则返回默认值
        return row["value"] if row else default

    def set_setting(self, key, value):
        """
        写入 / 更新一项全局设置。

        参数:
            key (str): 设置项名称。
            value (object): 设置项的值，会被强制转为字符串存储。

        说明:
            采用 SQLite 的 UPSERT 语法：
            `ON CONFLICT(key) DO UPDATE` 表示若主键冲突（已存在同 key 记录），
            则更新 value 为新值；否则执行正常 INSERT 插入新记录。
        """
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",  # excluded 引用拟插入的新值
                (key, str(value)),  # 统一转为字符串保存
            )

    def add_favorite(
        self,
        prompt,
        negative="",
        category="",
        tags="",
        ftype="prompt",
        image_path="",
        model="",
        params=None,
        note="",
    ):
        """
        新增一条收藏记录。

        参数:
            prompt (str): 正向提示词（必填）。
            negative (str): 反向提示词，默认空字符串。
            category (str): 分类，默认空。
            tags (str): 标签，默认空。
            ftype (str): 收藏类型，默认 'prompt'。
            image_path (str): 关联图片路径，默认空。
            model (str): 关联模型名，默认空。
            params (dict): 附加参数，会被序列化为 JSON 存储；为 None 时存 '{}'。

        返回:
            int: 新插入记录的自增主键 id（`lastrowid`）。

        说明:
            `json.dumps(..., ensure_ascii=False)` 保证中文等非 ASCII 字符
            以原文（而非 \\uXXXX 转义）形式存入 JSON 字符串，便于阅读。
        """
        with self.connect() as conn:
            # 前端状态可能因为回填或异步刷新而短暂过期，数据库层仍应保证
            # 相同的提示词组合或同一张图片不会被重复收藏。
            if ftype == "image" and image_path:
                existing = conn.execute(
                    "SELECT id FROM favorites WHERE type='image' AND image_path=? ORDER BY id DESC LIMIT 1",
                    (image_path,),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id FROM favorites WHERE type=? AND prompt=? AND negative=? ORDER BY id DESC LIMIT 1",
                    (ftype, prompt, negative),
                ).fetchone()
            if existing:
                return existing["id"]
            # 插入一条收藏，created_at 使用当前时间戳
            cur = conn.execute(
                "INSERT INTO favorites(prompt,negative,category,tags,note,type,image_path,model,params_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    prompt,
                    negative,
                    category,
                    tags,
                    note,
                    ftype,
                    image_path,
                    model,
                    json.dumps(params or {}, ensure_ascii=False),  # 参数对象 → JSON 字符串
                    time.time(),  # 当前 Unix 时间戳
                ),
            )
            # 返回新记录的自增 id
            return cur.lastrowid

    def list_favorites(self, ftype=None):
        """
        查询收藏列表。

        参数:
            ftype (str | None): 按类型过滤；为 None 时返回全部。

        返回:
            list[dict]: 收藏记录列表，每条为字典形式；按创建时间倒序排列
                        （最新的在前）。

        说明:
            通过 `dict(row)` 把 sqlite3.Row 转为普通字典，方便上层直接使用。
        """
        with self.connect() as conn:
            if ftype:
                # 按类型过滤，并按创建时间倒序（最新在前）
                rows = conn.execute(
                    "SELECT * FROM favorites WHERE type=? ORDER BY created_at DESC",
                    (ftype,),
                ).fetchall()
            else:
                # 不过滤，返回全部收藏
                rows = conn.execute(
                    "SELECT * FROM favorites ORDER BY created_at DESC"
                ).fetchall()
        # 将 Row 对象列表转换为字典列表
        return [dict(row) for row in rows]

    def delete_favorite(self, favorite_id):
        """
        删除一条收藏记录。

        参数:
            favorite_id (int): 要删除的收藏记录主键 id。
        """
        with self.connect() as conn:
            # 按主键精确删除
            conn.execute("DELETE FROM favorites WHERE id=?", (favorite_id,))

    def set_favorite_note(self, favorite_id, note):
        """更新指定收藏的备注。"""
        with self.connect() as conn:
            conn.execute("UPDATE favorites SET note=? WHERE id=?", (note, favorite_id))

    def add_history(self, prompt, negative, model, params, workflow="", image_path=""):
        """
        新增一条生成历史记录。

        参数:
            prompt (str): 正向提示词。
            negative (str): 反向提示词。
            model (str): 使用的模型名。
            params (dict): 生成参数，序列化为 JSON 存储。
            workflow (str): 工作流内容或引用，默认空。
            image_path (str): 结果图片路径，默认空。

        返回:
            int: 新记录的自增主键 id。
        """
        with self.connect() as conn:
            # 重试轮询或进程恢复时，同一输出图片不应重复出现在历史中。
            if image_path:
                existing = conn.execute(
                    "SELECT id FROM history WHERE image_path=? ORDER BY id DESC LIMIT 1",
                    (image_path,),
                ).fetchone()
                if existing:
                    return existing["id"]
            # 插入历史记录，created_at 用当前时间戳
            cur = conn.execute(
                "INSERT INTO history(prompt,negative,model,params_json,workflow,image_path,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (prompt, negative, model, json.dumps(params, ensure_ascii=False),
                 workflow, image_path, time.time()),
            )
            return cur.lastrowid

    def save_generation_task(self, prompt_id, params, workflow_path):
        """保存或更新已提交给 ComfyUI 的任务参数快照。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO generation_tasks(prompt_id,params_json,workflow_path,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(prompt_id) DO UPDATE SET params_json=excluded.params_json, workflow_path=excluded.workflow_path",
                (prompt_id, json.dumps(params or {}, ensure_ascii=False), workflow_path, time.time()),
            )

    def get_generation_task(self, prompt_id):
        """读取任务快照；找不到时返回空字典。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT params_json,workflow_path,created_at FROM generation_tasks WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
        if not row:
            return {}
        try:
            params = json.loads(row["params_json"] or "{}")
        except Exception:
            params = {}
        return {"params": params, "workflow_path": row["workflow_path"] or "", "created_at": row["created_at"]}

    def list_history(self, limit=200):
        """
        查询生成历史列表。

        参数:
            limit (int): 最多返回的记录条数，默认 200。

        返回:
            list[dict]: 历史记录字典列表，按创建时间倒序。

        说明:
            LIMIT 使用 `?` 参数化占位，配合 limit 变量安全拼接。
        """
        with self.connect() as conn:
            # 取最近 limit 条记录，倒序排列
            rows = conn.execute(
                "SELECT * FROM history ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_history(self):
        """
        清空全部历史记录。

        说明:
            执行全表 DELETE。注意 SQLite 不会自动重置自增计数器，
            若后续插入，id 会继续递增而非从 1 重新开始。
        """
        with self.connect() as conn:
            # 删除 history 表中所有数据
            conn.execute("DELETE FROM history")

    def delete_history(self, history_id):
        """
        删除单条历史记录。

        参数:
            history_id (int): 要删除的记录主键 id。
        """
        with self.connect() as conn:
            # 按主键精确删除
            conn.execute("DELETE FROM history WHERE id=?", (history_id,))

    def add_asset(self, path, thumb, prompt, model, params, saved=0):
        """
        新增一条图片素材记录。

        参数:
            path (str): 原图路径。
            thumb (str): 缩略图路径。
            prompt (str): 生成该图所用的提示词。
            model (str): 生成该图所用的模型。
            params (dict): 生成参数，序列化为 JSON 存储。
            saved (int): 是否已保存标记，非零视为 1（已保存）。

        返回:
            int: 新记录的自增主键 id。

        说明:
            `1 if saved else 0` 将传入值归一化为 0/1，保证字段取值规范。
        """
        with self.connect() as conn:
            # ComfyUI 输出路径可作为幂等键，避免多个监视器或重试重复写入资产。
            if path:
                existing = conn.execute(
                    "SELECT id FROM assets WHERE path=? ORDER BY id DESC LIMIT 1",
                    (path,),
                ).fetchone()
                if existing:
                    return existing["id"]
            # 插入素材记录
            cur = conn.execute(
                "INSERT INTO assets(path,thumb,prompt,model,params_json,saved,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    path,
                    thumb,
                    prompt,
                    model,
                    json.dumps(params, ensure_ascii=False),  # 参数 → JSON
                    1 if saved else 0,  # 归一化为 0/1
                    time.time(),
                ),
            )
            return cur.lastrowid

    def set_asset_saved(self, asset_id, saved=1):
        """
        更新素材的"已保存"标记。

        参数:
            asset_id (int): 素材记录主键 id。
            saved (int): 目标保存状态，非零视为 1（已保存）。
        """
        with self.connect() as conn:
            # 按主键更新 saved 字段，同样归一化为 0/1
            conn.execute(
                "UPDATE assets SET saved=? WHERE id=?",
                (1 if saved else 0, asset_id),
            )

    def delete_asset(self, asset_id):
        """删除指定 id 的素材记录。"""
        with self.connect() as conn:
            conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))

    def set_lora_note(self, model, lora_name, note):
        """
        写入 / 更新某模型 + LoRA 组合的备注笔记。

        参数:
            model (str): 模型名称。
            lora_name (str): LoRA 名称。
            note (str): 备注内容。

        说明:
            使用 UPSERT 语法，以 (model, lora_name) 联合唯一键为准：
            - 记录不存在 → 插入新记录；
            - 记录已存在 → 更新 note 与 updated_at。
        """
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO lora_notes(model,lora_name,note,updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(model,lora_name) DO UPDATE SET note=excluded.note, updated_at=excluded.updated_at",
                (model, lora_name, note, time.time()),  # updated_at 刷新为当前时间
            )

    def get_lora_note(self, model, lora_name):
        """
        查询某模型 + LoRA 组合的备注笔记。

        参数:
            model (str): 模型名称。
            lora_name (str): LoRA 名称。

        返回:
            str: 备注内容；若不存在则返回空字符串 ''。
        """
        with self.connect() as conn:
            # 按联合唯一键查询单条备注
            row = conn.execute(
                "SELECT note FROM lora_notes WHERE model=? AND lora_name=?",
                (model, lora_name),
            ).fetchone()
        # 查到返回 note，否则返回空串
        return row["note"] if row else ""

    def list_lora_notes(self):
        """
        查询全部 LoRA 备注。

        返回:
            list[dict]: 所有备注记录的字典列表（默认按插入顺序）。
        """
        with self.connect() as conn:
            # 读取全部备注（未排序，按主键顺序）
            rows = conn.execute("SELECT * FROM lora_notes").fetchall()
        return [dict(row) for row in rows]

    def list_assets(self, limit=300):
        """
        查询素材列表。

        参数:
            limit (int): 最多返回的条数，默认 300。

        返回:
            list[dict]: 素材记录字典列表，按创建时间倒序（最新在前）。
        """
        with self.connect() as conn:
            # 取最近 limit 条素材，倒序排列
            rows = conn.execute(
                "SELECT * FROM assets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_workflow(self, name, path, model, config):
        """
        新增一条工作流保存记录。

        参数:
            name (str): 工作流名称。
            path (str): 工作流文件路径。
            model (str): 关联的模型名称。
            config (dict): 工作流配置，序列化为 JSON 存储。

        返回:
            int: 新记录的自增主键 id。
        """
        with self.connect() as conn:
            # 插入工作流记录
            cur = conn.execute(
                "INSERT INTO workflows(name,path,model,config_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (name, path, model, json.dumps(config, ensure_ascii=False), time.time()),
            )
            return cur.lastrowid

    def list_workflows(self):
        """
        查询全部工作流记录。

        返回:
            list[dict]: 工作流记录字典列表，按创建时间倒序。
        """
        with self.connect() as conn:
            # 读取全部工作流，倒序排列
            rows = conn.execute(
                "SELECT * FROM workflows ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]
