"""Text-only OpenAI request normalization and embedded chat templates."""
import json
import math
import re
import time

import fastjsonschema
from jinja2.sandbox import SandboxedEnvironment

JSON_RULES = r'''
value ::= object | array | string | number | "true" ws | "false" ws | "null" ws
object ::= "{" ws (string ":" ws value ("," ws string ":" ws value)*)? "}" ws
array ::= "[" ws (value ("," ws value)*)? "]" ws
string ::= "\"" char* "\"" ws
char ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
number ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [+-]? [0-9]+)? ws
integer ::= "-"? ("0" | [1-9] [0-9]*) ws
boolean ::= ("true" | "false") ws
null ::= "null" ws
ws ::= [ \t\n\r]*
'''


def reject_external_refs(value):
    if isinstance(value, dict):
        if "$ref" in value and not str(value["$ref"]).startswith("#"):
            raise ValueError("Tool schemas may use local JSON references, not external references")
        for child in value.values():
            reject_external_refs(child)
    elif isinstance(value, list):
        for child in value:
            reject_external_refs(child)


def validate_arguments(schema, arguments):
    reject_external_refs(schema)
    fastjsonschema.compile(schema, use_default=False)(arguments)


def normalize_request(body):
    if not isinstance(body, dict):
        raise ValueError("Request must be a JSON object")
    if isinstance(body.get("n", 1), bool) or body.get("n", 1) != 1:
        raise ValueError("Only n=1 is supported")
    if body.get("logprobs") or body.get("top_logprobs"):
        raise ValueError("Log probabilities are not supported")
    if body.get("enable_thinking") is True:
        raise ValueError("This release uses thinking-disabled profiles")
    if not isinstance(body.get("stream", False), bool):
        raise ValueError("stream must be a boolean")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list")
    normalized = []
    for original in messages:
        if not isinstance(original, dict):
            raise ValueError("Each message must be an object")
        message = dict(original)
        role = message.get("role")
        if role == "developer":
            role = "system"
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"Unsupported message role: {role}")
        message["role"] = role
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(x, dict) or x.get("type") != "text" for x in content):
                raise ValueError("Only text content blocks are supported")
            if any(not isinstance(x.get("text", ""), str) for x in content):
                raise ValueError("Text-block text must be a string")
            content = "\n".join(x.get("text", "") for x in content)
        if content is not None and not isinstance(content, str):
            raise ValueError("Message content must be text")
        message["content"] = content or ""
        message.setdefault("reasoning_content", "")
        if not isinstance(message["reasoning_content"], str):
            raise ValueError("reasoning_content must be a string")
        if message.get("tool_calls"):
            if not isinstance(message["tool_calls"], list):
                raise ValueError("Assistant tool_calls must be a list")
            calls = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ValueError("Each assistant tool call needs a function object")
                call = dict(call); function = dict(call["function"])
                if isinstance(function.get("arguments"), str):
                    function["arguments"] = json.loads(function["arguments"])
                if not isinstance(function.get("arguments"), dict):
                    raise ValueError("Assistant tool arguments must be a JSON object")
                call["function"] = function; calls.append(call)
            message["tool_calls"] = calls
        else:
            message["tool_calls"] = None
        normalized.append(message)
    tools = body.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("tools must be a list of at most 128 functions")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
            raise ValueError("Each tool needs a function object")
        function = tool.get("function", {})
        name = function.get("name", "")
        if tool.get("type") != "function" or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", name):
            raise ValueError("Only named function tools are supported")
        if name in names:
            raise ValueError("Tool function names must be unique")
        names.add(name)
        parameters = function.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("Tool parameters must be an object schema")
        reject_external_refs(parameters)
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if not isinstance(choice.get("function"), dict):
            raise ValueError("tool_choice needs a function object")
        selected = choice.get("function", {}).get("name")
        if choice.get("type") != "function" or selected not in names:
            raise ValueError("tool_choice must select an advertised function")
    elif choice not in ("auto", "none", "required"):
        raise ValueError("Unknown tool_choice")
    if choice == "required" and not tools:
        raise ValueError("tool_choice=required needs tools")
    response_format = body.get("response_format", {"type": "text"})
    if response_format is None:
        response_format = {"type": "text"}
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be an object")
    response_format = {"type": "text", **response_format}
    if response_format.get("type") not in ("text", "json_object"):
        raise ValueError("Only text and json_object response formats are supported; JSON Schema output is not implemented")
    if response_format["type"] == "json_object" and tools:
        raise ValueError("Use either JSON object output or tools in one request")
    stop = body.get("stop", [])
    if stop is None:
        stop = []
    if isinstance(stop, str):
        stop = [stop]
    if not isinstance(stop, list) or len(stop) > 8 or any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in stop):
        raise ValueError("stop must contain at most eight short nonempty strings")
    if stop and (response_format["type"] == "json_object" or choice == "required" or isinstance(choice, dict)):
        raise ValueError("Stop strings cannot be combined with forced tool calls or JSON object output")
    maximum = body.get("max_completion_tokens", body.get("max_tokens", 256))
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 131072:
        raise ValueError("max_tokens must be a positive integer")
    temperature = body.get("temperature", 0.7)
    if isinstance(temperature, bool) or not isinstance(temperature, (float, int)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")
    top_p = body.get("top_p", 0.95)
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not math.isfinite(top_p) or not 0 < top_p <= 1:
        raise ValueError("top_p must be above 0 and at most 1")
    for name, low, high, default in (("presence_penalty", -2, 2, 0), ("frequency_penalty", -2, 2, 0), ("repeat_penalty", .01, 10, 1)):
        value = body.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} is outside its supported numeric range")
    for name, low, high, default in (("top_k", 0, 1000000, 40), ("seed", 0, 0xFFFFFFFF, 0xFFFFFFFF)):
        value = body.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    return {**body, "messages": normalized, "tools": tools, "tool_choice": choice,
            "response_format": response_format, "max_tokens": maximum, "temperature": temperature,
            "top_p": top_p, "stop": stop}


class ChatTemplate:
    def __init__(self, source, bos_token, eos_token):
        environment = SandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        environment.filters["tojson"] = lambda obj, **kwargs: json.dumps(obj, ensure_ascii=False, **kwargs)
        environment.globals["raise_exception"] = self.fail
        environment.globals["strftime_now"] = lambda fmt: time.strftime(fmt)
        self.template = environment.from_string(source)
        self.bos_token, self.eos_token = bos_token, eos_token

    @staticmethod
    def fail(message):
        raise ValueError(str(message))

    def render(self, request):
        messages = request["messages"]
        choice = request["tool_choice"]
        tools = request["tools"] if choice != "none" else []
        if isinstance(choice, dict) or choice == "required":
            messages = [dict(x) for x in messages]
            instruction = "Use a function tool for the next response."
            if isinstance(choice, dict):
                instruction += " Call " + choice["function"]["name"] + "."
            messages.insert(0, {"role": "system", "content": instruction})
        if request["response_format"]["type"] == "json_object":
            messages = [{"role": "system", "content": "Return one JSON object, without Markdown."}] + messages
        return self.template.render(messages=messages, tools=tools, add_generation_prompt=True,
                                    enable_thinking=False, bos_token=self.bos_token, eos_token=self.eos_token)


def resolve_local_schema(schema, root):
    seen = set()
    while "$ref" in schema:
        reference = schema["$ref"]
        if reference in seen or not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError("Forced tool schemas need non-cyclic local object references")
        seen.add(reference)
        target = root
        for part in reference[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or key not in target:
                raise ValueError("Unresolved local tool schema reference")
            target = target[key]
        if not isinstance(target, dict):
            raise ValueError("Referenced tool schema must be an object")
        schema = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
    return schema


def grammar_for(request, architecture):
    if request["response_format"]["type"] == "json_object":
        return "root ::= ws object\n" + JSON_RULES
    choice = request["tool_choice"]
    if not (choice == "required" or isinstance(choice, dict)):
        return None
    tools = request["tools"]
    if isinstance(choice, dict):
        tools = [x for x in tools if x["function"]["name"] == choice["function"]["name"]]
    if architecture not in ("spark2_5", "qwen35moe", "qwen35", "qwen3"):
        raise ValueError("Forced tools have only been implemented for Spark and Qwen templates")
    rules = []
    for index, tool in enumerate(tools):
        function = tool["function"]; root_schema = function.get("parameters", {})
        schema = resolve_local_schema(root_schema, root_schema)
        if schema.get("type", "object") != "object":
            raise ValueError("Function parameters must use an object schema")
        required = schema.get("required", [])
        props = schema.get("properties", {})
        if not isinstance(required, list) or not isinstance(props, dict) or any(not isinstance(v, dict) for v in props.values()):
            raise ValueError("Tool properties must be object schemas and required must be a list")
        if any(key not in props for key in required):
            raise ValueError("Required tool fields must be declared in properties")
        if architecture == "qwen3":
            # Qwen3's trained call is a JSON object, unlike Qwen3.5's XML
            # parameters. Constrain the wrapper/name here; validate its generic
            # JSON argument object against the advertised schema after decoding.
            parts = [json.dumps("<tool_call>"), "ws", json.dumps("{"), "ws",
                     json.dumps('"name"'), "ws", json.dumps(":"), "ws",
                     json.dumps(json.dumps(function["name"])), "ws", json.dumps(","), "ws",
                     json.dumps('"arguments"'), "ws", json.dumps(":"), "ws", "object",
                     json.dumps("}"), "ws", json.dumps("</tool_call>")]
            rules.append(f"call{index} ::= " + " ".join(parts))
            continue
        spark = architecture == "spark2_5"
        prefix = "<tool_call>" + (function["name"] if spark else "\n<function=" + function["name"] + ">\n")
        parts = [json.dumps(prefix)]
        for name, field in props.items():
            field = resolve_local_schema(field, root_schema)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name):
                raise ValueError("Forced tool argument names need letters, digits, dots or underscores")
            typ = field.get("type")
            typ = typ if typ in ("object", "array", "string", "integer", "number", "boolean", "null") else "value"
            value = "(" + " | ".join(json.dumps(json.dumps(x, ensure_ascii=False)) for x in field["enum"]) + ") ws" if field.get("enum") else typ
            begin = f"<arg_key>{name}</arg_key><arg_value>" if spark else f"<parameter={name}>\n"
            end = "</arg_value>" if spark else "\n</parameter>\n"
            segment = json.dumps(begin) + " " + value + " " + json.dumps(end)
            parts.append(segment if name in required else "(" + segment + ")?")
        parts.append(json.dumps("</tool_call>" if spark else "</function>\n</tool_call>"))
        rules.append(f"call{index} ::= " + " ".join(parts))
    return "root ::= " + " | ".join(f"call{i}" for i in range(len(rules))) + "\n" + "\n".join(rules) + "\n" + JSON_RULES


def parse_tool_calls(text, tools):
    """Decode Spark/Qwen3.5 XML-like and Qwen3 JSON calls; never execute them."""
    available = {x["function"]["name"]: x["function"] for x in tools}
    calls = []; cursor = 0; content = []
    decoder = json.JSONDecoder()
    while True:
        start = text.find("<tool_call>", cursor)
        if start < 0:
            content.append(text[cursor:]); break
        content.append(text[cursor:start]); position = start + len("<tool_call>")
        position += len(text[position:]) - len(text[position:].lstrip())
        if text.startswith("{", position):
            # raw_decode locates the actual end of nested JSON, so a quoted
            # </tool_call> or <parameter=...> is ordinary argument content.
            value, consumed = decoder.raw_decode(text[position:])
            if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
                raise ValueError("JSON tool call needs name and arguments fields")
            name, arguments = value["name"], value["arguments"]
            if not isinstance(name, str) or name not in available:
                raise ValueError("Model selected an unadvertised tool")
            if not isinstance(arguments, dict):
                raise ValueError("Model tool arguments must be a JSON object")
            position += consumed
            position += len(text[position:]) - len(text[position:].lstrip())
            if not text.startswith("</tool_call>", position):
                raise ValueError("Unclosed JSON tool call")
            position += len("</tool_call>")
            validate_arguments(available[name].get("parameters", {"type": "object"}), arguments)
            calls.append({"name": name, "arguments": arguments}); cursor = position
            continue
        qwen = text.startswith("<function=", position)
        if qwen:
            match = re.match(r"<function=([A-Za-z_][A-Za-z0-9_.-]*)>", text[position:])
        else:
            match = re.match(r"([A-Za-z_][A-Za-z0-9_.-]*)", text[position:])
        if not match: raise ValueError("Malformed model tool call")
        name = match.group(1); position += match.end(); arguments = {}
        if name not in available: raise ValueError("Model selected an unadvertised tool")
        while True:
            position += len(text[position:]) - len(text[position:].lstrip())
            closing = "</function>" if qwen else "</tool_call>"
            if text.startswith(closing, position):
                position += len(closing)
                if qwen:
                    position += len(text[position:]) - len(text[position:].lstrip())
                    if not text.startswith("</tool_call>", position): raise ValueError("Unclosed tool call")
                    position += len("</tool_call>")
                break
            pattern = r"<parameter=([^>]+)>" if qwen else r"<arg_key>([^<]+)</arg_key><arg_value>"
            match = re.match(pattern, text[position:])
            if not match: raise ValueError("Malformed model tool argument")
            key = match.group(1); position += match.end()
            position += len(text[position:]) - len(text[position:].lstrip())
            ending = "</parameter>" if qwen else "</arg_value>"
            try:
                value, consumed = decoder.raw_decode(text[position:])
                end = position + consumed
                end += len(text[end:]) - len(text[end:].lstrip())
                if not text.startswith(ending, end): raise ValueError("Not a JSON-delimited argument")
            except (ValueError, json.JSONDecodeError):
                end = text.find(ending, position)
                if end < 0: raise ValueError("Unclosed model tool argument")
                value = text[position:end].strip()
            if key in arguments: raise ValueError("Duplicate model tool argument")
            arguments[key] = value; position = end + len(ending)
        validate_arguments(available[name].get("parameters", {"type": "object"}), arguments)
        calls.append({"name": name, "arguments": arguments}); cursor = position
    return "".join(content).strip(), calls
