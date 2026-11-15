"""Exact llama.cpp C ABI for the pinned GenieX 0.11.0 native release.

Definitions follow include/llama.h at 93cf736a8d1d46aa922e27eb64aa712bcd5ebc30.
The native libraries are loaded only in isolated model workers.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sysconfig
import time

P, I, U, B, F = C.c_void_p, C.c_int32, C.c_uint32, C.c_bool, C.c_float


class TensorOverride(C.Structure):
    _fields_ = [("pattern", C.c_char_p), ("buft", P)]


class ModelParams(C.Structure):
    _fields_ = [("devices", C.POINTER(P)), ("tensor_buft_overrides", C.POINTER(TensorOverride)),
        ("n_gpu_layers", I), ("split_mode", I), ("load_mode", I), ("lazy_mode", I), ("main_gpu", I),
        ("tensor_split", C.POINTER(F)), ("progress_callback", P), ("progress_callback_user_data", P),
        ("kv_overrides", P)] + [(x, B) for x in
        ["vocab_only", "check_tensors", "use_extra_bufts", "no_host", "no_alloc", "load_mtp"]]


class ContextParams(C.Structure):
    _fields_ = [(x, U) for x in ["n_ctx", "n_batch", "n_ubatch", "n_seq_max", "n_rs_seq", "n_outputs_max", "n_outputs_max_per_seq"]] + [
        (x, I) for x in ["n_threads", "n_threads_batch", "ctx_type", "rope_scaling_type", "pooling_type", "attention_type", "flash_attn_type"]] + [
        (x, F) for x in ["rope_freq_base", "rope_freq_scale", "yarn_ext_factor", "yarn_attn_factor", "yarn_beta_fast", "yarn_beta_slow"]] + [
        ("yarn_orig_ctx", U), ("defrag_thold", F), ("cb_eval", P), ("cb_eval_user_data", P), ("type_k", I), ("type_v", I),
        ("moe_cache_size", C.c_size_t), ("moe_cache_cpu_share", F), ("abort_callback", P), ("abort_callback_data", P)] + [
        (x, B) for x in ["embeddings", "offload_kqv", "no_perf", "op_offload", "swa_full", "kv_unified"]] + [
        ("samplers", P), ("n_samplers", C.c_size_t), ("ctx_other", P)]


class Batch(C.Structure):
    _fields_ = [("n_tokens", I), ("token", C.POINTER(I)), ("embd", C.POINTER(F)), ("pos", C.POINTER(I)),
               ("n_seq_id", C.POINTER(I)), ("seq_id", C.POINTER(C.POINTER(I))), ("logits", C.POINTER(C.c_int8))]


class Perf(C.Structure):
    _fields_ = [(x, C.c_double) for x in ["t_start_ms", "t_load_ms", "t_p_eval_ms", "t_eval_ms"]] + [
        (x, I) for x in ["n_p_eval", "n_eval", "n_reused", "n_reallocs_same", "n_reallocs_diff"]]


class ChainParams(C.Structure):
    _fields_ = [("no_perf", B)]


def runtime_manifest():
    return json.loads(Path(__file__).with_name("native.lock.json").read_text(encoding="utf-8"))


def verify_runtime(directory):
    directory = Path(directory)
    manifest = runtime_manifest()
    checks = []
    for filename, expected in manifest["files"].items():
        path = directory / filename
        digest = None
        if path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        checks.append({"file": filename, "exists": path.is_file(), "sha256_matches": digest == expected})
    extras = sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix.lower() in
                    (".dll", ".so", ".cat", ".inf") and p.name not in manifest["files"]) if directory.is_dir() else []
    return {"core": manifest["llama_cpp_commit"], "files": checks, "unexpected_native_files": extras,
            "verified": all(x["sha256_matches"] for x in checks) and not extras}


def native_platform():
    return os.name == "nt" and sysconfig.get_platform().lower().replace("-", "_") == "win_arm64"


class Api:
    def __init__(self, directory, power="balanced", experts=False, gpu=False):
        if not native_platform():
            raise RuntimeError("Use native ARM64 CPython on Windows; x64 emulated Python cannot load this runtime")
        if not verify_runtime(directory)["verified"]:
            raise RuntimeError("Native runtime hash mismatch; reinstall the pinned complete runtime instead of mixing DLLs")
        self.directory = Path(directory)
        os.environ["ADSP_LIBRARY_PATH"] = str(self.directory)
        os.environ["GGML_HEXAGON_HOSTBUF"] = "0"
        os.environ["GGML_HEXAGON_POWER_MODE"] = power
        for name in ("GGML_HEXAGON_OPFILTER", "GGML_OPENCL_OPFILTER"):
            os.environ.pop(name, None)
        if experts:
            os.environ["GGML_OPENCL_OPFILTER" if gpu else "GGML_HEXAGON_OPFILTER"] = "MUL_MAT_ID"
        self.dll_dirs = [os.add_dll_directory(str(self.directory))]
        self.llama = C.CDLL(str(self.directory / "llama.dll"))
        self.ggml = C.CDLL(str(self.directory / "ggml.dll"))
        self.base = C.CDLL(str(self.directory / "ggml-base.dll"))
        self.cpu = C.CDLL(str(self.directory / "ggml-cpu.dll"))
        for library, name, result, parameters in [
            (self.llama, "llama_model_default_params", ModelParams, []),
            (self.llama, "llama_context_default_params", ContextParams, []),
            (self.llama, "llama_backend_init", None, []), (self.llama, "llama_backend_free", None, []),
            (self.ggml, "ggml_backend_load_all_from_path", None, [C.c_char_p]),
            (self.ggml, "ggml_backend_dev_by_name", P, [C.c_char_p]),
            (self.base, "ggml_backend_cpu_buffer_type", P, []),
            (self.llama, "llama_model_load_from_file", P, [C.c_char_p, ModelParams]),
            (self.llama, "llama_model_free", None, [P]),
            (self.llama, "llama_init_from_model", P, [P, ContextParams]),
            (self.llama, "llama_free", None, [P]), (self.llama, "llama_n_ctx", U, [P]),
            (self.llama, "llama_model_get_vocab", P, [P]),
            (self.llama, "llama_vocab_n_tokens", I, [P]),
            (self.llama, "llama_model_chat_template", C.c_char_p, [P, C.c_char_p]),
            (self.llama, "llama_model_meta_val_str", I, [P, C.c_char_p, P, C.c_size_t]),
            (self.llama, "llama_vocab_bos", I, [P]), (self.llama, "llama_vocab_eos", I, [P]),
            (self.llama, "llama_tokenize", I, [P, C.c_char_p, I, C.POINTER(I), I, B, B]),
            (self.llama, "llama_token_to_piece", I, [P, I, P, I, I, B]),
            (self.llama, "llama_vocab_is_eog", B, [P, I]),
            (self.llama, "llama_get_memory", P, [P]),
            (self.llama, "llama_memory_clear", None, [P, B]),
            (self.llama, "llama_batch_get_one", Batch, [C.POINTER(I), I]),
            (self.llama, "llama_decode", I, [P, Batch]),
            (self.llama, "llama_synchronize", None, [P]),
            (self.llama, "llama_perf_context", Perf, [P]),
            (self.llama, "llama_perf_context_reset", None, [P]),
            (self.llama, "llama_sampler_chain_default_params", ChainParams, []),
            (self.llama, "llama_sampler_chain_init", P, [ChainParams]),
            (self.llama, "llama_sampler_chain_add", None, [P, P]),
            (self.llama, "llama_sampler_init_greedy", P, []),
            (self.llama, "llama_sampler_init_dist", P, [U]),
            (self.llama, "llama_sampler_init_top_k", P, [I]),
            (self.llama, "llama_sampler_init_top_p", P, [F, C.c_size_t]),
            (self.llama, "llama_sampler_init_min_p", P, [F, C.c_size_t]),
            (self.llama, "llama_sampler_init_temp", P, [F]),
            (self.llama, "llama_sampler_init_penalties", P, [I, F, F, F]),
            (self.llama, "llama_sampler_init_grammar", P, [P, C.c_char_p, C.c_char_p]),
            (self.llama, "llama_sampler_accept", None, [P, I]),
            (self.llama, "llama_sampler_sample", I, [P, P, I]),
            (self.llama, "llama_sampler_free", None, [P]),
            (self.llama, "llama_state_get_size", C.c_size_t, [P]),
            (self.llama, "llama_state_get_data", C.c_size_t, [P, P, C.c_size_t]),
            (self.llama, "llama_state_set_data", C.c_size_t, [P, P, C.c_size_t]),
        ]:
            function = getattr(library, name)
            function.restype, function.argtypes = result, parameters
            setattr(self, name, function)
        params, context = self.llama_model_default_params(), self.llama_context_default_params()
        if C.sizeof(ModelParams) != 80 or C.sizeof(ContextParams) != 176 or context.type_k != 1:
            raise RuntimeError("Unexpected pinned native ABI defaults")
        self.llama_backend_init()
        self.llama_backend_free_on_close = True
        self.ggml_backend_load_all_from_path(str(self.directory).encode("utf-8"))

    def metadata(self, model, key):
        buffer = C.create_string_buffer(65536)
        count = self.llama_model_meta_val_str(model, key.encode(), buffer, len(buffer))
        return buffer.value.decode("utf-8") if 0 <= count < len(buffer) else ""

    def piece(self, vocab, token, special=False):
        size = 256
        while size <= 65536:
            buffer = C.create_string_buffer(size)
            count = self.llama_token_to_piece(vocab, token, buffer, size, 0, special)
            if count >= 0:
                return buffer.raw[:count]
            size = -count + 1
        raise ValueError("Unreasonably large token text")

    def tokenize(self, vocab, text):
        data = text.encode("utf-8")
        count = self.llama_tokenize(vocab, data, len(data), None, 0, False, True)
        count = -count if count < 0 else count
        array = (I * max(count, 1))()
        actual = self.llama_tokenize(vocab, data, len(data), array, count, False, True)
        if actual < 0:
            raise ValueError("Unable to tokenize the formatted prompt")
        return list(array[:actual])

    def decode(self, context, ids):
        array = (I * len(ids))(*ids)
        code = self.llama_decode(context, self.llama_batch_get_one(array, len(ids)))
        if code:
            raise RuntimeError(f"Native decode failed with code {code}")
        self.llama_synchronize(context)

    def close(self):
        if getattr(self, "llama_backend_free_on_close", False):
            self.llama_backend_free()
            self.llama_backend_free_on_close = False
        for handle in self.dll_dirs:
            handle.close()


class Context:
    def __init__(self, api, path, profile, device="CPU", experts=False, vocab_only=False):
        self.api, self.model, self.context = api, None, None
        params = api.llama_model_default_params()
        params.load_mode = 1
        params.use_extra_bufts = bool(profile.get("repack", True))
        params.no_host, params.vocab_only = True, vocab_only
        params.n_gpu_layers = 0 if device == "CPU" else profile.get("gpu_layers", -1)
        native_device = api.ggml_backend_dev_by_name(device.encode())
        if not native_device:
            raise RuntimeError(f"Backend {device} unavailable; use doctor and the vendor-supported driver path")
        self.devices = (P * 2)(native_device, None)
        params.devices = self.devices
        self.overrides = (TensorOverride * 2)(
            TensorOverride(b"ffn_(gate|up|down)_exps", api.ggml_backend_cpu_buffer_type()), TensorOverride(None, None))
        if experts:
            params.tensor_buft_overrides = self.overrides
        started = time.perf_counter()
        self.model = api.llama_model_load_from_file(str(path).encode("utf-8"), params)
        self.timings = {"model_seconds": time.perf_counter() - started, "context_seconds": 0.0}
        if not self.model:
            raise RuntimeError(f"Model loading failed for {device}; see the local worker log")
        if vocab_only:
            return
        try:
            context = api.llama_context_default_params()
            context.n_ctx, context.n_batch, context.n_ubatch = profile.get("context", 2048), profile.get("batch", 512), profile.get("microbatch", 512)
            context.n_seq_max, context.n_outputs_max, context.n_outputs_max_per_seq = 1, 1, 1
            context.n_threads, context.n_threads_batch = profile.get("threads", 6), profile.get("batch_threads", 10)
            context.flash_attn_type = int(profile.get("flash_attention", device != "GPUOpenCL"))
            context.no_perf = False
            context.swa_full, context.kv_unified = profile.get("swa_full", False), profile.get("kv_unified", False)
            context.op_offload = device != "CPU" and profile.get("op_offload", True)
            context.offload_kqv = device != "CPU" and profile.get("kv_offload", True)
            started = time.perf_counter()
            self.context = api.llama_init_from_model(self.model, context)
            self.timings["context_seconds"] = time.perf_counter() - started
            if not self.context:
                raise RuntimeError("Native context initialization failed")
        except BaseException:
            self.close()
            raise

    @property
    def vocab(self):
        return self.api.llama_model_get_vocab(self.model)

    def close(self):
        if self.context:
            self.api.llama_free(self.context); self.context = None
        if self.model:
            self.api.llama_model_free(self.model); self.model = None
