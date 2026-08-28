# World Model × Inference / KV Cache Optimization

Session note for future Research Copilot runs when the user asks for world model inference optimization, especially to forward to AI infra friends.

## User intent

The user is not primarily asking for generic world model / robotics control papers. They specifically asked to focus on:

- world model inference-time optimization
- KV cache optimization for autoregressive video/world models
- long-horizon video/world-model serving efficiency
- relation to physical AI/world action models only when it affects inference/runtime design

They said they once learned inference optimization for world models and wanted more basic + current trend knowledge. Avoid drifting to unrelated "world model" matches such as quantum "physical world" or generic video generation.

## Query terms that worked

Use targeted terms such as:

- `autoregressive video diffusion world models KV cache compression`
- `world model inference optimization temporal cache compression sparse attention`
- `KV cache optimization video generation diffusion transformer physical AI`
- `constant memory KV cache video world model linear attention`
- `state-space video world model inference long context`

Avoid broad `world model physical` or `steering` queries; they pulled quantum physics / unrelated physical-science matches.

## High-signal papers / artifacts found

### TempCache — recommended anchor

**Fast Autoregressive Video Diffusion and World Models with Temporal Cache Compression and Sparse Attention**

- arXiv: https://arxiv.org/abs/2602.01801
- Accepted to ICML 2026 according to arXiv search snippet.
- Main idea: training-free attention framework for autoregressive video diffusion/world models.
- Diagnoses three redundancy sources:
  1. near-duplicate cached keys across frames → temporal KV cache compression
  2. slowly evolving queries/keys → sparse self-attention via ANN matching
  3. long-prompt cross-attention where few tokens matter per frame → frame-relevant token selection
- Claimed result from search snippet: 5–10× end-to-end speedup and nearly constant GPU memory over long rollouts.
- Why it matters: drop-in optimization for existing world-model/video-diffusion pipelines; constant-memory behavior is especially relevant to real-time robot/world-model rollouts.

### Forcing-KV

**Forcing-KV: Hybrid KV Cache Compression for Efficient Autoregressive Video Diffusion Models**

- arXiv HTML: https://arxiv.org/html/2605.09681
- Main idea: head-wise functional specialization.
- Static heads: transitions/intra-frame fidelity → structured static pruning.
- Dynamic heads: inter-frame motion/consistency → segment-wise similarity pruning.
- Claimed result from search snippet: >29 FPS on single H200 at 480P, 30% cache memory reduction, up to 2.82× at 1080P.
- Interpretation: orthogonal to TempCache. TempCache is content/temporal redundancy; Forcing-KV is attention-head-role redundancy. Mention that they may compose.

### FlowCache

**Flow Caching for Autoregressive Video Generation**

- arXiv HTML: https://arxiv.org/html/2602.10825
- Main idea: existing caching assumes uniform denoising across frames; AR video chunks differ. Introduces chunkwise caching plus joint importance–redundancy KV compression with fixed memory bounds.
- Claimed result from search snippet: 2.38× on MAGI-1, 6.7× on SkyReels-V2, negligible quality degradation.
- Interpretation: useful for deployment because fixed memory bounds improve predictability.

### SANA-Video / architecture direction

**SANA-Video: Efficient Video Generation with Block Linear Diffusion Transformer**

- Project: https://nvlabs.github.io/Sana/Video
- Main idea from search snippet: block-wise autoregressive generation with constant-memory state via block linear attention; NVFP4 deployment on RTX 5090.
- Claimed: 16× faster than comparable small video models, 5s 720p from 71s to 29s with NVFP4, training cost ~1% of MovieGen.
- Interpretation: architecture-level avoidance of KV blowup, complementary to training-free cache compression.

### Related trend reference

**Long-Context State-Space Video World Models** (ICCV 2025)

- Direction: state-space / linear-attention style approaches for very long rollouts, replacing or reducing full attention KV growth.

## Synthesis pattern for briefs

For this topic, do not send a bare link list. Use a structure like:

1. One-sentence landscape: AR video diffusion/world models create cache/memory explosion; field is solving it through cache compression, sparse attention, and architectural changes.
2. Anchor paper: TempCache, with why it matters for deployment/robot control.
3. Adjacent methods: Forcing-KV and FlowCache, explain how each differs and composes.
4. Architecture direction: SANA-Video / linear attention / SSM.
5. Practical advice for AI infra reader: start with training-free TempCache; monitor head-aware compression; plan for linear/SSM architectures for next-gen world models.

## WeChat formatting

A richer digest can be long, but use plain text or conservative Markdown-lite. Earlier iLink delivery succeeded with a simple plain-text script and then with an insight-rich plain-text version. If a complex markdown digest fails silently, retry as plain text without `**`, emoji-heavy headings, or long tables.
