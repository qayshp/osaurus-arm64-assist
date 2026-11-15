# Benchmarks: assistance is a workload choice

These are historical measurements from one Surface Laptop 7 on November 14–15, 2026: Snapdragon X Plus X1P64100, 10 CPU cores, Adreno X1-85, Hexagon v73 and 16 GB installed RAM. They explain why the companion exposes explicit execution profiles rather than promising that an accelerator is always faster.

The favorable and unfavorable results below belong together. Fresh companion validation and CI results are separate evidence; these tables do not certify a new installation, another Snapdragon generation, Intel/AMD NPUs, long contexts, or a particular battery-life benefit.

## Raptor: a useful warm handoff, with expensive startup

The new comparison used the same unchanged `Raptor-0.7-4B-Q6_K.gguf` (`spark2_5`) and matched native libraries for all rows. The handoff processes the prompt on NPU, makes the first token available, then copies context state once to CPU for subsequent generation. It is different from retaining selected Ornith expert operations on CPU.

**Fixed workload: exactly 512 input tokens and 128 output tokens.** One warm-up, then three measured repetitions; table values are medians. First-text time is already inside total response time. Loading is excluded here and reported immediately below.

| Mode | First text available | Native decode tokens/s | Total response |
|---|---:|---:|---:|
| CPU | 4.44 s | 14.61 | 13.21 s |
| NPU offload | **0.82 s** | 12.01 | 11.45 s |
| NPU prompt → CPU generation | 0.86 s | **14.85** | **9.48 s** |

The warmed handoff reduced the fixed-workload median total by 28.2% versus CPU and 17.2% versus NPU offload. Its prompt stage was about 0.82 s, the 75,510,684-byte state transfer about 42.7 ms, and continuation decoding about 8.56 s. Reset, sampling and host work also contribute to total. Independent medians need not sum exactly.

**Startup and memory regressions must not be hidden:** these are single observed fresh-process starts, with Windows's existing file cache. The first control is a separate 32-input-token arithmetic request producing `42` in two visible tokens, not a 128-token answer.

| Mode | Runtime ready | First arithmetic answer including startup | Peak full working set during loading/inference |
|---|---:|---:|---:|
| CPU | **3.87 s** | **5.26 s** | **7.21 GiB** |
| NPU offload | 5.60 s | 7.08 s | 7.62 GiB |
| NPU prompt → CPU generation | 13.58 s | 21.96 s | 10.37 GiB |

The two-context handoff briefly left about 12 MiB of system physical memory available. A loading peak is not a claim that the process stayed at that footprint throughout inference. It is still a substantial concern on a 16 GB laptop, especially with longer contexts or more applications. This makes an optional warm-session mode more defensible than an automatic default.

Real text tasks produced the following complete-response medians. Free-form output lengths vary; see the counts and actual responses in the evidence files.

| Task | CPU | NPU offload | NPU prompt → CPU generation |
|---|---:|---:|---:|
| Meeting reply, 81 input tokens | 3.26 s | 3.60 s | **3.07 s** |
| Fact extraction, 750 input / 10 visible output tokens | 7.93 s | **2.27 s** | 2.32 s |
| Document summary, 761 input tokens | 12.10 s | 7.77 s | **6.20 s** |

NPU offload remained the better short-extraction result. A ten-token output gives CPU handoff little decoding time to recover its cost. All nine extraction answers were correct. The other tasks were timed free-form responses, not broad quality certifications or strict word-limit passes.

Evidence: [summary](../evidence/raptor-state-handoff.json), [CPU requests](../evidence/raptor-cpu-requests.json), [NPU requests](../evidence/raptor-npu-requests.json), [handoff requests](../evidence/raptor-hybrid-requests.json).

## Original Ornith Q4: extraction improves; long generation regresses

The original 35B-A3B `Q4_K_M` file is 21,713,463,040 bytes, about 20.22 GiB. It exceeds the laptop's physical RAM. The successful path memory-maps CPU weights and disables CPU repacking. In the hybrid, routed expert matrix multiplication remains on CPU, while HTP0 handles supported remaining work. The log recorded about 18,872.81 MiB of CPU-mapped model buffers and 1,411.46 MiB of HTP model buffers. “Offloaded layers” does not mean the expert calculations left the CPU.

**Fixed 512-input/128-output workload**, one warm-up plus three measured repetitions, loading excluded:

| Mode | First text available | Native decode tokens/s | Total response |
|---|---:|---:|---:|
| CPU | 31.91 s | **9.35** | **45.36 s** |
| CPU experts + NPU | **21.46 s** | 3.69 | 55.95 s |

The NPU-assisted path starts about 10.45 s sooner but finishes the controlled long output later. That is a real regression for this workload.

| Real task | CPU total | CPU experts + NPU total | Output counts, CPU / hybrid |
|---|---:|---:|---:|
| Meeting reply, 77 input tokens | 29.63 s | 29.63 s | 57–78 / 57–61 |
| Fact extraction, 733 input tokens | 66.17 s | **48.96 s** | 10 / 10 |
| Summary, 742 input tokens | 88.26 s | 85.09 s | 116–127 / 104–113 |

The extraction median improved by about 26%; all six answers were correct. The summary's smaller difference accompanies different output lengths and is not a clean equal-work speed result. Runtime-ready construction was 3.01 s on CPU and 6.22 s for the hybrid, excluding deferred file reads in the first request. The original model SHA-256 remained unchanged; no Q2 compression was introduced. Different numerical kernels can still change wording.

Evidence: [summary, ranges and placement](../evidence/ornith-q4-assist.json), [CPU requests](../evidence/ornith-q4-cpu-requests.json), [hybrid requests](../evidence/ornith-q4-npu-requests.json).

## Lower-precision Ornith Q2: it did not solve full NPU loading

A community Q2_K quantization of the same 35B model was downloaded and checksum-verified. Its 13,085,779,616-byte file is about 39.7% smaller than the original Q4. Quantization preserves parameter count while approximating the weights more coarsely; it can affect quality. It was not a substitute 9B model.

The following fixed 512/128 test used the matching GenieX SDK with batch 512, microbatch 128 and context 2048. Both accelerator rows retain CPU expert work.

| Mode | First text | Decode tokens/s | Total response |
|---|---:|---:|---:|
| CPU | 5.87 s | **23.78** | **11.11 s** |
| Adreno-assisted, CPU experts | 18.59 s | 11.13 | 30.24 s |
| NPU-assisted, CPU experts | **4.40 s** | 13.61 | 13.91 s |

| Real task | CPU | Adreno-assisted | NPU-assisted |
|---|---:|---:|---:|
| Meeting reply | **5.41 s** | 13.31 s | 6.32 s |
| Fact extraction | 17.11 s | 44.37 s | **12.99 s** |
| Summary | **17.25 s** | 53.86 s | 18.37 s |

The extraction median improved about 24%, but CPU and NPU ranges overlapped. All nine extraction answers were correct. This does not establish broad Q2 quality equivalence to Q4. The CPU wrapper's loading/repacking behavior also differs from the later Q4 C-API test, so a Q4/Q2 timing ratio would not isolate quantization.

Full NPU offload failed before an answer: `fastrpc_mmap`, error `0x00000200`, initially mapping 993,017,856 bytes. Disabling host buffers and reducing batches did not fix it. Smaller mapping limits changed the failed mapping to 479,584,256 bytes but still failed. A six-virtual-session attempt enumerated the sessions but did not complete before its 240 s deadline; termination/cleanup made the parent observation about 252.94 s. Its unfinished report and timeout classify it as failure, even though its interrupted child had no exception string. **There is no valid full-NPU generation rate for these failed attempts.** The log suggests possible I/O virtual address-space exhaustion; it does not prove a universal physical NPU size limit.

Evidence: [all successful modes, failures and timeout](../evidence/ornith-q2-comparison.json).

## Earlier GPU and small-model results remain part of the record

The November 14 Raptor benchmark used the same GenieX 0.11 runtime but different wrapper/default settings from the later handoff prototype. Fixed 512/128 medians were:

| Mode | First token | Decode tokens/s | Prompt plus generation |
|---|---:|---:|---:|
| CPU, 6 decode threads | 4.387 s | **16.04** | 12.365 s |
| CPU, 10 decode threads | 4.897 s | 13.84 | 14.227 s |
| Adreno, 6 host threads | 3.314 s | 14.83 | 11.942 s |
| NPU balanced, 6 host threads | 0.812 s | 10.59 | 12.893 s |
| NPU burst, 6 host threads | **0.789 s** | 12.43 | **11.091 s** |

Adreno was competitive here; it was not a universally slower option. The real extraction took 6.932 s on the default CPU path, 5.803 s on Adreno, 2.373 s on balanced NPU and 2.208 s on burst NPU. The earlier follow-up summary reused 707 cached tokens, unlike the later fresh-context handoff comparison. Burst improved throughput but no energy measurement establishes it as the most efficient setting.

The earlier original-Q4 CPU/OpenCL comparison used the b12359 engine, mmap and no repack. Its CPU fixed test was about 29.98 s versus 47.17 s for Adreno with CPU experts. The real extraction was 54.30 s versus 105.84 s. Full GPU loading failed; an NPU load was stopped when commit headroom became too low. A failure to load is neither a slow throughput result nor proof that every GPU/NPU is incapable of larger-than-RAM hybrid execution. The follow-up summary reused 601 prefix tokens.

For the tiny Qwen3-0.6B Q4_0 control, the 512/128 workload had CPU/Adreno/NPU first-token medians of 535.74/508.93/226.65 ms and decode rates of 93.12/62.59/38.28 tokens/s. One warm-up and five measured repetitions were used. This showed faster prompt handling on NPU and faster token generation on CPU; it did not predict the larger models' behavior. Whole-response wall time and loading stages were not separately retained for this control.

Evidence: [earlier Raptor fixed runs](../evidence/raptor-earlier-fixed.json), [earlier Raptor HTTP cases and counts](../evidence/raptor-earlier-http.json), [earlier Q4 CPU/OpenCL](../evidence/ornith-q4-earlier-opencl.json), [tiny Qwen controls](../evidence/qwen-control.json).

## What can be concluded

- Raptor's phase handoff is a promising warm-session option on this exact tested stack. Its startup and memory costs are material.
- Ornith's CPU-expert/NPU split can help input-heavy, short-output work while regressing controlled long generation.
- Full NPU offload is still a useful option; it remained the fastest Raptor short-extraction result.
- Adreno has useful measured cases in the earlier Raptor tests. A general GPU-assist policy or GPU-prefill/CPU-decode handoff was not established.
- No battery/energy claim, broad quality equivalence, strict-JSON guarantee, universal model compatibility or recommendation to enable assist for all models follows from these measurements.

See [methodology](methodology.md) for the exact document, questions, token accounting and timing boundaries, and [support limits](support-and-troubleshooting.md) before applying the results to another system.
