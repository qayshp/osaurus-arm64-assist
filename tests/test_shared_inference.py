"""Shared preferences reach native calls; discovery stays off inference paths."""
import ctypes as C
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from osaurus_arm64_assist.discovery import DiscoveryRefresh
from osaurus_arm64_assist.engine import Engine
from osaurus_arm64_assist.native import Context, ModelParams, ContextParams
from osaurus_arm64_assist.protocol import ChatTemplate, normalize_request


class SharedInferenceTests(unittest.TestCase):
    def test_min_p_and_penalty_window_reach_the_sampler(self):
        engine = Engine.__new__(Engine)
        engine.api, engine.primary = Mock(), SimpleNamespace(vocab=1)
        engine.context_limit, engine.architecture = 2048, "spark2_5"
        request = normalize_request({"messages": [{"role": "user", "content": "test"}],
                                     "min_p": .25, "repeat_penalty": 1.1, "repeat_last_n": -1})
        engine.sampler(request, [1, 2])
        engine.api.llama_sampler_init_penalties.assert_called_once_with(2048, 1.1, 0., 0.)
        engine.api.llama_sampler_init_min_p.assert_called_once_with(.25, 1)
        self.assertEqual(engine.api.llama_sampler_accept.call_count, 2)

    def test_template_preserve_thinking_is_shared_without_replacing_messages(self):
        template = ChatTemplate("{{ preserve_thinking }}|{{ enable_thinking }}|{{messages[0].content}}", "", "")
        request = normalize_request({"messages": [{"role": "user", "content": "hello"}],
                                     "chat_template_kwargs": {"preserve_thinking": True}})
        self.assertEqual(template.render(request), "True|False|hello")
        with self.assertRaises(ValueError):
            normalize_request({**request, "chat_template_kwargs": {"messages": []}})

    def test_context_flags_reach_pinned_abi_without_changing_cpu_offload(self):
        for device in ("CPU", "HTP0"):
            with self.subTest(device=device):
                api = Mock()
                api.llama_model_default_params.return_value = ModelParams()
                api.llama_context_default_params.return_value = ContextParams()
                api.ggml_backend_dev_by_name.return_value = 1
                api.ggml_backend_cpu_buffer_type.return_value = 2
                api.llama_model_load_from_file.return_value = 3
                api.llama_init_from_model.return_value = 4
                ctx = Context(api, "fixture.gguf", {"flash_attention": False, "swa_full": True,
                              "kv_unified": True, "kv_offload": False, "op_offload": True}, device)
                params = api.llama_init_from_model.call_args.args[1]
                self.assertEqual(C.sizeof(params), 176)
                self.assertEqual(params.flash_attn_type, 0)
                self.assertTrue(params.swa_full and params.kv_unified)
                self.assertFalse(params.offload_kqv)
                self.assertEqual(params.op_offload, device != "CPU")
                ctx.close()

    def test_discovery_only_refreshes_advertised_changes_and_retries_errors(self):
        value = {"osaurus_url": "http://127.0.0.1:1337", "models": [{"id": "shared/test", "mode": "hybrid", "context": 2048}]}
        reader = Mock()
        watcher = DiscoveryRefresh(lambda: value, reader=reader, autostart=False)
        watcher.poll_once(); watcher.poll_once()
        self.assertEqual(reader.call_count, 1)
        value["models"][0]["sampling_defaults"] = {"temperature": .1}
        watcher.poll_once()
        self.assertEqual(reader.call_count, 1)
        value["models"] = []
        reader.side_effect = OSError("temporarily unavailable")
        watcher.poll_once(); watcher.poll_once()
        self.assertEqual(reader.call_count, 3)
        reader.side_effect = None
        watcher.poll_once(); watcher.poll_once()
        self.assertEqual(reader.call_count, 4)
        self.assertEqual(reader.call_args.args[1], "/v1/models?refresh=1")
        value["models_source"] = "fixed"
        value["models"] = [{"id": "manual", "mode": "cpu"}]
        watcher.poll_once()
        self.assertEqual(reader.call_count, 5, "opting out must withdraw automatic profiles from Osaurus discovery")
        watcher.close()


if __name__ == "__main__":
    unittest.main()
