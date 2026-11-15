"""Synthetic settings and tiny GGUF headers; no real models, APIs or DLLs."""
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from osaurus_arm64_assist import gguf_metadata
from osaurus_arm64_assist.model_settings import build_profile, inspect_gguf, sanitize_resolved, sanitize_settings


def entry(**changes):
    return {"id": "Synthetic-Raptor-Q6_K", "architecture": "spark2_5", "quantization": "Q6_K",
        "kind": "model", "contextLength": 1048576, "bestFitContext": 131072,
        "sizeBytes": 3690735072, **changes}


def settings(override=None, **defaults):
    return {"defaults": {"batchSize": 2048, "uBatchSize": 512, "useMmap": True,
        "useMlock": False, "repack": True, "kvUnified": True, "kvOffload": True,
        "opOffload": True, "swaFull": False, **defaults}, "override": override}


def resolved(**values):
    baseline = {"context_size": "131072", "gpu_layers": "0", "temperature": "0.80", "top_k": "40",
        "top_p": "0.95", "min_p": "0.05", "repeat_penalty": "1.00", "seed": "-1",
        "repeat_last_n": "64", "presence_penalty": "0.00", "frequency_penalty": "0.00",
        "chat_template_kwargs": '{"enable_thinking":false}'}
    baseline.update(values)
    return {"rows": [{"key": key, "value": value, "source": "plan" if key in ("context_size", "gpu_layers") else "model", "applies": True} for key, value in baseline.items()],
            "argv": ["never", "execute", "private-path"], "extraArgs": "never execute"}


def profile(e=None, s=None, r=None, runtime=None, **options):
    return build_profile(e or entry(), s or settings(), r or resolved(),
        runtime or {"localContextSize": None, "localGpuLayers": None, "localAccelerator": "Auto"},
        "synthetic.gguf", **options)


class ModelSettingsTests(unittest.TestCase):
    def test_known_raptor_uses_backend_auto_context_and_model_sampling_without_io(self):
        inputs = (entry(), settings(), resolved(), {"localContextSize": None, "localGpuLayers": None})
        before = copy.deepcopy(inputs)
        with mock.patch.object(Path, "stat", side_effect=AssertionError("mapper must not stat weights")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("mapper must not open weights")):
            value = build_profile(*inputs, "synthetic.gguf")
        self.assertEqual(inputs, before)
        self.assertEqual((value["mode"], value["context"], value["batch"], value["microbatch"]), ("hybrid", 2048, 2048, 512))
        self.assertEqual((value["threads"], value["batch_threads"]), (6, 10))
        self.assertEqual(value["sampling_defaults"], {"temperature": .8, "top_k": 40, "top_p": .95,
            "min_p": .05, "repeat_penalty": 1., "presence_penalty": 0., "frequency_penalty": 0.,
            "repeat_last_n": 64, "seed": 0xFFFFFFFF})
        self.assertIn("contextSize", value["sync"]["adapted"])
        self.assertEqual(value["template_kwargs"], {"enable_thinking": False})
        self.assertIsNone(value["_fallback_reason"])
        self.assertEqual(value["_osaurus"], {"model_id": "Synthetic-Raptor-Q6_K"})

    def test_known_ornith_quantizations_choose_experts_without_repack(self):
        for quant in ("Q4_K_M", "Q2_K"):
            with self.subTest(quant=quant):
                value = profile(entry(architecture="qwen35moe", quantization=quant, sizeBytes=21713463040),
                    r=resolved(temperature="0.6", top_k="20", chat_template_kwargs='{"preserve_thinking":true}'))
                self.assertEqual(value["mode"], "cpu-experts")
                self.assertFalse(value["repack"])
                self.assertEqual(value["sampling_defaults"]["temperature"], .6)
                self.assertEqual(value["template_kwargs"], {"preserve_thinking": True})

    def test_unknown_arch_or_quant_uses_stock_engine_with_actual_plan_context(self):
        for e in (entry(architecture="unverified"), entry(quantization="IQ2_XS")):
            with self.subTest(entry=e):
                value = profile(e)
                self.assertEqual(value["mode"], "osaurus")
                self.assertEqual(value["context"], 131072)
                self.assertEqual(value["_fallback_reason"], "native_compatibility_unverified")

    def test_cached_header_is_authoritative_over_inventory_metadata(self):
        value = profile(entry(_gguf={"architecture": "qwen35moe", "quantization": "Q2_K", "has_chat_template": True}))
        self.assertEqual(value["mode"], "cpu-experts")
        self.assertEqual(profile(entry(_gguf={"has_chat_template": False}))["mode"], "osaurus")

    def test_explicit_context_and_global_context_are_honored_or_use_stock_engine(self):
        self.assertEqual(profile(s=settings({"contextSize": 1024}))["context"], 1024)
        self.assertEqual(profile(runtime={"localContextSize": 1536})["context"], 1536)
        value = profile(s=settings({"contextSize": 8192}), r=resolved(context_size="8192"))
        self.assertEqual((value["mode"], value["context"]), ("osaurus", 8192))
        self.assertIn("contextSize", value["sync"]["unsupported"])
        self.assertEqual(profile(s=settings({"contextSize": 1024}), runtime={"localContextSize": 8192})["mode"], "hybrid")
        self.assertEqual(profile(s=settings({"contextSize": "1024"}))["mode"], "osaurus")

    def test_proxy_context_rejects_plan_above_training_or_invalid_display(self):
        value = profile(entry(architecture="unknown", contextLength=8192, bestFitContext=4096), r=resolved(context_size="131072"))
        self.assertEqual(value["context"], 4096)
        value = profile(entry(architecture="unknown", contextLength=8192, bestFitContext=4096), r=resolved(context_size="4096 --unsafe"))
        self.assertEqual(value["context"], 4096)

    def test_explicit_cpu_preference_is_distinct_from_stock_engine_auto_plan(self):
        self.assertEqual(profile()["mode"], "hybrid", "plan gpu_layers=0 is not a user choice")
        self.assertEqual(profile(s=settings({"gpuLayerCount": 0}))["_fallback_reason"], "explicit_cpu_preference")
        self.assertEqual(profile(runtime={"localGpuLayers": 0})["mode"], "osaurus")
        self.assertEqual(profile(s=settings({"gpuLayerCount": -1}), runtime={"localGpuLayers": 0})["mode"], "hybrid")
        self.assertEqual(profile(s=settings({"gpuLayerCount": 4}))["_fallback_reason"], "gpu_placement_unsupported")
        self.assertEqual(profile(runtime={"localGpuLayers": 4})["mode"], "osaurus")
        gpu = profile(s=settings({"gpuLayerCount": 4}), mode="gpu")
        self.assertEqual((gpu["mode"], gpu["gpu_layers"]), ("gpu", 4))

    def test_explicit_native_modes_and_gpu_constraints(self):
        for mode in ("cpu", "npu", "gpu", "hybrid"):
            with self.subTest(mode=mode):
                value = profile(mode=mode, identifier="manual/alias")
                self.assertEqual(value["mode"], mode)
                self.assertEqual(value["id"], "manual/alias")
                self.assertEqual(value["_requested_mode"], mode)
        self.assertEqual(profile(mode="gpu", s=settings({"flashAttention": True}))["mode"], "osaurus")
        self.assertEqual(profile(mode="gpu", s=settings({"flashAttention": False}))["mode"], "gpu")
        self.assertEqual(profile(entry(architecture="unknown"), mode="hybrid")["mode"], "osaurus")

    def test_user_native_fields_and_f16_cache_settings_apply(self):
        value = profile(s=settings({"threads": 4, "batchThreads": 8, "batchSize": 1024,
            "uBatchSize": 256, "flashAttention": False, "cacheTypeK": "F16", "cacheTypeV": "f16",
            "kvOffload": False, "opOffload": False, "kvUnified": False, "swaFull": True}))
        self.assertEqual((value["threads"], value["batch_threads"], value["batch"], value["microbatch"]), (4, 8, 1024, 256))
        self.assertFalse(value["flash_attention"]); self.assertFalse(value["kv_offload"])
        self.assertFalse(value["op_offload"]); self.assertFalse(value["kv_unified"]); self.assertTrue(value["swa_full"])
        self.assertEqual(value["mode"], "hybrid")

    def test_explicit_incompatible_settings_do_not_get_silently_ignored(self):
        changes = [{"cacheTypeK": "q8_0"}, {"cacheTypeV": "q4_0"}, {"useMmap": False},
            {"useMlock": True}, {"loadMode": "dio"}, {"useDirectIo": True}, {"threads": 0},
            {"uBatchSize": 4096, "batchSize": 128}, {"repack": True}]
        for change in changes:
            with self.subTest(change=change):
                e = entry(sizeBytes=21713463040) if "repack" in change else entry()
                value = profile(e, s=settings(change))
                self.assertEqual(value["mode"], "osaurus")
                self.assertTrue(value["sync"]["unsupported"])

    def test_sparse_explicit_unimplemented_settings_use_stock_even_when_equal_to_defaults(self):
        self.assertEqual(profile(s=settings({"temperature": .13, "isEmpty": False}))["mode"], "hybrid")
        self.assertEqual(profile(s=settings({"useMlock": False}))["mode"], "hybrid")
        self.assertEqual(profile(s=settings({"futureFlag": False}, futureFlag=False))["mode"], "osaurus")
        self.assertEqual(profile(s=settings({"cachePrompt": True}, cachePrompt=True))["mode"], "osaurus")
        self.assertEqual(profile(s=settings({"futureFlag": 0}, futureFlag=False))["mode"], "osaurus")

    def test_sampling_overrides_preserve_zero_and_reject_executable_display_strings(self):
        value = profile(s=settings({"temperature": 0, "topK": 0, "seed": 0, "minP": 0,
            "presencePenalty": 0, "frequencyPenalty": 0, "repeatLastN": 0}))
        for key in ("temperature", "top_k", "seed", "min_p", "presence_penalty", "frequency_penalty", "repeat_last_n"):
            self.assertEqual(value["sampling_defaults"][key], 0)
        for change in ({"temperature": "0.8; unsafe"}, {"seed": "-1"}, {"topP": 0}, {"temperature": float("nan")}):
            self.assertEqual(profile(s=settings(change))["mode"], "osaurus")
        self.assertEqual(profile(r=resolved(temperature="0.8 --unsafe"))["mode"], "osaurus")

    def test_model_template_flags_merge_with_user_override_and_preserve_thinking(self):
        value = profile(r=resolved(chat_template_kwargs='{"preserve_thinking":true}'),
            s=settings({"chatTemplateKwargs": '{"enable_thinking":false}'}))
        self.assertEqual(value["mode"], "hybrid")
        self.assertEqual(value["template_kwargs"], {"preserve_thinking": True, "enable_thinking": False})
        for options in ({"enable_thinking": True}, {"messages": []}, {"tools": []}, {"privatePrompt": "do not cache"}):
            self.assertEqual(profile(s=settings({"chatTemplateKwargs": json.dumps(options)}))["mode"], "osaurus")

    def test_sanitizers_never_retain_argv_custom_templates_or_private_values(self):
        marker = "private-user-content-must-not-be-cached"
        s = settings({"extraArgs": marker, "chatTemplate": marker, "lora": marker, "futureSecret": marker})
        r = resolved(); r.update(argv=[marker], extraArgs=marker)
        for row in r["rows"]: row["reason"] = marker
        safe_s, safe_r = sanitize_settings(s), sanitize_resolved(r)
        self.assertNotIn(marker, json.dumps([safe_s, safe_r]))
        self.assertEqual(set(safe_s["_unsupported"]), {"extraArgs", "chatTemplate", "lora", "futureSecret"})
        self.assertEqual(profile(s=safe_s, r=safe_r)["mode"], "osaurus")
        value = profile(s=s)
        self.assertNotIn(marker, json.dumps(value["sync"]))

    def test_inherited_diagnostic_names_are_bounded_and_resident_preferences_use_stock_engine(self):
        safe = sanitize_settings({"defaults": {}, "override": {}, "_unsupported": ["ropeScale", "private/path", "x"*200, None]})
        self.assertEqual(safe["_unsupported"], ["ropeScale"])
        safe = sanitize_resolved({"rows": [], "_invalid": ["context_size", "private/path", None]})
        self.assertEqual(safe["_invalid"], ["context_size"])
        self.assertEqual(profile(runtime={"maxResidentModels": 1})["mode"], "hybrid")
        for limit in (2, 0, True, "2"):
            with self.subTest(limit=limit):
                value = profile(runtime={"maxResidentModels": limit})
                self.assertEqual(value["mode"], "osaurus")
                self.assertIn("maxResidentModels", value["sync"]["unsupported"])


def gguf_string(value):
    data = value.encode("utf-8")
    return struct.pack("<Q", len(data)) + data


def gguf_scalar(key, kind, value):
    return gguf_string(key)+struct.pack("<I", kind)+(gguf_string(value) if kind == 8 else struct.pack("<"+gguf_metadata._SCALARS[kind], value))


class HeaderReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"fixture.gguf"

    def write(self, entries, version=3):
        self.path.write_bytes(b"GGUF"+struct.pack("<IQQ", version, 1, len(entries))+b"".join(entries)+b"tensor-payload-not-read")

    def test_metadata_skips_tokenizer_arrays_and_template_text(self):
        array = gguf_string("tokenizer.ggml.tokens")+struct.pack("<IIQ", 9, 8, 3)+b"".join(gguf_string(s) for s in ["a", "café", "你好"])
        self.write([gguf_scalar("general.architecture", 8, "spark2_5"), array,
            gguf_scalar("tokenizer.chat_template", 8, "private-template-must-not-be-returned"),
            gguf_scalar("general.file_type", 4, 18), gguf_scalar("spark2_5.context_length", 4, 1048576)])
        result = inspect_gguf(self.path)
        self.assertEqual((result["architecture"], result["quantization"], result["context_length"]), ("spark2_5", "Q6_K", 1048576))
        self.assertTrue(result["has_chat_template"])
        self.assertLess(result["header_bytes"], result["size_bytes"])
        self.assertNotIn("private-template", json.dumps(result))

    def test_split_and_nonchat_classification_is_available_without_tensor_reads(self):
        self.write([gguf_scalar("general.architecture", 8, "clip"), gguf_scalar("general.type", 8, "adapter"),
            gguf_scalar("split.count", 2, 3), gguf_scalar("split.no", 2, 1), gguf_scalar("split.tensors.count", 10, 123)])
        result = inspect_gguf(self.path)
        self.assertEqual((result["architecture"], result["general_type"], result["split_count"], result["split_no"]), ("clip", "adapter", 3, 1))
        self.assertFalse(result["has_chat_template"])

    def test_corrupt_counts_types_versions_duplicates_and_truncation_are_rejected(self):
        malformed = [b"NOPE", b"GGUF"+struct.pack("<IQQ", 1, 0, 0),
            b"GGUF"+struct.pack("<IQQ", 3, 0, 100001),
            b"GGUF"+struct.pack("<IQQ", 3, 0, 1)+gguf_string("x")+struct.pack("<I", 999),
            b"GGUF"+struct.pack("<IQQ", 3, 0, 1)+gguf_string("x")+struct.pack("<IIQ", 9, 8, 2000001)]
        for content in malformed:
            self.path.write_bytes(content)
            with self.subTest(content=content[:24]), self.assertRaises(ValueError): inspect_gguf(self.path)
        self.write([gguf_scalar("general.architecture", 8, "one"), gguf_scalar("general.architecture", 8, "two")])
        with self.assertRaises(ValueError): inspect_gguf(self.path)
        self.write([gguf_scalar("split.count", 2, 1), gguf_scalar("split.no", 2, 1)])
        with self.assertRaises(ValueError): inspect_gguf(self.path)

    def test_header_limit_and_concurrent_change_are_rejected(self):
        self.write([gguf_scalar("general.architecture", 8, "spark2_5")])
        with mock.patch.object(gguf_metadata, "MAX_HEADER_BYTES", 24):
            with self.assertRaises(ValueError): inspect_gguf(self.path)
        size = self.path.stat().st_size
        with mock.patch.object(Path, "stat", side_effect=[SimpleNamespace(st_size=size, st_mtime_ns=1),
                SimpleNamespace(st_size=size+1, st_mtime_ns=2)]):
            with self.assertRaises(ValueError): inspect_gguf(self.path)


if __name__ == "__main__":
    unittest.main()
