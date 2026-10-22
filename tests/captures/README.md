# Sanitized real captures

These are **real transcripts**, not synthetic ones. They are the
conversations in which this tool was built, captured so the test suite
has something a synthetic fixture cannot provide: the actual shapes,
the actual tool names, the actual prose, and the actual edge cases
that only appear in a real session.

Synthetic fixtures test the author's understanding of a format. Real
captures test the format.

## What was sanitized

- **Secrets** — the transcripts are read through the same redaction
  layer the tool ships with, so any credential-shaped text is
  replaced before it lands here.
- **The operator's name** and the chat transport are removed.
- **Timestamps** are kept (they are the point of a transcript) but the
  absolute dates that would identify a real working day are shifted.
- **Paths** are rewritten to `/proj`-style placeholders.

## What was NOT sanitized

The **prose is real**. If a sentence in here is awkward, that is
because a real session contains awkward sentences. This is a deliberate
choice: these files are the regression corpus, and a synthetic corpus
would not have caught the bugs a real one does — a truncated UTF-8
tail, a message with both prose and a tool call, a role=system row, a
session that ended mid-flight.

If you would rather not have your own working text in a public repo,
the fixtures under `tests/fixtures/` are synthetic and cover the same
adapters. The captures are an addition, not a replacement.

## Versions

`VERSIONS.json` records the version of each harness the capture came
from, so a format change upstream is visible when a capture stops
parsing.
