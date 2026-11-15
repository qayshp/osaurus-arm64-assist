# Support boundary and reproducibility

This is an experimental community companion for Windows ARM64 on Snapdragon. The historical acceleration evidence comes from one Snapdragon X Plus/Hexagon v73 Surface Laptop 7. It is not a universal NPU abstraction, an official Osaurus component, or proof of Intel/AMD NPU support.

## Execution choices

| Profile mode | Intended split | Historical evidence | Main tradeoff |
|---|---|---|---|
| `cpu` | CPU execution | All tested models | Often best sustained decode; large mapped models can page heavily |
| `npu` | Normal Hexagon layer offload with CPU host/fallback work | Small Qwen and Raptor | Fast prompt processing; decode can be slower; model operations/types must be supported |
| `hybrid` | NPU prompt/first token, one state copy, CPU continuation | Raptor 0.7 4B Q6_K, `spark2_5` | Two model/context objects, larger RAM/startup cost; state compatibility must be checked |
| `cpu-experts` | Routed expert math/tensors on CPU, supported remaining work on NPU | Ornith 1.5 35B-A3B Q4_K_M and Q2_K, `qwen35moe` | Can aid input-heavy work while slowing long generation |
| `gpu` | Normal Adreno OpenCL offload with CPU host/fallback work | Earlier Qwen and Raptor comparisons | Supported types and dispatch/memory costs matter; no universal advantage |
| `gpu-experts` | Expert tensors/operations on CPU, eligible remaining work on OpenCL | Earlier Ornith CPU/OpenCL experiments | The measured large-Q4 and Q2 cases were slower than CPU |

These describe the tested execution strategies, not permission to assume an arbitrary GGUF works in every mode. A new model, quantization, architecture, cache type or runtime revision needs its own load, correctness and performance tests. The benchmarked state handoff uses the same model weights, architecture and context/cache configuration in both contexts; it does not establish a portable on-disk session format between unrelated versions or devices.

Adreno OpenCL results are retained in [the benchmark record](benchmarks.md). A GPU-prefill/CPU-generation handoff was not established. Historical execution is separate from current companion acceptance, and GPU assistance must not be advertised merely because a DLL can enumerate an adapter.

## Installable software and machine prerequisites are separate

The user-level companion can be reinstalled independently of model files and device drivers. Follow the root README and checked-in lifecycle scripts for the actual commands and current command names. The server entry point is:

```powershell
python -m osaurus_arm64_assist serve --config <configuration.json> --host 127.0.0.1 --port 8082
```

The original Osaurus local API uses port 1337. A separately installed GenieX provider may use 8081. This companion defaults to 8082 so it need not replace either. Explicit profile names and provider model IDs let a user select assistance; merely downloading a GGUF into Osaurus does not activate the companion.

Reproducibility requires all of the following:

1. **Windows ARM64 and an appropriate Snapdragon NPU stack.** Apply official manufacturer/Windows updates through their normal supported route. A different Qualcomm hardware-driver installer is not an inference-runtime dependency to apply casually.
2. **Trusted signed DSP provisioning.** Catalog/INF registration may require administrator rights and is distinct from a per-user runtime folder. The historical success used a previously registered signed HTP package. Do not treat a ZIP checksum or valid catalog signature alone as proof that a fresh machine is provisioned to execute it.
3. **A matched native runtime.** Use the lock file and official release assets. Keep host stubs, GGML libraries, HTP interface and DSP payload together. Do not copy a newer `.so` or DLL into an older bundle because the name matches.
4. **An appropriate ARM64 Python/runtime environment.** Use the install script's prerequisite check and pinned dependencies. An x64 Python process is not a native ARM64 host for these DLLs.
5. **A separately acquired compatible model.** Models are not included here. Record the exact GGUF hash and follow the publisher's license and access conditions.
6. **An actual bounded NPU execution test.** Enumeration, signatures, successful package extraction, CI and API health are not substitutes for a model executing correctly on the physical NPU.

Do not use test-signing, trust-store modifications or Secure Boot changes as the community setup path. The historical successful path used production signing. No binary or model redistribution in this repository implies rights to redistribute every component in a downloaded vendor archive.

## When loading fails

- Confirm CPU mode can load the same GGUF with the chosen context and memory policy. A default repacking path can need a large extra private allocation.
- Verify the exact architecture and tensor quantizations are supported by the pinned backend. Smaller IQ quantizations are not automatically supported on Hexagon because Q2_K happens to be.
- Distinguish physical RAM, system commit, pagefile and NPU mapping address space. The system's DDR RAM is shared with CPU, GPU, NPU and applications; there is no independent extra Adreno VRAM pool to add to the laptop's RAM.
- Preserve the first concrete loader/driver error and classify the run as failed or incomplete. Do not turn a timeout or missing answer into a throughput result.
- A `fastrpc_mmap` failure can suggest a mapping constraint, but does not establish a universal hardware capacity limit. Follow the exact runtime's official documentation rather than applying unbounded virtual-session or mapping experiments automatically.
- Close optional apps manually when appropriate. Do not kill essential services, security tools, connectivity or another person's work to fit a model. The companion does not need a general service-disable recipe.

An existing provider or model can be left available while an experimental companion profile is tested. Use a distinct loopback endpoint and model name. A fallback should be explicit and observable, so an unnoticed CPU fallback is not reported as an NPU speed measurement.

## When assistance is slower

Compare complete responses at equal token counts, and show startup separately. NPU prefill can win while token generation loses. Small answers may not repay a state copy, and short prompts may not amortize accelerator setup. A larger context, another app, paging or thermal changes can alter the result.

For the handoff mode, measure both contexts' startup and memory. The Raptor prototype warmed to 9.48 s for a fixed request but took 21.96 s from backend initialization to its first tiny control answer. That is why a warm benchmark alone cannot justify enabling it automatically for every request or model.

## What clean removal should preserve

Uninstall only the companion's own registration, processes and installation directory. Verify process identity before stopping it and path containment before recursive deletion. Preserve user models, Osaurus conversations and settings, other providers, device drivers, signed DSP provisioning and files outside the selected installation root. Do not change machine startup or service configuration unless a user explicitly requests a separate managed-startup option.

If the user edited provider/configuration data after setup, removal should detect the change rather than blindly restoring an old global configuration. Reinstallation should use the lock file and a fresh owned environment, without requiring the original experiment workspace.

## What has not been established

Broad model quality, strict JSON/schema enforcement for every model, all tool-call formats, multimodal inputs, large contexts, sleep/resume, every Windows/Qualcomm driver combination, energy per response, long-duration stability and support on other NPU vendors require separate evidence. Consult the project's current tests and release notes for companion behavior; historical performance data alone is not an acceptance certificate.
