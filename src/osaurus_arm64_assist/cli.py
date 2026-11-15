"""Install-local configuration and diagnostics; model files stay with Osaurus."""
import argparse
import json
import os
from pathlib import Path
import sys
import sysconfig
import urllib.error
import urllib.request

from . import __version__
from . import config as settings


def output(value, path=None):
    text = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False)
    if path:
        Path(path).write_text(text+"\n", encoding="utf-8")
    print(text)


def api(config, path, body=None):
    url = f"http://127.0.0.1:{config.get('port',8082)}" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data, {"Authorization": "Bearer "+config["api_key"], "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=360) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", {}).get("message", str(error))
        except (ValueError, AttributeError):
            message = str(error)
        raise ValueError(message) from error


def discover(directory=None):
    directory = Path(directory) if directory else Path(os.environ.get("LOCALAPPDATA", str(Path.home())))/"osaurus/models"
    result = []
    if directory.is_dir():
        for current, folders, files in os.walk(directory, followlinks=False):
            folders[:] = [name for name in folders if not (Path(current)/name).is_symlink()
                          and not getattr(Path(current)/name, "is_junction", lambda: False)()]
            for name in files:
                path = Path(current)/name
                if path.suffix.lower() == ".gguf":
                    result.append({"osaurus_model": path.stem, "path": str(path.resolve()), "bytes": path.stat().st_size})
                if len(result) >= 4096:
                    raise ValueError("Model discovery exceeded 4096 files; use a narrower --models-dir")
    return sorted(result, key=lambda value: (value["osaurus_model"].lower(), value["path"].lower()))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="osaurus-arm64-assist")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--config", type=Path, required=True); init.add_argument("--runtime-dir", required=True)
    serve = sub.add_parser("serve"); serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host"); serve.add_argument("--port", type=int); serve.add_argument("--instance-id")
    doctor = sub.add_parser("doctor"); doctor.add_argument("--config", type=Path, required=True); doctor.add_argument("--json", action="store_true")
    models = sub.add_parser("models"); model_sub = models.add_subparsers(dest="operation", required=True)
    scan = model_sub.add_parser("discover"); scan.add_argument("--config", type=Path); scan.add_argument("--models-dir", type=Path)
    listing = model_sub.add_parser("list"); listing.add_argument("--config", type=Path, required=True)
    add = model_sub.add_parser("add"); add.add_argument("--config", type=Path, required=True)
    add.add_argument("--id", required=True); add.add_argument("--mode", choices=settings.MODES, required=True)
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--path", type=Path); source.add_argument("--osaurus-model")
    add.add_argument("--models-dir", type=Path); add.add_argument("--context", type=int, default=2048)
    add.add_argument("--threads", type=int, default=6); add.add_argument("--batch-threads", type=int, default=10)
    add.add_argument("--batch", type=int, default=512); add.add_argument("--microbatch", type=int, default=512)
    add.add_argument("--power-mode", default="balanced"); add.add_argument("--replace", action="store_true")
    add.add_argument("--repack", action=argparse.BooleanOptionalAction, default=None)
    add.add_argument("--allow-experimental-architecture", action="store_true")
    add.add_argument("--fallback-mode", choices=["cpu"])
    remove = model_sub.add_parser("remove"); remove.add_argument("--config", type=Path, required=True); remove.add_argument("--id", required=True)
    config = sub.add_parser("config"); config_sub = config.add_subparsers(dest="operation", required=True)
    export = config_sub.add_parser("export"); export.add_argument("--config", type=Path, required=True); export.add_argument("--output", type=Path, required=True)
    importing = config_sub.add_parser("import"); importing.add_argument("--config", type=Path, required=True); importing.add_argument("--input", type=Path, required=True)
    smoke = sub.add_parser("self-test"); smoke.add_argument("--config", type=Path, required=True); smoke.add_argument("--model", required=True)
    smoke.add_argument("--output", type=Path); smoke.add_argument("--tools", action="store_true"); smoke.add_argument("--json-object", action="store_true")
    unload = sub.add_parser("unload"); unload.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if getattr(args,"output",None) and getattr(args,"config",None) and args.output.resolve()==args.config.resolve():
            raise ValueError("Output must not overwrite the live configuration")
        if args.command == "init":
            if args.config.exists():
                raise ValueError("Configuration already exists; init will not overwrite its key or model profiles")
            settings.atomic_json(args.config, settings.new_config(args.runtime_dir))
            output({"initialized": True, "config": str(args.config.resolve()), "models": 0}); return
        if args.command == "models" and args.operation == "discover":
            output(discover(args.models_dir)); return
        config = settings.load(args.config)
        if args.command == "serve":
            from .server import serve
            serve(args.config, args.host, args.port, args.instance_id)
        elif args.command == "doctor":
            from .native import native_platform, verify_runtime
            from .lifecycle import memory_status
            runtime = verify_runtime(config["runtime_dir"])
            report = {"version": __version__, "python_platform": sysconfig.get_platform(),
                "native_arm64_python": native_platform(), "runtime_integrity": runtime,
                "models": [{"id": model["id"], "mode": model["mode"], "exists": Path(model["path"]).is_file(),
                            "context_length": model.get("context",2048)} for model in config["models"]],
                "memory": memory_status(), "execution_verified": False,
                "note": "File integrity and Python architecture are static checks. Run an explicit model self-test to establish backend execution; use scripts/Doctor.ps1 for signed DSP and OEM driver inventory."}
            output(report)
            if not native_platform() or not runtime["verified"]:
                raise SystemExit(1)
        elif args.command == "models":
            if args.operation == "list":
                output([{**model, "exists": Path(model["path"]).is_file()} for model in config["models"]]); return
            if args.operation == "add":
                path = args.path
                if args.osaurus_model:
                    matches = [x for x in discover(args.models_dir) if x["osaurus_model"].casefold() == args.osaurus_model.casefold()]
                    if len(matches) != 1:
                        raise ValueError("Osaurus model selection must have exactly one matching filename stem; use discover or an explicit --path")
                    path = Path(matches[0]["path"])
                path = path.resolve()
                if not path.is_file():
                    raise ValueError("Selected GGUF file does not exist")
                existing = [x for x in config["models"] if x["id"] == args.id]
                if existing and not args.replace:
                    raise ValueError("That profile exists; use --replace to update only that profile")
                repack = args.repack if args.repack is not None else path.stat().st_size <= 8*1024**3 and "experts" not in args.mode
                profile = {"id": args.id, "path": str(path), "mode": args.mode, "context": args.context,
                    "threads": args.threads, "batch_threads": args.batch_threads, "batch": args.batch,
                    "microbatch": args.microbatch, "power_mode": args.power_mode, "repack": repack,
                    "allow_experimental_architecture": args.allow_experimental_architecture}
                if args.fallback_mode:
                    profile["fallback_mode"] = args.fallback_mode
                config["models"] = [x for x in config["models"] if x["id"] != args.id]+[profile]
                settings.validate(config); config.pop("_config_directory",None)
                settings.atomic_json(args.config, config)
                output({"registered": profile, "copied_model": False, "execution_verified": False})
            else:
                if not any(x["id"] == args.id for x in config["models"]):
                    raise ValueError("Unknown profile")
                config["models"] = [x for x in config["models"] if x["id"] != args.id]
                config.pop("_config_directory",None); settings.atomic_json(args.config,config)
                output({"removed_profile":args.id,"deleted_model":False})
        elif args.command == "config":
            if args.operation == "export":
                settings.atomic_json(args.output,settings.public_config(config)); output({"exported":str(args.output.resolve()),"contains_api_key":False})
            else:
                backup=json.loads(args.input.read_text(encoding="utf-8-sig"))
                if not isinstance(backup,dict): raise ValueError("Backup must be an object")
                for name in ("models","idle_seconds","memory_floor_mib","port","origins"):
                    if name in backup: config[name]=backup[name]
                settings.validate(config); config.pop("_config_directory",None)
                settings.atomic_json(args.config,config)
                output({"imported":True,"new_installation_key_and_runtime_preserved":True,"restart_if_running":True})
        elif args.command == "unload":
            output(api(config,"/admin/unload",{}))
        elif args.command == "self-test":
            profile=next((x for x in config["models"] if x["id"]==args.model),None)
            if not profile: raise ValueError("Unknown profile")
            request={"model":args.model,"messages":[{"role":"user","content":"Reply with only the answer: what is 17 + 25?"}],"temperature":0,"max_tokens":32}
            response=api(config,"/v1/chat/completions",request)
            answer=response["choices"][0]["message"]["content"]
            report={"model":args.model,"requested_mode":profile["mode"],"answer":answer,
                "arithmetic_correct":str(answer).strip() in ("42","42.","17 + 25 = 42.","17 + 25 = 42"),
                "assist":response.get("assist"),"usage":response["usage"]}
            if args.json_object:
                request.update(messages=[{"role":"user","content":"Return a JSON object with an integer field answer equal to 42."}],response_format={"type":"json_object"},max_tokens=64)
                parsed=json.loads(api(config,"/v1/chat/completions",request)["choices"][0]["message"]["content"])
                report["json_object_correct"]=parsed.get("answer")==42
                request.pop("response_format")
            if args.tools:
                request.update(messages=[{"role":"user","content":"Use add_numbers to add 17 and 25."}],max_tokens=128,
                    tools=[{"type":"function","function":{"name":"add_numbers","description":"Add two integers.","parameters":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},"required":["a","b"],"additionalProperties":False}}}],
                    tool_choice={"type":"function","function":{"name":"add_numbers"}})
                calls=api(config,"/v1/chat/completions",request)["choices"][0]["message"].get("tool_calls",[])
                report["tool_arguments"]=json.loads(calls[0]["function"]["arguments"]) if calls else None
                report["tool_correct"]=report["tool_arguments"] in ({"a":17,"b":25},{"a":25,"b":17})
            output(report,args.output)
            if not report["arithmetic_correct"] or report.get("json_object_correct") is False or report.get("tool_correct") is False:
                raise SystemExit(1)
    except (ValueError,OSError,json.JSONDecodeError) as error:
        print(json.dumps({"error":str(error)},ensure_ascii=True),file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
