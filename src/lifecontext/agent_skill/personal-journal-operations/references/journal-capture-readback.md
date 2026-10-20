# CLI capture and exact read-back

```bash
CLI=lifecontext
"$CLI" status --json
printf '%s\n' '# Offer from Media Miami' 'Entry date: 2026-09-30 (Europe/Vienna).' '' \
  'Complete original entry, including supplied date/timezone.' |
  "$CLI" retain src_journal_20260930_01 1 \
    --idempotency-key journal-20260930-01
"$CLI" evidence src_journal_20260930_01 --version 1 --include-content --json
"$CLI" recall --query 'What happened on 30 September 2026?'
```

The vault names every entry `YYYY-MM-DD - Title`. Open the source text with
these two lines, then a blank line and the complete original text:

1. `# Title`: a few words in the entry's language that say what it is about,
   with no date in it.
2. `Entry date: YYYY-MM-DD (Timezone).`: the day the entry is about.

Keep both in every correction (`curate correct`) so the later version keeps the
same name. Without a title line the name uses the first sentence; without a
date line it uses the day of capture.

Choose a unique ID/key for each new entry. Reuse them with identical bytes for
retries. For a file, use `retain SOURCE_ID 1 --input /path/to/source.txt` with
an idempotency key. Read-back must match the original; recall is a separate
index check and its results are ranked, not exhaustive. The service saves
custody, indexes the source and writes managed Markdown.

`recall` may report `omitted_unresolvable` above zero: those provider results could not
be tied to one exact local source version and were left out, never shown. Report the count
instead of treating the answer as complete. A slow recall is normal under load; run one at
a time with a long timeout and do not retry it while the first is still running.
