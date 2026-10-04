# Dataset licences and attribution

The corpora under `datasets/` are redistributed so that the series' examples run offline from a
clean clone. Each one keeps its own licence; none of them is covered by this repository's
Apache-2.0 licence unless stated.

## `datasets/small/iceberg-docs/` — Apache Iceberg documentation

- **Source:** https://github.com/apache/iceberg, paths `docs/docs/*.md` and `format/*.md`
- **Commit:** `a5f46053bf78b3fb91699d76ecda93737dec6b51` (fetched 2026-10-04)
- **Copyright:** Copyright 2017-2026 The Apache Software Foundation
- **Licence:** Apache License, Version 2.0 — the full text is in `iceberg-docs/LICENSE`, and the
  project's `NOTICE` file is in `iceberg-docs/NOTICE`, both copied verbatim.
- **Changes:** the files are unmodified copies, flattened into one folder (`docs/docs/*.md` at the
  top level, `format/*.md` under `format/`). The `assets/` images are not included. Links
  between pages are left as they were and may not resolve from this folder.

Apache Iceberg is a trademark of The Apache Software Foundation. The series is not affiliated
with or endorsed by the ASF; the documentation is included only as a realistic, well-written
technical corpus for a retrieval system to index.

## `datasets/golden/` — golden sets

Written for this series. Apache-2.0, same as the repository.

## Adding a corpus

Add a section here before adding files: source, commit or version, copyright holder, licence,
and what was changed. Only redistribute content whose licence allows it.
