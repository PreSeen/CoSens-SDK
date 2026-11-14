# Local Recipe generation: what a Recipe needs to carry

November 2026. These notes cover the change to `cosens.local` Recipe generation listed in the changelog under "Python, unreleased": what we changed, how we measured it, and what the measurements do and do not show.

## Question

`cosens.local.LocalRSI` learns a Recipe from a few confirmed tasks and hands it to an agent on the next task of the same kind. With the 0.6 prompt, Recipes learned from GDPevo's few-shot training tasks often helped no more than giving the agent nothing. We wanted to know what the Recipes were missing, and whether changing only the generation prompt and limits could close the gap.

## Setup

- **Benchmark.** GDPevo task groups. Each group has 5 training tasks with reference answers and up to 5 test tasks of the same kinds, scored by the group's own evaluators. A group runs in its own environment with an API the agent queries.
- **Agent that solves the tasks.** Claude Code as the harness with `qwen3.8-27b` as the model, the same for every condition.
- **Recipe generator.** Codex CLI (`gpt-6-sol`, reasoning `medium`) through `CodexCLIModel`. Each attempt trains on the 5 tasks in order: the agent solves a task, the reference result comes back as feedback, and a new Recipe version is generated. The final Recipe is given to the agent on every test task.
- **Score.** acc@3: each test task is solved 3 times (one per training attempt) and the scores are averaged.
- **Comparisons.** *Base* is the agent with no Recipe. *GDPevo's own few-shot* is GDPevo's skill-writing method (an agent that reads all training inputs and reference answers and writes a skill directory), run with the same agent and model.

## What the Recipes were missing

Reading the failed rubric items against the Recipes showed four recurring gaps.

1. **Conventions were dropped as "case-specific".** The 0.6 prompt said to keep case-specific names and answers in evidence only. Generators applied that to the conventions the next task needs, such as a report title format (`{Client Name} Export Failure - Resolution Report`) or a lookup table, and told the agent not to treat them as general.
2. **Hidden formulas were left unfitted.** When a reference gave scores beside the labels that produced them, Recipes repeated the scores and warned against inferring a formula, so the agent invented its own scale.
3. **Mappings were written as symptoms.** "No service → reseat the SIM" also fires for a locked SIM. A mapping needs the record values that triggered it, such as `sim_status = missing`.
4. **A single trigger was credited with an outcome that several values produced.** One training claim was blocked when it had no AP bill, no vendor and only a partial receipt. A Recipe that recorded "no bill → blocked" then blocked a test claim that had no bill but was otherwise complete, and the score fell below Base.

The size limits (8 steps, 5 lessons, 6,000 characters) also forced a Recipe that covered several kinds of task to drop earlier kinds when it learned a new one.

## What changed

- Keep every convention that carries across tasks exactly: value tables, thresholds, formulas, allowed values, status rules, required output fields, and the format of any name the answer must produce, written as a pattern with placeholders. A convention seen in one confirmed reference is the default for that kind of task.
- Where a reference gives numeric results next to the labels or inputs that produced them, fit the simplest rule that reproduces every reference row, check it against each row, and write it as the default.
- Write each case-to-action mapping with the field values that set that case apart from the other reference cases. Do not credit the outcome to one of those values unless the references show that value deciding it alone. State what the references do when no mapping matches.
- When improving a Recipe, keep the steps and lessons for other kinds of task with their evidence, and say which kind each step is for. A new version may cite evidence the previous version already cited.
- Raise the limits to 30 steps, 20 lessons and 32,000 characters.

## Results

acc@3, in percent. Groups 5 and 6 have 3 test tasks; the others have 5.

| Task group | Base | GDPevo's own few-shot | `cosens.local` 0.6 | This change |
|---|---:|---:|---:|---:|
| 2 | 42.53 | 47.42 | 43.97 | **55.26** |
| 3 | 44.47 | 60.80 | 38.95 | **61.01** |
| 4 | about 20.2 | no skill produced | not run | **57.77** |
| 5 | 51.31 | 54.37 | not run | **55.23** (repeat: 61.11) |
| 6 (held out, trained from scratch) | 58.33 | 63.12 | not run | **68.21** |

- **Group 6 is the cleanest check.** No prompt change looked at it, and the released prompt trained there from scratch, with no offline regeneration. It scored 68.21, against 58.33 for Base and 63.12 for GDPevo's own. It was level with or ahead of GDPevo's own on all three test tasks (75.0 / 42.6 / 87.0 against 70.8 / 42.6 / 75.9).
- On the four groups where GDPevo's own method produced a skill (2, 3, 5, 6), this change averages 59.93, against 56.43 for GDPevo's own and 49.16 for Base.
- On group 4, GDPevo's own skill writer ran out its 2-hour limit in every try. Local Recipe generation finished and raised the agent from about 20 to 58.
- The final Recipes are 20,000-26,000 characters, under the 32,000-character limit these runs used.
- A 14,000-character limit was tried afterwards, and the limit stays at 32,000. Regenerating the saved contexts of groups 2, 3 and 5 at 14,000 (three attempts each, five versions per attempt) finished only for group 2 (13,318-13,994 characters). In groups 3 and 5 all six attempts failed at version 3 or 4, because the model's output stayed over 14,000 characters (14,236-17,147) after the three retries. The recall budget was raised from 2,000 to 8,000 tokens.

### Changes that did not help

Each of these was added, measured on at least two groups, and removed:

- Stating the cutoff that separates labelled records, such as days until renewal. Recipes stated thresholds, but scores did not move.
- Naming an escalation or hand-off route as the default for unmatched cases. This helped the one task it targeted and cost more on others.
- Stating which mapping the references apply first when several match.
- A final consolidation pass over all five training tasks that merges case mappings into general rules. It gained 3 points on one group over two runs and nothing on the others, which is within the run-to-run spread.

## Limits of these measurements

- **Few samples, large spread.** Each figure is 3 tries per task. Rerunning the same Recipes moved a group score by up to 15 points, so differences of a few points are not established.
- **Not all held out.** The changes were developed while reading failures on groups 3 and 5, so groups 2 and 6 are the held-out checks of the final prompt. Group 5 was held out for the previous version of the mapping rule, which fell below Base there. That failure led to the "distinguishing values" rule.
- **Recipes regenerated offline.** To compare prompts without solving the training tasks again, most Recipes were regenerated from the saved training records. Those records came from runs guided by earlier Recipes, which can differ slightly from a fresh run. Only Base, GDPevo's own method, the first group 5 run and the group 6 run trained from scratch.
- **What is left is mostly not in the material.** Most remaining lost points depend on conventions that no training reference or environment record states. Examples are the thresholds behind a risk score's reason codes, and the rule that a locked SIM goes to a human. A generator that learns only from the references cannot recover them.
- **One agent model.** All results use `qwen3.8-27b` as the solving model.
