# Osaurus ARM64 Assist

An experimental community companion for selecting CPU, Adreno GPU and Qualcomm Hexagon NPU inference profiles in Osaurus on Windows ARM64.

**Alpha software.** Acceleration experiments were performed on one Surface Laptop 7 with a Snapdragon X Plus, Hexagon v73 and 16 GB of RAM, using a 2048-token context. This is a Snapdragon/Hexagon project; Intel and AMD NPUs are unsupported. Other Snapdragon generations and model architectures need their own validation. It is not an official Osaurus, Qualcomm, Microsoft or llama.cpp component.

The companion exposes a local OpenAI-compatible provider. **0.1.3-alpha.1** automatically follows Osaurus's completed local model inventory and supported model settings. Download or configure a model in Osaurus, then select its companion entry; no per-model registration or second weight copy is required. Known compatible profiles can use Hexagon assistance. Other chat models, or preferences the companion cannot honor, use Osaurus's original engine with the actual route reported. Embedding, speech, adapter and projector assets are excluded from this text-chat catalog.

This repository contains source, setup scripts and sanitized experimental evidence; it contains no model weights or native runtime binaries. See [model and settings sharing](docs/model-sharing.md) for compatibility, context, offline behavior and fixed benchmark profiles. The [new live acceptance receipts](docs/companion-acceptance.md#automatic-model-and-settings-acceptance) include working inference, settings changes and failed short-budget Qwen answer checks. The [release listing](https://github.com/qayshp/osaurus-arm64-assist/releases) identifies published builds.

Companion 0.1.2 adds automatic lifecycle management: the normal installer enables a small hidden watcher for your Windows sign-in. It starts the companion server when you open the official Store version of Osaurus and stops it after Osaurus actually quits. Closing to the tray keeps Osaurus running. Models still load only when you request inference, and their idle interval follows your existing Osaurus setting.

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
| `osaurus` | Delegate to the source model in Osaurus's original local engine | Preserves unsupported model/settings behavior; responses report `actual_mode: osaurus-native` |

The same GGUF can have multiple profiles without copying its weights. Discovery and profile construction do **not** prove successful execution. A new quantization, architecture, cache configuration or native runtime needs separate tests. The automatic native policy recognizes Raptor `spark2_5` Q6_K and Ornith `qwen35moe` Q4_K_M/Q2_K; it is not a general NPU compatibility claim. Stock-engine delegation and opt-in CPU fallback are reported and must not be counted as successful NPU runs.

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
```

The installer creates a private Python environment and installation-specific API key, obtains the pinned official GenieX archive, and verifies the archive and native-file hashes. Starting with 0.1.2, fresh setup also enables a new, owned shortcut in your **per-user Windows Startup folder**, launching the hidden lifecycle watcher at sign-in. It preserves your existing Osaurus shortcuts and Osaurus startup preference; updates respect a watcher entry you disabled in Windows Startup Apps. This user-level setup needs no administrator access; model downloads, driver provisioning and Windows power settings remain separate. If Python detection fails, rerun `Install.ps1` with `-PythonPath` pointing to your native ARM64 `python.exe`. Advanced manual-only installations can use `-NoAutoStart`.

Open Osaurus and keep it running during initial connection. Completed chat models already in Osaurus appear automatically after the next refresh, normally within about five seconds. Download future models and change their preferences in Osaurus. A model is not loaded merely because it appears in the companion catalog.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start.ps1 -RootPath $AssistRoot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Connect-Osaurus.ps1 -RootPath $AssistRoot
& $AssistPython -m osaurus_arm64_assist models list --config $AssistConfig
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Status.ps1 -RootPath $AssistRoot -Json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Doctor.ps1 -RootPath $AssistRoot -Json
```

During initial setup, `Start.ps1` can also launch the hidden loopback server on `127.0.0.1:8082` for testing. In everyday use, the watcher manages it with Osaurus. Models load on demand; starting the server alone does not load every profile or prove NPU execution. The server uses one admitted inference request at a time and releases the previous worker when switching profiles. By default it follows the **Idle unload** preference already selected in **Osaurus → Settings → Models → Local engine**. Historical results use a 2048-token context; raising it can materially increase memory and has not been validated here.

The connection script adds an owned OpenAI-compatible provider named **NPU Hybrid**, stores its credential through the Osaurus secret mechanism, and preserves existing providers, agents and defaults. The short label fits Osaurus's provider badge; it names the companion, whose reported execution route can also be CPU, GPU or the original engine. Select the desired model under that provider in Osaurus. Selecting the original local model continues to use Osaurus's original engine. The companion base URL is `http://127.0.0.1:8082/v1`; this is separate from Osaurus's own local API, commonly on port 1337.

Upgrades reconnect an existing owned registration automatically. The former default label, **Osaurus ARM64 Assist**, becomes **NPU Hybrid** in place: the provider ID, credential reference, model IDs and agent selections remain the same. A custom label selected with `Connect-Osaurus.ps1 -ProviderName 'Your label'` is preserved. To apply only the connection/migration step after updating the installed scripts, rerun `Connect-Osaurus.ps1 -RootPath $AssistRoot`; see [provider labels and recovery](docs/provider-label.md).

For an optional explicit smoke test, copy a profile ID from `models list`. This command checks a tiny arithmetic answer plus experimental JSON-object and forced-tool behavior. It actually loads/runs the selected profile and can be expensive; the automatic catalog alone makes no execution claim.

```powershell
$AssistModel = 'your-selected-profile-id'
& $AssistPython -m osaurus_arm64_assist self-test --config $AssistConfig --model $AssistModel --json-object --tools
```

## Model settings follow Osaurus

The companion inherits supported typed preferences and resolved model sampling defaults, then applies explicit request sampling overrides. Known Raptor Q6_K profiles choose phase handoff; known Ornith Q4_K_M/Q2_K profiles choose CPU-expert assistance. Other chat models and unsupported explicit settings delegate to the source model in Osaurus, with `actual_mode: osaurus-native` and a fallback reason. Source discovery does not promise universal NPU compatibility.

For assisted profiles, context **Auto** currently means the tested 2048 tokens. A large stock-engine plan is not copied into an NPU allocation. An explicit source context larger than 2048 uses the stock engine. Default caching/scheduling policies also have documented backend adaptations; the companion does not claim identical internals or cross-request KV reuse. See [the setting-by-setting contract](docs/model-sharing.md), including offline diagnostics and fixed opt-outs.

## Optional fixed benchmark profiles

For reproducible CPU/GPU/NPU comparisons, deliberately register fixed aliases that do not follow later Osaurus settings edits. These examples reuse the tested Raptor file and do not copy weights. Use the exact `osaurus_model` filename stem printed by `models discover`; duplicate stems are rejected, so use `--path` or `--models-dir` to disambiguate.

```powershell
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/cpu --mode cpu --osaurus-model Raptor-0.7-4B-Q6_K --settings-source fixed --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --repack
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/npu --mode npu --osaurus-model Raptor-0.7-4B-Q6_K --settings-source fixed --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --power-mode balanced --repack
& $AssistPython -m osaurus_arm64_assist models add --config $AssistConfig --id raptor/hybrid-fixed --mode hybrid --osaurus-model Raptor-0.7-4B-Q6_K --settings-source fixed --context 2048 --threads 6 --batch-threads 10 --batch 512 --microbatch 512 --power-mode balanced --repack
```

For original Ornith Q4, use `--osaurus-model Ornith-1.5-35B-Q4_K_M --settings-source fixed --no-repack`, with separate `--mode cpu` and `--mode cpu-experts` IDs and all other settings matched. These larger-model runs have substantial memory costs. Restart the companion after editing manual profiles/configuration. To update an existing profile deliberately, repeat `models add` with `--replace`; `models remove --config ... --id ...` removes only the profile, never the model file. Advanced global `"models_source": "fixed"` disables automatic catalog/settings following independently of idle/lifecycle behavior.

## Automatic startup and shutdown with Osaurus

With the default installation, open Osaurus normally and select a companion profile. The watcher recognizes the official Store app in the same Windows user session, starts the server, and refreshes provider discovery when it is ready. Minimizing Osaurus or closing its window to the system tray keeps the app and companion running. A true app exit stops the companion server and model workers after a short confirmation delay.

A small watcher remains in your signed-in session, waiting for Osaurus to open again. It has a memory cost; large model weights and NPU contexts are allocated only when inference is requested. Your existing Osaurus launch-at-login choice is preserved. See [automatic lifecycle](docs/automatic-lifecycle.md) for startup ownership, diagnostics, disabling the feature and manual operation.

## Idle unload follows Osaurus

Starting with companion 0.1.1, set the idle interval in **Osaurus → Settings → Models → Local engine → Idle unload**. The companion reads that preference automatically, including **Never unload**; no second idle setting is needed. Changes are checked about every five seconds and take effect after a successful refresh without restarting the companion. Existing companion configurations that omit `idle_source` also follow Osaurus after upgrading.

The companion tracks activity for its own model worker. Inference keeps that worker active; health checks, model discovery and preference polling leave its idle clock unchanged. Idle expiry waits for inference to finish. A previously read value is cached for the same Osaurus endpoint, including a zero/never value across companion restarts. If no valid value is available, the default fallback is 300 seconds. `Status.ps1 -Json` reports whether the effective policy is live, cached, fallback or a deliberate fixed override.

See [idle policy and diagnostics](docs/idle-policy.md) for offline behavior, alternate Osaurus profiles and the advanced fixed override. Automatic following uses an internal Windows app API observed on Osaurus 1.0.3; it validates the response and reports a fallback if that interface changes. The packaged 0.1.0 acceptance records below retain their original version and behavior.

## Protocol and acceptance status

The native implementation includes text chat, multi-turn conversations using embedded GGUF templates, SSE streaming, sampling controls, tool-call parsing/argument validation, architecture-specific forced-tool grammar for Spark/Qwen formats, and a grammar-backed `json_object` response mode. JSON Schema output, multimodal native input and universal tool-template compatibility are outside the native alpha's contract. Tools and JSON objects are buffered until validated rather than exposing partial native markup to clients. Stock-engine delegation retains the source engine's own capabilities and limits; a proxied response is not proof that the native path implements the same feature.

Automated tests cover request/config validation, embedded Spark/Qwen history rendering, tool delimiters inside JSON strings, schema validation, Unicode streaming, HTTP errors/authentication, serial request admission, worker lifecycle, runtime integrity and model-preserving registry/backup operations. These tests use synthetic files and mocked native workers. **They do not establish physical NPU execution or full Osaurus agent compatibility.**

The packaged **0.1.0** companion was also exercised on the documented Snapdragon laptop. Raptor hybrid passed seven synthetic checks directly and the same seven through Osaurus 1.0.3, including a prompt longer than one native batch, conversation history, Unicode SSE, forced tool calls with a tool-result round trip, and JSON-object output. Ornith Q4 passed arithmetic and Unicode streaming in both CPU-expert/NPU and CPU-expert/Adreno modes. Clean removal and reinstall preserved the existing Osaurus configuration and model files, restored seven profiles, and rotated the installation credential. The small Qwen control's exact-answer failures are retained too. See [packaged companion acceptance](docs/companion-acceptance.md) for the receipts, timing boundaries and limits. Newer automatic model/settings acceptance is tracked separately; earlier passes do not certify that phase, every Osaurus agent or every model.

Historical arithmetic and exact fact-extraction answers were checked. Free-form replies/summaries were timed, not comprehensively quality-graded. Long-term stability, large contexts, sleep/resume, thermal behavior and energy per answer remain unproven. See [support and troubleshooting](docs/support-and-troubleshooting.md).

## Rerun the fixed-token comparison

With the companion running and the profile registered, the benchmark command reruns the historical synthetic workload: 512 direct input token IDs, 128 generated tokens, one warm-up, three measured repetitions and input seed 42. Input IDs use `random.Random(seed).randrange(32000)`, without a chat template or tokenization. Generation is greedy and ignores EOG stopping so each completed run performs the same requested token count. This is a throughput/latency workload, not a meaningful question or model-quality test.

```powershell
$AssistResult = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'raptor-hybrid-fixed.json'
& $AssistPython -m osaurus_arm64_assist benchmark --config $AssistConfig --model raptor/hybrid-fixed --output $AssistResult --prompt-tokens 512 --generated-tokens 128 --repetitions 3 --warmup 1 --seed 42
```

Repeat with the fixed `raptor/cpu` and `raptor/npu` aliases, using distinct output files and the same settings. Automatic source defaults can change batch, sampling and other settings, so use fixed aliases for a controlled comparison. Ornith profiles can use the same workload if there is sufficient memory. The authenticated `/admin/benchmark` endpoint holds one inference lock for the entire benchmark, validates context/repetition limits, and uses a separate worker command; the ordinary chat endpoint cannot inject direct token IDs.

Results include exact input/generated IDs, separate warm-up and measured runs, per-run usage/stages/actual execution mode, loading information, and measured-only medians. Engine response timing excludes model loading; `manager_seconds` includes that run's load/IPC overhead, and top-level `api_seconds` covers the complete warm-up-plus-measured invocation. Failed or incomplete generation returns an error instead of a throughput result. Configuration output omits model paths, runtime paths and the API key. Review results before publishing; new runs are separate evidence and need not reproduce historical numbers under different memory, cache, power or thermal conditions.

## Stop, back up, reinstall and remove

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Status.ps1 -RootPath $AssistRoot
& $AssistPython -m osaurus_arm64_assist unload --config $AssistConfig
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop.ps1 -RootPath $AssistRoot

$AssistBackup = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'osaurus-arm64-assist-settings.json'
& $AssistPython -m osaurus_arm64_assist config export --config $AssistConfig --output $AssistBackup
```

`Stop.ps1` stops the watcher before stopping the companion server and workers. The login shortcut remains available for future sign-ins. To disable automatic lifecycle management persistently, use `scripts/Disable-AutoStart.ps1 -RootPath $AssistRoot`; it removes the owned startup registration and stops the watcher and its managed server. The advanced `auto_start: false` choice is retained in configuration backups. Manual `Start.ps1` starts only the server; `Enable-AutoStart.ps1` resumes the watcher within the current sign-in session.

The settings backup omits the API key, but includes local model paths and settings; treat it as local configuration rather than public benchmark evidence. Re-running `Install.ps1` against a stopped, owned installation preserves its configuration and model references. To restore after a clean installation, use `Install.ps1 -ConfigBackup $AssistBackup`; it keeps the new installation's API key and runtime location. Model weights remain external and must still exist at the referenced paths.

For removal, first select another model for any Osaurus agent/default that uses this provider. The disconnect guard refuses to rewrite those choices for you.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Disconnect-Osaurus.ps1 -RootPath $AssistRoot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Uninstall.ps1 -RootPath $AssistRoot -KeepConfig
```

`-KeepConfig` writes a tokenless backup outside the removed directory and refuses to overwrite an existing backup; supply a new `-BackupPath` if needed. Uninstall stops only identified companion processes and removes its owned provider/credential, login shortcut and dedicated installation. It preserves model files, other startup entries, other applications, device drivers and signed DSP provisioning. It rejects model paths inside the installation directory so removing the companion cannot remove its registered weights. Optional machine-wide driver/DSP maintenance is a separate operation.

## Reproducibility and evidence

- [Benchmark tables](docs/benchmarks.md): favorable and unfavorable CPU/GPU/NPU results, startup/RAM costs and failed full-NPU attempts.
- [Workloads and timing methodology](docs/methodology.md): the exact synthetic Project Harbor document/questions, input/output token counts, caching differences and additive stage definitions.
- [Curated experiment history](docs/experiment-history.md): original PR startup failure, official Windows/Surface update versus driver/runtime distinctions, and the progression from Qwen to Raptor and Ornith.
- [Machine-readable evidence](evidence/README.md): sanitized request/output records, CSV timing rows and a checksummed manifest; no raw chat history, local configuration, credentials or model weights.
- [Support boundary](docs/support-and-troubleshooting.md): tested architectures, mapping/commit failures, provisioning and memory limitations.
- [Idle policy](docs/idle-policy.md): automatic Osaurus preference following, cached/offline behavior and status diagnostics.
- [Automatic lifecycle](docs/automatic-lifecycle.md): the owned login shortcut, app/tray/quit behavior and manual controls.
- [Models and settings](docs/model-sharing.md): automatic inventory, inherited preferences, stock-engine fallback, offline state and benchmark opt-outs.

`runtime.lock.json` pins the official GenieX **0.11.0** Windows ARM64 archive and every required native-file SHA-256. Its llama.cpp core revision is `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30`; the Python declarations and packaged native allowlist are tied to it. `requirements.lock` pins Python distributions and hashes. These are reproducibility pins, not a claim that the versions are the newest available. Do not mix arbitrary replacement DLLs or DSP payloads into the bundle.

The originating [llama.cpp PR 29052](https://github.com/ggml-org/llama.cpp/pull/29052) provides a Windows ARM64 build/release path for an existing Hexagon backend. It does not configure Osaurus or implement this companion's phase-selection policy. The exact original runtime failed during startup on the tested machine; later GenieX-based success is recorded separately.

## Contributing and upstream coordination

This repository is intended for community experiments, reproducible reports and a maintainable external provider. Start with [CONTRIBUTING.md](CONTRIBUTING.md), retain slower/failed cases in benchmark submissions, and redact local data before sharing logs. CI checks Python behavior, syntax, PowerShell parsing and public evidence integrity; CI has no physical NPU.

The public [Osaurus repository](https://github.com/osaurus-ai/osaurus) is the MIT-licensed macOS implementation. Public documentation describes Windows as a separate implementation, but a usable public Windows checkout was not located during the November 15 research. That does not establish a permanent source-release policy or an official future release promise. See [source status and exact contribution routes](docs/upstream.md), including the official community forum, public documentation opportunities and a separate source-policy clarification draft.

The [native integration discussion outline](docs/native-integration-proposal.md) proposes explicit profiles, real context/memory metadata and clean lifecycle behavior. It and the source-policy question are local drafts; this project has not posted them upstream or claimed maintainer agreement. Native Windows app changes need the appropriate maintainers and contribution process.

Original companion code/documentation use [MIT](LICENSE). Downloaded runtimes, dependencies, embedded template fixtures and model weights retain their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
