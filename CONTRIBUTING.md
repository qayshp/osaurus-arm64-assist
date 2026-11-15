# Contributing

Useful contributions include reproducible fixes, protocol/lifecycle tests, model profiles with real hardware evidence, clearer documentation and benchmark reports that retain regressions as well as wins.

This project is an experimental community companion, not an official Osaurus, Qualcomm or llama.cpp release. Changes here do not grant approval to submit elsewhere. Follow each upstream project's current instructions and maintainers' preferred contribution route; do not automatically open upstream PRs or post comments on someone else's behalf.

## Before changing behavior

Read [support limits](docs/support-and-troubleshooting.md), [benchmark methodology](docs/methodology.md) and the implementation. Explain the concrete trigger and expected behavior. Keep native ABI/runtime changes separate from unrelated server or UI changes so failures remain diagnosable.

For a runtime or model profile change, record the source and hashes, architecture/quantization, context/cache settings and signed-DSP prerequisites. Do not mix arbitrary native libraries. A fake backend or CI pass is useful for protocol tests but is not evidence of physical NPU execution.

## Verification

Run the repository's unit tests and compile check:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

Use the install/start/stop/status/doctor scripts from the current README for lifecycle checks. Validate removal and reinstallation in an owned temporary root before claiming cleanup is safe. Do not delete models, alter unrelated providers, change machine startup or stop unknown services to make a test pass.

Real acceleration reports must include [the benchmark fields](.github/ISSUE_TEMPLATE/benchmark.yml), exact token counts, complete response time and all relevant worse/failure cases. Show startup and peak memory alongside warmed speed. A correct extraction does not establish broad quality or battery efficiency.

## Publishing evidence

Use only synthetic or explicitly shareable prompts. Remove local paths, account identifiers, keys, app configuration, process command lines and private documents. Do not commit native runtime binaries, model weights, downloaded archives, virtual environments or large traces. Record upstream release links and checksums instead.

The checked-in historical evidence is a curated snapshot. If correcting it, identify the source field and preserve the original measurement context. Do not silently rewrite an old benchmark as though it tested a new companion release.

## Native Osaurus support

See [upstream research](docs/upstream.md) and the [discussion outline](docs/native-integration-proposal.md). Windows-native app changes need a maintainer-supported code/contribution path. This repository can independently improve the loopback companion and public documentation.
