# Experience in motion — SVG animation preview

Design preview only. This directory is outside `dist/` and is not published by the production release workflow.

From the repository root:

```sh
python3 -m http.server 4188 --bind 127.0.0.1 --directory prototypes
```

Open `http://127.0.0.1:4188/experience-loop/`.

## Script / storyboard

The authoritative scene data and playback code are in `script.mjs`.

| Scene | Action | Active relationship |
| --- | --- | --- |
| 1 | You ask whether the pilot can launch Friday. | You → Your agent |
| 2 | Bring project context into the task. | Your experience → Memory → Your agent |
| 3 | The answer identifies the unresolved export dependency. | Your agent → You |
| 4 | You ask it to check dependencies before proposing dates. | You → Your agent (Feedback label) |
| 5 | Form a candidate approach, validate it, then make it reusable. | Your agent → Learn; Memory → Learn |
| 6 | You ask about rolling out a different dashboard. | You → Your agent; Learn → Your agent; Learn → Memory |
| 7 | Ask about unresolved dependencies before choosing a date. | Learn → Your agent → You |

One cycle takes 37.5 seconds, then stops. Pause, Next, Restart and the seven scene controls allow manual review. Reduced motion starts paused and disables moving dashes. Hidden tabs do not continue the timer. Theme switches the prototype's palette only; it does not change production preferences.

The SVG uses project documents and authored sample conversations. It does not call an LLM, access memories, upload documents or validate a real method. The Learn preview and app connection boundaries remain visible. The initial prototype is a desktop relationship diagram; phones can pan the same diagram for review. Production mobile layout remains a separate implementation step after this visual direction is accepted.


## Relationship revision — 2026-10-18

You sits upper-left, Your experience lower-left. Your agent (Reason), Memory and Learn share one dashed Your second brain enclosure. A single You ↔ Agent path carries Prompt, Reply and Feedback labels; reply reverses both the arrowhead and moving dashes. Every active data path reveals its own label. Memory → Learn carries relevant experience; the reverse direction records a validated lesson. Feedback is a message in the conversation, not a separate diagram node.


Straight-line revision: Your experience and Memory share the same centerline. Imports, Memory ↔ Learn and Agent ↔ Learn each use one straight SVG path. Agent → Learn carries task material and feedback; Learn → Agent reverses that same path to provide validated methods.


Placement confirmed on 2026-10-18: this relationship animation belongs in Build → How it works. It is not the Connect consumer homepage demonstration. The enclosure is labeled “Your second brain”.


Alignment revision: lower cards share 280 × 150 dimensions, 112-unit horizontal gaps, and common text baselines. You and Agent share a 230-unit height and the same top/header/body baselines. Agent spans the Memory/Learn columns, with matching left and right edges. All card content uses 24-unit horizontal padding; all flow connections remain single straight paths.


Latest label decision: the enclosure is called CoSens SDK. This supersedes the earlier Your second brain label; the prototype describes the SDK's task and experience flow.

The SDK enclosure has a subtle neutral background in both themes. Its fill renders behind the connections so the group color cannot cover flow paths.
