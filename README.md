# CoSens

This repository contains the website and the RSI service with its local `memory` package.
See [documentation](docs/README.md), [RSI API reference](docs/RSI_WORKFLOW_API.md), [service setup](docs/RSI_SERVICE.md), and [Memory SDK](packages/memory/README.md).
The website development instructions follow below.

Source: [PreSeen/cosens.ai](https://github.com/PreSeen/cosens.ai) (private). Local repository: `/Users/derek/Projects/cosens.ai/`.

## Pages and readers

- `/`: Connect is the default homepage. Why / What explain the value of an independent experience layer. A dinner-planning example shows how preferences and feedback change the next step. The hero uses the interactive three-layer concept graphic.
- `/build/`: Developer page. Existing-client wrapper in a right-side terminal, SDK runtime animation, direct Memory / Recipe / Feedback API control and curated Memory benchmarks.
- `/docs/`: Defaults to the Connect guide. Eight connection/import guides and eight SDK/concept/API articles distinguish actual remote MCP/plugin capabilities from SDK and Learn previews.
- `/pricing/`: Free / Plus / Pro plans, experience storage, shared processing and agent-execution credit, team rules and email-based plan enquiries.
- `/benchmarks/`: Full score table, protocols, provenance, method resources and missing reproduction information. Source scores are unchanged by the demo release.

Navigation is Connect / Build / Docs / Pricing on every page and on phones. Dinner example links point to `/#how-it-works`; SDK flow links point to `/build/#how-it-works`.

## Develop

No dependency installation is needed for the static site or Node tests.

```sh
npm run build
npm test
python3 -m http.server 4173 --bind 127.0.0.1 --directory dist
```

Open `http://127.0.0.1:4173/`, `/build/`, `/docs/`, `/benchmarks/` and `/pricing/`.

Build renders the canonical SDK SVG into Build, renders benchmark tables from JSON, and updates content hashes for stylesheets and modules (including the shared app import in Docs). Commit the generated `dist/` changes with their sources. A clean checkout must remain unchanged after building.

## Diagram and interaction sources

- `dist/app.mjs`: Shared code examples, clipboard handling, accessible tabs, three-layer interaction and the Connect dinner scenario. The dinner choices check arrival before last order while preserving dietary and quiet-place preferences.
- `dist/assets/sdk-workflow.svg`: Authoritative SDK diagram. Agent, Memory and Learn share one CoSens SDK enclosure with a subtle neutral fill. You is upper-left; Your experience lower-left. The bottom cards have equal dimensions and spacing. All connections are single straight paths.
- `scripts/render-sdk-workflow.mjs`: Embeds that exact SVG into `dist/build/index.html`.
- `dist/sdk-flow.mjs`: Seven-scene animation: prompt, context recall, reply, feedback, candidate/validation, new task, method reuse. Prompt/Reply/Feedback share one bidirectional edge. The Agent/Learn and Memory/Learn pairs also use one edge each, with direction and labels changing by scene.
- `prototypes/experience-loop/`: Reviewed design prototype and storyboard. Preserved as design history outside the production output; production SVG and script above are authoritative after integration.

The SDK animation provides play/pause, next, restart and scene buttons. Reduced-motion preference starts it paused and disables animated dashes; offscreen figures and hidden tabs pause the timer. Playback starts only when the figure enters the viewport. It uses the shared page theme. The seven scenes loop continuously. Hover pauses only over the seven progress-line buttons below the figure; leaving that strip resumes playback unless manually paused. Hovering the figure or story text does not pause playback. Titles and captions appear above the figure. The diagram scrolls inside its own container on narrow screens so technical labels remain readable without overflowing the page. Story captions and controls reflow to a phone layout.

The original three-layer diagram appears on Connect and has independent hover/focus/touch controls and a ten-second timer. Connect's dinner interaction keeps its own timer and choices. Each region pauses on desktop hover/focus; touch stops automatic rotation for that visit. No timer starts model calls or background writes.

## Sensable AI on Connect

Connect introduces the term after the second-brain hero, defines it in What, and closes the dinner example with the difference between preferences shaping an answer and feedback shaping the next approach. The What figure places Sensable AI at the center of a clockwise Reason / Remember / Improve loop. Only the dashed ring moves; labels stay fixed. Hover, keyboard focus within the figure, and the manual button pause motion. Reduced-motion preference displays a static figure. The circle is an explanatory concept, not a backend learning status indicator.

## Terminology

Connect calls the assistant “Your AI.” Build runtime explanations call the task-handling application “Your agent.” LLM refers only to the underlying model component, including the existing LLM client. Component labels LLM / Memory / Learn remain valid on Connect as secondary explanations. Connection CTAs and executable identifiers retain their established names. The Docs overview defines the three terms explicitly; avoid rotating them as synonyms for the same actor.

## Product and evidence boundaries

Brand prose uses CoSens, with gray Co and blue Sens. Executable identifiers such as `cosens(client)` and the domain remain lowercase. Outcomes are Reason / Remember / Improve; components are LLM / Memory / Learn. The developer enclosure is labeled CoSens SDK.

The wrapper, Memory/Recipe/Feedback APIs and complete Learn loop remain previews. The public MCP surface offers memory tools; it does not expose the complete method-learning and Recipe-management flow. Editing a Recipe creates a candidate in the proposed design, not adoption. Independent validation and applicable scope precede reuse.

All website scenarios are authored demonstrations. The SDK animation's validation step explains a process; it is not a completed live evaluation. The dinner venue data is scripted and makes no live restaurant lookup. No private screenshots, customer claims, account changes or uploads are part of website QA. Gmail and other app synchronization remain Coming soon.

Connection/upload guides retain the reviewed memory-connector `origin/main` contract at `67be9b10c`: ChatGPT supplies a host file reference; shell-capable agents run the one-time command returned by `document_upload`; Claude can recall already imported material. Wait for processing completion and `recall_ready: true` when provided. These are code-reviewed guides, not a new cross-client account acceptance result.

## Benchmark contract

`dist/benchmarks/results.json` is the source of truth for scores, sources, protocols and disclosure fields. One row per benchmark; memory systems are columns. Prefer a directly reported Overall metric, label any subset explicitly, preserve source scales, and keep missing scores as null / —. Never invent an overall score or combine heterogeneous metrics into a total. Memory benchmarks do not prove the complete SDK or Learn loop.

The Build page retains its four-method curated comparison; the report has the full table and linked methods. Both include the experimental-results disclaimer. Historical references remain distinct from controlled company runs.

## Theme

Before first paint, each HTML shell embeds a synchronous theme choice and the initial palette, ahead of external CSS and module loads. `scripts/render-theme-bootstrap.mjs` generates them from the same `readPreference` / `resolveTheme` functions and palette rules used by the normal runtime. This prevents a light first frame while the theme module is still loading. The module then binds controls and time refreshes.

Auto is the default. The visitor's browser timezone controls local day (07:00–18:59) and night (19:00–06:59). Manual Light/Dark choices persist under `cosens-theme`; Auto restores local-time behavior. The footer shows timezone and local time. Theme refreshes each minute, on visibility and on window focus; no timezone is transmitted to an API.

## Release and verification

Cloudflare Pages project: `cosens`; static output: `dist/`. Domains remain `cosens.ai` and `www.cosens.ai`.

Pushes to `main` use `.github/workflows/cloudflare-pages.yml` to build, test, check generated files, deploy and compare production bytes against the checkout. The run summary contains the deployment receipt. Pull requests only build/test. CI uses the existing restricted `CLOUDFLARE_API_TOKEN` repository secret; no credentials belong in source. Official actions are pinned.

An authorized manual release remains available from clean, committed `main`:

```sh
npm run deploy
```

The script checks build, tests and Wrangler authentication, deploys to the existing project, then verifies live HTML and versioned resources. `.wrangler/deploy-receipt.json` records the verified commit and URLs. Repeat content verification without deployment with:

```sh
npm run verify:live
```

Browser acceptance separately checks page navigation, dinner choices/reset, SDK flow directions/labels/playback, code tabs/copy, Docs search/menu, all articles, light/dark modes and phone overflow. A deployment receipt does not prove backend learning or account-level integration behavior.

## 2026-10-18 copy and API navigation revision

Connect leads with “Give your AI a second brain” and the three-layer concept graphic; Build leads with “Build your second brain with one line of code” and a right-side terminal. The Why section asks “AI knows a lot. But does it know you?” Memory API, Recipe API and Feedback API have consistent labels and dedicated Docs routes. Feedback submission is a preview interface, not evidence that learning or validation completed.


## Pricing — 2026-10-19 approved contract

Public plans: Free $0, Plus $9.9/month, Pro $39.9/month. Experience storage is 100 MB / 1 GB / 5 GB and includes graph data as well as saved content. Monthly shared credit is $1 / $10 / $40, described as “for experience processing and agent execution”. These explicit decisions supersede the older PRD credit figures.

Recipe allowances count active custom Recipes (3 / 10 / 50), not versions. Iteration draws from experience processing credit; do not add a separate iteration counter. Directly paid external model calls and AI subscriptions are separate from CoSens-provided model usage. SDK, Recipe management and complete Learn remain previews.

The Free CTA opens the connection guide. Plus, Pro and enterprise links open `mailto:hello@cosens.ai` with specific subjects; adjacent text explains the email handoff. The site does not accept payment, activate plans or verify mailbox delivery. No billing backend, quota enforcement, automated top-up or mailbox configuration is part of this release.

`dist/pricing/index.html` is the page source; `dist/pricing/pricing.css` scopes its presentation. The shared theme bootstrap and asset-version build include the fifth shell. Docs `#pricing` is the public Plans & usage article. The approved public plan contract is recorded in this README and the pricing page; historical design notes remain in Git history. Internal cost parameters do not appear in `dist`.

Pricing HTML uses route-scoped `Cache-Control: public, max-age=0, must-revalidate, no-transform` in `dist/_headers`. Cloudflare otherwise rewrites its public email links and injects a decoder, which breaks the strict production-byte check. Keep that check intact; preserve the original contact HTML at the edge. See [Cloudflare email obfuscation](https://developers.cloudflare.com/waf/tools/scrape-shield/email-address-obfuscation/) and [Pages headers](https://developers.cloudflare.com/pages/configuration/headers/).

## Connect example disclosure — 2026-10-19

The first learning-loop example stays visible. The second dinner-planning example uses a native, initially closed details element; its heading opens it with mouse or keyboard. “That’s Sensable AI.” and its takeaway remain outside the disclosure and visible when it is closed.

## Company logo banner — 2026-10-19

Connect includes the approved selected single-row company banner between the hero and Sensable AI. Logos are extracted from page 17 of the June 2026 English PreSeen deck, stored under `dist/assets/companies`, and rendered as theme-colored alpha masks. The row has no visible heading or caption. Its accessible name identifies current and past PreSeen-team customers. An icon-only control pauses the motion. Desktop motion pauses on hover, focus or the pause checkbox; touch and reduced-motion layouts use a scrollable static row. Benchmark data is unchanged.


## Home / Avatar local preview — 2026-10-20

Branch: `codex/avatar-home-local-preview`. Production deployment is not requested for this revision; do not merge to `main` merely to show a preview, because its push workflow publishes to Cloudflare.

```sh
npm run build
npm test
npm run preview
```

Open `http://127.0.0.1:4191/` and `/avatar/`. Home retains the three-layer concept and work examples, followed by independent Avatar, MCP / Connector and Developer sections. Shared navigation is generated by `scripts/render-navigation.mjs`; all seven shells participate in first-paint theme and asset versioning.

`/request-access/?interest=avatar` (or `mcp`, `sdk`) preselects the relevant interest and allows multiple choices. The application is a local interaction preview only: no request is transmitted or persisted, and submission explicitly says it has not been sent. Connecting the production application endpoint, saving/updating interests and completing account-level access are outside this preview. Log in opens the existing `https://cloud.preseen.ai/` workspace; sign-in behavior has not been exercised here.

Avatar includes the six-stage 22-second storyboard, independent motion holds, keyboard scene navigation, three work stories and the revised sourced comparison table. Learn and SDK keep their preview labels. Benchmark score data is unchanged.
