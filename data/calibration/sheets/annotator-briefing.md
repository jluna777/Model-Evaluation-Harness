# Annotator Briefing — Judge-Calibration Labeling Session

You are one of two independent annotators. Your verdicts become the human
gold standard an automated judge is measured against, and the agreement
between the two annotators sets the ceiling for how good that judge can
provably be. Please read this page fully before opening your sheet.

## The task

Your sheet (`sheet-second-annotator.csv` or `sheet-owner.csv`) has one row
per judgment (the second annotator's full sheet has 140). Each row shows:

- one **candidate value** — what an AI model extracted for one field
  (`issue_summary` or `requested_action`) from one customer-support email
- the **reference value** — the approved correct answer for that field
- the email's subject (full email bodies are in `emails-reference.md`,
  keyed by `item_id` — read the email whenever groundedness is in question)

The easiest way to work is the grader: run `uv run python tools/grade.py`
from the repository root (or double-click `tools/grade.bat`) — it opens
the grader in your browser with every sheet one click away and the
emails preloaded, so any row can expand to show its full email in
place. It saves straight back to your sheet file and keeps every
column except your two editable ones read-only. Working directly in
Excel/a text editor is equally fine.

For each row, fill in exactly two cells:

- **verdict** — `pass` or `fail` (lowercase)
- **critique** — one short line saying why (especially for fails)

## The verdict rule

This is the same rubric the automated judge uses. Apply it literally:

> pass = same issue/action as the reference, with no missing essentials —
> additional detail is acceptable when it is accurate and grounded in the
> email; fail = content not grounded in the email (invented or
> hallucinated), contradicting the email or reference, or missing something
> essential; wording may differ freely.

What this means in practice:

- **Wording never matters.** A paraphrase that carries the same essentials
  is a pass, however different it sounds.
- **Extra detail is fine if the email really says it.** If the candidate
  adds something true and traceable to the email text, that is a pass —
  even if the reference doesn't mention it.
- **Fail only for three things:** content the email does not support
  (invented/hallucinated), content that contradicts the email or the
  reference, or a missing essential (the core issue or the core ask is
  absent or wrong).
- **Length and style are never the signal.** Verbose-but-correct is a pass;
  concise-but-missing-the-point is a fail.
- When an email contains multiple requests, the reference reflects the
  customer's one primary request (the newest message's actual ask; requests
  the customer withdrew or replaced do not count). A candidate that
  resurrects a withdrawn request or buries the real one is missing/adding
  essentials — judge accordingly.

## Session rules

1. **Work alone.** Do not discuss any row, verdict, or impression with the
   other annotator until both sheets are complete and handed back. The
   measurement depends on your independence.
2. **One sitting** if possible (plan ~2–2.5 hours for the full 140-row
   sheet; many rows resolve in under a minute).
3. **Only edit the `verdict` and `critique` columns.** Do not reorder rows,
   delete rows, or change any other cell — each row is cryptographically
   bound to the exact candidate text you're judging, and edits elsewhere
   will be detected and rejected by the tooling.
4. Save the file in place as CSV when done.
5. If a row genuinely stumps you, still pick the verdict the rubric points
   to and say why in the critique — there is a built-in adjudication step
   for disagreements, and your honest reading is exactly the data we want.
