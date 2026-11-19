# Models and settings follow Osaurus

**0.1.3-alpha.1** adds automatic local-model inventory and settings following. This document describes its implementation contract. [New installation and hardware receipts](companion-acceptance.md#automatic-model-and-settings-acceptance) are tracked separately from the earlier 0.1.0 observations; those older successes do not certify every synchronization path or model.

## Everyday use

Download a local model or change its settings in Osaurus. About every five seconds, the companion refreshes the completed local inventory and resolved settings, then updates its provider entries. Select the companion entry in Osaurus to use that route. The original model remains available through Osaurus's own engine. No per-model companion registration or second weight copy is required in the default mode.

This is a read-only integration with the source inventory/settings. The companion does not rewrite your source model preferences, infer settings by executing generated command lines, or download weights itself. Existing manual aliases can retain their IDs while following the matching source model. Optional fixed aliases are described below.

## Which route actually runs?

| Source model / condition | Automatic route | Interpretation |
|---|---|---|
| Raptor `spark2_5`, Q6_K | NPU prompt → CPU continuation (`hybrid`) | Strategy validated for the documented Raptor file; two contexts have material startup/RAM cost |
| Ornith `qwen35moe`, Q4_K_M or Q2_K | CPU experts + NPU (`cpu-experts`) | A different split; longer generation can be slower than CPU |
| Other chat model/quantization | Original Osaurus engine (`osaurus`) | Native compatibility remains with the source engine; no general NPU claim |
| Explicit preference unsupported by this companion | Original Osaurus engine | Preserve the preference rather than silently discard it |
| Embedding, speech, adapter or projection asset | Excluded from the chat catalog | Presence of a GGUF file does not make it a chat model |
| In-progress download, incomplete split set or invalid metadata | Pending/excluded with diagnostics | No speculative inference or partial-file load is performed |

Discovery checks local inventory state and bounded GGUF metadata without loading a native model or tensor payload. Split sets are grouped rather than advertised as independent shards. Architecture and overall quantization guide routing; they are not proof that every tensor type, file variant or larger model works on this NPU.

Stock delegation reports **`actual_mode: osaurus-native`**, with a safe fallback reason. Osaurus then applies its own engine/device policy. That label is not a claim of NPU execution. Native assisted responses retain their actual mode and timing metadata; unsupported-setting names are visible in synchronization diagnostics.

## Context: Auto is a backend choice

If the per-model `contextSize` and global `localContextSize` are both unset/Auto, **Raptor Q6_K hybrid uses 8192 tokens**. An explicit source preference up to **16384** is supported for that strategy, capped by model metadata. Other native models/modes retain their tested **2048-token policy**. A stock-engine plan of 131072 tokens is not copied into an NPU allocation.

To request 16K Raptor hybrid, set the Raptor local-model context to **16384 in Osaurus**; the companion follows it without separate registration or configuration. Explicit preferences above the applicable tested ceiling delegate to the source engine and preserve the requested context. Input, output and template tokens all consume context. See [long-context measurements and limits](long-context.md).

## Settings contract

| Osaurus preference | Native assisted behavior |
|---|---|
| Context | Raptor Q6_K hybrid Auto 8192, explicit up to 16384; other native modes 2048; above-ceiling preferences use the stock engine |
| Batch / microbatch | Apply typed values; source defaults are 2048 / 512; invalid combinations cannot silently change the request |
| Threads / batch threads | Apply typed explicit values; backend Auto currently uses six / ten |
| Repacking | Raptor hybrid above 4096 uses no CPU weight repack to retain memory headroom; an explicit repack-on preference there uses the stock engine. Other small non-expert models may repack; large/expert profiles default to no repack |
| Flash attention | Honor supported CPU/HTP boolean preference; explicit OpenCL flash-on uses the stock engine |
| K/V cache types | F16 is supported; other explicit cache types use the stock engine |
| KV offload / operation offload / unified KV / full SWA | Map supported booleans to native context parameters |
| Explicit GPU layer count | Zero respects CPU preference via the stock engine; positive placement is applied only where the selected GPU route can honor it; an NPU route does not pretend to honor an exact GPU split |
| Memory mapping / direct I/O / locked memory | Current native loading uses mmap; incompatible explicit loading controls use the stock engine |
| Temperature / top-k / top-p / min-p | Follow validated resolved model/engine values unless the request supplies an override |
| Repeat / presence / frequency penalties and repeat window | Follow supported validated values, including explicit zeros where valid |
| Seed | Preserve a supported explicit seed; source `-1` maps to the native random-seed sentinel |
| Embedded template flags | Supported boolean `enable_thinking:false` and `preserve_thinking` are inherited/merged; preserving thinking text alone does not enable thinking generation |
| Thinking enabled, custom template, unsupported template arguments | Use the original engine to preserve semantics |
| Multiple resident models | Native companion requests remain serial with one active worker; incompatible source residency preference uses the stock engine |
| Advanced samplers, adapters, RoPE, placement or other unimplemented controls | Unsupported non-null explicit preferences use the stock engine, even when equal to a source default; harmless supported no-op exceptions are validated separately. Raw arguments/templates/credential values are not copied into diagnostics |

Diagnostics distinguish **applied**, **adapted** and **unsupported** setting names. This is not a promise that the two engines have identical internals. For example, the native path clears context at every new request: a source `cachePrompt:true` default does not establish cross-request KV reuse. Default/Auto caching and scheduler policy remain backend adaptations; an explicitly supplied unsupported cache preference delegates to Osaurus. Unsupported advanced changes can route back to Osaurus even when the architecture normally qualifies for assistance.

Before allocating a new assisted worker, the companion checks Osaurus's chat-model residency. If Osaurus already has a loaded, loading, busy or pinned model, the request delegates to that engine with `fallback_reason: osaurus_residency_policy`. Osaurus can then apply its own eviction and residency settings. Missing or changed residency APIs report `osaurus_residency_unavailable` and also delegate. An existing worker skips this check only when it can be reused with the requested model and load settings. A switch that needs a new worker checks Osaurus again. Another independent Osaurus request can still load a model after this read.

Before starting a native worker, the companion releases any incompatible worker and checks available Windows system commit against `memory_floor_mib` (512 MiB by default). If commit is already below the floor, the API returns HTTP 503 with code `memory_pressure`. Free memory or wait for other work to finish, then retry. The floor applies to commit, which includes page-file capacity. It does not estimate whether the model will fit in physical RAM. The background guard still monitors commit after the worker starts.

## Request precedence and safe changes

Explicit request sampling fields have precedence over inherited source model defaults. Zero is a value, not omission. Output-limit aliases are resolved with request provenance so an inherited `max_completion_tokens` does not override an explicitly supplied request `max_tokens`. Model-derived sampler values are parsed as bounded numbers; display text and generated `argv` are never executed.

Model/template and load settings are captured for the admitted request. A settings refresh does not interrupt an active response or replace its context mid-generation. A subsequent request can load a new native context when required. Sampling-only changes do not require loading weights again. A known inventory removal prevents later discovery/use of the removed source without deleting the model file on the companion's behalf.

## Offline/cache behavior and diagnostics

The last valid snapshot is cached for its source endpoint. A temporary API failure preserves that snapshot and marks it cached/stale; it does not masquerade as deletion of every model. An endpoint change cannot reuse another endpoint's inventory blindly. With no valid snapshot, the source is unavailable until a successful refresh. A deliberate fixed source reports `fixed`. Cached discovery is not a promise that a source file still exists or that a stock-engine route can answer while Osaurus is unavailable.

Use `models list --config ...`, `doctor --config ... --json`, or `scripts/Status.ps1 -Json`. Running-service health exposes `model_sync`; authenticated `/sync` reports safe profile summaries. Status includes live/cached/unavailable/fixed state, errors, inventory/exclusion/pending counts and native/proxy profile counts. Local cache/configuration can contain model locations and should remain private; public reports should use sanitized diagnostics and synthetic requests.

The source interfaces are internal Windows Osaurus APIs observed on version 1.0.3. They are validated rather than treated as a permanent public API. If a future app version changes the schema, a reported stale/unavailable state is preferable to applying unrecognized settings.

## Fixed profiles for experiments

Ordinary use needs no model registration. For a controlled benchmark, add a deliberate fixed alias:

```powershell
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/hybrid-fixed --mode hybrid --osaurus-model Raptor-0.7-4B-Q6_K --settings-source fixed --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --repack
```

That alias retains its explicit companion settings instead of inheriting source edits. Default aliases use `settings_source: osaurus`. To disable automatic inventory/settings following for the entire companion, the advanced local configuration option is `"models_source": "fixed"`; only manually registered profiles are used. This is independent of [idle preference following](idle-policy.md) and [automatic app lifecycle](automatic-lifecycle.md). Both choices are retained in tokenless configuration backups.

When comparing CPU/GPU/NPU, use fixed aliases with matching context, batch, threads and weights, and retain all tested modes, startup, memory costs and failures. The original controlled benchmarks used batch/microbatch 512; the automatic source default batch 2048 is a different configuration. Do not reuse old timing tables as proof of the new shared settings' performance.
