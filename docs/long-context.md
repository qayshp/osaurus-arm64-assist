# Raptor hybrid beyond 2K

The former 2048-token cap was the companion's validation policy. Raptor's model metadata and the native runtime support larger allocations, but that alone does not establish correct NPU inference or enough memory on a 16 GB laptop. Version 0.1.4 uses **8192 tokens on Auto** for `spark2_5` Q6_K hybrid and accepts an explicit source context up to **16384**. Other native strategies remain at 2048.

Set **16384 in the Raptor local-model context setting in Osaurus** to use 16K. Leave it on Auto for 8K. No companion profile registration or second context setting is required. Above-ceiling explicit contexts use Osaurus's original engine rather than being clamped silently. Fixed benchmark aliases keep their configured values.

## What was tested

All documents and conversation histories are synthetic. A deterministic shipping document places separate eight-digit approval codes at its beginning, middle and end. The conversation version puts the first code in an early user turn and adds many intervening turns. The requested answer is a JSON object with the three exact codes. Seed 42 and complete synthetic requests/answers are retained in the receipts. These are extraction/retention checks, not a comprehensive model-quality evaluation.

| Native context and load policy | Actual input tokens | Output tokens | Quality and execution checks |
|---|---:|---:|---|
| [4K hybrid, repack on](../evidence/raptor-context-4k.json) | 3141 / 3232 | 39 | 2/2 passed |
| [8K hybrid, repack on; CPU control](../evidence/raptor-context-8k-repack.json) | 6216 / 6379 | 39 | Hybrid 3/4 passed; fourth hit the 1 GiB commit guard. CPU 4/4 passed |
| [8K hybrid, repack off](../evidence/raptor-context-8k.json) | 6216 / 6379 | 39 | 4/4 passed, including repeated requests |
| [16K hybrid, repack off](../evidence/raptor-context-16k.json) | 12366 / 12685 | 39 | 2/2 passed |

Passing also requires the actual configured native context, `actual_mode: hybrid`, no stock-engine fallback, positive serialized state bytes and transfer time, and more than one generated token so continuation runs after the handoff. An allocated large context with a short prompt is insufficient. [Provenance](../evidence/raptor-context-provenance.json) records the setup and retained failures.

A separate [near-boundary capacity test](../evidence/raptor-context-16k-boundary.json) passed two measured runs with **16128 input and 32 generated tokens** in a 16384-token hybrid context. Those deterministic random-token runs exercise nearly the entire cache and CPU handoff; they do not establish answer quality. Their state transfers were about 675 MiB.

## Memory and speed tradeoff

The hybrid route holds NPU and CPU contexts and temporarily serializes populated KV state between them. Raptor uses a mixture of sliding-window and full attention. State grows with supplied history, and the model's much larger training-context metadata is not a promise of practical NPU capacity on this laptop.

CPU weight repacking adds another substantial allocation. At 8K with repacking enabled, one of four repeated hybrid requests crossed the configured **1 GiB available-commit floor**, returning HTTP 503. With repacking disabled, all four passed and the minimum sampled available commit was about **2.47 GiB**. The quantization and model file did not change. Memory samples describe the entire system, not isolated per-model RAM; background activity can change them.

For Raptor hybrid above 4096, the backend therefore selects `repack:false` when the source has no explicit repack preference. An explicit source `repack:true` there delegates to Osaurus to preserve the preference. This can reduce continuation speed: the resident 8K hybrid document request took **17.32 s**, and the conversation cases took **24.80 / 24.33 s**. The corresponding repacked CPU control took **131.37 s** for the resident document and **125.43 / 126.21 s** for conversation. Those are this specific short-output workload, with different load policies and variable system conditions, not a universal speed ratio.

The 8K hybrid cold document request took **36.55 s**, including **14.70 s** loading. The 16K cold document request took **50.31 s**, including **10.84 s** loading; its resident conversation request took **39.09 s**. At 16K the populated handoff state was about **543–554 MiB**. Full load and inference stages are retained, rather than mixing load time with resident response time.

Physical RAM remains tight: the near-boundary 16K run briefly sampled only about **4 MiB of available physical RAM**, despite roughly 2 GiB available commit. Paging and background activity can affect responsiveness. This is why 8K is the default and 16K is an explicit choice, not a comfortable-memory or stress-test certification.

## Boundaries

Every request currently clears KV and reprocesses the supplied history. Keeping weights resident avoids reloading them; it does not reuse the previous turn's prompt cache. More context permits longer conversations but still incurs repeated prefill and transfer work. Input, template, tool definitions and output must all fit the configured context.

The quality client uses JSON output, which the server validates before emitting text chunks. Its client first-text time therefore measures validated-object delivery. Engine first-token, prefill, transfer and continuation timings remain separately available. These checks do not measure energy, battery life, physical NPU utilization counters or long-duration stability.

Reproduce with `tools/context_acceptance.py --help` and explicitly fixed test profiles. Its `--dry-run` writes synthetic requests without HTTP. A CPU comparison uses the same model file/context; each load policy remains recorded. Optional exact-token performance tests are separate from text-answer checks. No old 2K benchmark files were changed.
