# Discussion outline: Windows ARM64 inference profiles

Status: **local engineering draft, not posted upstream**. This is a proposal for discussion, not a claim that Osaurus has agreed to implement it. Use [the current source/channel research](upstream.md) to choose the appropriate venue and check for an existing request before posting.

## Problem and useful behavior

Windows ARM64 users can have a capable CPU, an integrated Adreno GPU and a Hexagon NPU sharing limited system RAM. The best device choice depends on model architecture, quantization, prompt length, output length, loading policy and whether the model is already warm. A single “use accelerator” preference does not communicate those tradeoffs.

A useful native or supported external-engine integration would expose explicit per-model execution profiles:

- CPU baseline.
- Normal NPU offload, with honest CPU host/fallback behavior.
- CPU expert tensors/operations with supported remaining work on NPU for validated MoE models.
- NPU prompt processing followed by one validated state transfer to CPU for continuation.
- GPU/OpenCL comparison or assistance profiles when separately validated; current evidence does not establish a GPU phase-handoff policy.

The user should be able to select these deliberately and see which path actually ran. A load failure or CPU fallback must be observable rather than silently counted as successful NPU acceleration.

## Evidence supporting an option, not a universal default

On one Surface Laptop 7 (X Plus, Hexagon v73, 16 GB), the warmed Raptor Q6_K handoff reduced a 512-input/128-output median from 13.21 s CPU and 11.45 s NPU to 9.48 s. First text remained near NPU speed and continuation near CPU speed. The same prototype took 13.58 s to runtime-ready, 21.96 s to its initial tiny control answer, and peaked at 10.37 GiB full working set. NPU-only remained slightly faster for the ten-token extraction.

With unchanged Ornith Q4 weights, CPU-expert/NPU assistance reduced exact extraction from 66.17 s to 48.96 s but made the controlled 128-token total worse: 55.95 s versus 45.36 s CPU. Earlier Adreno Raptor measurements were competitive with CPU; earlier Q4 OpenCL assistance was substantially slower. Full-NPU Q2 loading still failed despite its smaller file.

These favorable and unfavorable results are documented together in [benchmarks.md](benchmarks.md), with [exact workloads and timing boundaries](methodology.md). There is no measured battery advantage or broad model-quality certification. The historical tests used a 2048-token context and a sparse hardware matrix.

## Suggested integration contract

1. **Versioned engine manifest.** Name CPU architecture, device family, supported model/tensor types, native ABI/runtime pin, payload checksums and trusted signed-DSP prerequisites. Do not mix arbitrary host and DSP libraries.
2. **Per-model explicit profiles.** Keep full offload, expert assistance and phase handoff distinct. Expose the actual context limit, loading/repack policy, CPU threads, power mode and cache types needed to reproduce a profile.
3. **Shared-memory budget.** Show process working set, private/system commit headroom and loading peaks. Count both contexts for handoff. Do not treat a GPU-like device's unknown memory budget or each virtual NPU session as a new independent RAM pool.
4. **Measured readiness and response stages.** Separate model construction, first-use faults/warm-up, prompt processing, first text, handoff, continuation and complete response. Include startup and adverse results in recommendations.
5. **A robust lifecycle.** Cancellation, serial request admission where necessary, memory-pressure refusal, clean unload/idle timeout, process crash reporting, sleep/resume recovery and an explicit CPU fallback should be testable.
6. **Model/template capabilities.** Support actual conversation/system histories and only the tool/JSON/multimodal features that are implemented and verified. Advertise unsupported features honestly. A single-user benchmark renderer is not sufficient for a general agent.
7. **Reinstall and removal.** Install per-user where possible, preserve model files, avoid changing boot/certificate policy, and remove only owned providers/processes/files. Machine-wide signed-DSP or hardware-driver maintenance remains a separate supported operation.

## Questions for Osaurus maintainers

- Is a custom OpenAI-compatible companion the preferred supported boundary today?
- Would a versioned external-engine/manifest API be appropriate, or should a validated Hexagon runtime be managed natively by Windows Osaurus?
- Which Windows repository or contribution process should an outside backend contributor use?
- What capability and failure metadata should a companion expose so the app can present a clear per-model choice?
- Would maintainers accept tested Windows setup/benchmark documentation independently of native app changes?

The related [Windows extension-host request](https://community.osaurus.ai/t/windows-plugin-extension-support-with-a-versioned-host-api/38) asks for a broader stable host contract. Keep a source/licensing roadmap question separate from this inference feature, respecting the forum's one-idea-per-topic guidance.

## Work that can proceed independently

The community project can maintain a reproducible loopback endpoint, runtime lock, model profiles, protocol tests, sanitized benchmarks and clean lifecycle scripts. It can gather a larger hardware/model matrix and document regressions. Native Windows integration, distribution of an official NPU engine, automatic selection policy and the app's source-release policy require the Osaurus maintainers' participation.
