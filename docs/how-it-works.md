# What OPRO is doing (and how to verify it)

This explains, in plain terms, what `mise run optimize` / `optimize-local`
actually does, and records the operational checks used to answer common
questions (with the exact commands so you can re-run them).

---

## The big idea: "LLM as optimizer"

OPRO = **O**ptimization by **PRO**mpting (Google DeepMind,
*"Large Language Models as Optimizers"*). The thing being optimized is a
**natural-language instruction** (a prompt such as `"Let's solve the problem."`),
and the goal is to find the wording that makes a model score highest on a task
(here, GSM8K math word problems).

### Is this machine learning? Is the AI doing the optimization?

- **No model training happens.** There is no gradient descent, no
  backpropagation, no weight updates. The LLMs (Qwen locally, or Gemini/OpenAI
  in cloud mode) are **frozen** — their parameters never change.
- **Yes, an AI is the optimizer.** This is *black-box / derivative-free
  optimization* where an LLM proposes candidate solutions from feedback. The
  optimizer LLM plays the role gradient descent plays in normal ML.

It is better called **prompt optimization** than model training. It *uses* ML
models and ML-style evaluation (train/test split, accuracy metric) but trains
nothing.

### The loop (two LLM roles)

| Role | What it does | Classic-optimization analogue |
|------|--------------|-------------------------------|
| **Scorer** | Runs a candidate instruction on the training problems and measures accuracy | the objective function `f(x)` |
| **Optimizer** | Reads the history of `(instruction, score)` pairs and proposes new, better instructions | the optimization algorithm (replaces gradients) |

Steps: score some instructions → feed the scored list to the optimizer →
it proposes new ones aiming to beat the best so far → score those → repeat. The
instruction scores climb over steps. That climb *is* the optimization.

Mental image: normal ML adjusts **numbers** (weights) using gradients; OPRO
adjusts **words** (instructions) using an LLM's judgment, guided by accuracy
scores.

### Seen live in a run

Seed instruction `"Let's solve the problem."` scored **0.5** (50% on the 22
training problems). The optimizer then generated new candidates to beat it:

1. `"Let's solve the problem."` (seed, 0.5)
2. `"Solve the math word problem below and provide only the final numerical answer."`
3. `"You are an expert math tutor. Solve the word problem step-by-step, showing your reasoning and calculations clearly before providing the final answer."`

Each new candidate is then scored on the training set, and the best feed the
next step. Log line `discarding generated instructions with score less than 0.3`
= the optimizer keeps only candidates good enough to iterate on. (The meta-prompt
shows scores as 0–100; stored scores are 0–1 accuracy, scaled when shown.)

---

## Operational Q&A (with commands)

### Q: Is it actually using the local LLM, on the GPU?

**Yes.** Checks used:

```bash
# 1. Is the OPRO process running, and with which backend flags?
pgrep -fl optimize_instructions.py
#  -> --optimizer=gpt-3.5-turbo --scorer=gpt-3.5-turbo  (OpenAI-compatible path)

# 2. Where is it routed?
eval "$(mise env)"; echo "$OPENAI_BASE_URL $OPENAI_MODEL_OVERRIDE"
#  -> http://127.0.0.1:8000/v1  Qwen3.6-27B-OptiQ-4bit

# 3. Is the local server up?
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" -w "\n%{http_code}\n"
#  -> HTTP 200

# 4. Proof traffic goes local, not to OpenAI/Gemini:
lsof -nP -iTCP:8000 | grep -i python
#  -> python ... 127.0.0.1:xxxxx->127.0.0.1:8000 (ESTABLISHED)

# 5. GPU utilization (Apple Silicon / Metal, no sudo):
ioreg -r -d 1 -c IOAccelerator | grep -oE '"Device Utilization %"=[0-9]+|"In use system memory"=[0-9]+'
#  -> "Device Utilization %"=100   "In use system memory"=21144977408  (~19.7 GB)
```

Findings: GPU at **100%**, ~**19.7 GB** of unified memory resident (matches the
18.6 GB `Qwen3.6-27B-OptiQ-4bit` weights), served by the **oMLX** runtime
(`oMLX.app` / `omlx-server`) which uses Apple Silicon **Metal**. No cloud calls.

> The MLX server's normal RSS looks small (~0.2 GB) because MLX uses **unified
> memory** — weights are GPU/wired memory, shown under the accelerator's
> "In use system memory", not process RSS. Expected, not a problem.

### Q: How long will a run take?

Calibrate from the live log instead of guessing:

```bash
LOG=<run log>
calls=$(grep -ac "HTTP Request" "$LOG")           # each = one local model call
el=$(( $(date +%s) - $(stat -f %B "$LOG") ))      # seconds since log start
python3 -c "print(f'{$calls} calls in {$el}s = {$el/$calls:.1f}s/call')"
```

Finding: **~15 s per scorer call** — because the GSM8K scorer must generate a
full multi-step solution (~200+ tokens) per problem, not a 1-token answer. So
cost is dominated by the **number of calls**, not wall-clock per step.

Rough call count for `optimize`:
`num_generated_instructions_in_each_step × train-subset-size × num_search_steps`.

- Full GSM8K (200 steps, 261 train ex, 8/step) ≈ **400k calls** → days.
- Smoke (3 steps, 22 ex, 3/step) ≈ 229 calls → ~**60 min** locally at 15 s/call.
- For a true ~3–4 min check, shrink to ~4 train ex / 1 step / 2 instructions.

See the sizing knobs in [`run-optimize-findings.md`](./run-optimize-findings.md).

### Q: What does `data/` contain?

Benchmark datasets the scorer grades instructions against (no code). Checks:

```bash
ls data/                                   # dataset folders + README
cat data/README.md                         # sources/attribution
head -2 data/gsm_data/gsm_train.tsv        # question \t answer \t worked-solution
head -c 400 data/BIG-Bench-Hard-data/*.json | head   # {"examples":[{"input","target"}]}
```

| Folder | Size | Format | Content |
|--------|------|--------|---------|
| `gsm_data` | 4.5M | TSV `question ⇥ answer ⇥ solution` (7,473 train rows) | GSM8K math word problems — used by current runs |
| `BIG-Bench-Hard-data` | 2.6M | 27 JSON, `{"examples":[{"input","target"}]}` | BBH reasoning tasks |
| `MMLU-data` | 6.2M | 57 files (test split) | MMLU multiple-choice, 57 subjects |
| `MultiArith-data` | 252K | 1 JSON | MultiArith arithmetic problems |
| `AQuA-data` | 128K | 1 JSON | AQuA algebraic multiple-choice + rationales |

Each example is an `(input, target)` pair: the scorer runs a candidate
instruction on `input`, parses the answer, and checks it against `target` to
compute accuracy. `--dataset` selects the folder; the code supports `gsm8k`,
`bbh`, `mmlu`. Data is from the original benchmark repos (copyrights theirs).
