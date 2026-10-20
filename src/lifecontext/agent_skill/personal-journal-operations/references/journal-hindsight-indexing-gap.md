# Journal index status

For document-stage recovery use [document workflows](document-wiki-workflows.md);
`curate reindex` below is for journal sources.

Distinguish sources from versions. Imports preserve every historical version
in custody and Markdown but submit only the latest active version per source
to Hindsight. Older imported rows may retain `pending/unknown`; this does not
mean a current entry is waiting for indexing.

For a current source, inspect CLI `evidence` and its completed operation, then
check attributed `recall`. Readiness and projection success alone do not prove
indexing. A provider failure must be reported separately from saved custody.
Use `curate reindex SOURCE_ID --expected-version VERSION` only for the current
version after diagnosing the failure; never resubmit an older revision over
its current document or capture a duplicate to bypass an error.
