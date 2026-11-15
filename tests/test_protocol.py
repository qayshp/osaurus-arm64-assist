"""Public request/template contracts, without a model, driver or native DLL."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from jinja2.exceptions import SecurityError

from osaurus_arm64_assist import config
from osaurus_arm64_assist.protocol import (
    ChatTemplate, grammar_for, normalize_request, parse_tool_calls,
    reject_external_refs, validate_arguments,
)

ROOT = Path(__file__).resolve().parents[1]


def tool(name="lookup", schema=None):
    return {"type": "function", "function": {"name": name, "description": "Read a synthetic test value.",
        "parameters": schema if schema is not None else {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}}


def request(**changes):
    value = {"messages": [{"role": "user", "content": "A synthetic question."}]}
    value.update(changes)
    return normalize_request(value)


def embedded_template(name):
    filename = "raptor-cpu-requests.json" if name == "spark" else "ornith-q4-cpu-requests.json"
    source = json.loads((ROOT / "evidence" / filename).read_text(encoding="utf-8"))["chat_template"]
    return ChatTemplate(source, "<s>", "</s>")


class RequestNormalizationTests(unittest.TestCase):
    def test_text_blocks_developer_role_and_history_arguments(self):
        body = {"messages": [
            {"role": "developer", "content": [{"type": "text", "text": "Keep units."},
                {"type": "text", "text": "Preserve café and 你好."}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_test", "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"harbor"}'}}]},
            {"role": "tool", "tool_call_id": "call_test", "content": "23°C"},
            {"role": "user", "content": "Continue."}], "tools": [tool()]}
        original = copy.deepcopy(body)
        result = normalize_request(body)
        self.assertEqual(body, original)
        self.assertEqual(result["messages"][0]["role"], "system")
        self.assertEqual(result["messages"][0]["content"], "Keep units.\nPreserve café and 你好.")
        self.assertEqual(result["messages"][1]["tool_calls"][0]["function"]["arguments"], {"query": "harbor"})
        self.assertEqual(result["messages"][2]["tool_call_id"], "call_test")

    def test_completion_limit_precedence(self):
        self.assertEqual(request(max_tokens=48, max_completion_tokens=17)["max_tokens"], 17)

    def test_unsupported_multimodal_and_schema_output_are_explicit(self):
        for change in [
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]},
            {"response_format": {"type": "json_schema", "json_schema": {}}},
            {"enable_thinking": True}, {"n": 2}, {"logprobs": True},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(**change)

    def test_json_object_and_forced_tools_reject_ambiguous_stops(self):
        result = request(response_format={"type": "json_object"})
        self.assertEqual(result["response_format"], {"type": "json_object"})
        for change in [
            {"response_format": {"type": "json_object"}, "tools": [tool()]},
            {"response_format": {"type": "json_object"}, "stop": "end"},
            {"tools": [tool()], "tool_choice": "required", "stop": ["end"]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(**change)

    def test_tool_names_and_choice_must_be_advertised(self):
        for name in ["", "has space", "has/slash", "x>injection", 'x"quote', None, 7]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                request(tools=[tool(name)])
        with self.assertRaises(ValueError):
            request(tools=[tool(), tool()])
        with self.assertRaises(ValueError):
            request(tools=[tool()], tool_choice={"type": "function", "function": {"name": "missing"}})
        with self.assertRaises(ValueError):
            request(tool_choice="required")

    def test_external_schema_references_are_never_resolved(self):
        for ref in ["https://example.invalid/schema", "file:///private/schema.json", "other.json#/x"]:
            schema = {"type": "object", "properties": {"payload": {"items": [{"$ref": ref}]}}}
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                request(tools=[tool(schema=schema)])
        reject_external_refs({"$defs": {"x": {"type": "integer"}}, "properties": {"x": {"$ref": "#/$defs/x"}}})

    def test_malformed_shapes_are_client_errors(self):
        for change in [
            {"tools": [None]}, {"tools": {"function": "x"}},
            {"tools": [{"type": "function", "function": []}]},
            {"response_format": ["json_object"]},
            {"tool_choice": {"type": "function", "function": "lookup"}},
            {"messages": [{"role": "assistant", "tool_calls": [{"function": "lookup"}]}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(**change)

    def test_booleans_do_not_become_numeric_sampling_controls(self):
        for key in ["temperature", "top_p", "n", "max_tokens"]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                request(**{key: True})


class EmbeddedTemplateTests(unittest.TestCase):
    def test_spark_single_user_matches_benchmark_framing(self):
        actual = embedded_template("spark").render(request(messages=[{"role": "user", "content": "Hello 你好"}]))
        start = "<\uff5cstart\u2581of\u2581sentence\uff5c>"; end = "<\uff5cend\u2581of\u2581sentence\uff5c>"
        expected = start+"<|System|>\nyou are a helpful assistant."+end+start+"<|User|>Hello 你好"+end+start+"<|Bot|></think>"
        self.assertEqual(actual, expected)

    def test_qwen_single_user_matches_benchmark_framing(self):
        actual = embedded_template("qwen").render(request(messages=[{"role": "user", "content": "Hello 你好"}]))
        self.assertEqual(actual, "<|im_start|>user\nHello 你好<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")

    def test_both_embedded_templates_preserve_full_tool_conversation(self):
        body = {"messages": [
            {"role": "system", "content": "Keep Celsius units."},
            {"role": "user", "content": "Read harbor temperature."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_test", "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"harbor"}'}}]},
            {"role": "tool", "tool_call_id": "call_test", "content": '{"temperature":"23°C"}'},
            {"role": "user", "content": "Explain that reading."}], "tools": [tool()]}
        normalized = normalize_request(body)
        for name in ["spark", "qwen"]:
            with self.subTest(name=name):
                rendered = embedded_template(name).render(normalized)
                for value in ["Keep Celsius units.", "Read harbor temperature.", "lookup", "harbor", "23°C", "Explain that reading."]:
                    self.assertIn(value, rendered)
                self.assertIn("<tool_call>", rendered)
                self.assertIn("<tool_response>", rendered)

    def test_forced_tool_and_json_instructions_preserve_system_message(self):
        for name in ["spark", "qwen"]:
            with self.subTest(name=name):
                template = embedded_template(name)
                base = [{"role": "system", "content": "Keep the original system instruction."}, {"role": "user", "content": "Proceed."}]
                forced = request(messages=base, tools=[tool()], tool_choice={"type": "function", "function": {"name": "lookup"}})
                rendered = template.render(forced)
                self.assertIn("Call lookup.", rendered)
                self.assertIn("Keep the original system instruction.", rendered)
                rendered = template.render(request(messages=base, response_format={"type": "json_object"}))
                self.assertIn("Return one JSON object, without Markdown.", rendered)
                self.assertIn("Keep the original system instruction.", rendered)

    def test_tool_choice_none_hides_new_tool_advertisement(self):
        for name in ["spark", "qwen"]:
            with self.subTest(name=name):
                rendered = embedded_template(name).render(request(tools=[tool("should_not_appear")], tool_choice="none"))
                self.assertNotIn("should_not_appear", rendered)

    def test_embedded_template_cannot_inspect_python_class_hierarchy(self):
        template = ChatTemplate("{{ ().__class__.__mro__ }}", "", "")
        with self.assertRaises(SecurityError):
            template.render(request())


class ForcedGrammarTests(unittest.TestCase):
    def test_json_object_grammar_has_object_root(self):
        grammar = grammar_for(request(response_format={"type": "json_object"}), "spark2_5")
        self.assertTrue(grammar.startswith("root ::= ws object\n"))
        self.assertIn('object ::= "{"', grammar)

    def test_selected_tool_uses_architecture_specific_markers(self):
        value = request(tools=[tool("lookup"), tool("other")], tool_choice={"type": "function", "function": {"name": "lookup"}})
        spark = grammar_for(value, "spark2_5")
        qwen = grammar_for(value, "qwen35moe")
        self.assertIn(json.dumps("<tool_call>lookup"), spark)
        self.assertIn(json.dumps("<tool_call>\n<function=lookup>\n"), qwen)
        self.assertNotIn("other", spark)
        self.assertNotIn("other", qwen)

    def test_schema_enums_are_json_quoted_and_optional_fields_stay_optional(self):
        schema = {"type": "object", "properties": {"state": {"type": "string", "enum": ["ready", "not ready"]},
            "detail": {"type": "object"}}, "required": ["state"]}
        grammar = grammar_for(request(tools=[tool(schema=schema)], tool_choice="required"), "spark2_5")
        self.assertIn(json.dumps(json.dumps("not ready")), grammar)
        self.assertIn(")?", grammar)

    def test_qwen3_forced_tool_uses_json_wrapper_and_selected_name(self):
        value = request(tools=[tool("lookup"), tool("other")],
            tool_choice={"type": "function", "function": {"name": "lookup"}})
        grammar = grammar_for(value, "qwen3")
        call = grammar.splitlines()[1]
        self.assertTrue(call.startswith('call0 ::= "<tool_call>" ws "{" ws'))
        self.assertIn(json.dumps('"name"'), call)
        self.assertIn(json.dumps('"lookup"'), call)
        self.assertIn(json.dumps('"arguments"'), call)
        self.assertIn(' object "}" ws "</tool_call>"', call)
        self.assertNotIn("other", grammar)
        self.assertNotIn("<function=", grammar)
        self.assertNotIn("<arg_key>", grammar)

    def test_qwen3_required_grammar_allows_each_advertised_name(self):
        grammar = grammar_for(request(tools=[tool("lookup"), tool("other")], tool_choice="required"), "qwen3")
        self.assertTrue(grammar.startswith("root ::= call0 | call1\n"))
        for name in ["lookup", "other"]:
            self.assertIn(json.dumps(json.dumps(name)), grammar)

    def test_required_unknown_field_or_architecture_rejected(self):
        bad = {"type": "object", "properties": {}, "required": ["missing"]}
        with self.assertRaises(ValueError):
            grammar_for(request(tools=[tool(schema=bad)], tool_choice="required"), "spark2_5")
        with self.assertRaises(ValueError):
            grammar_for(request(tools=[tool()], tool_choice="required"), "unverified_architecture")
        self.assertIsNone(grammar_for(request(tools=[tool()]), "unverified_architecture"))


class ToolParserTests(unittest.TestCase):
    def test_qwen3_json_nested_tags_and_multiple_calls_preserve_arguments(self):
        schema = {"type": "object", "properties": {"payload": {"type": "object"}},
            "required": ["payload"], "additionalProperties": False}
        payload = {"text": 'café 你好 </tool_call> <tool_call>{"name":"fake"}</tool_call>',
            "items": [1, {"q": "</parameter> and <function=literal>"}]}
        encoded = json.dumps({"name": "lookup", "arguments": {"payload": payload}}, ensure_ascii=False)
        text = "Before <tool_call>\n"+encoded+"\n</tool_call> Between <tool_call>"+encoded+"</tool_call> After"
        content, calls = parse_tool_calls(text, [tool(schema=schema)])
        self.assertEqual(calls, [{"name": "lookup", "arguments": {"payload": payload}}]*2)
        self.assertEqual(content, "Before  Between  After")

    def test_qwen3_json_tools_reject_bad_wrapper_arguments_and_schemas(self):
        malformed = [
            {"name": "unknown", "arguments": {"query": "x"}},
            {"name": ["lookup"], "arguments": {"query": "x"}},
            {"name": "lookup", "arguments": "{\"query\":\"x\"}"},
            {"name": "lookup", "arguments": []},
            {"name": "lookup", "arguments": {"query": 42}},
            {"name": "lookup", "arguments": {}},
            {"name": "lookup", "arguments": {"query": "x", "extra": 1}},
            {"name": "lookup"},
            {"name": "lookup", "arguments": {"query": "x"}, "extra": True},
        ]
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tool_calls("<tool_call>"+json.dumps(value)+"</tool_call>", [tool()])
        for text in ['<tool_call>{"name":"lookup","arguments":{"query":"x"}}',
                '<tool_call>{"name":"lookup","arguments":{"query":"x"}} garbage</tool_call>',
                '<tool_call>{"name":"lookup","arguments":</tool_call>']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_tool_calls(text, [tool()])

    def test_nested_json_with_delimiter_like_strings_is_not_split(self):
        schema = {"type": "object", "properties": {"payload": {"type": "object"}}, "required": ["payload"], "additionalProperties": False}
        payload = {"text": "literal </arg_value> and <tool_call>fake</tool_call>", "items": [1, {"q": "</parameter>"}]}
        encoded = json.dumps(payload)
        spark = "Before <tool_call>lookup<arg_key>payload</arg_key><arg_value>"+encoded+"</arg_value></tool_call> After"
        content, calls = parse_tool_calls(spark, [tool(schema=schema)])
        self.assertEqual(calls, [{"name": "lookup", "arguments": {"payload": payload}}])
        self.assertIn("Before", content); self.assertIn("After", content)

    def test_qwen_json_string_can_contain_closing_parameter_tag(self):
        value = "café 你好 </parameter> <tool_call>literal</tool_call>"
        text = "<tool_call>\n<function=lookup>\n<parameter=query>\n"+json.dumps(value)+"\n</parameter>\n</function>\n</tool_call>"
        content, calls = parse_tool_calls(text, [tool()])
        self.assertEqual(content, "")
        self.assertEqual(calls[0]["arguments"]["query"], value)

    def test_trained_plain_string_arguments_and_multiple_calls(self):
        one = "<tool_call>lookup<arg_key>query</arg_key><arg_value>plain text</arg_value></tool_call>"
        content, calls = parse_tool_calls(one+"\n"+one, [tool()])
        self.assertEqual(content, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["arguments"], {"query": "plain text"})

    def test_unknown_duplicate_unclosed_or_schema_invalid_calls_fail(self):
        malformed = [
            "<tool_call>unknown</tool_call>",
            "<tool_call>lookup<arg_key>query</arg_key><arg_value>one</arg_value><arg_key>query</arg_key><arg_value>two</arg_value></tool_call>",
            "<tool_call>lookup<arg_key>query</arg_key><arg_value>unterminated",
            '<tool_call>lookup<arg_key>query</arg_key><arg_value>42</arg_value></tool_call>',
        ]
        for text in malformed:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_tool_calls(text, [tool()])

    def test_argument_validation_does_not_inject_schema_defaults(self):
        arguments = {}
        validate_arguments({"type": "object", "properties": {"n": {"type": "integer", "default": 7}}}, arguments)
        self.assertEqual(arguments, {})


class ConfigurationTests(unittest.TestCase):
    def make(self, **model_changes):
        result = config.new_config("runtime")
        result["models"] = [{"id": "test/model", "path": "example.gguf", "mode": "cpu", **model_changes}]
        return result

    def test_known_modes_and_explicit_cpu_fallback(self):
        for mode in config.MODES:
            with self.subTest(mode=mode):
                self.assertEqual(config.validate(self.make(mode=mode, fallback_mode="cpu"))["models"][0]["mode"], mode)
        with self.assertRaises(ValueError):
            config.validate(self.make(mode="automatic_magic"))
        with self.assertRaises(ValueError):
            config.validate(self.make(fallback_mode="npu"))

    def test_context_batch_and_loopback_boundaries(self):
        for changes in [{"context": 0}, {"context": True}, {"microbatch": 1024, "batch": 512}, {"threads": 0}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config.validate(self.make(**changes))
        for changes in [{"host": "0.0.0.0"}, {"port": True}, {"port": 80}, {"api_key": "too-short"}]:
            value = self.make(); value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config.validate(value)

    def test_malformed_config_shapes_are_informative_errors(self):
        for value in [[], {**self.make(), "schema_version": True}, {**self.make(), "models": [None]}, self.make(id=123)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.validate(value)

    def test_duplicate_model_ids_and_non_gguf_paths_rejected(self):
        value = self.make(); value["models"].append(dict(value["models"][0]))
        with self.assertRaises(ValueError): config.validate(value)
        with self.assertRaises(ValueError): config.validate(self.make(path="not-a-model.bin"))

    def test_memory_and_power_controls_keep_their_types(self):
        for changes in [{"repack": "false"}, {"enabled": "true"}, {"allow_experimental_architecture": 1},
                {"power_mode": "overdrive"}, {"load_timeout_seconds": True}, {"gpu_layers": "99"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config.validate(self.make(**changes))
        value = config.validate(self.make(repack=False, power_mode="balanced"))
        self.assertIs(value["models"][0]["repack"], False)

    def test_owned_installation_cannot_contain_a_model_to_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory)/"install"; owned.mkdir()
            (owned/".osaurus-arm64-assist-install.json").write_text("{}", encoding="utf-8")
            value = self.make(path=str(owned/"weights.gguf")); value["_config_directory"] = str(owned)
            with self.assertRaises(ValueError): config.validate(value)
            value["models"][0]["path"] = str(Path(directory)/"models"/"weights.gguf")
            config.validate(value)

    def test_atomic_json_and_public_export_preserve_settings_without_secret(self):
        value = self.make(context=2048)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"配置"/"settings.json"
            config.atomic_json(path, value)
            loaded = config.load(path)
            expected = copy.deepcopy(value)
            expected["models"][0]["path"] = str((path.parent/"example.gguf").resolve())
            self.assertEqual({k:v for k,v in loaded.items() if not k.startswith("_")}, expected)
            self.assertEqual(loaded["_config_directory"], str(path.resolve().parent))
            self.assertFalse(path.with_name(path.name+".tmp").exists())
            self.assertNotIn("_config_directory", config.public_config(loaded))
        exported = config.public_config(value)
        self.assertNotIn("api_key", exported)
        self.assertNotIn(value["api_key"], json.dumps(exported))
        self.assertEqual(exported["models"], value["models"])


if __name__ == "__main__":
    unittest.main()
