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
| The pipeline that ended and the pipeline that loops | arrives with A1 | | A1 Fig 1 |
| Cairn 1.0: seven subsystems | arrives with A2 | | A2 Fig 1, root README |
