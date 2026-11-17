# Trustworthy AI Assignment: Input and Output Guardrails

This is my implementation of a small policy knowledge assistant with guardrails
around both the user input and the generated answer. I chose a policy-assistant
example because it makes it easy to check whether answers are actually backed
by the supplied policy snippets.

The project is intentionally deterministic and uses only the Python standard
library. I used a small response-provider function instead of a live LLM so the
same safety behaviour can be tested every time. A real LLM can be connected at
the `generator` function boundary in `GuardrailPipeline`.

## What is included

- Input PII detection and redaction for email, phone, and ID-like values
- Prompt-injection detection and refusal before generation
- Sensitive-topic handling for self-harm, medical/legal advice, and requests
  for someone else’s private information
- Output schema validation
- Citation and groundedness validation against supplied references
- Output PII redaction
- Safe fallback responses when validation fails
- 30 automated acceptance tests and a separate labelled metric evaluation

## Project layout

```text
trustworthy_ai/guardrails.py  # Main implementation
tests/test_guardrails.py      # 30 acceptance tests
evaluation/evaluate.py        # Precision/recall evaluation runner
evaluation/results.json       # Latest generated metrics
docs/architecture.md          # Architecture diagram
docs/test-plan.md             # Test inputs, expected and actual behaviour
docs/evaluation-report.md     # Results, failure analysis, trade-offs
demo.py                       # Small local demonstration
```

## Setup

Python 3.11 or newer is required. There are no third-party packages to install.

```bash
git clone <your-repository-url>
cd Trustworthy-AI
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

## Run it

Run the demo:

```bash
python demo.py
```

Run all acceptance tests:

```bash
python -m unittest discover -s tests -v
```

Run the labelled evaluation and regenerate the results file:

```bash
python evaluation/evaluate.py
```

## Design choices

The pipeline checks input first, then calls the response provider only if the
question is safe enough to continue. It validates the provider's answer before
returning it. This is useful because safety instructions alone cannot guarantee
that a model will not produce an unsupported claim or leak information.

The input rules have a clear order: injection and requests for private data are
refused; self-harm is escalated; medical/legal requests get a limited response;
ordinary PII is redacted so the person can still ask their policy question.

For the output format, every normal answer needs `answer`, `citations`,
`status`, and `safety_note`. The citation IDs must exist in the supplied
references. The groundedness check also rejects new numeric claims and requires
meaningful word overlap with cited text. This is a simple, inspectable heuristic
rather than full factual reasoning.

See [the architecture diagram](docs/architecture.md) for the flow and
[the detailed evaluation report](docs/evaluation-report.md) for metrics,
failure cases, trade-offs, and the remaining risks. The assignment sections
(design, implementation choices, evaluation, and risk assessment) are also
combined in [REPORT.md](REPORT.md).

## Evaluation summary

All 30 acceptance tests pass. The labelled evaluation found that the current
rules are precise for the included examples but have recall gaps:

| Guardrail | Precision | Recall |
|---|---:|---:|
| Input PII | 1.00 | 0.75 |
| Prompt injection | 1.00 | 0.80 |
| Self-harm topic | 1.00 | 0.50 |
| Unsupported-claim detection | 1.00 | 0.75 |
| Output PII | 1.00 | 0.67 |

The main problems are obfuscated PII, paraphrased injection, and unsupported
details that resemble a real policy statement. For that reason, I would only
use this implementation as a restricted classroom/demo system, not as a
production safety layer.
