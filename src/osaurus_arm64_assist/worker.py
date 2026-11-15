"""A disposable native worker, with an IPC channel separate from native logs."""
import os
from pathlib import Path
import sys
import threading
import time


def watch_parent(parent_pid):
    if os.name != "nt":
        return
    import ctypes as C
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = C.c_void_p
    kernel.OpenProcess.argtypes = [C.c_ulong, C.c_bool, C.c_ulong]
    kernel.WaitForSingleObject.argtypes = [C.c_void_p, C.c_ulong]
    handle = kernel.OpenProcess(0x100000, False, parent_pid)
    if not handle:
        os._exit(1)
    while True:
        if kernel.WaitForSingleObject(handle, 1000) == 0:
            os._exit(1)


def entry(connection, runtime_dir, profile, log_path, parent_pid):
    # C libraries may write directly to OS descriptors, so stdout is never the IPC transport.
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        os.dup2(log.fileno(), 1); os.dup2(log.fileno(), 2)
        sys.stdout = sys.stderr = log
        threading.Thread(target=watch_parent, args=(parent_pid,), daemon=True).start()
        from .engine import Engine
        from .protocol import parse_tool_calls
        import json
        engine = None
        try:
            engine = Engine(runtime_dir, profile)
            connection.send({"type": "ready", "mode": profile["mode"], "load_seconds": engine.load_seconds,
                             "load_stages": engine.load_stages, "architecture": engine.architecture,
                             "context_length": engine.context_limit})
            while True:
                command = connection.recv()
                if command.get("type") == "unload":
                    break
                if command.get("type") not in ("generate", "benchmark"):
                    continue
                request = command["request"]
                try:
                    finished = None
                    benchmark = command["type"] == "benchmark"
                    events = engine.generate(request, token_ids=command["token_ids"], ignore_eos=True) if benchmark else engine.generate(request)
                    for event in events:
                        if event["type"] == "done":
                            finished = event
                        else:
                            connection.send(event)
                    if finished is None:
                        raise RuntimeError("Native generation did not finish")
                    if benchmark:
                        connection.send(finished)
                        continue
                    started = time.perf_counter()
                    message = {"role": "assistant", "content": finished["text"]}
                    if request["response_format"]["type"] == "json_object":
                        parsed = json.loads(finished["text"])
                        if not isinstance(parsed, dict):
                            raise ValueError("Model did not complete a JSON object")
                    if request["tools"] and request["tool_choice"] != "none":
                        content, calls = parse_tool_calls(finished["text"], request["tools"])
                        if calls:
                            import uuid
                            message["content"] = content or None
                            message["tool_calls"] = [{"id": "call_"+uuid.uuid4().hex[:20], "type": "function",
                                "function": {"name": x["name"], "arguments": json.dumps(x["arguments"], ensure_ascii=False)}} for x in calls]
                            finished["finish_reason"] = "tool_calls"
                        elif request["tool_choice"] == "required" or isinstance(request["tool_choice"], dict):
                            raise ValueError("The model did not complete its required tool call")
                    finished["message"] = message
                    finished["metrics"]["postprocess_seconds"] = time.perf_counter() - started
                    connection.send(finished)
                except (ValueError, TypeError, KeyError) as error:
                    connection.send({"type": "error", "kind": "invalid_request_or_model_output", "message": str(error)})
                except Exception as error:
                    connection.send({"type": "error", "kind": "native_inference_failed", "message": str(error)})
                    break
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            try:
                connection.send({"type": "error", "kind": "model_load_failed", "message": str(error)})
            except (EOFError, BrokenPipeError, OSError):
                pass
        finally:
            if engine:
                engine.close()
            connection.close()
