"""Persistent native contexts and a checked, one-time inference-state handoff."""
import codecs
import ctypes as C
import json
import time

from .native import Api, Context
from .protocol import ChatTemplate, grammar_for


class StopBuffer:
    def __init__(self, stops):
        self.stops, self.buffer, self.stopped = stops, "", False

    def push(self, text, final=False):
        self.buffer += text
        matches = [self.buffer.find(stop) for stop in self.stops if stop in self.buffer]
        if matches:
            result = self.buffer[:min(matches)]
            self.buffer, self.stopped = "", True
            return result
        keep = max((len(x)-1 for x in self.stops), default=0)
        if final or keep == 0:
            result, self.buffer = self.buffer, ""
        elif len(self.buffer) > keep:
            result, self.buffer = self.buffer[:-keep], self.buffer[-keep:]
        else:
            result = ""
        return result


class Engine:
    def __init__(self, runtime_dir, profile):
        self.profile = dict(profile)
        self.api = self.primary = self.secondary = None
        started = time.perf_counter()
        self.load_stages = {}
        try:
            mode = profile["mode"]
            gpu, experts = mode.startswith("gpu"), mode in ("cpu-experts", "gpu-experts")
            before = time.perf_counter()
            self.api = Api(runtime_dir, profile.get("power_mode", "balanced"), experts=experts, gpu=gpu)
            self.load_stages["runtime_verify_and_backend_seconds"] = time.perf_counter() - before
            before = time.perf_counter()
            probe = Context(self.api, profile["path"], {**profile, "repack": False}, vocab_only=True)
            try:
                self.architecture = self.api.metadata(probe.model, "general.architecture")
                source = self.api.llama_model_chat_template(probe.model, None)
                if not source:
                    raise ValueError("The GGUF must contain a chat template; no guessed template is used")
                source = source.decode("utf-8")
                bos_id, eos_id = self.api.llama_vocab_bos(probe.vocab), self.api.llama_vocab_eos(probe.vocab)
                bos = self.api.piece(probe.vocab, bos_id, True).decode("utf-8") if bos_id >= 0 else ""
                eos = self.api.piece(probe.vocab, eos_id, True).decode("utf-8") if eos_id >= 0 else ""
                self.template = ChatTemplate(source, bos, eos)
            finally:
                probe.close()
            self.load_stages["metadata_and_template_seconds"] = time.perf_counter() - before
            if mode == "hybrid" and self.architecture != "spark2_5" and not profile.get("allow_experimental_architecture"):
                raise ValueError("State handoff was validated for spark2_5 (Raptor). Explicit experimental-architecture opt-in is required for another architecture")
            if experts and self.architecture != "qwen35moe" and not profile.get("allow_experimental_architecture"):
                raise ValueError("CPU-expert assistance was validated for qwen35moe (Ornith). Explicit experimental-architecture opt-in is required for another architecture")
            device = "CPU" if mode == "cpu" else "GPUOpenCL" if gpu else "HTP0"
            self.primary = Context(self.api, profile["path"], profile, device, experts)
            self.load_stages["primary"] = self.primary.timings
            if mode == "hybrid":
                self.secondary = Context(self.api, profile["path"], profile, "CPU")
                self.load_stages["secondary_cpu"] = self.secondary.timings
            self.context_limit = min(profile.get("context", 2048), self.api.llama_n_ctx(self.primary.context))
            self.load_seconds = time.perf_counter() - started
        except BaseException:
            self.close()
            raise

    def sampler(self, request, ids):
        api, vocab = self.api, self.primary.vocab
        chain = api.llama_sampler_chain_init(api.llama_sampler_chain_default_params())
        try:
            repetition = request.get("repeat_penalty", 1.0)
            frequency, presence = request.get("frequency_penalty", 0.0), request.get("presence_penalty", 0.0)
            if repetition != 1.0 or frequency or presence:
                window = request.get("repeat_last_n", 64)
                penalty = api.llama_sampler_init_penalties(self.context_limit if window == -1 else window, repetition, frequency, presence)
                if not penalty:
                    raise RuntimeError("Unable to initialize repetition penalties")
                for token in ids:
                    api.llama_sampler_accept(penalty, token)
                api.llama_sampler_chain_add(chain, penalty)
            grammar = grammar_for(request, self.architecture)
            if grammar:
                constrained = api.llama_sampler_init_grammar(vocab, grammar.encode("utf-8"), b"root")
                if not constrained:
                    raise ValueError("Unable to compile the requested output grammar")
                api.llama_sampler_chain_add(chain, constrained)
            if request["temperature"] == 0:
                api.llama_sampler_chain_add(chain, api.llama_sampler_init_greedy())
            else:
                api.llama_sampler_chain_add(chain, api.llama_sampler_init_top_k(int(request.get("top_k", 40))))
                api.llama_sampler_chain_add(chain, api.llama_sampler_init_top_p(request["top_p"], 1))
                if request.get("min_p", 0):
                    api.llama_sampler_chain_add(chain, api.llama_sampler_init_min_p(request["min_p"], 1))
                api.llama_sampler_chain_add(chain, api.llama_sampler_init_temp(request["temperature"]))
                api.llama_sampler_chain_add(chain, api.llama_sampler_init_dist(int(request.get("seed", 0xFFFFFFFF))))
            return chain
        except BaseException:
            api.llama_sampler_free(chain)
            raise

    def generate(self, request, token_ids=None, ignore_eos=False):
        api, primary = self.api, self.primary
        started = time.perf_counter()
        if token_ids is None:
            before = time.perf_counter()
            formatted = self.template.render({**request, "chat_template_kwargs": {
                **self.profile.get("template_kwargs", {}), **request.get("chat_template_kwargs", {})}})
            format_seconds = time.perf_counter() - before
            if len(formatted) > 4 * 1024 * 1024:
                raise ValueError("Formatted prompt is too large")
            before = time.perf_counter(); ids = api.tokenize(primary.vocab, formatted)
            tokenize_seconds = time.perf_counter() - before
        else:
            if not isinstance(token_ids, list) or len(token_ids) > self.context_limit:
                raise ValueError("Direct token IDs must be a bounded list")
            vocab_size = api.llama_vocab_n_tokens(primary.vocab)
            if any(isinstance(token, bool) or not isinstance(token, int) or not 0 <= token < vocab_size for token in token_ids):
                raise ValueError("Direct token ID is outside this model's vocabulary")
            ids = list(token_ids)
            format_seconds = tokenize_seconds = 0.0
        if not ids or len(ids) + request["max_tokens"] > self.context_limit:
            raise ValueError(f"Prompt ({len(ids)} tokens) plus requested output ({request['max_tokens']}) exceeds the configured {self.context_limit}-token context")
        before = time.perf_counter()
        for item in (self.primary, self.secondary):
            if item:
                api.llama_memory_clear(api.llama_get_memory(item.context), True)
                api.llama_perf_context_reset(item.context)
        reset_seconds = time.perf_counter() - before
        sampler = self.sampler(request, ids)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stop_buffer = StopBuffer(request["stop"])
        active = primary.context
        first, sampled, transfer_seconds, state_bytes, decoding_seconds, sampling_seconds = None, 0, 0.0, 0, 0.0, 0.0
        first_token = None
        text_parts, generated_ids = [], []
        try:
            before = time.perf_counter()
            batch = self.profile.get("batch", 512)
            for offset in range(0, len(ids), batch):
                api.decode(active, ids[offset:offset+batch])
            prompt_seconds = time.perf_counter() - before
            finish = "length"
            for index in range(request["max_tokens"]):
                before = time.perf_counter()
                token = api.llama_sampler_sample(sampler, active, -1)
                if not ignore_eos and api.llama_vocab_is_eog(primary.vocab, token):
                    finish = "stop"; sampling_seconds += time.perf_counter() - before
                    break
                sampled += 1
                generated_ids.append(token)
                if first_token is None:
                    first_token = time.perf_counter() - started
                fragment = decoder.decode(api.piece(primary.vocab, token))
                delta = stop_buffer.push(fragment)
                sampling_seconds += time.perf_counter() - before
                if delta:
                    if first is None:
                        first = time.perf_counter() - started
                    text_parts.append(delta)
                    yield {"type": "token", "text": delta}
                if stop_buffer.stopped:
                    finish = "stop"; break
                if index + 1 < request["max_tokens"]:
                    if index == 0 and self.secondary:
                        before = time.perf_counter()
                        size = api.llama_state_get_size(primary.context)
                        state = C.create_string_buffer(size)
                        written = api.llama_state_get_data(primary.context, state, size)
                        consumed = api.llama_state_set_data(self.secondary.context, state, written) if written == size else 0
                        if not written or consumed != written:
                            raise RuntimeError("State handoff failed its byte-count check; no CPU prompt recomputation is substituted")
                        active = self.secondary.context
                        api.llama_synchronize(active); api.llama_perf_context_reset(active)
                        state_bytes = written; del state
                        transfer_seconds = time.perf_counter() - before
                    before = time.perf_counter(); api.decode(active, [token])
                    decoding_seconds += time.perf_counter() - before
            if not stop_buffer.stopped:
                final = stop_buffer.push(decoder.decode(b"", final=True), final=True)
                if final:
                    if first is None:
                        first = time.perf_counter() - started
                    text_parts.append(final)
                    yield {"type": "token", "text": final}
            perf = api.llama_perf_context(active)
            yield {"type": "done", "text": "".join(text_parts), "finish_reason": finish,
                   "generated_ids": generated_ids,
                   "usage": {"prompt_tokens": len(ids), "completion_tokens": sampled, "total_tokens": len(ids)+sampled},
                   "metrics": {"request_seconds": time.perf_counter()-started, "format_seconds": format_seconds,
                        "tokenize_seconds": tokenize_seconds, "context_reset_seconds": reset_seconds,
                        "prompt_seconds": prompt_seconds, "first_text_seconds": first, "first_token_seconds": first_token,
                        "sampling_and_text_seconds": sampling_seconds, "continuation_decode_seconds": decoding_seconds,
                        "state_transfer_seconds": transfer_seconds, "state_bytes": state_bytes,
                        "native_decode_tokens_per_second": perf.n_eval/(perf.t_eval_ms/1000) if perf.t_eval_ms else None,
                        "token_count_convention": "all sampled output tokens including EOG; fixed-token benchmark" if ignore_eos else "sampled non-EOG output tokens; no billing"}}
        finally:
            api.llama_sampler_free(sampler)

    def close(self):
        for item in (self.secondary, self.primary):
            if item:
                item.close()
        self.secondary = self.primary = None
        if self.api:
            self.api.close(); self.api = None
