# P6-10B Design QA

final result: blocked

## Comparison inputs

- Source visual truth:
  - `docs/assets/tui/runtime-ask-panel-reference.png`: 1066 × 439
  - `docs/assets/tui/runtime-permission-panel-layout-reference.png`: 1137 × 559
  - `docs/assets/tui/runtime-tool-flow-reference.png`: 822 × 988
  - `docs/assets/tui/runtime-write-card-reference.png`: 966 × 358
  - `docs/assets/tui/runtime-edit-card-reference.png`: 1239 × 312
  - `docs/assets/tui/runtime-timeline-order-reference.png`: user-confirmed chronological flow target
  - `docs/assets/tui/runtime-interaction-overlay-reference.png`: user-confirmed blocking interaction target
- Real-terminal problem evidence:
  - `docs/assets/tui/runtime-timeline-order-current.png`: model text and tool cards grouped into separate piles
  - `docs/assets/tui/runtime-ask-keyboard-instability-current.png`: visible Ask panel whose keyboard focus is unstable
- Candidate directory: `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/`
- Capture method: public Engine event projection into a real `MicaTuiApp`, then Textual `save_screenshot` SVG. No real Provider, credentials, HOME or dangerous commands were used.
- Normalization: Edge headless screenshot of each SVG, then crop of browser chrome. 110×36 / 80×24 / 40×16 PNG pixels match the Rich SVG viewBox unless noted. 24×8 keeps the same SVG (`viewBox 311 × 245.2`) but is recorded as a window-fitted PNG because that viewport is smaller than Edge chrome. The dedicated Write 110×36 capture is also recorded as a window-fitted PNG so the two-row facts remain in frame.
- Candidate terminal viewports and PNG pixel sizes:
  - `ask-110x36.png`: 110 × 36 cells, 1360 × 928 pixels
  - `permission-110x36.png`: 110 × 36 cells, 1360 × 928 pixels
  - `tools-110x36.png`: 110 × 36 cells, 1360 × 928 pixels
  - `conversation-110x36.png`: 110 × 36 cells, 1360 × 928 pixels
  - `history-scroll-110x36.png`: 110 × 36 cells, 1360 × 928 pixels
  - `write-110x36.png`: 110 × 36 cells, SVG `viewBox 1360 × 928.4`, window-fitted PNG 1398 × 954
  - `conversation-80x24.png`: 80 × 24 cells, 994 × 636 pixels
  - `nocolor-80x24.png`: 80 × 24 cells, 994 × 636 pixels
  - `conversation-40x16.png`: 40 × 16 cells, 506 × 440 pixels
  - `conversation-24x8.png`: 24 × 8 cells, SVG 311 × 245.2, window-fitted PNG 500 × 400
  - `tools-24x8.png`: 24 × 8 cells, SVG 311 × 245.2, window-fitted PNG 500 × 400
  - `permission-24x8.png`: 24 × 8 cells, SVG `viewBox 311 × 245.2`, window-fitted PNG 500 × 400
- Full-view comparisons:
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-ask-reference-vs-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-permission-reference-vs-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-tools-reference-vs-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-write-reference-vs-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-edit-reference-vs-tools-110x36.png`
- Focused-region comparisons:
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-ask-focused.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-permission-focused.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-write-focused.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/comparison-edit-focused.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/focus-ask-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/focus-permission-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/focus-write-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/focus-edit-110x36.png`
  - `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/qa/focus-bash-110x36.png`

## First-pass and rework history

The first P6-10B pass captured real runtime states and found two actionable defects.

- [P0 resolved] Structured Ask mounted only a hidden `Input`. Textual sizes `height: auto` from children, not parent `render()`, so 110×36 painted an empty cyan-bordered box. `_AskContent` now owns title, question, numbered options, descriptions, `Type something.` and the select/navigate/notes/cancel hint. Input stays hidden in options mode.
- [P1 resolved] Permission hint `↑/↓ to navigate · Enter to confirm · Esc to cancel · PageUp/PageDown to scroll` collided with `Mica · working...` on 110×36. Wide terminals (`height >= 16`) now use padding `(1,1,1,1)` and a one-row bottom margin. The unique work-status, composer and two-row facts remain below the panel.
- [P1 resolved in this rework] The restored 24×8 permission regression proved facts overflow (`runtime-facts.bottom == 9` on an 8-row screen) because short-terminal top padding plus `border-top` consumed an extra row. Short terminals now use padding `(0,1,0,1)` and no extra margin. Post-fix visual evidence is `C:/Users/10601/AppData/Local/Temp/mica-p6-10b/permission-24x8.png` with matching SVG `permission-24x8.svg`. Measured regions after the fix: panel `y=1 height=1`, status `y=2 height=1`, composer `y=3 height=3`, facts `y=6 height=2`, `facts.bottom == 8`. The screenshot shows the cyan permission rule, `Mica · working.`, composer and `sessions:` still on-screen. 40×16 keeps internal PageDown movement. 24×8 PageDown does not displace `scroll_y` on this 1-row border viewport; that is accepted extreme-terminal degradation, not an open P2.

Reference screenshots are layout truth only. Permanent allow, amend-command, Git, token, cost, cache and foreign chrome from those images remain out of scope.

## Real Windows-terminal findings after the checkpoint

The user's manual review reopened P6-10B with five foundational findings that the earlier headless screenshot pass did not cover:

- [P0 automated resolution, manual confirmation pending] P6-10C now closes the active model step only after an accepted `ToolPrepared`, preserves the completed widgets in place, and mounts the next model step after the tool batch. Public DOM-order tests, a two-request runtime test and the 110×36 comparison capture confirm the required interleaving; final acceptance still requires the user's real Windows-terminal replay.
- [P0 automated resolution, manual confirmation pending] P6-10D now uses priority App bindings for global `Esc` cancellation and the two-press `Ctrl+C` exit state machine. Public Pilot tests cover Provider streaming, real Git Bash execution, Ask, permission, idle behavior and cleanup; the user still owns the final Windows-terminal check that `Ctrl+C` reaches Textual rather than the outer console fallback.
- [P1 automated resolution, manual confirmation pending] P6-10E routes Ask and permission navigation at the App event boundary according to the active interaction owner. Public Pilot tests cover arrows, ASCII digits, Enter, notes/custom input and panel scrolling even when the local view does not retain focus.
- [P1 automated resolution, manual confirmation pending] P6-10E restores focus after `AppBlur → AppFocus` in the fixed order Ask, permission, active-run transcript, idle composer. Selection, typed interaction text, transcript scroll position and composer draft remain intact in automated tests.
- [P1 automated resolution, manual confirmation pending] P6-10E provides one lazily mounted bottom `InteractionOverlay`. While Ask or permission is active it replaces work-status, composer and facts without removing the composer; after settlement it restores the same composer and draft. Ask settles to one transcript record, while permission does not enter history.

The new problem and target images are durable inputs for P6-10C through P6-10E. Keyboard, cancellation and application-focus behavior cannot be accepted from screenshots alone; they require public key-event tests plus a final real Windows-terminal review.

## Final evidence

- Ask 110×36 shows `Follow-up`, the question, numbered options with descriptions, `Type something.`, and the existing hint. The options-mode block cursor is a Textual focus artifact, not a second input box.
- Permission 110×36 shows the safe bash command preview, `Do you want to proceed?`, `1. Yes` / `2. No` only, the muted hint, then a one-row gap above `Mica · working...`, composer and `sessions:` facts.
- Dedicated Write 110×36 (`write-110x36.png` / `write-110x36.svg`) shows a complete in-transcript `WriteToolCard`: tool name `write`, safe relative path `notes.md`, multi-line plain-text body, Markdown fence, blank lines, Unicode `保持正文可见。`, and fold `... (5 more lines, 15 total, ctrl+o to expand)`. The card sits above the unique work-status; composer and two-row facts remain uncovered. This capture, not the later tools viewport or the collapsed history `write notes.md` row, is the Write comparison input.
- Tools 110×36 keeps read/edit/bash cards inside the transcript. Edit shows path plus minus/plus rows; bash final output includes `48 passed`. Cards do not cover status, composer or facts.
- History-scroll 110×36 keeps earlier user/assistant turns, unique work-status, composer and two-row facts. It is history-scroll evidence only, not the Write card comparison source.
- Conversation 110×36 / 80×24 / 40×16 keep one left reading axis, one work-status, complete composer border and two-row facts.
- NO_COLOR 80×24 remains readable by text hierarchy: user line, thinking, reply, tool card, work-status, composer and facts.
- 24×8 still compresses transcript density, but status, composer and `sessions:` facts stay on-screen. After the short-terminal padding fix, `permission-24x8.png` shows the permission rule, status, composer and facts without overlap or overflow. PageDown cannot displace `scroll_y` on this 1-row border viewport; that is accepted extreme-terminal degradation, not an open P2. Actual PageDown movement remains covered at 40×16.

## Fidelity review

- Typography: passed; regular option/command copy, muted hints, restrained status emphasis.
- Spacing: passed for required runtime viewports. Ask content child sizes the panel; wide permission keeps a one-row gap above status; short permission stays compact; composer four-border and two-row facts remain.
- Colors: passed; existing semantic tokens, cyan ask/permission rule, green tool cards, readable NO_COLOR hierarchy. No new palette.
- Copy: passed; no unimplemented permission options, no simulated greeting, no fabricated Git/token/cost/cache/extension data.
- History-scroll: passed; older turns remain reachable above the unique work-status.
- Write card: passed; complete `WriteToolCard` title, path, body, Unicode/Markdown/blank lines and fold are visible in `write-110x36.png` and `qa/focus-write-110x36.png`.
- Responsiveness: passed for 110×36, 80×24, 40×16 and 24×8. 24×8 density loss, including PageDown producing no observable displacement on a 1-row permission viewport, is recorded as accepted extreme-terminal degradation because status, composer and facts remain visible and no remaining overlap or overflow is actionable.
- Runtime safety: passed; TUI still consumes only public events and safe snapshots.

## Final boundary

The screenshot-based review resolved the original Ask sizing and permission spacing findings. P6-10C through P6-10E have subsequently passed master code review and automated verification, while their real Windows-terminal confirmation remains user-owned. Commit `25acc3f570c6a04e838789ec9c9d585d9b8ec9d6` remains a checkpoint only. P6-10C through P6-10E must now be manually rechecked before P6-10B or Phase 6 can close; Phase 7 and Phase 8 have not started.
