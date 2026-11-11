# Model and server build

This is the reference stack the appliance was shaped around, plus the builds the installer uses on other machines.

## Reference machine

Apple M4, 10 cores (4 performance, 6 efficiency), 24 GB unified memory. No serial number, OS build, or host name belong in this doc.

## Chat model

As of the 2026-11-09 audit the reference machine serves the IQ3_XS file below. The `mix_q3gud_im` file is kept beside it as the fallback, and the installer still picks `mix_q3gud_im` for 20 GB+ machines (that default is unchanged).

| Piece | Running on the reference machine |
| --- | --- |
| File on disk | `m.gguf`, 14483689408 bytes |
| Sidecar name | MiniCPM-V-4.7-35B-A3B (IQ3_XS) |
| Quant | IQ3_XS |
| Fallback beside it | `m.gguf.mix_q3gud_im`, 15503944896 bytes |
| Measured decode | about 53.7 tokens/s average over about 2.88M generated tokens, read from the server's `/metrics` counters (not a bench). Prompt about 382 tokens/s. Draft acceptance about 87%. |
| Expected sustained decode | about 40 tokens/s or better |

The table below is the published `mix_q3gud_im` build the installer downloads.

| Piece | Value |
| --- | --- |
| Family | MiniCPM-V 4.7 35B-A3B (about 3B active, vision and text) |
| File on disk | stored as `m.gguf` |
| Published name | `MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf` |
| Bytes | 15503944896 (same size as the OpenBMB file) |
| Quant | `mix_q3gud_im`: Q4_K_M mix, routed experts at Q3_K, importance matrix |
| Sidecar name | MiniCPM-V-4.7-35B-A3B (3-bit, mix_q3gud_im) |
| Weights | https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B |
| GGUF repo | https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B-gguf |
| Direct file | https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B-gguf/resolve/main/MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf |

Vision projector actually loaded: `mmproj-model-f16-4x.gguf` (910491648 bytes on the published repo).

https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B-gguf/resolve/main/mmproj-model-f16-4x.gguf

A second projector, `mmproj-model-f16.gguf`, was on disk and was not the one the server loaded.

Speculative draft file loaded: `dspark-MiniCPM-V-4_7-35B-A3B-Q4_K_M.gguf` (237004800 bytes). The launch scripts do not record a public URL for that draft file. Leave it unset unless you have your own draft GGUF.

## llama.cpp that served the vision model

| Piece | Value |
| --- | --- |
| Commit | f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb |
| Subject | server: fix swa checkpoint invalidation logic |
| Version string | 0.7.0-dev (build 1, commit f174553), `build_info` b1-f174553 |
| Compiler | Apple clang, Darwin arm64 |
| Libraries | libggml 0.27.0, libllama 0.7.0, libmtmd 0.7.0 |
| Metal | GGML_METAL=ON, GGML_METAL_EMBED_LIBRARY=ON |
| BLAS | GGML_BLAS=ON, GGML_BLAS_VENDOR=Apple |
| CUDA / Vulkan | OFF |
| Server | LLAMA_BUILD_SERVER=ON |
| Build type | Release |

Reproduce that Metal build from a llama.cpp checkout:

```sh
git checkout f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb
python3 scripts/build_llamacpp.py --source /path/to/llama.cpp --commit f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb --accel metal --target-os darwin
```

The helper prints the cmake line and, without `--dry-run`, runs it. On a Mac the flags are Metal, embedded metallib, and Apple BLAS.

## Server flags that were actually running

The process listened on 127.0.0.1 port 18080. A small web proxy listened on 127.0.0.1 port 8080 and forwarded chat to 18080.

```text
llama-server \
  -m <model.gguf> \
  --host 127.0.0.1 --port 18080 \
  -c 16384 -ngl 99 --jinja -np 1 -fa on --metrics \
  -ctk q8_0 -ctv q8_0 --n-cpu-moe 0 \
  --cache-ram 512 --prio 2 \
  --mmproj <mmproj-model-f16-4x.gguf> \
  --spec-type draft-dspark \
  -md <draft.gguf> -ngld 99 \
  --spec-draft-n-max 4 --spec-draft-p-min 0.5 \
  --spec-draft-sampling greedy
```

Context is 16384. GPU layers 99 (everything). Parallel slots 1. Flash attention on. K/V cache q8_0. MoE experts stay on the GPU (`--n-cpu-moe 0`). Cache RAM cap 512. Process priority 2.

Thread count, batch (`-b`), and micro-batch (`-ub`) were not set. The binary used its own defaults.

The public alias was `MiniCPM-V-4.7-35B-A3B`. A second alias was set so older clients kept a stable name. Put any extra alias in your own config. It is not required.

## Embedding server (separate process)

A second llama.cpp, tag v0.6.0, commit d81235049384534c167caea52b85a694f6103d14, served embeddings only:

```text
llama-server -m bge-small-en-v1.5-q8_0.gguf \
  --embeddings --pooling mean -ngl 99 -c 4096 -np 8 \
  -b 4096 -ub 512 -t 8 --host 127.0.0.1 --port 8097
```

The work-brain module calls that URL for vectors and mixes them with the full-text index. Search still works with the full-text index when the embed server is down. `hailports setup` writes an embed service that runs:

```
llama-server -m models/bge-small-en-v1.5-q8_0.gguf --embeddings --pooling mean -ngl 99 -c 4096 -np 8 -b 4096 -ub 512 -t 8 --host 127.0.0.1 --port 8097
```

The weight file is downloaded by you. It is not in this repo.

## What the installer downloads

Prebuilt llama.cpp release `b12078` from ggml-org, chosen by OS, CPU, and accelerator:

| Machine | Asset |
| --- | --- |
| macOS Apple silicon | llama-b12078-bin-macos-arm64.tar.gz |
| macOS Intel | llama-b12078-bin-macos-x64.tar.gz |
| Linux x64 CPU | llama-b12078-bin-ubuntu-x64.tar.gz |
| Linux arm64 CPU | llama-b12078-bin-ubuntu-arm64.tar.gz |
| Linux NVIDIA | llama-b12078-bin-ubuntu-cuda-12.8-x64.tar.gz |
| Linux AMD ROCm | llama-b12078-bin-ubuntu-rocm-10.0-x64.tar.gz |
| Linux Vulkan | llama-b12078-bin-ubuntu-vulkan-x64.tar.gz (arm64 variant exists) |
| Windows CPU | llama-b12078-bin-win-cpu-x64.zip (arm64 exists) |
| Windows NVIDIA | llama-b12078-bin-win-cuda-12.4-x64.zip |
| Windows Vulkan | llama-b12078-bin-win-vulkan-x64.zip |
| Windows ROCm | llama-b12078-bin-win-rocm-10.0-x64.zip |

Those Ubuntu builds are what Debian, Fedora, and Arch try first. If the dynamic linker refuses the binary, build from source with `scripts/build_llamacpp.py`.

Model pick by memory:

| Memory | Choice |
| --- | --- |
| under 8 GB | Qwen2.5 1.5B Instruct Q4_K_M |
| 8 GB up to 20 GB | Qwen2.5 7B Instruct Q4_K_M |
| 20 GB or more, Metal or CUDA | MiniCPM-V 4.7 mix_q3gud_im plus the 4x projector |
| `--model tiny` | stories260K, for a plumbing test |

Pass `--model tiny|small|balanced|vision` to override.

## Required weight files after a reboot

`llama-server` exits if a file named on the command line is missing. Service
units use KeepAlive / Restart=on-failure, so a missing vision projector turns
into a tight crash loop on the next start.

A process that already loaded the projector can keep serving after that file
is deleted from disk. The next start or reboot is when the gap shows up.

The model service command runs `python -m hailports_setup.weight_preflight`
first. The wrapper checks `-m`, `--mmproj`, and draft (`-md`) paths. A
missing file is copied from `weight-backup/` when a same-named spare is
there. For the published MiniCPM-V 4.7 4x projector it can also fetch the
URL above (910491648 bytes) by the names `mmproj.gguf` or
`mmproj-model-f16-4x.gguf`. If a required file is still missing, the wrapper
waits and logs instead of starting `llama-server`.

Keep a spare copy of every required weight file in `weight-backup/` (or pass
`--backup-dir` to point at another volume). Point `--backup-dir` at a path
you control; do not rely on the previous process still having the mapping.


## API mode

`hailports setup` asks for the mode first. API mode does not build llama.cpp and does not download a GGUF. Hybrid mode still uses the local build above for reading, summarizing, retrieval, drafting, and compression, and calls the configured cloud provider only when the routing policy says the job is hard, unsure, or too long.


## Output quality (2026-11-10)

The decode rate in the table above is the server. A fast server still writes a bad note if the note shape or the facts in front of it are wrong.

- Instruct sampling, only when the caller does not set a temperature: 0.7, top_p 0.95, top_k 100, min_p 0, repeat penalty 1.03. That is the model's instruct card.
- Overnight notes use a grammar whose lines are about a sentence long (about 140 to 160 characters, 8 result lines). A 680-character line made the model repeat itself, and those notes were discarded. That path uses repeat penalty 1.05, and a note that starts repeating is cut off.
- A work question is answered from the local index first. If the index has no excerpt, the reply says so instead of guessing. Draft jobs that already carry the long stable prefix are not rewritten, so their prompt cache stays valid.
- A public deep-research ask fetches more pages than a normal question. A work question stays on the local index.
- PDF text covers 30 pages. Plain text, markdown, csv, and html are read as text. Pictures go to the vision projector.
- One decode slot is the default. A second slot is allowed only when the memory level is 40 or higher at server start. Two slots share one GPU, so each stream is slower. Do not open the second slot just to look busy.
- Record the change with `scripts/stack-change.sh` before the session ends. See `docs/SOP-STACK-CHANGES.md`.

Work questions are embedded and the nearest notes are retrieved by meaning. The model then picks the passage that answers the question and writes from that passage. A keyword list is not the lookup.
A work answer may use up to about 8k tokens of the 16k window. The model fact-checks a grounded reply, and a clean miss can go to a premium model. The 32k window is armed and applies on the next start only when the memory level is 40 or higher.
