# Diagrams

House notation, used on the site and in every README so a reader recognises the system in
both places:

- Boxes are services, stores or entities. Solid arrows are data flow, with the payload named.
  Dashed lines are control or observation.
- One accent colour (`#2563eb`) marks the thing the diagram is about; everything else is grey.
- Sources are editable (`.mmd` Mermaid, or `.excalidraw`); the committed `.svg` is the export
  the article embeds. When the source and the export disagree, the export is what readers
  saw, so fix the source and re-export rather than editing the SVG by hand.

| Diagram | Source | Export | Used in |
| --- | --- | --- | --- |
| The lineage spine | `lineage-spine.mmd` | `lineage-spine.svg` | A2 Fig 3, `reference.md`, `packages/schemas/README.md` |
| Cairn: seven subsystems | `cairn.mmd` | `cairn.svg` | A2 Fig 1, root README |
| Stage 0 deployment: three containers, one bind mount | `stage-0-deployment.mmd` | `stage-0-deployment.svg` | A3 Fig 1, root README, `platform/README.md` |
| The request path: six steps, the fifth before the sixth | `stage-0-request-path.mmd` | `stage-0-request-path.svg` | A3 Fig 2 |
| The pipeline that ended and the pipeline that loops | arrives with A1 | | A1 Fig 1 |
