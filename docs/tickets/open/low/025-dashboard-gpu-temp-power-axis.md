---
id: 025
title: Dashboard "GPU temperature / power" panel mixes °C and W on one axis
status: open
priority: low
created: 2026-07-06
closed:
tags: [observability, grafana, bug]
related: [observability/dashboards/wes-overview.json, "#018"]
---

## Problem
The "GPU temperature / power" panel (id 5 in
`observability/dashboards/wes-overview.json`) plots two series with different
units on a **single Y-axis whose unit is hardcoded `celsius`**:
- `nvidia_smi_temperature_gpu` (°C, ~40-80)
- `nvidia_smi_power_draw_watts` (W, ~15-300)

So power (a much larger number) dominates the scale and is mislabeled as
degrees; temperature is squashed and hard to read.

(Filed 2026-07-06 from an owner report about this panel — the report was
truncated in chat, so confirm the exact symptom; the dual-unit axis above is
the observable defect regardless.)

## Approach
Give power its own right-hand Y-axis via a per-series field override (unit
`watt` on the power series, `celsius` on the temp series), or split into two
panels. Edit the versioned JSON, copy to the Pi, restart grafana-server (see
docs/observability.md).

## Acceptance
- [ ] temperature reads on a °C axis, power on a W axis, both legible
- [ ] change is in the versioned dashboard JSON (not just the live UI)

## Notes
Cosmetic; the underlying metrics are correct. Same care applies to the "VRAM"
panel (single unit, fine) — this is specific to the mixed-unit panel.
