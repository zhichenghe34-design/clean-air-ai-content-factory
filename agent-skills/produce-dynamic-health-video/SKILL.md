---
name: produce-dynamic-health-video
description: Turn an approved Chinese health-education script, captions, and optional narration into a reproducible animated 9:16 HyperFrames project. Use when the CleanAir Content Factory agent must generate or regenerate motion graphics, avoid static slide-like videos, create a motion_plan.json, or quality-check animation before FFmpeg delivery.
---

# Produce a dynamic health video

Create animation only after script approval and compliance review. Never use motion to imply product evidence that the script does not contain.

## Required inputs

- Read `approved_script.json`, `review.json`, captions or structured scenes, and the narration duration.
- Stop if `review.json` is blocked.
- Prefer `voice.wav`; allow a silent preview only for a visual forward-test.

## Build the motion plan

1. Map every narration beat to one visual claim. Keep 4–8 scenes for a 45–60 second video.
2. Give every scene one semantic visual type and two motion layers: a primary explanatory motion plus a secondary ambient or emphasis motion.
3. Use at least three visual families across the video. Do not repeat a full-screen title-card layout scene after scene.
4. Start the next scene's meaningful content under the outgoing wipe. Never reveal an empty scene after a transition.
5. Keep captions to two lines. Keep highlighted Chinese phrases unbroken.
6. End by gathering the prior evidence into one actionable decision rule.
7. Save the result as `motion_plan.json`; follow the schema and examples in [references/motion-language.md](references/motion-language.md).

Write the topic, audience, duration, and scenes to a UTF-8 JSON input, then create the validated plan without passing Chinese text through a shell pipe:

```powershell
python agent-skills/produce-dynamic-health-video/scripts/create_motion_plan.py `
  --input path/to/motion_input.json `
  --output path/to/motion_plan.json
```

## Generate the project

Run the trusted project-local builder; do not invent shell commands or download templates:

```powershell
python agent-skills/produce-dynamic-health-video/scripts/build_motion_project.py `
  --plan path/to/motion_plan.json `
  --output path/to/animation_project `
  --voice path/to/voice.wav
```

The builder only accepts the bundled template and known visual types. Then run HyperFrames `check` before `render`.

## Quality gate

- Require 1080×1920, 30fps, normal video stream, and an audio stream for final delivery.
- Require 0 HyperFrames runtime, layout, and motion errors.
- Inspect a contact sheet plus frames immediately after every transition.
- Reject a result if any scene is static-only, if fewer than three visual families are used, if a caption exceeds two lines, or if a transition reveals a blank composition.
- If the dynamic renderer is unavailable, label the static FFmpeg output as a fallback. Never present it as the intended animated deliverable.

Keep source, plan, QC report, and final MP4 together so the next Agent can reproduce or locally rerun the result.
