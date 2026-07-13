# Ground-truth corrections

Facts collected from the homeowner's snapshot-by-snapshot review that no
geometry rule can infer from the sheets (which runs descend, walls the
linework breaks but reality doesn't, labeled dimensions, sheet errors).

Applied by `pdf2plan.py --fix <file>` AFTER all rule-based extraction, so
the parser stays generic and the corrections survive regeneration:

- `stairs[]`: match a run by centroid within 90" of `near`; set `down` /
  `direction`, or `split_y` a fused double-flight well into `south` /
  `north` runs with their own properties.
- `openings[]`: match by opening centre within 30" of `near`; `remove`,
  set `width` / `type`, or mark `shut` (viewer renders that door closed).
- `fixtures.remove_chimneys`: disable the chimney detector for the sheet.

Every application (or miss) is printed as a `fix:` line during generation.

Known items deliberately NOT corrected here:
- garage / basement floors sit below the main level (multi-level floors
  are not modeled yet; the runs render as down-wells)
- the ~20 ft sunroom behind the dining-room door (door kept shut)
- the real chimney location (homeowner will place it later)
- the front-entry transom ("decorative window" above the door)
