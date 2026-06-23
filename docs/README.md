# OPRO — Running `optimize` and `evaluate`

This adaptation of Google's OPRO is driven through [`mise`](https://mise.jdx.dev)
tasks. It supports two backends:

- **Cloud** — Gemini as the scorer, OpenAI as the optimizer.
- **Local** — a local OpenAI-compatible server (e.g. MLX / LM Studio) for both
  roles, with no cloud APIs.

The original code targeted Google's **PaLM `text-bison`** API and the **pre-1.0
`openai`** SDK, both of which are retired. This repo has been migrated to current
APIs. For the full investigation and benchmarks, see
[`run-optimize-findings.md`](./run-optimize-findings.md).

---

## Quick start

```bash
# Cloud (Gemini scorer + OpenAI optimizer)
mise run optimize
mise run evaluate

# Fully local (one OpenAI-compatible server for both roles)
mise run optimize-local
mise run evaluate-local
```

> ⚠️ At default settings a full run is **~400k model calls / multiple days**.
> For iteration, apply the [smoke-test sizing](#runtime--cost-keep-runs-small)
> first.

---

## Prerequisites

- `mise` and `uv` installed.
- A `.mise.env` file in the repo root (git-ignored) holding secrets — see below.
- For local mode: an OpenAI-compatible server running (default assumed at
  `http://127.0.0.1:8000/v1`).

---

## Configuration

Non-secret run config lives in `.mise.toml` under `[env]` (committed); secrets
and machine-specific values live in `.mise.env` (git-ignored, loaded via
`_.file`).

### `.mise.toml` `[env]` (committed defaults)

| Var | Default | Meaning |
|-----|---------|---------|
| `OPTIMIZER_GPT` | `gpt-3.5-turbo` | Optimizer LLM (OpenAI-path logical name) |
| `SCORER_TEXT` | `text-bison` | Scorer selector (`text-bison` → Gemini) |
| `INSTRUCTION_POS` | `Q_begin` | Instruction position |
| `DATASET` | `gsm8k` | Dataset (`gsm8k` / `bbh` / `mmlu`) |
| `TASK` | `train` | Data fold for `optimize` |

### `.mise.env` (git-ignored secrets)

**Cloud mode:**
```
PALM_API_KEY="<google-generative-ai-key>"
OPENAI_API_KEY="sk-proj-..."
```

**Local mode** (adds four vars; see [gotchas](#gotchas)):
```
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY='<local-server-key>'        # single-quote if it contains $
OPENAI_MODEL_OVERRIDE=Qwen3.6-27B-OptiQ-4bit
OPENAI_DISABLE_THINKING=1
```

---

## Tasks

| Task | Scorer | Optimizer | Notes |
|------|--------|-----------|-------|
| `optimize` | Gemini (`gemini-2.5-flash`) | OpenAI `gpt-3.5-turbo` | train fold |
| `evaluate` | Gemini (`gemini-2.5-flash`) | — | test fold |
| `optimize-local` | local server | local server | both roles local |
| `evaluate-local` | local server | — | test fold, local |

`mise tasks` lists them. Adjust dataset/fold/position via the `[env]` vars.

---

## Cloud mode

- **Scorer** routes through `prompt_utils.call_palm_server_from_cloud`, which now
  calls **Gemini** `generate_content` (`gemini-2.5-flash`) — the legacy PaLM
  `text-bison` / `generateText` API is gone.
- **Optimizer** uses the **OpenAI v1** client (`chat.completions.create`).

Provide `PALM_API_KEY` (Google Generative AI) and `OPENAI_API_KEY` (`sk-...`) in
`.mise.env`, then `mise run optimize`.

---

## Local mode

Both roles keep the logical name `gpt-3.5-turbo` (so the GPT-path parsing logic
stays valid); only the endpoint and model id are redirected via env vars read in
`prompt_utils`:

- `OPENAI_BASE_URL` — points the OpenAI client at the local server.
- `OPENAI_MODEL_OVERRIDE` — the real local model id used for every call.
- `OPENAI_DISABLE_THINKING` — sends
  `chat_template_kwargs={"enable_thinking": false}` so reasoning models (Qwen3)
  answer in ~1 token instead of hundreds. Essential for the scorer.

### Recommended models

| Role | Pick | Why |
|------|------|-----|
| Both | **`Qwen3.6-27B-OptiQ-4bit`** (thinking off) | ~0.7 s/call warm, strong reasoning |
| Speed alt | `gemma-4-26B-A4B-it-QAT` | MoE, faster |
| Scorer-only alt | `phi-4-14B` / `Llama-3.2-3B` | fast; smaller = weaker on math |

> A local MLX server typically serves **one** model at a time and reloads when
> the `model` id changes — keep `OPENAI_MODEL_OVERRIDE` the same for both roles.

---

## Runtime & cost — keep runs small

The scorer is the bottleneck: each search step scores
`num_generated_instructions_in_each_step` × (train-subset size) examples,
**sequentially**, across `num_search_steps`. For gsm8k defaults that is
**~400,000 scorer calls** for a full run — days of wall-clock and (in cloud
mode) hundreds of dollars.

For a smoke test, edit `opro/optimization/optimize_instructions.py`:

| Knob | Line | Default | Test value |
|------|------|---------|-----------|
| `num_search_steps` | 708 | 200 | **5** |
| `train_ratio` (gsm8k) | 644 | 0.035 (261 ex) | **0.005** (~37 ex) |
| `num_generated_instructions_in_each_step` | 707 | 8 | 4 |

≈ 5 × 4 × 37 ≈ **740 calls** → minutes instead of days.

---

## Gotchas

- **`$` in a key:** mise's dotenv parser expands `$NAME`, so a key like
  `zaq1@WSXcde3$RFV` is silently truncated → 401. **Single-quote** such values in
  `.mise.env`.
- **Global redirect:** while `OPENAI_BASE_URL` is set in `.mise.env`, *all*
  OpenAI-path calls go local — including the optimizer of the plain
  `optimize`/`evaluate` tasks. Remove that line to return to cloud OpenAI.
- **Keys appear in `ps`:** the mise tasks pass keys as CLI flags, so they show in
  the process list. Treat the host as trusted.
- **Auth errors fail fast:** a 401/permission error raises immediately instead of
  retrying forever (old behavior).

---

## Summary of changes from upstream OPRO

- `opro/prompt_utils.py`
  - Scorer (`call_palm_server_from_cloud`) → Gemini `generate_content`.
  - OpenAI calls → v1 client + v1 exceptions; fail-fast on auth errors.
  - `OPENAI_MODEL_OVERRIDE` / `OPENAI_DISABLE_THINKING` for local servers.
- `opro/optimization/optimize_instructions.py`,
  `opro/evaluation/evaluate_instructions.py`
  - Scorer/optimizer model partials → `gemini-2.5-flash`.
- `.mise.toml`
  - `[env]` run config + `.mise.env` loading; `optimize`, `evaluate`,
    `optimize-local`, `evaluate-local` tasks.

See [`run-optimize-findings.md`](./run-optimize-findings.md) for the detailed log,
benchmarks, and the secret-scrub / push-protection history.
