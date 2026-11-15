# From PR investigation to a companion endpoint

This is a curated engineering record of experiments performed on November 14–15, 2026. It deliberately omits the private conversation, local configuration, credentials, user paths, process inventories and model binaries. [The benchmark report](benchmarks.md) retains favorable and unfavorable measurements. [Evidence provenance](../evidence/README.md) describes the public projections.

## The original objective

The investigation began with [llama.cpp PR 29052](https://github.com/ggml-org/llama.cpp/pull/29052): could it make the Snapdragon NPU usable in the Windows toolchain behind Osaurus? The checked-out PR head was `38abb0d8b8fa38dd58c2ce4d1de5efdd27f7e95b`, with merge base `972d2313bc0bf0a45f634f77d95c9fb03aeab12c`. Those pins describe the reviewed revision, not a promise about the PR's current head or status.

The reviewed diff primarily added Windows ARM64 Hexagon release packaging and signing documentation around an existing GGML Hexagon backend. It also changed a CMake compiler probe to build static libraries. It did not create a new inference engine or an Osaurus integration. The original backend-only CI artifact lacked the assembled host toolset and signed catalog; the assembled fork release supplied the host tools but still required trusted DSP provisioning. Device enumeration alone did not establish successful NPU inference.

The exact PR-era executable eventually failed at DSP session startup with `0x8000040e`. It was not used as the successful benchmark engine. That failure is retained as part of the result rather than presented as an NPU performance measurement.

## Official updates and signed runtime provisioning

The laptop received the official Surface System Hardware Update `200.0.53.0` through Windows Update. The observed post-update stack included NPU driver `30.0.222.0`, Adreno `31.0.148.0`, FastRPC `1.0.4338.2200`, Compute DSP `2.0.4478.2200` and UEFI `175.361.235`.

Three different layers must be distinguished:

1. **Device drivers/firmware**, supplied through the device manufacturer and Windows Update.
2. **A trusted signed catalog/INF for DSP payloads**, which Windows must accept for the HTP libraries to execute.
3. **The matched host and DSP runtime libraries** used by the inference program.

A production-signed GenieX 0.7 HTP package had been registered before the later successful tests. The matched GenieX 0.11 host/DSP bundle then worked on that already provisioned stack without another hardware-driver installer. The separate newer Qualcomm hardware-driver installer was not needed or installed. No boot settings, test-signing mode, Secure Boot configuration or certificate trust-store changes were used.

This history is **not** proof that downloading a runtime ZIP into a user folder is sufficient on a fresh machine. The companion's user-level installation, trusted DSP registration, and device-driver state are separate concerns. A doctor/smoke test must establish actual execution. Do not assume that arbitrary host and DSP versions can be mixed.

Working runtime source: [Qualcomm GenieX 0.11.0 release](https://github.com/qualcomm/GenieX/releases/tag/v0.11.0), with native llama.cpp core `93cf736a8d1d46aa922e27eb64aa712bcd5ebc30`. The native libraries were kept together. The companion's lock file pins its reproducible download; no runtime binaries or model weights are redistributed in this repository.

## Bring-up and original Osaurus connection

A small Qwen3-0.6B Q4_0 model ran successfully on the NPU using the matched signed runtime. Sequential CPU, Adreno OpenCL and Hexagon measurements showed faster NPU prompt processing but faster CPU token generation. The tiny model was a bring-up control, not evidence that all useful model sizes have the same tradeoff.

An explicit custom OpenAI-compatible provider connected a loopback GenieX server to Osaurus. Chat, streaming, bounded arithmetic, a forced simple tool call and a synthetic tool-result round trip were exercised. Strict JSON formatting was not consistently enforced. That protocol validation does not certify all models, tools, schemas, templates or future companion versions.

The installed Windows Osaurus local engine used CPU. Adding an external provider did not automatically accelerate ordinary local-model execution or change model defaults. The companion project continues to use this explicit provider boundary; it does not patch the Windows app or make claims about its future source-code licensing.

## Models that were useful locally

Raptor 0.7 4B Q6_K already existed in the local Osaurus library. Testing that actual model showed a substantial NPU advantage for reading a longer prompt and returning a short answer. Adreno was competitive in some Raptor workloads. CPU remained a strong decoder, and excessive CPU thread counts were not automatically beneficial.

The original Ornith 1.5 35B-A3B Q4_K_M file also already existed locally and was known to run on CPU. Its 20.22 GiB file exceeds physical RAM. A mapped CPU path could use it without requiring the whole file to remain resident. CPU repacking had to be disabled in the controlled loading path to avoid an extra large private allocation. A CPU-expert/OpenCL hybrid worked but was slower in those tests. Full GPU loading failed, and a full-NPU probe was stopped under commit pressure. These results do not establish a universal hardware prohibition on larger-than-RAM models.

A smaller community Q2_K quantization of the same 35B family was found, downloaded through Osaurus and checksum-verified. The file was about 40% smaller. It still did not complete full NPU loading after host-buffer, mapping-size and virtual-session experiments. The measured NPU result instead retained the expert operations on CPU. Q2 uses coarser weight precision, so it was not accepted as evidence of preserving Q4 answer quality.

## Assistance with unchanged weights

The next experiment retained the original Q4 file and explicitly placed the large expert tensors on CPU while running supported remaining work on NPU. Memory mapping, disabled CPU repacking and explicit small-context/batch settings made this path runnable. The direct public llama.cpp C API exposed loading controls that were not available through the stock GenieX Windows model-config wrapper. No native library patch or compiler installation was needed.

That Q4 hybrid improved exact document extraction but regressed fixed-length generation. This justified testing explicit choices rather than making assistance the default.

Raptor's later prototype used a different division: NPU prompt processing followed by a single saved-state transfer to a CPU context. It preserved NPU-like first-text time and CPU-like steady decoding in the warmed fixed test. The two-context implementation increased startup time and memory demand sharply. Correct arithmetic and nine exact fact extractions supported the bounded pipeline check; they did not prove broad quality equivalence.

## What is and is not shipped from this investigation

The repository documents the complete progression above, publishes sanitized numerical evidence and exact synthetic workloads, and provides a separate companion implementation. Historical reports are not acceptance tests for the companion. The current implementation, its supported modes, automated checks and actual integration smoke results must be inspected separately.

The app's default behavior was preserved during the historical tests. Optional apps were closed manually to reduce interference; the Windows AI workload service remained present, with intermittent NPU activity reported. The pagefile stayed automatically managed. No general process-killing, service-disabling, driver-upgrade or boot-policy recipe is implied for community users.

No upstream PR, issue comment or forum post was made as part of the historical work. Native Osaurus support requires its maintainers' agreement and implementation. Contributions to llama.cpp or GenieX must follow those projects' current contribution rules, independently of this community repository.
