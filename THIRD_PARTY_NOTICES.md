# Third-party notices

The companion's original code and documentation are licensed under the root MIT license. That license does not relicense separately downloaded runtime components, Python dependencies or model weights. This is not an official Osaurus, Qualcomm, Microsoft or llama.cpp distribution.

## llama.cpp / GGML

The companion's native declarations and state/decoding integration follow the public llama.cpp API at the pinned revision recorded in the runtime lock. The native runtime itself is obtained from its upstream release, not redistributed in this source repository.

Source: <https://github.com/ggml-org/llama.cpp>

```text
MIT License

Copyright (c) 2023-2026 The ggml authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Qualcomm GenieX

The reproducible runtime download uses an official pinned GenieX release. The source project uses BSD-3-Clause. Downloaded archives can include additional component notices, DSP payload conditions and runtime licenses; retain and consult those notices. A verified download is not a license to redistribute every embedded binary under this companion's MIT license.

Source: <https://github.com/qualcomm/GenieX>

```text
BSD 3-Clause License

Copyright (c) 2024-2026, Qualcomm Technologies, Inc. and/or its subsidiaries.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Python and templating dependencies

Python is a separate prerequisite under its [PSF licensing](https://docs.python.org/3/license.html). Jinja and MarkupSafe, when installed by this project's pinned dependency configuration, are separate BSD-3-Clause packages maintained by the Pallets projects: [Jinja license](https://github.com/pallets/jinja/blob/main/LICENSE.txt), [MarkupSafe license](https://github.com/pallets/markupsafe/blob/main/LICENSE.txt). Their package licenses remain included with installed distributions. No vendored copy is implied by listing these dependencies.

The separately installed fastjsonschema dependency is BSD-3-Clause licensed, copyright Michal Horejsek and contributors; its installed distribution retains its [upstream license](https://github.com/horejsek/python-fastjsonschema/blob/master/LICENSE). It validates model-produced tool arguments; it does not execute function tools.

## Model files and names

No GGUF weights are included. The model names, sizes and hashes in the evidence identify test inputs. Obtain models from their publishers and follow the specific model's license and access terms; this project neither grants model rights nor bypasses gated access. Generated sample outputs and synthetic prompts are published only as benchmark evidence.

The embedded chat-template metadata retained with the historical request evidence identifies the tested model input format and is used by compatibility tests. Publisher-authored template material remains subject to its original model/source terms rather than being relicensed as original companion code. Those metadata snapshots contain no weights.

- The [Raptor GGUF publisher](https://huggingface.co/OsaurusAI/Raptor-0.7-4B-GGUF) declares Apache-2.0. Its [LICENSE at revision f562cb0288f6385a9e34d214d1e19be5a1cec3de](https://huggingface.co/OsaurusAI/Raptor-0.7-4B-GGUF/blob/f562cb0288f6385a9e34d214d1e19be5a1cec3de/LICENSE) is retained in [docs/licenses/raptor-apache-2.0.txt](docs/licenses/raptor-apache-2.0.txt).
- The [Ornith GGUF publisher metadata](https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF) declares MIT. The historical template's existing attribution comments are retained. The related [Qwen3.5 source model](https://huggingface.co/Qwen/Qwen3.5-35B-A3B) declares Apache-2.0; its [LICENSE at revision 59d61f3ce65a6d9863b86d2e96597125219dc754](https://huggingface.co/Qwen/Qwen3.5-35B-A3B/blob/59d61f3ce65a6d9863b86d2e96597125219dc754/LICENSE) is retained in [docs/licenses/qwen-apache-2.0.txt](docs/licenses/qwen-apache-2.0.txt). These references identify publisher/upstream terms and do not claim to replace them with the companion license.

Osaurus's public macOS application license is separate from the Windows application's source/licensing status. See [the dated research](docs/upstream.md). Public documentation links are references; its content has not been copied wholesale under an assumed app license.
