# Osaurus ARM64 Assist

An experimental community companion for selecting CPU, Adreno GPU and Qualcomm Hexagon NPU inference profiles in Osaurus on Windows ARM64.

**Alpha software.** Acceleration experiments were performed on one Surface Laptop 7 with a Snapdragon X Plus, Hexagon v73 and 16 GB of RAM, using a 2048-token context. This is a Snapdragon/Hexagon project; Intel and AMD NPUs are unsupported. Other Snapdragon generations and model architectures need their own validation. It is not an official Osaurus, Qualcomm, Microsoft or llama.cpp component.

The companion exposes a local OpenAI-compatible provider. Models remain in Osaurus's model directory, and you explicitly select a companion profile in Osaurus. Downloading a model through Osaurus does not automatically enable NPU assistance. This repository contains source, setup scripts and sanitized experimental evidence; it contains no model weights or native runtime binaries.

## Why offer assistance?

Prompt processing and token generation have different performance characteristics. For the tested Raptor model, NPU prompt processing followed by CPU generation improved warm response time. A separate Ornith strategy keeps routed experts on CPU while sending supported remaining work to the NPU. These strategies have different costs and are separate profile modes.

The following historical results are from November 14–15, 2026. They motivate an option, rather than an automatic default. Fixed-workload rows use exactly 512 input and 128 output tokens. Warm response times exclude loading; startup and memory are shown separately.

| Model and workload | CPU | NPU strategy | Result and cost |
|---|---:|---:|---|
| Raptor Q6_K, fixed warm request | 13.21 s | Full NPU 11.45 s; NPU prompt → CPU generation **9.48 s** | Handoff improved the warm total, with NPU-like first text and CPU-like continuation |
| Raptor Q6_K, short fact extraction | 7.93 s | Full NPU **2.27 s**; handoff 2.32 s | Full NPU remained slightly faster for this short answer |
| Raptor, first arithmetic answer including startup | **5.26 s** | Full NPU 7.08 s; handoff 21.96 s | Handoff's two model/context objects made startup substantially worse |
| Raptor, peak full working set | **7.21 GiB** | Full NPU 7.62 GiB; handoff 10.37 GiB | Handoff briefly left only about 12 MiB of physical memory available on this 16 GB machine |
| Original Ornith Q4, fixed warm request | **45.36 s** | CPU experts + NPU 55.95 s | Earlier first text did not repay slower generation |
| Original Ornith Q4, short fact extraction | 66.17 s | CPU experts + NPU **48.96 s** | Input-heavy extraction improved without changing the Q4 weights |
| Ornith Q2, fixed warm request | **11.11 s** | CPU experts + NPU 13.91 s; CPU experts + Adreno 30.24 s | Both assisted paths lost on this workload; full NPU loading failed before producing an answer |

Adreno had useful earlier Raptor results too: fixed-workload totals were 12.365 s on CPU and 11.942 s on GPU, while a short extraction took 6.932 s and 5.803 s respectively. Those runs used different wrapper/settings from the later handoff comparison and should not be mixed into a single ranking.

Read the **[complete balanced benchmark record](docs/benchmarks.md)** for all modes, failed attempts, startup regressions, token counts and limitations. No battery/energy advantage or broad quality equivalence was established. Unchanged weights do not guarantee identical wording across numerical kernels.

## Execution profiles

| Mode | Behavior | Current experimental boundary |
|---|---|---|
| `cpu` | CPU baseline | Also useful when acceleration cannot load a model |
| `npu` | Normal Hexagon offload, with CPU host/fallback operations | Historical small-Qwen and Raptor execution; full Ornith offload failed |
| `hybrid` | NPU prompt/first token, one context-state transfer, CPU continuation | Raptor 0.7 4B Q6_K, architecture `spark2_5`; two-context RAM and startup costs |
| `cpu-experts` | Expert work on CPU, supported remaining work on NPU | Ornith 1.5 35B-A3B, architecture `qwen35moe`; workload-dependent benefit |
| `gpu` | Adreno OpenCL offload with CPU host/fallback operations | Historical Qwen/Raptor comparisons; model support varies |
| `gpu-experts` | CPU expert work with supported remaining work on Adreno | Historical Ornith comparisons were slower than CPU |

The same GGUF can have multiple profiles without copying its weights. Registration checks configuration and file presence; it does **not** prove successful execution. A new quantization, architecture, cache configuration or native runtime needs separate load, correctness and performance tests. CPU fallback is opt-in (`--fallback-mode cpu`) and reported; it must not be counted as a successful NPU run.

## Prerequisites

- Windows ARM64 on appropriate Snapdragon hardware, with supported OEM/Windows NPU, GPU and FastRPC drivers. Use Windows Update or the device manufacturer's supported route for hardware-driver updates.
- **Native ARM64 CPython 3.11 or newer**, including `venv` and `pip`. Use an ARM64 Windows build from [python.org](https://www.python.org/downloads/windows/). An x64 Python running under emulation cannot load the ARM64 native libraries. Installation detects the interpreter and accepts an explicit `-PythonPath` when needed.
- Windows PowerShell 5.1 or later, and Git if cloning the repository.
- Osaurus for Windows with the custom-provider/CLI configuration support used by these scripts. The observed application version was 1.0.3; this is not a promise that every application release has the same CLI/schema.
- A separately acquired, compatible GGUF already on disk. Respect its publisher's license and access conditions. The commands below reuse existing Osaurus downloads and download no weights.
- Trusted signed HTP/DSP library provisioning for NPU execution. This is distinct from downloading the runtime or updating the hardware driver.

The historical successful machine already had a registered production-signed GGML HTP package. A valid ZIP hash, catalog signature or driver inventory does not prove that a fresh machine can execute the NPU payload. The ordinary installer does not provision machine-wide packages. See [signed provisioning and support limits](docs/support-and-troubleshooting.md) before using the separate elevated `scripts/Install-DspPackage.ps1` step; a clean-machine provisioning path has not been established by the historical benchmarks. No test-signing, Secure Boot changes or trust-store modifications are part of this setup.

The 16 GB test machine was under severe memory pressure during larger-model and two-context tests. Close optional apps yourself when appropriate, save work, and test one profile at a time. The installer/server does not automatically kill apps or disable services. Cleanup of other applications requires your own deliberate authorization and choices.

## Quick start with existing models

Run these commands in a regular PowerShell terminal. Each script invocation uses `-ExecutionPolicy Bypass` for that new PowerShell process only; it does not change the machine or user execution policy. Keep the source checkout outside the installation directory.

```powershell
git clone https://github.com/qayshp/osaurus-arm64-assist.git
Set-Location .\osaurus-arm64-assist

$AssistRoot = Join-Path $env:LOCALAPPDATA 'OsaurusArm64Assist'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install.ps1 -RootPath $AssistRoot

$AssistPython = Join-Path $AssistRoot '.venv\Scripts\python.exe'
$AssistConfig = Join-Path $AssistRoot 'config.json'
& $AssistPython -m osaurus_arm64_assist models discover
```

The installer creates a private Python environment and installation-specific API key, obtains the pinned official GenieX archive, and verifies the archive and native-file hashes. It does not download models, install drivers, register startup tasks or change Windows power settings. If Python detection fails, rerun `Install.ps1` with `-PythonPath` pointing to your native ARM64 `python.exe`.

Use the exact `osaurus_model` filename stem printed by discovery. These examples match the tested files. Duplicate stems from different publishers are rejected; use `--path` with the explicit GGUF path, or `--models-dir` to narrow discovery.

```powershell
# Raptor: NPU prompt processing followed by CPU generation.
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/hybrid --mode hybrid --osaurus-model Raptor-0.7-4B-Q6_K --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --power-mode balanced --repack

# Optional, memory-hungry Ornith profile: retain the original Q4 weights.
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id ornith-q4/assist --mode cpu-experts --osaurus-model Ornith-1.5-35B-Q4_K_M --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --power-mode balanced --no-repack

& $AssistPython -m osaurus_arm64_assist models list --config $AssistConfig
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start.ps1 -RootPath $AssistRoot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Doctor.ps1 -RootPath $AssistRoot -Json
```

Start launches a hidden loopback server on `127.0.0.1:8082`. Models load on demand; starting the server alone does not load every profile or prove NPU execution. The server uses one admitted inference request at a time, releases the previous worker when switching profiles, and defaults to a five-minute idle unload. Historical results use a 2048-token context; raising it can materially increase memory and has not been validated here.

Before connecting Osaurus, request an explicit model smoke test. This command checks a tiny arithmetic answer plus experimental JSON-object and forced-tool behavior. It can be expensive because it actually loads and runs the selected profile.

```powershell
& $AssistPython -m osaurus_arm64_assist self-test --config $AssistConfig --model raptor/hybrid --json-object --tools
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Connect-Osaurus.ps1 -RootPath $AssistRoot
```

The connection script adds an owned OpenAI-compatible provider named **Osaurus ARM64 Assist**, stores its credential through the Osaurus secret mechanism, and preserves existing providers, agents and defaults. Select `raptor/hybrid` or another configured profile explicitly in Osaurus. The companion base URL is `http://127.0.0.1:8082/v1`; this is separate from Osaurus's own local API, commonly on port 1337.

To compare optional Raptor baselines, register the same file under separate IDs before starting or after stopping the server:

```powershell
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/cpu --mode cpu --osaurus-model Raptor-0.7-4B-Q6_K --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --repack
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/npu --mode npu --osaurus-model Raptor-0.7-4B-Q6_K --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --power-mode balanced --repack
```

For an Ornith CPU baseline, use the same Q4 stem with `--id ornith-q4/cpu --mode cpu --no-repack`. Keep every other setting matched. Restart the companion after editing profiles/configuration, and refresh the provider's model list in Osaurus. To update an existing profile deliberately, repeat `models add` with `--replace`; `models remove --config ... --id ...` removes only the profile, never the model file.

## Protocol and acceptance status

The implementation includes text chat, multi-turn conversations using embedded GGUF templates, SSE streaming, sampling controls, tool-call parsing/argument validation, architecture-specific forced-tool grammar for Spark/Qwen formats, and a grammar-backed `json_object` response mode. JSON Schema output, multimodal input and universal tool-template compatibility are outside this alpha's contract. Tools and JSON objects are buffered until validated rather than exposing partial native markup to clients.

Automated tests cover request/config validation, embedded Spark/Qwen history rendering, tool delimiters inside JSON strings, schema validation, Unicode streaming, HTTP errors/authentication, serial request admission, worker lifecycle, runtime integrity and model-preserving registry/backup operations. These tests use synthetic files and mocked native workers. **They do not establish physical NPU execution or full Osaurus agent compatibility.** Hardware acceptance of this packaged companion is being tracked separately from the historical prototype evidence; review actual self-test results and execution metadata before relying on a profile.

Historical arithmetic and exact fact-extraction answers were checked. Free-form replies/summaries were timed, not comprehensively quality-graded. Long-term stability, large contexts, sleep/resume, thermal behavior and energy per answer remain unproven. See [support and troubleshooting](docs/support-and-troubleshooting.md).

## Stop, back up, reinstall and remove

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Status.ps1 -RootPath $AssistRoot
& $AssistPython -m osaurus_arm64_assist unload --config $AssistConfig
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop.ps1 -RootPath $AssistRoot

$AssistBackup = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'osaurus-arm64-assist-settings.json'
& $AssistPython -m osaurus_arm64_assist config export --config $AssistConfig --output $AssistBackup
```

The settings backup omits the API key, but includes local model paths and settings; treat it as local configuration rather than public benchmark evidence. Re-running `Install.ps1` against a stopped, owned installation preserves its configuration and model references. To restore after a clean installation, use `Install.ps1 -ConfigBackup $AssistBackup`; it keeps the new installation's API key and runtime location. Model weights remain external and must still exist at the referenced paths.

For removal, first select another model for any Osaurus agent/default that uses this provider. The disconnect guard refuses to rewrite those choices for you.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Disconnect-Osaurus.ps1 -RootPath $AssistRoot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Uninstall.ps1 -RootPath $AssistRoot -KeepConfig
```

`-KeepConfig` writes a tokenless backup outside the removed directory and refuses to overwrite an existing backup; supply a new `-BackupPath` if needed. Uninstall stops only identified companion processes, removes only its owned provider/credential and dedicated installation, and preserves model files, other applications, device drivers and signed DSP provisioning. It rejects model paths inside the installation directory so removing the companion cannot remove its registered weights. Optional machine-wide driver/DSP maintenance is a separate operation.

## Reproducibility and evidence

- [Benchmark tables](docs/benchmarks.md): favorable and unfavorable CPU/GPU/NPU results, startup/RAM costs and failed full-NPU attempts.
- [Workloads and timing methodology](docs/methodology.md): the exact synthetic Project Harbor document/questions, input/output token counts, caching differences and additive stage definitions.
- [Curated experiment history](docs/experiment-history.md): original PR startup failure, official Windows/Surface update versus driver/runtime distinctions, and the progression from Qwen to Raptor and Ornith.
- [Machine-readable evidence](evidence/README.md): sanitized request/output records, CSV timing rows and a checksummed manifest; no raw chat history, local configuration, credentials or model weights.
- [Support boundary](docs/support-and-troubleshooting.md): tested architectures, mapping/commit failures, provisioning and memory limitations.

`runtime.lock.json` pins the official GenieX **0.11.0** Windows ARM64 archive and every required native-file SHA-256. Its llama.cpp core revision is `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30`; the Python declarations and packaged native allowlist are tied to it. `requirements.lock` pins Python distributions and hashes. These are reproducibility pins, not a claim that the versions are the newest available. Do not mix arbitrary replacement DLLs or DSP payloads into the bundle.

The originating [llama.cpp PR 29052](https://github.com/ggml-org/llama.cpp/pull/29052) provides a Windows ARM64 build/release path for an existing Hexagon backend. It does not configure Osaurus or implement this companion's phase-selection policy. The exact original runtime failed during startup on the tested machine; later GenieX-based success is recorded separately.

## Contributing and upstream coordination

This repository is intended for community experiments, reproducible reports and a maintainable external provider. Start with [CONTRIBUTING.md](CONTRIBUTING.md), retain slower/failed cases in benchmark submissions, and redact local data before sharing logs. CI checks Python behavior, syntax, PowerShell parsing and public evidence integrity; CI has no physical NPU.

The public [Osaurus repository](https://github.com/osaurus-ai/osaurus) is the MIT-licensed macOS implementation. Public documentation describes Windows as a separate implementation, but a usable public Windows checkout was not located during the November 15 research. That does not establish a permanent source-release policy or an official future release promise. See [source status and exact contribution routes](docs/upstream.md), including the official community forum, public documentation opportunities and a separate source-policy clarification draft.

The [native integration discussion outline](docs/native-integration-proposal.md) proposes explicit profiles, real context/memory metadata and clean lifecycle behavior. It and the source-policy question are local drafts; this project has not posted them upstream or claimed maintainer agreement. Native Windows app changes need the appropriate maintainers and contribution process.

Original companion code/documentation use [MIT](LICENSE). Downloaded runtimes, dependencies, embedded template fixtures and model weights retain their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
