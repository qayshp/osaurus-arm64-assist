# Osaurus source status and contribution routes

Checked against public/connected sources on **November 15, 2026**. This document is research and a suggested contribution route. No forum post, upstream issue, PR or comment was sent by this project during preparation.

## What is public and what remains unknown

The public [osaurus-ai/osaurus repository](https://github.com/osaurus-ai/osaurus) contains the macOS Swift application and is MIT licensed. Its [contribution guide](https://github.com/osaurus-ai/osaurus/blob/main/docs/CONTRIBUTING.md) describes Apple Silicon, macOS and Xcode development. That is a usable public contribution target for the Mac implementation; it is not the Windows C#/WinUI implementation.

The public [documentation repository README](https://github.com/osaurus-ai/osaurus-docs/blob/master/README.md#osaurus-for-windows) describes Windows as a separate implementation and references `osaurus-ai/winsaurus`. A usable public checkout of that repository was not located: the repository endpoint returned 404 to the connected GitHub reader and did not appear in the checked public organization listing. A 404 does not establish whether a repository is private, moved or absent, and does not establish a permanent source-release policy.

The [public Windows documentation baseline](https://github.com/osaurus-ai/osaurus-docs/blob/master/windows-baseline.json) references a Windows release branch/commit but still describes an older 0.1.9 baseline, reviewed November 3. The locally tested app was 1.0.3. Do not treat the documentation baseline as a current release inventory.

Public branding is ambiguous: the [About page](https://osaurus.ai/about) broadly describes an open-source/MIT Windows and Mac application, while the [download page](https://osaurus.ai/download) explicitly labels the Mac download open source and sends Windows users to the Store. The checked sources did not supply a public Windows implementation checkout or an official future source-release date. No checked official statement established that Windows must remain closed forever. A maintainer clarification is needed rather than inferring either a promise or a permanent refusal.

## Where to ask

The official [Windows announcement](https://osaurus.ai/blog/windows-beta) directs feedback to the community. The administrator's [Windows welcome topic](https://community.osaurus.ai/t/welcome-to-the-osaurus-for-windows-beta-read-this-first/17) distinguishes:

| Need | Official community route |
|---|---|
| A new native inference/backend feature | [Feature Requests](https://community.osaurus.ai/c/feature-requests/8) |
| Windows bugs or general Windows feedback | [Windows Beta](https://community.osaurus.ai/c/support/windows-beta/11), using the relevant feedback tag |
| Configuration/how-to questions | [Support](https://community.osaurus.ai/c/support/7), tagged for Windows |

The [Feature Requests introduction](https://community.osaurus.ai/t/about-the-feature-requests-category/13) asks for one idea per topic and adding a use case to an existing matching idea. Search before posting. A related existing topic is [Windows plugin/extension support with a versioned host API](https://community.osaurus.ai/t/windows-plugin-extension-support-with-a-versioned-host-api/38). It requests an extension contract; the checked thread was not an implementation commitment or an NPU roadmap.

The public Mac contribution guide also offers a [GitHub feature-request template](https://github.com/osaurus-ai/osaurus/issues/new?template=feature_request.yml), [Discussions](https://github.com/osaurus-ai/osaurus/discussions) and [Discord](https://discord.gg/osaurus). For a Windows-native engine change, the dedicated community forum is the clearest first route. Ask maintainers which code repository or supported adapter boundary they want before preparing a native app patch.

The [public Windows documentation directory](https://github.com/osaurus-ai/osaurus-docs/tree/master/windows) is a separate possible contribution target for tested setup documentation. Its licensing and contribution rules must be checked independently; this project links to it rather than copying its content under an assumed Mac-app license.

## Why the companion boundary is useful now

The [Windows quick start](https://docs.osaurus.ai/windows/quickstart) supports custom OpenAI-compatible providers. An explicit loopback companion can therefore be useful without modifying Windows Osaurus. Its [local engine documentation](https://docs.osaurus.ai/windows/models#the-engine) describes Auto choosing CUDA, Vulkan or CPU; it does not document an automatic Hexagon assistance policy.

The provider should advertise its **actual configured context**, such as 2048, rather than a model's much larger training limit. The [remote-provider metadata contract](https://docs.osaurus.ai/remote-providers#provider-metadata-and-multimodal-input) and [context-metadata issue 2970](https://github.com/osaurus-ai/osaurus/issues/2970) explain why positive metadata matters. The installed Windows schema and observed API behavior remain authoritative where Windows and Mac documentation differ.

The Windows CLI can merge a provider-only configuration without pruning unrelated sections. The [CLI reference](https://docs.osaurus.ai/windows/cli#osaurus-config) distinguishes ordinary merging from explicit pruning. Removal should inspect a fresh export and remove only the owned provider; it should not replay an old full configuration over later user changes. Executable backend installation is separate from an Osaurus content bundle, as the [Windows import reference](https://docs.osaurus.ai/windows/cli#osaurus-import) explains.

## Two locally drafted discussions

The [native integration proposal](native-integration-proposal.md) is an engineering outline for maintainers to discuss. It includes the warm speedups, slower cases, memory costs and startup regressions, with links to reproducible evidence. It has not been posted.

A separate source-policy question can ask:

> The public documentation describes the Windows app as a separate implementation and references winsaurus, but a usable public checkout was not located. What is the canonical source location and intended licensing/source-release policy for the Windows C#/WinUI app? How can outside contributors add or maintain an inference backend today: through an external provider, a supported host API, documentation, or a native app patch?

This is a request for clarification, not an assertion about a policy that has not been published.

## Library contributions and the originating PR

[llama.cpp PR 29052](https://github.com/ggml-org/llama.cpp/pull/29052) concerns a Windows ARM64 build/release path for the existing Hexagon backend. It does not select Osaurus profiles or implement per-request NPU-prefill/CPU-decode handoff. A signed, version-matched runtime and a higher-level inference policy are related but separate work.

After discussion, possible library work includes a stable manifest for pinned backend/ABI/runtime versions, exposing Windows loading/repack/tensor-placement controls in GenieX's public model configuration, or a reusable and tested phase-handoff interface in llama.cpp. Each belongs with its own maintainers and contribution rules. A community companion can supply a minimal reproducer and evidence without making an automated upstream submission or claiming that app maintainers have agreed to integrate it.
