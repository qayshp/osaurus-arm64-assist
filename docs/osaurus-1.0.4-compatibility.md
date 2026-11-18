# Osaurus for Windows 1.0.4 compatibility

On **November 18, 2026**, companion **0.1.4** passed **14/14 synthetic Raptor checks** with the installed **Osaurus for Windows 1.0.4.0**: seven directly against the companion and seven through Osaurus. All seven routed requests were attributed to the automatically shared Raptor hybrid profile at **8192 context**, with a state transfer and no stock-engine fallback. The [machine-readable revalidation receipt](../evidence/osaurus-1.0.4-revalidation.json) records the cases, outputs and execution evidence.

This is a compatibility recheck on the same Snapdragon X Plus / Hexagon v73 / 16 GB Surface laptop. It does not replace the original benchmark evidence, certify every model or agent, or establish that every newer Osaurus version is compatible. The companion remains an external community provider with its separately pinned GenieX native runtime.

## What Windows 1.0.4 changes

The installed application's 1.0.4 release-notes resource and the [official Windows release announcement](https://community.osaurus.ai/t/osaurus-1-0-4-for-windows-is-out/73), published November 17, describe GitHub skill update review with local-edit preservation and undo, plus support for external agent programs speaking **Pi's RPC protocol**. Fixes cover speech-model installation, workflow setup, command-capable agent selection, available-tool reporting, network discovery and invite-dialog handling.

Pi RPC supplies an **agent runtime** that runs agent conversations through Osaurus's models. The companion supplies an **OpenAI-compatible model inference provider** that executes supported GGUF work through CPU, Adreno or Hexagon profiles. These operate at different layers: the new agent option does not itself select an NPU backend, supply a signed HTP payload or implement the companion's NPU-prefill/CPU-continuation handoff. The existing OpenAI-compatible provider path remains usable in this tested Windows build.

Windows and macOS release numbers must be checked separately. The public macOS application's [changelog](https://osaurus.ai/changelog) is not evidence of the Windows Store build's features. Likewise, the [Windows documentation baseline](https://raw.githubusercontent.com/osaurus-ai/osaurus-docs/master/windows-baseline.json) still identifies an earlier documented build; the installed package, its embedded notes and the Windows-specific announcement identify the version tested here.

## What passed

Both request paths used the automatically discovered **Raptor 0.7 4B Q6_K** profile, architecture `spark2_5`, with settings inherited from Osaurus. Auto selected an 8192-token context and the larger-context memory policy documented in [Raptor context support](long-context.md). The weights and quantization were retained.

| Synthetic check | Direct companion | Through Osaurus 1.0.4 |
|---|---|---|
| Arithmetic | Pass | Pass |
| Unicode SSE streaming | Pass | Pass |
| Conversation history | Pass | Pass |
| Project Harbor fact extraction | Pass | Pass |
| Forced tool call | Pass | Pass |
| Tool-result round trip | Pass | Pass |
| JSON-object output | Pass | Pass |

These are bounded API checks with synthetic input. The receipt retains the actual requests, token usage and answers. They do not exercise the complete Osaurus UI agent harness, every tool policy, every template or arbitrary real-world documents. They also do not replace the separate [long-context extraction and capacity tests](long-context.md).

The health snapshot during the checks read model sharing and idle policy as **live**. Four automatic chat profiles were available, two non-chat assets were excluded, and the source idle preference resolved to 300 seconds. Timer expiry itself was not waited out. Native weights were explicitly unloaded after the checks; the subsequent health snapshot reported no loaded model or attached native worker. Starting the updated app automatically started the companion; quitting Osaurus then stopped the companion server and returned the watcher to `waiting`, with no automatic error. These are single startup/quit observations, rather than a full lifecycle/sleep/resume campaign.

The largest input in this suite was 983 tokens. The configured 8192-token context was verified, but this recheck did not repeat the near-8K or 16K capacity tests.

## How the route was verified

Direct companion responses contain an `assist` extension reporting the requested and actual modes, configured context and stage metrics. Osaurus normalizes that private extension out of the responses it returns. Its absence after routing therefore does not demonstrate a fallback or a failed handoff.

For each routed request, the test recorded companion `/metrics` before and after the request, ran requests serially, and required a **fresh** record for the selected automatic profile. Each of the seven records reported `actual_mode: hybrid`, `context_length: 8192`, no fallback reason and positive `state_bytes`. This attributes the requests to the intended native handoff path. The recheck did **not** independently sample physical NPU utilization counters, power or energy; those conclusions must not be inferred from the route metadata.

Programmatic requests through Osaurus must also use its provider-qualified routing ID. The companion's own API accepts the raw profile ID; Osaurus accepts `<provider-id>/<companion-model-id>`. Obtain the provider ID from the selected model's `owned_by` field or the provider registration, and preserve the complete inner profile ID, including its `osaurus/` prefix. The display name **NPU Hybrid** is not the provider ID.

The historical `tools/hardware_acceptance.py` tool keeps these names separate with `--model` and `--routing-id`. Its direct-companion assertions still target 2048 context; that older command alone does not reproduce this 8K, fresh-metrics recheck.

The initial test used an unqualified ID and received `model_not_found`. Earlier successful Windows tests had already used the provider prefix; correcting the test identifier restored routing. This was a test-harness mistake, not evidence of an application regression. A later answer-only run was extended with the fresh-metrics checks above before being counted as execution acceptance.

## Separate installed InferSSD recheck

InferSSD is a different inference runtime. Updating Osaurus does not update its packaged engine, change its tensor loader or turn its GPU backend into an NPU backend. The installed package tested on the same date identified **build 266**, revision **`e91c7d7a000b69ef69ee0e20fd1345254a504bd7`**. It ran as an **x64 program under Windows ARM64 emulation**. These results apply to that installed package and the tested defaults.

| Installed InferSSD check | Observed result | Interpretation |
|---|---|---|
| Original Ornith Q4, CPU / SSD-expert startup, context 512 and 4 GiB budget | Failed before readiness with missing `blk.40.ssm_conv1d.weight` | No generated answer and no valid throughput result |
| Small Qwen3 Q4_0 CPU control, context 512 | Started; arithmetic value and extracted invoice values were correct, but the answer included an equation and the object was wrapped in Markdown | Coherent inference; both strict formatting checks failed |
| Same Qwen3 with the installed default Adreno Vulkan path, context 512 | Started; both replies were garbled | Startup/device enumeration did not establish correct GPU inference |

The CPU and Vulkan controls used two short prompts, not a broad quality evaluation or a matched throughput comparison. No new machine profile was measured for these rechecks. The GPU result does not apply automatically to other backends, devices or engine settings, and no tuning outcome is claimed here.

Upstream [InferSSD PR #294](https://github.com/wici-ai/inferssd/pull/294), merged into `main` on **November 17, 2026**, changes the treatment of extra NextN/MTP blocks and addresses the same class of Qwen35MoE missing-tensor failure. **That change is not in the installed build tested above.** Its merge does not establish that this Ornith file, this Windows ARM64/emulated setup or Adreno inference will work after an update. A package containing the change needs a separate startup and correctness test; the failed installed-build result remains part of the record.

The official [Windows Vulkan engine feed](https://appapi.wici.ai/versioning/inferssd/config/latest-windows-vulkan.json) still offered build 266 / `e91c7d7` when checked on November 18. The desktop feed offered 0.1.3. This was a read-only metadata check, with no package update or update-signature validation performed; the normal engine feed had not yet supplied the November 17 fix.

## What this does not remeasure

The historical [CPU/GPU/NPU comparisons](benchmarks.md) retain their original runtime, application, context and load policies. This recheck does not provide a new speed ranking, battery claim or universal model-compatibility result. It did not rerun full Ornith NPU offload, every Ornith assistance workload, cold-boot provisioning or long-duration stress tests.

For current usage, keep the **NPU Hybrid** provider and select the shared Raptor entry. Its tested Auto context remains 8K; explicit 16K support is backed by the separate context evidence. Keep stock-engine fallback visible for unsupported architectures or explicit preferences. After future application updates, repeat request correctness and execution attribution rather than relying on a provider badge or an unchanged model listing.
