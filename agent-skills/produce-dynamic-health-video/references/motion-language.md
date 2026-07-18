# Motion language and plan contract

## What was learned from the accepted first video

The useful improvement was not “add transitions.” It was to turn each scientific condition into a visible action:

| Meaning | Visual family | Primary motion | Secondary motion |
|---|---|---|---|
| Large numerical claim | `stat-ring` | count or scale the number while drawing a ring | pulse the ring glow |
| Hidden conditions | `magnifier` | scan evidence fields with a magnifier | reveal small lines in sequence |
| Dose and volume | `liquid-chamber` | raise liquid and expand a chamber outline | pop in labels |
| Duration and concentration | `clock-wave` | rotate a hand and animate a waveform | slide in time copy |
| Method and report source | `report-scan` | raise a report and move a scan line | reveal rows |
| Lab versus home | `compare` | bring two environments in from opposite sides | pulse the not-equal mark |
| Final rule | `orbit-summary` | gather conditions around a central rule | breathe the center glow |

Use motion to explain relationships, not as decoration.

## `motion_plan.json`

Top-level fields:

- `topic`, `audience`, `duration_seconds`
- `format`: fixed at 1080×1920, 30fps
- `design_system`: named colors and style
- `director_rules`: the constraints applied to this run
- `scenes`: 4–8 ordered scene objects

Each scene requires:

- `id`, `index`, `start`, `end`
- `kicker`, `title`, `caption`
- `visual_type`
- `primary_motion`, `secondary_motion`, `transition`
- `entrance_lead_seconds`: normally `0.22`, so content enters before the wipe fully clears

The last scene must use `orbit-summary`. Scene timings must be continuous and end at `duration_seconds`.

## Failure patterns to prevent

- A different static card for each paragraph is still a slideshow.
- A wipe alone does not make a scene animated.
- Entering all content only after the wipe clears produces a visible dead frame.
- Inline emphasis can shrink unexpectedly when a generic descendant selector overrides it; give the emphasized final phrase a specific selector.
- Long Chinese captions with individually styled words can wrap into orphan characters; keep emphasized spans `inline-block` and `white-space: nowrap`.
- Automated checks do not replace frame inspection at transitions and at the final call-to-action.
