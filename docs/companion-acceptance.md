# Packaged companion acceptance observations

These November 15, 2026 observations exercise the community companion installed on the same Surface Laptop 7 used for the [historical experiments](benchmarks.md): Snapdragon X Plus X1P64100, ten CPU cores, Adreno X1-85, Hexagon v73 and 16 GB installed RAM. They establish bounded behavior on that machine, rather than general support for all models, NPUs or Osaurus workflows.

The native target is the pinned Qualcomm GenieX 0.11.0 Windows ARM64 release, with llama.cpp core `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30`. The companion package identifies itself as 0.1.0. These receipts were produced during November 15 initial alpha verification, including runs after several public commits; the original prototype benchmarks and successive companion acceptance runs are separate observations. [Provenance](../evidence/companion-acceptance-provenance.json) identifies hardware, profile intent and receipt basenames without local paths or credentials.

## What the receipts establish

| Profile / receipt | Observed checks | Result | Boundary |
|---|---|---|---|
| [Raptor Q6_K, initial NPU prompt → CPU generation suite](../evidence/companion-raptor-hybrid.json) | Seven direct companion cases: arithmetic, Unicode SSE, conversation history, Harbor extraction, forced tool, tool-result round trip and JSON object | **7/7 passed** | 2048 context, `spark2_5`; retained for its own observations and startup variability |
| [Raptor after final reinstall, direct and through Osaurus](../evidence/companion-raptor-osaurus.json) | The same seven cases directly, then all seven through Osaurus's API, including long extraction, tools and JSON | **14/14 passed**, plus a separate **32-input/8-output benchmark smoke passed** | Direct calls check hybrid mode/state transfer; routed responses and usage were checked through the registered provider |
| [Original Ornith Q4, CPU experts + NPU](../evidence/companion-ornith-q4-assist.json) | Arithmetic and Unicode SSE | **2/2 passed** | Quick smoke only; `qwen35moe`, no CPU repacking, 2048 context |
| [Original Ornith Q4, CPU experts + Adreno](../evidence/companion-ornith-q4-gpu-assist.json) | Arithmetic and Unicode SSE | **2/2 passed** | Quick smoke only; not a new GPU performance recommendation |
| [Qwen3 0.6B CPU control](../evidence/companion-qwen-cpu.json) | Exact arithmetic output and Unicode copy | **0/2 exact-quality checks passed; both transports passed** | Failure details are retained below; small-model instruction following is not assumed |
| [Owned server-crash cleanup](../evidence/companion-server-crash.json) | One loaded Raptor worker after the owned server was terminated | Worker exited in **0.0167 s** | Job attachment and child exit were observed once; not a general recovery/stability certification |
| [Removal](../evidence/companion-clean-removal.json) and [reinstallation](../evidence/companion-clean-reinstall.json) | Owned installation/provider removal, tokenless backup, seven restored profiles, credential rotation and unrelated configuration comparison | **All recorded checks passed** | Performed on this already-provisioned laptop; no claim of fresh-machine DSP setup |
| [Preserved model content](../evidence/companion-preserved-models.json) | Fresh full-file SHA-256 of original Q4 Ornith and Q6_K Raptor | **Both match prior benchmark identities** | Independent content check beyond the lifecycle receipts' file-existence checks |

The initial and final Raptor suites overlap. Do not add 7 and 14 into a claim of 21 distinct capabilities, or treat successive suites as performance repetitions. The earlier ten-case routing receipt is superseded by the final fourteen-case suite and is omitted from the public evidence. Routed calls use Osaurus's local API and registered provider, not a visual/manual exercise of every application interaction. Routed responses lack companion-specific `assist` fields because that metadata was not forwarded in the observed Osaurus response.

The acceptance client checks usage totals, requested/reported actual mode, advertised context, expected synthetic outputs and hybrid state-transfer evidence where available. Its receipt explicitly says **“API observations; this client does not sample physical NPU counters.”** Successful host-side API metadata and state bytes are useful execution evidence; they are not an independent counter trace proving utilization, device occupancy or energy efficiency. These tests do not bypass the need for signed-DSP provisioning and runtime integrity checks.

## Current packaged fixed-token comparison

The completed companion CLI rerun used the same Raptor Q6_K file for all four modes, a 2048-token context, batch/microbatch 512, six decode threads, ten batch threads, CPU repacking enabled and balanced NPU power. Each mode ran one warm-up followed by **three measured repetitions**, sequentially, with the context cleared for each request. Independent checks confirmed that every input array is the same 512 IDs generated by `random.Random(42).randrange(32000)`, and every warm-up/measured run reports exactly 512 input / 128 generated tokens with 128 captured output IDs.

These are new **packaged-companion medians**, not replacements for or extra repetitions of the historical prototype table. Loading and warm-up are excluded from the engine response totals below. First token is already included in total response time.

| Mode | First sampled token | Native decode tokens/s | Complete engine response | Complete-response range across three runs |
|---|---:|---:|---:|---:|
| CPU | 4.6597 s | 14.97 | 13.1823 s | 13.0457–13.9120 s |
| Adreno OpenCL | 3.3477 s | 14.59 | 12.1362 s | 11.8469–12.3600 s |
| NPU offload | 0.8214 s | 12.20 | 11.3148 s | 11.3042–11.5731 s |
| NPU prompt → CPU generation | **0.8148 s** | **15.93** | **8.8427 s** | 8.6569–9.0124 s |

On this fixed warm workload, handoff reduced the observed median total by 32.9% versus CPU, 21.9% versus NPU offload and 27.1% versus GPU offload. The first-token difference between the two NPU paths is small and does not establish a systematic handoff prefill advantage. The hybrid's median one-time transfer was 75,510,684 bytes in 27.8 ms. The first output token comes from prompt logits; the other 127 tokens require continuation steps. Native decode speed is not a count of all 128 outputs divided by the complete request time.

Warm-ups and all individual runs remain available: [CPU](../evidence/companion-fixed-raptor-cpu.json), [NPU](../evidence/companion-fixed-raptor-npu.json), [Adreno](../evidence/companion-fixed-raptor-gpu.json), [handoff](../evidence/companion-fixed-raptor-hybrid.json), and [independent count/input/median validation](../evidence/companion-fixed-raptor-validation.json). Reported actual modes matched the requested modes without CPU fallback. Hybrid state transfer was nonzero; the other modes reported zero transfer. No failed or incomplete run was converted into a timing result.

The benchmark implementation matches source commit [`b735a4ebdf65572946680ac39160e8e8da985ab5`](https://github.com/qayshp/osaurus-arm64-assist/commit/b735a4ebdf65572946680ac39160e8e8da985ab5); later public documentation commits preceded the reinstall/verification. The expected GGUF identity is 3,690,735,072 bytes, SHA-256 `d3ee40578d3c747697e76a1534551c4e6822b632f68e7c9a2b1508c94a2b9349`, linked to the separate full-file preservation rehash. The benchmark endpoint itself does not hash the weights.

This rerun still has practical limits. Hybrid was already loaded at the beginning of its warm-up; CPU/NPU/GPU warm-ups included new native loads, so whole-command API duration is not a fair startup ranking. Measured rows are warm, but thermal state, file cache and earlier requests were not randomized or tightly controlled. Generated token IDs differ across modes; this synthetic throughput test does not establish identical answers or general quality equivalence. The startup and memory costs below, the NPU-only short-answer advantage and Ornith's slower long-generation results remain part of the assessment.

## Startup remains a material cost

The first arithmetic response in the direct Raptor acceptance suite took **33.84 s from client request to completion**. Its native construction was 25.09 s and its engine request took 8.36 s. The earlier prototype's first arithmetic answer took 21.96 s from backend initialization. The packaged path was therefore slower in this observation; the public record retains that result.

The timing boundaries differ: the new client measurement includes worker/process/API overhead, while the old prototype stopwatch starts at backend initialization. Windows file-cache, paging and background state were not controlled. This is not a paired regression test that isolates a single code change. It does show that the prototype's best warm behavior cannot stand in for actual first-use responsiveness. The final reinstall suite observed a different **27.73 s** direct first answer and **17.92 s** native load; that variability is also retained rather than averaged away.

Selected **single warm observations** from the seven-case direct Raptor suite are below. They are neither three-run medians nor replacements for the historical controlled 512/128 benchmark.

| Case | Prompt / generated tokens | Client complete time | Engine first text | State transferred |
|---|---:|---:|---:|---:|
| Unicode SSE copy | 28 / 5 | 0.610 s | 0.246 s | 4,130,364 bytes |
| Conversation history | 67 / 5 | 0.569 s | 0.224 s | 9,882,084 bytes |
| Harbor fact extraction | 750 / 10 | 2.114 s | 1.388 s | 110,610,924 bytes |
| Forced `add_numbers` tool | 123 / 19 | 1.917 s | 0.344 s | 18,140,964 bytes |
| Tool-result round trip | 74 / 2 | 0.451 s | 0.230 s | 10,914,444 bytes |
| JSON object | 42 / 6 | 0.709 s | 0.223 s | 6,195,084 bytes |

For forced tools and JSON, engine “first text” is internal native generation. The API buffers that output until it has a validated complete result, so it is not the client's first visible structured response. Client complete timing includes transport and postprocessing; it is not an extra stage to add to engine time. The larger state copy for the 750-token document is a different context state from the historical fixed-512 workload's roughly 72 MiB copy.

The original Ornith Q4 quick smoke took 16.07 s for the first arithmetic answer using CPU experts + NPU, including 6.27 s native load. Its warm Unicode response took 5.14 s. CPU experts + Adreno took 26.19 s including 12.65 s native load for arithmetic, and 8.97 s for warm Unicode. These isolated smoke observations have no matched CPU baseline and do not establish a new acceleration ratio. Historical slower GPU and longer-output NPU-assisted cases remain in the [balanced benchmark tables](benchmarks.md).

In the final suite, direct Harbor extraction consumed 750 prompt tokens and produced 10; through Osaurus it consumed 983 and produced 14. Both answers passed the semantic extraction check, but they are different work. Their 2.01 s and 3.54 s client times cannot isolate routing overhead. The separate benchmark smoke verifies exact deterministic input IDs and exactly 32 input / 8 generated tokens, reported hybrid mode and nonzero transferred state. Its one repetition is an API-path acceptance check, not the full historical 512/128 throughput comparison.

## Small-model failures stay visible

The Qwen CPU transport returned well-formed responses with consistent usage and CPU metadata. It nevertheless failed both strict expected-output tests:

- Arithmetic expected exactly `42`, but returned `17 + 25 = 42.`. The arithmetic was correct; the requested output format was not.
- Unicode copy expected `café 你好`, but returned only `café`. This is missing requested content, rather than proof that SSE transport corrupted UTF-8.

The receipt remains marked unsuccessful. It is not silently promoted to a pass because another model succeeds or because a looser mathematical-answer check would accept its first response. Nor does this two-case smoke prove that Qwen can never perform those tasks. Model capability, request semantics, transport correctness and device execution are separate checks.

## Removal, reinstallation and process cleanup

The server-crash receipt records one owned worker, attached Windows job protection, and successful worker exit about 17 ms after its server was terminated. This is a bounded orphan-cleanup observation, not proof of automatic reload after every native crash, cancellation timing under all loads, or stability across sleep/resume.

Removal left the dedicated installation directory absent and the owned provider absent. Its tokenless backup retained seven model profiles and nonsecret settings. Reinstallation restored those seven profiles, created a new installation API key, and connected the owned provider at the intended local endpoint. The nine unrelated providers, three agents, default agent and other recorded Osaurus configuration remained unchanged. Reinstallation increased the provider count from nine to ten by adding only the companion provider.

The lifecycle comparison receipts explicitly did not hash model content themselves. A separate complete SHA-256 pass over the 21,713,463,040-byte Ornith Q4 file and 3,690,735,072-byte Raptor Q6_K file matched the exact identities used for the historical benchmarks. This supports content preservation in addition to the registered-file existence checks.

These are local removal/reinstallation observations with actual model references and application configuration. They do not establish first-time signed-DSP provisioning on a clean machine. The full 512/128 repeated fixed-token comparison above is a separate measurement phase from the small final acceptance smoke.

After explicit unload, the [final state receipt](../evidence/companion-final-state.json) records a healthy connected companion on loopback, seven advertised profiles, no loaded model and zero owned native workers. Available physical memory was about 9,381 MiB at that moment. The original GenieX models remained available and unrelated Osaurus configuration still matched the recorded baseline. No startup task was installed. This is a point-in-time resting-state observation, rather than a promise that free RAM stays constant or a running inference profile uses no memory.

## Reproduce and interpret

Use the root [setup instructions](../README.md) to register existing model files and start the companion. The public `tools/hardware_acceptance.py` client uses synthetic requests and writes sanitized receipts; inspect its help for explicit model/target arguments. `benchmark` provides the separate deterministic fixed-token throughput workload. Neither command is an automatic model-quality certification.

Keep hardware acceptance distinct from unit tests: unit tests exercise parsing, protocol, bounds and lifecycle logic using synthetic files or fake workers. Passing them does not execute the physical NPU. Conversely, these few hardware successes do not establish broad tool reliability, JSON Schema output, multimodal support, larger contexts, long-duration operation, comfortable RAM headroom or battery benefits. Keep failed checks, startup time and memory regressions alongside warm successes when sharing results.
