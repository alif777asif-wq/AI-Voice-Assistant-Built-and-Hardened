# Upwork portfolio entry sheet

Project to add under Portfolio > Add a project. Limits used are the stricter of Upwork's two published numbers
(Portfolio Guide, January 2026: title 40, description 300; help article: title 70, description 600; role 100;
captions 300; up to five skills; images up to 4000 x 4000 px).

| Field | Entry |
|---|---|
| Project title | LLM Agent Tool-Call Security Hardening |
| Project role | Author: security audit, code fixes and verification harness |
| Project description | Independent security audit of an AI voice assistant's tool layer. The original code broke 6 of 9 safety rules; after my fixes, 0 of 16,738 hostile and control calls do. Public attack lists plus 1,049 cases I authored (disclosed). One-command rerun with proof logs. |
| Skills (up to 5) | Python, Application Security, Information Security, Test Automation, Large Language Model |
| Project link | https://github.com/alif777asif-wq/llm-agent-tool-call-security-hardening |
| Test data | No synthetic or randomly generated data. The inputs are public lists (pinned to commits, every file checksummed) plus 1,049 calls authored for this audit (canary payloads, scheme and option URLs, a protected-path list, confirmation scenarios, malformed-argument cases, negative controls). They are disclosed in the README and in a quiet caption on every image. |

**The project link works only after you run the push command below.** Nothing has been pushed or published from here,
so the repository does not exist on GitHub until you run it.

## Gallery order

1. `01-llm-agent-security-hardening-outcome.jpg` (message: **Outcome**). Caption: Outcome: the original code broke 6 of 9 safety rules (1,858 of 16,738 replayed tool calls). After the fix: 0. Judged by tripwire programs, canary files and an audit hook, not by the code itself.
2. `02-ai-agent-tool-security-requirements-task.jpg` (message: **Task**). Caption: The task: nine safety rules an AI agent's tools must keep. The original code broke 6 of them.
3. `03-llm-agent-tool-call-audit-approach.jpg` (message: **Approach**). Caption: Approach: a scripted hostile model drives the real parser, orchestrator, dispatcher and tool engine; tripwire programs, canary files and an audit hook judge the result.
4. `04-llm-agent-security-test-data-public-sources.jpg` (message: **Data**). Caption: Data: public attack lists plus real application names, web addresses and file paths, each source pinned to a commit and every file to a SHA-256.
5. `05-llm-agent-security-audit-verifiable-proof.jpg` (message: **Proof**). Caption: Proof: each fix was re-introduced as a bug and the audit caught all ten; the tests, a kernel-level trace and a live process test agree.

Upload the `.jpg` files from `portfolio/exports/` (4000 x 3000, 300 DPI, sRGB, 4:4:4; where a `-upload.jpg` exists
use that one, it is the highest quality that fits under 5 MB). The PNG files are lossless masters.

## Push command

```
cd ~/Downloads && unzip -o llm-agent-tool-call-security-hardening.zip && cd llm-agent-tool-call-security-hardening
git init -b main && git add . && git commit -m "Audit and harden LLM agent tool-call layer, with reproducible proof"
gh repo create llm-agent-tool-call-security-hardening --public --source=. --remote=origin --push
```

One-time setup if missing: `gh auth login`, and `git config user.name` / `user.email`.

## Character counts

| Field | Characters | Limit | Status |
|---|---:|---:|---|
| Project title | 38 | 40 | ok |
| Project role | 59 | 100 | ok |
| Project description | 264 | 300 | ok |
| Caption, image 01 | 194 | 300 | ok |
| Caption, image 02 | 93 | 300 | ok |
| Caption, image 03 | 168 | 300 | ok |
| Caption, image 04 | 144 | 300 | ok |
| Caption, image 05 | 135 | 300 | ok |
| Skills | 5 | 5 | ok |

Truthful-description checklist: no client, employer, contract, testimonial, review or user count is claimed anywhere; the project is original and independent.
