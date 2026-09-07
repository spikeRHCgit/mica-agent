# P6-10A Design QA

final result: passed

## Comparison inputs

- Source visual truth: `docs/assets/tui/startup-reference-wide.png`
- Source pixels: 1487 × 1058
- Previous negative captures retained in `docs/assets/tui/`:
  - `startup-current-bottom-aligned.png`
  - `composer-current-incomplete-border.png`
- Current deletion/problem captures retained in `docs/assets/tui/`:
  - `startup-remove-logo-wordmark.png`
  - `startup-remove-simulated-greeting.png`
  - `startup-remove-unimplemented-key-hints.png`
  - `startup-facts-current-layout.png`
  - `startup-context-paths-current-problem.png`
- Current facts layout reference: `docs/assets/tui/startup-facts-pi-layout-reference.png`
- Candidate directory: `C:/Users/10601/AppData/Local/Temp/mica-p6-10a-final/`
- Candidate terminal viewports and PNG pixel sizes:
  - `startup-110x36.png`: 110 × 36 cells, 1359 × 939 pixels
  - `startup-100x30.png`: 100 × 30 cells, 1238 × 782 pixels
  - `startup-80x24.png`: 80 × 24 cells, 994 × 636 pixels
  - `startup-40x16.png`: 40 × 16 cells, 506 × 441 pixels
  - `startup-24x8.png`: 24 × 8 cells, 311 × 246 pixels
- Side-by-side comparison: `C:/Users/10601/AppData/Local/Temp/mica-p6-10a-final/comparison-reference-vs-110x36.png`
- All candidates were generated from real Textual components in the same empty focused-composer state.

## First-pass and second-pass history

The first P6-10A pass resolved top anchoring, neutral canvas, resource framing, complete composer geometry,
fixed facts height, narrow scrollbar contrast, 68-cell resource width and height-aware compaction. The first
rework screenshots then passed the automated layout review.

The user's second visual review identified the remaining actionable details: the full/compact Logo still had a
repeated lowercase wordmark, the ready area simulated a model greeting, the independent shortcut footer promised
unconfirmed controls, the facts row still carried noisy W/M/P/time/tool values, and loaded AGENTS rows showed only
`yes`. These were corrected in the current pass rather than hidden or renamed.

## Final evidence

- [P2 resolved] Full and compact Logo matrices are unchanged except that the repeated lowercase `mica` label is
  removed. Plain mode still renders the standalone `Mica` fallback.
- [P2 resolved] Startup ready content keeps only `✓ Mica is ready.  Type your request below or run a command.`;
  no greeting line, UserMessageCard, AssistantResponseView or synthetic session message is created.
- [P2 resolved] The independent shortcut-hint component was removed from the App tree, resize logic and shell CSS.
  Existing input bindings and the composer inline send hint remain unchanged.
- [P1 resolved] Facts are fixed at two rows: workspace is left-aligned, model is right-aligned on row one, and row
  two begins with exact `sessions:`. W/M/P labels, elapsed time and tool counts are absent.
- [P1 resolved] CLI uses `selected.meta.title.strip() or selected.meta.session_id` for the session label. A named
  session therefore displays its real title; an unnamed session falls back to the stable ID.
- [P1 resolved] CLI supplies `~/.mica/AGENTS.md` for a loaded default global source and `./AGENTS.md` for a loaded
  workspace source. Unloaded, unreadable, empty, oversized and invalid sources display `none`; compatibility callers
  that provide only `agent_sources` also fail closed to `none` rather than inventing a path.
- [P2 resolved] Facts use Rich terminal cell widths and display-only tail ellipsis. Full snapshot values remain frozen
  and complete; long Unicode, CJK and Windows-path cases do not grow the two-row region.
- [P2 resolved] Removing the footer returns its height to transcript. The unique work-status remains directly above
  the composer, and the composer keeps one complete four-sided border.

## Fidelity review

- Typography: passed; regular descriptive copy, restrained ready emphasis and muted facts hierarchy.
- Spacing: passed; stable bottom inset, 68-cell resource column, two-row facts and no footer placeholder height.
- Colors: passed; neutral black canvas, existing semantic colors, restrained borders and no saturated scrollbar block.
- Logo: passed; full/compact use the established blue facet matrices without a companion wordmark; plain fallback is
  still readable as `Mica`.
- Copy: passed; no simulated greeting, no unimplemented shortcut promise, no fabricated provider/token/cost/cache/Git
  data, and only real startup/context facts are displayed.
- AGENTS paths: passed; only loaded sources receive safe single-line relative display paths and no absolute HOME path.
- Responsiveness: passed for all five required viewports. Composer borders, focus/input, work-status, two-row facts,
  transcript visibility and no-horizontal-overflow rules remain valid at 110 × 36, 100 × 30, 80 × 24, 40 × 16 and
  24 × 8, including NO_COLOR and long Unicode/CJK facts.
- Runtime safety: passed; no Provider, Store, ToolExecutor, ContextBuilder private state or repository reference PNG is
  read by TUI rendering.

## Final boundary

No executable P0, P1 or P2 issue remains in the current screenshot and test evidence. The user subsequently completed
and approved a real Windows-terminal visual recheck. The separately identified streaming-cursor race was fixed and the
unified automated acceptance run passed, so P6-10A is closed. P6-10B, Phase 7 and Phase 8 have not started.
