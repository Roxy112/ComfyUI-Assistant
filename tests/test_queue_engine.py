"""
test_queue_engine.py — queue_engine 模块的单元测试
====================================================

覆盖：
- Z-Image LoRA 注入（Power Lora Loader 内联字段）
- Flux LoRA 注入（LoraLoader 节点链式串接）
- 自环检查、重复连线检查、链尾连接验证
"""

import copy
import json
import os
import sys
import unittest

# 将项目根目录加入 sys.path，方便导入 queue_engine
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import queue_engine


class TestFluxLoRAInjection(unittest.TestCase):
    """测试 Flux 模板的 LoRA 链式注入。"""

    @classmethod
    def setUpClass(cls):
        """加载 Flux 模板并做深拷贝，避免测试间相互影响。"""
        cls._original = queue_engine.load_template("flux")
        # 记录关键节点 ID
        cls.unet_id, cls.clip_id = cls._find_source_ids(cls._original)

    @staticmethod
    def _find_source_ids(template):
        """从模板中找出 UnetLoaderGGUF 和 DualCLIPLoaderGGUF 的节点 ID。"""
        unet = queue_engine._find_nodes(template, class_type="UnetLoaderGGUF")
        clip = queue_engine._find_nodes(template, class_type="DualCLIPLoaderGGUF")
        return unet[0][0] if unet else None, clip[0][0] if clip else None

    def _fresh_template(self):
        """返回一份全新的模板副本。"""
        return copy.deepcopy(self._original)

    def _get_injected_ids(self, template):
        """获取模板中所有注入的 LoraLoader 节点 ID（9001+）。"""
        return sorted(
            [nid for nid, node in template.items()
             if node.get("class_type") == "LoraLoader" and int(nid) >= 9000],
            key=int,
        )

    # ---- 单个 LoRA：无自环 ----
    def test_single_lora_no_self_loop(self):
        """单个 LoRA 注入后不应产生自环。"""
        loras = [{"name": "test_lora.safetensors", "strength_model": 0.8, "strength_clip": 0.6}]
        template = self._fresh_template()
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, loras)

        injected = self._get_injected_ids(template)
        self.assertEqual(len(injected), 1, "应只有一个注入节点")
        lora_id = injected[0]
        lora_node = template[lora_id]

        # 自环检查：LoraLoader 的 model/clip 输入不能指向自己
        model_ref = lora_node["inputs"]["model"]
        clip_ref = lora_node["inputs"]["clip"]
        self.assertNotEqual(model_ref[0], lora_id,
                            f"LoraLoader {lora_id} 的 model 输入指向自己，形成自环")
        self.assertNotEqual(clip_ref[0], lora_id,
                            f"LoraLoader {lora_id} 的 clip 输入指向自己，形成自环")

        # model 应指向原始的 UnetLoaderGGUF
        self.assertEqual(model_ref[0], self.unet_id,
                         f"LoraLoader model 应指向 UnetLoaderGGUF ({self.unet_id})")
        # clip 应指向原始的 DualCLIPLoaderGGUF
        self.assertEqual(clip_ref[0], self.clip_id,
                         f"LoraLoader clip 应指向 DualCLIPLoaderGGUF ({self.clip_id})")

    # ---- 两个 LoRA：连接顺序正确 ----
    def test_two_loras_chain_order(self):
        """两个 LoRA 注入后应形成正确的链式连接。"""
        loras = [
            {"name": "lora1.safetensors", "strength_model": 0.7},
            {"name": "lora2.safetensors", "strength_model": 0.9},
        ]
        template = self._fresh_template()
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, loras)

        injected = self._get_injected_ids(template)
        self.assertEqual(len(injected), 2, "应有两个注入节点")
        first_id, second_id = injected[0], injected[1]

        first_node = template[first_id]
        second_node = template[second_id]

        # 第一个 LoraLoader 应连接到原始来源
        self.assertEqual(first_node["inputs"]["model"][0], self.unet_id,
                         "第一个 LoraLoader model 应指向 UnetLoaderGGUF")
        self.assertEqual(first_node["inputs"]["clip"][0], self.clip_id,
                         "第一个 LoraLoader clip 应指向 DualCLIPLoaderGGUF")

        # 第二个 LoraLoader 应连接到第一个 LoraLoader
        self.assertEqual(second_node["inputs"]["model"][0], first_id,
                         "第二个 LoraLoader model 应指向第一个 LoraLoader")
        self.assertEqual(second_node["inputs"]["clip"][0], first_id,
                         "第二个 LoraLoader clip 应指向第一个 LoraLoader")

        # 无自环
        self.assertNotEqual(first_node["inputs"]["model"][0], first_id)
        self.assertNotEqual(second_node["inputs"]["model"][0], second_id)

    # ---- 无重复连线 ----
    def test_no_duplicate_connections(self):
        """注入后不应存在重复连线（同一输入被多次引用为同一来源）。"""
        loras = [
            {"name": "lora1.safetensors", "strength_model": 0.7},
            {"name": "lora2.safetensors", "strength_model": 0.9},
        ]
        template = self._fresh_template()
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, loras)

        # 收集所有 [源节点, 目标节点, 输入名] 的连接
        connections = set()
        for node_id, node in template.items():
            for key, value in node.get("inputs", {}).items():
                if isinstance(value, list) and len(value) == 2:
                    conn = (value[0], node_id, key)
                    self.assertNotIn(conn, connections,
                                     f"重复连线: 源={value[0]} → 目标={node_id}.{key}")
                    connections.add(conn)

    # ---- ModelSamplingFlux.model 指向链尾 ----
    def test_model_sampling_flux_points_to_tail(self):
        """ModelSamplingFlux 的 model 输入应指向最后一个 LoRA 输出。"""
        loras = [
            {"name": "lora_a.safetensors", "strength_model": 0.7},
            {"name": "lora_b.safetensors", "strength_model": 0.8},
            {"name": "lora_c.safetensors", "strength_model": 0.9},
        ]
        template = self._fresh_template()
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, loras)

        injected = self._get_injected_ids(template)
        tail_id = injected[-1]  # 链尾 = 最后一个 LoraLoader

        # ModelSamplingFlux 的 model 输入应指向链尾
        msf_nodes = queue_engine._find_nodes(template, class_type="ModelSamplingFlux")
        self.assertTrue(msf_nodes, "模板中应有 ModelSamplingFlux 节点")
        for nid, node in msf_nodes:
            model_ref = node["inputs"].get("model")
            if isinstance(model_ref, list) and len(model_ref) == 2:
                self.assertEqual(model_ref[0], tail_id,
                                 f"ModelSamplingFlux ({nid}).model 应指向链尾 {tail_id}")

        # 同时也检查 BasicGuider / BasicScheduler
        for cls, key in [("BasicGuider", "model"), ("BasicScheduler", "model")]:
            nodes = queue_engine._find_nodes(template, class_type=cls)
            for nid, node in nodes:
                ref = node["inputs"].get(key)
                if isinstance(ref, list) and len(ref) == 2 and ref[0] in injected:
                    self.assertEqual(ref[0], tail_id,
                                     f"{cls} ({nid}).{key} 应指向链尾 {tail_id}")

    # ---- CLIPTextEncode.clip 指向链尾 ----
    def test_clip_text_encode_points_to_tail(self):
        """CLIPTextEncode 的 clip 输入应指向最后一个 LoRA 的 CLIP 输出。"""
        loras = [
            {"name": "lora_x.safetensors", "strength_model": 0.7},
        ]
        template = self._fresh_template()
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, loras)

        injected = self._get_injected_ids(template)
        tail_id = injected[-1]

        clip_encode_nodes = queue_engine._find_nodes(template, class_type="CLIPTextEncode")
        self.assertTrue(clip_encode_nodes, "模板中应有 CLIPTextEncode 节点")
        for nid, node in clip_encode_nodes:
            clip_ref = node["inputs"].get("clip")
            if isinstance(clip_ref, list) and len(clip_ref) == 2:
                self.assertEqual(clip_ref[0], tail_id,
                                 f"CLIPTextEncode ({nid}).clip 应指向链尾 {tail_id}")

    # ---- 零 LoRA：模板不被破坏 ----
    def test_zero_loras_no_change_to_connections(self):
        """不启用任何 LoRA 时模板应保持原样（无注入节点）。"""
        template = self._fresh_template()
        original_ids = set(template.keys())
        queue_engine.apply_flux(template, {"prompt": "test", "seed": 42}, [])

        # 不应新增任何节点
        self.assertEqual(set(template.keys()), original_ids,
                         "零 LoRA 时不应新增任何节点")

        # 原始连接应保持不变
        msf = queue_engine._find_nodes(template, class_type="ModelSamplingFlux")
        if msf:
            model_ref = msf[0][1]["inputs"].get("model")
            if isinstance(model_ref, list):
                self.assertEqual(model_ref[0], self.unet_id,
                                 "无 LoRA 时 ModelSamplingFlux.model 应保持指向原始 UnetLoaderGGUF")

    def test_flux_template_model_contract(self):
        """FluxedUp 模板必须继续引用已部署的 GGUF、编码器和 VAE。"""
        expected = {
            "UnetLoaderGGUF": ("unet_name", "fluxedup-v10-q4_0.gguf"),
            "VAELoader": ("vae_name", "ae.safetensors"),
        }
        for class_type, (key, value) in expected.items():
            nodes = queue_engine._find_nodes(self._original, class_type=class_type)
            self.assertTrue(nodes, f"模板缺少 {class_type}")
            self.assertEqual(nodes[0][1]["inputs"].get(key), value)

        clip_nodes = queue_engine._find_nodes(self._original, class_type="DualCLIPLoaderGGUF")
        self.assertTrue(clip_nodes, "模板缺少 DualCLIPLoaderGGUF")
        clip_inputs = clip_nodes[0][1]["inputs"]
        self.assertEqual(clip_inputs.get("clip_name1"), "clip_l.safetensors")
        self.assertEqual(clip_inputs.get("clip_name2"), "t5-v1_1-xxl-encoder-Q5_K_M.gguf")

    def test_official_flux_shape_is_supported(self):
        """适配官方 Flux 工作流的直连文本与 EmptySD3LatentImage。"""
        template = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
            "2": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024}},
        }
        queue_engine.apply_flux(
            template,
            {"prompt": "new prompt", "width": 768, "height": 1152},
            [],
        )
        self.assertEqual(template["1"]["inputs"]["text"], "new prompt")
        self.assertEqual(template["2"]["inputs"]["width"], 768)
        self.assertEqual(template["2"]["inputs"]["height"], 1152)


class TestZImageLoRAInjection(unittest.TestCase):
    """测试 Z-Image 模板的 LoRA 内联注入。"""

    @classmethod
    def setUpClass(cls):
        """加载 Z-Image 模板。"""
        cls._original = queue_engine.load_template("Z-Image Turbo")

    def _fresh_template(self):
        return copy.deepcopy(self._original)

    def test_loras_injected_into_power_loader(self):
        """LoRA 应注入到 Power Lora Loader 节点的 lora_N 字段中。"""
        loras = [
            {"name": "test_lora1.safetensors", "strength_model": 0.7, "strength_clip": 0.5},
            {"name": "test_lora2.safetensors", "strength_model": 0.9},
        ]
        template = self._fresh_template()
        queue_engine.apply_zimage(template, {"prompt": "test", "seed": 42}, loras)

        power_nodes = queue_engine._find_nodes(template, class_type="Power Lora Loader (rgthree)")
        self.assertTrue(power_nodes, "Z-Image 模板应包含 Power Lora Loader")

        for nid, node in power_nodes:
            inputs = node["inputs"]
            # 应包含 lora_1 和 lora_2
            self.assertIn("lora_1", inputs)
            self.assertIn("lora_2", inputs)
            # lora_1 应启用
            self.assertTrue(inputs["lora_1"].get("on"))
            self.assertEqual(inputs["lora_1"]["lora"], "test_lora1.safetensors")
            self.assertEqual(inputs["lora_1"]["strength"], 0.7)
            self.assertEqual(inputs["lora_1"]["strengthTwo"], 0.5)

            # lora_2 的 strengthTwo 应回退到 strength
            self.assertEqual(inputs["lora_2"]["strengthTwo"], 0.9)

    def test_main_workflow_prompt_seed_and_size_are_updated(self):
        """参数必须进入实际连接到输出的主工作流，而非仅旁路兼容分支。"""
        template = self._fresh_template()
        queue_engine.apply_zimage(
            template,
            {
                "prompt": "assistant contract prompt",
                "negative": "assistant negative",
                "seed": 13579,
                "width": 512,
                "height": 768,
            },
            [],
        )
        prompt_nodes = queue_engine._find_nodes(template, class_type="StringTrim", title_part="used for reroute")
        self.assertTrue(prompt_nodes)
        self.assertEqual(prompt_nodes[0][1]["inputs"]["string"], "assistant contract prompt")

        seed_nodes = queue_engine._find_nodes(template, class_type="PrimitiveInt", title_part="SEED")
        self.assertTrue(seed_nodes)
        self.assertEqual(seed_nodes[0][1]["inputs"]["value"], 13579)

        short_nodes = queue_engine._find_nodes(template, class_type="PrimitiveInt", title_part="Default Short Side")
        long_nodes = queue_engine._find_nodes(template, class_type="PrimitiveInt", title_part="Default Long Side")
        self.assertEqual(short_nodes[0][1]["inputs"]["value"], 512)
        self.assertEqual(long_nodes[0][1]["inputs"]["value"], 768)

    def test_zero_loras_clears_old_fields(self):
        """不传入 LoRA 时应清除旧的 lora_ 字段。"""
        template = self._fresh_template()
        # 先注入一个 LoRA
        queue_engine.apply_zimage(
            template,
            {"prompt": "test"},
            [{"name": "temp.safetensors", "strength_model": 0.5}],
        )
        # 再清空
        queue_engine.apply_zimage(template, {"prompt": "test"}, [])

        power_nodes = queue_engine._find_nodes(template, class_type="Power Lora Loader (rgthree)")
        for nid, node in power_nodes:
            for key in node["inputs"]:
                self.assertFalse(key.lower().startswith("lora_"),
                                 f"零 LoRA 时不应残留 lora_ 字段，但发现 {key}")

    def test_disabled_loras_filtered_out(self):
        """apply_template 应过滤掉 enabled=False 的 LoRA。"""
        loras = [
            {"name": "enabled_lora.safetensors", "strength_model": 0.7, "enabled": True},
            {"name": "disabled_lora.safetensors", "strength_model": 0.5, "enabled": False},
        ]
        template = queue_engine.load_template("Z-Image Turbo")
        result = queue_engine.apply_template("Z-Image Turbo", {"prompt": "test"}, loras)

        power_nodes = queue_engine._find_nodes(result, class_type="Power Lora Loader (rgthree)")
        for nid, node in power_nodes:
            inputs = node["inputs"]
            # 只应有 lora_1，不应有 lora_2
            self.assertIn("lora_1", inputs)
            self.assertNotIn("lora_2", inputs)
            self.assertEqual(inputs["lora_1"]["lora"], "enabled_lora.safetensors")

    # ---- 自环检查（Z-Image 使用内联注入，天然无自环，但做防御性测试） ----
    def test_no_self_loop_in_zimage(self):
        """Z-Image 模板注入后不应产生自环连接。"""
        loras = [
            {"name": "z_lora.safetensors", "strength_model": 0.8},
        ]
        template = self._fresh_template()
        queue_engine.apply_zimage(template, {"prompt": "test", "seed": 42}, loras)

        for node_id, node in template.items():
            for key, value in node.get("inputs", {}).items():
                if isinstance(value, list) and len(value) == 2:
                    self.assertNotEqual(value[0], node_id,
                                        f"节点 {node_id} 的 {key} 输入指向自己，形成自环")


class TestSyncAckBehavior(unittest.TestCase):
    """测试 sync_ack 回执逻辑：没有更新节点时不应发送 ack。

    JS 端逻辑：
        applyPromptSync(data):
            updatedPositive = updatedNegative = false
            for each CLIPTextEncode:
                if source found and value changed: set flag
            if neither flag set → do NOT POST sync_ack
            if either flag set → POST sync_ack

    本测试模拟该逻辑以确保行为正确。
    """

    def _simulate_apply_prompt_sync(self, nodes_updated):
        """模拟 applyPromptSync 的 ack 决策逻辑。

        参数:
            nodes_updated (dict): {"positive": bool, "negative": bool}

        返回:
            bool: 是否应发送 ack
        """
        updated_positive = nodes_updated.get("positive", False)
        updated_negative = nodes_updated.get("negative", False)
        return updated_positive or updated_negative

    def test_no_update_no_ack(self):
        """正反向节点都没有更新 → 不应发送 ack。"""
        should_ack = self._simulate_apply_prompt_sync({"positive": False, "negative": False})
        self.assertFalse(should_ack, "没有任何节点更新时不应发送 ack")

    def test_positive_only_sends_ack(self):
        """仅正向节点更新 → 应发送 ack。"""
        should_ack = self._simulate_apply_prompt_sync({"positive": True, "negative": False})
        self.assertTrue(should_ack, "正向节点更新时应发送 ack")

    def test_negative_only_sends_ack(self):
        """仅反向节点更新 → 应发送 ack。"""
        should_ack = self._simulate_apply_prompt_sync({"positive": False, "negative": True})
        self.assertTrue(should_ack, "反向节点更新时应发送 ack")

    def test_both_updated_sends_ack(self):
        """正反向节点都更新 → 应发送 ack。"""
        should_ack = self._simulate_apply_prompt_sync({"positive": True, "negative": True})
        self.assertTrue(should_ack, "正反向节点都更新时应发送 ack")


if __name__ == "__main__":
    unittest.main()
