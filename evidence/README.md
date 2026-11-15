# Historical benchmark evidence

These files are sanitized projections of experiments conducted on November 14–15, 2026 on one Windows ARM64 Surface Laptop 7. They support the project's rationale, including slower cases and unsuccessful loads. They are not evidence that the current companion build passed a clean-machine install or that another device/model is supported.

The original raw results remain private/local. This public projection preserves selected numeric summaries, request timings, synthetic input/output text, token counts and model fingerprints. It omits local absolute paths, configuration exports, credentials, host identifiers, exact wall-clock timestamps, process inventories, private conversation, model weights and vendor binaries. Original artifact basenames in CSV/provenance fields are historical identifiers, not paths to a hidden public archive.

| File | Purpose |
|---|---|
| `environment.json` | Hardware/software observation and important controls/limitations |
| `model-identities.json` | Exact GGUF filenames, sizes and SHA-256 fingerprints |
| `workloads.json`, `project-harbor.txt` | Full synthetic document and exact tasks |
| `stage-timings.csv` | 127 recorded request/control/warm-up rows; blanks mean unrecorded stages |
| `load-observations.json` | Available single-start construction/control observations |
| `raptor-state-handoff.json`, `raptor-*-requests.json` | New CPU, NPU and NPU-prefill/CPU-decode experiment |
| `ornith-q4-assist.json`, `ornith-q4-*-requests.json` | New unchanged-Q4 CPU-expert/NPU experiment |
| `ornith-q2-comparison.json` | CPU, Adreno-assisted, NPU-assisted and failed full-NPU probes |
| `raptor-earlier-fixed.json`, `raptor-earlier-http.json` | Earlier CPU/OpenCL/NPU balanced/burst data and cache behavior |
| `ornith-q4-earlier-opencl.json` | Earlier CPU/OpenCL hybrid, failed full-device probes and cache behavior |
| `qwen-control.json` | Earlier tiny-model CPU/OpenCL/NPU bring-up controls |
| `manifest.json` | SHA-256 and size of the curated data files at publication |

All favorable and unfavorable modes were retained in the summaries. Failed/memory-guarded/timed-out probes do not supply valid inference throughput. An interrupted child with no exception string can still be an incomplete timeout; inspect its completion and parent-monitor fields.

For interpretation, read [benchmarks](../docs/benchmarks.md) and [methodology](../docs/methodology.md). Do not combine the older cached summaries with the newer fresh-context summaries or attribute Q4/Q2 ratios solely to quantization. Different wrappers, loading policies, sampling/default behavior and token-count conventions are recorded as limits.

New community measurements should include their own model/runtime hashes, hardware, settings, every tested mode, loading/first-use time, warm-up/repeat counts, token counts, complete response times, memory and failures. Do not upload real user prompts, local app configurations or unsanitized logs. CI can check structure and integrity; it cannot execute this NPU on an ordinary hosted x64 runner.
