---
name: write-in-voice
description: "Write new text in the user's own voice, calibrated from their writing samples. Use for 'write X in my voice', 'calibrate my voice', 'draft this in my style', or any request to produce prose matching a saved writing style."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [writing, voice, style, drafting, ghostwriting, prose]
    category: creative
    related_skills: [humanizer]
    config:
      - key: writing.voice_path
        description: Directory holding writing-voice corpora (one subdirectory per voice)
        default: "/opt/voice"
        prompt: Path to your writing voice directory
---

# Write in voice

Produce new writing that sounds like a specific person, using a corpus of that person's
own writing as the reference. This is a *generation* skill. For cleaning up text that
already exists, reach for `humanizer` instead; the two compose, and the write path below
calls into it.

## When to use this skill

- "Write a post about X in my voice"
- "Draft this announcement the way I'd write it"
- "Calibrate my voice" / "re-calibrate the `<name>` voice"
- Any request naming a voice that exists under the corpus directory

Do **not** use this skill to set the assistant's own conversational tone. It produces
artifacts in someone's voice; it does not change how you talk.

## Corpus layout

The corpus root is `writing.voice_path` (default `/opt/voice`). Its resolved value is
injected into your context when this skill loads — use that value, not a hardcoded path.

```text
<voice_path>/
  README.md                # user-facing instructions (not a voice)
  <voice-name>/
    samples/               # user-supplied writing, read-only in practice
    STYLE.md               # produced by calibration; hand-editable
    drafts/                # generated output
```

Any directory under `<voice_path>` containing a `samples/` subdirectory is a voice.
Ignore `README.md` and dotfiles when listing voices.

If the user does not name a voice and exactly one exists, use it and say which one you
picked. If several exist, ask which.

## Operation 1: Calibrate

Run once per voice, and again whenever samples are added or removed. Calibration is the
expensive step, so run it on request rather than re-deriving style on every draft.

1. List `<voice_path>/<name>/samples/`. If it is empty or missing, stop and tell the user
   where to put files.
2. Run the stats script and keep its output:

   ```bash
   python3 <skill_dir>/scripts/corpus-stats.py <voice_path>/<name>/samples
   ```

   Use its numbers verbatim. Never estimate sentence lengths or punctuation rates yourself:
   your estimates will be wrong, and wrong numbers make `STYLE.md` actively harmful.
3. Read the samples. With fewer than ~15 files read all of them; above that, read the ten
   longest and skim the rest.
4. Pick 2-4 exemplars: complete pieces that are the most representative and the most
   *finished*. Vary the format where you can (say, one long-form and one short) instead of
   picking four of the same kind. Record them as relative paths; do not copy the files.
5. Write `<voice_path>/<name>/STYLE.md` using `templates/STYLE.md`. Fill every section.
   Where the corpus gives you nothing, say so honestly and move on. Do not invent a pattern.
6. Tell the user the file is hand-editable and worth reading. Summarize the three or four
   most distinctive things you found instead of dumping the whole document back.

### What to look for

The stats script covers the countable dimensions. Everything else is your job: how pieces
open and close, what the author does instead of transitions, recurring structural moves,
the stance they take toward the reader, when they hedge and when they commit, running
jokes or pet phrases.

Your most valuable output is the **never does** list. Voice is defined as much by absence
as by habit, and absences do not show up in statistics. Does the corpus contain no
bulleted lists, no headers, no exclamation marks, no second-person address, no rhetorical
questions? Write each one down. Those constraints do more work at generation time than any
positive description.

## Operation 2: Write

1. Resolve the voice. Read `<voice_path>/<name>/STYLE.md`.
2. **If `STYLE.md` does not exist, stop.** Say the voice has not been calibrated yet and
   offer to calibrate now. Never fall back to generic prose that approximates the user's
   style; handing over text that is not in their voice, without saying so, costs them more
   than a refusal does.
3. Read the exemplars named in the `STYLE.md` frontmatter, in full. They do more for the
   output than the prose description does, since the description explains what the author
   does and the exemplars show it.
4. Draft, holding to `STYLE.md` — target the measured sentence-length range, respect the
   punctuation habits, use the author's structural moves.
5. Run a `humanizer` pass over the draft to strip AI tells.
6. Self-check against the **never does** list, item by item. Fix violations.
7. Present the draft. If asked to save, write it to `<voice_path>/<name>/drafts/` with a
   descriptive filename.

Show the finished draft in the reply. Writing to a file and reporting success is not enough.

## Pitfalls

- **Calibrating from unfinished writing.** The corpus gets imitated warts and all. When the
  output is bad, look at the inputs before adjusting the process.
- **Averaging away the voice.** Match the mean sentence length but lose the variance and
  you get something metrically correct and lifeless. Match the distribution, outliers
  included.
- **Topic bleed from exemplars.** They are there for style. A draft that starts borrowing
  their subject matter or examples is leaning on them too hard.
- **Small models.** Style matching is one of the harder tasks for a small local model. When
  output is close but flat, reach for a bigger drafting model before collecting more samples.
- **Stale calibration.** If `samples/` has changed since the `calibrated` date in
  `STYLE.md`, mention it and offer to re-calibrate.

## Verification

After calibrating, confirm `STYLE.md` exists and its `exemplars` paths resolve. For a quick
end-to-end check, draft one short paragraph and ask whether the user would have written it
that way. If the answer is no, the fix usually belongs in `STYLE.md` and not in the prompt.
