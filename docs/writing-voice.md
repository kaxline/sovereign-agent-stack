# How to generate text in your voice

Step-by-step guide to turning a folder of markdown files into a reusable writing voice, then
drafting new text in it. The mechanism is the repo-shipped `write-in-voice` Hermes skill.

You **calibrate** once, distilling your samples into a style guide, then **write** against
that guide as often as you like. Calibration is the expensive half, which is why it happens
on request instead of being re-derived on every draft.

For the service-level view of mounts and configuration, see [Hermes Agent](hermes.md#writing-in-your-voice).

## Before you start

You need the Hermes profile running:

```bash
docker compose up -d
```

Your corpus lives on the host and is visible to the agent through a bind mount:

| Host path | Container path | Tracked by git |
| --- | --- | --- |
| `data/voice/<name>/samples/` | `/opt/voice/<name>/samples/` | No (`data/` is gitignored) |

Nothing under `data/voice/` gets committed, so personal writing stays on your machine.

## Step 1: Choose your markdown files

Whatever you feed in sets the ceiling on what comes out. The agent imitates the corpus warts
and all, so curate it rather than dumping a folder in.

**Include** finished pieces you would be happy to publish again. Aim for **5 or more pieces
of 500+ words**, and weight quality over volume: eight strong essays will do more than forty
rough notes.

**Exclude** anything not written in the voice you want:

- Co-authored or heavily edited text, where the voice is not purely yours
- Reference material, changelogs, and API docs that are mostly headers, tables, and lists
- Drafts you abandoned because the writing was not working

That second point matters more than it looks. Calibration measures prose, and in a document
made of headers and bullet fragments every fragment counts as one very short sentence. Feed
it reference docs and the measured sentence length collapses, producing a style guide that
tells the agent to write in clipped fragments. Flowing prose is what you want here.

Recognised extensions are `.md`, `.markdown`, `.txt`, `.rst`, and `.text`. Subdirectories are
scanned recursively, and dotfiles are skipped.

## Step 2: Create the voice

Pick a short, memorable name. You will be typing it in every prompt.

```bash
mkdir -p data/voice/my-voice/samples
cp ~/writing/essays/*.md data/voice/my-voice/samples/
```

Confirm the agent can see the files:

```bash
docker compose exec hermes ls /opt/voice/my-voice/samples
```

The mount is live, so files you add appear immediately. Adding samples needs **no** Hermes
restart; only changing the skill itself does.

If that listing comes back empty, check the [troubleshooting](#troubleshooting) table below.

## Step 3: Calibrate

In a Hermes session (dashboard at `http://localhost:9119`, or the CLI):

```text
Calibrate the my-voice writing voice.
```

The agent measures your corpus with a statistics script, reads the samples, picks two to four
exemplars, and writes `data/voice/my-voice/STYLE.md`. That measurement step exists to ground
the style guide in real numbers instead of a model's guess at what they probably are.

Expect a short report of the most distinctive things it found. The real deliverable is
`STYLE.md`, not the summary.

## Step 4: Read and edit STYLE.md

**Most people skip this step. It is the one that pays back the most.**

```bash
open data/voice/my-voice/STYLE.md    # or your editor of choice
```

It is a normal markdown file describing sentence rhythm, paragraph shape, vocabulary,
punctuation habits, how you open and close, and your stance toward the reader. Correcting it
by hand improves output faster than anything else you can do, including adding samples or
rewording prompts.

Two parts deserve extra attention.

**The "Never does" list.** Voice is defined as much by absence as by habit, and absences do not
show up in statistics. Never use exclamation marks? Never address the reader as "you"? Never
open with a thesis statement, never use bulleted lists in prose pieces? Write each one down.
The agent checks drafts against this list item by item, which makes these constraints more
useful at generation time than any amount of positive description.

**The `exemplars` frontmatter.** These are the specific sample files loaded in full whenever
you draft, and they carry real weight: the prose description explains what you do, while the
exemplars show it. Swap them out if the picks are unrepresentative.

```yaml
exemplars:
  - samples/on-pricing.md
  - samples/quarterly-letter.md
```

## Step 5: Write

```text
Write a 600-word post about pricing in the my-voice voice.
```

Behind the scenes the agent loads `STYLE.md` and the exemplars, drafts against the measured
targets, runs a `humanizer` pass to strip AI tells, then self-checks the result against your
"Never does" list before showing it to you.

Some variations worth knowing:

```text
Draft a short announcement about the new release in the my-voice voice.
Rewrite the paragraph below in the my-voice voice: <paste>
Write this in my voice, but keep it under 200 words.
```

If the voice has not been calibrated, the agent stops and offers to calibrate instead of
handing back generic prose. That refusal is on purpose. Text that is not in your voice, handed
over as though it were, costs you more than an empty reply.

## Step 6: Iterate

Drafts are shown in the reply. Ask the agent to save one and it writes to
`data/voice/my-voice/drafts/`.

When a draft is not quite right, **fix `STYLE.md` rather than the prompt**. A prompt tweak
fixes one draft; a `STYLE.md` correction fixes every future draft. Output that is metrically
correct but lifeless usually means averaging: the mean sentence length matched, the variance
lost. Real voices mix long sentences with hard short ones, and the "Sentence rhythm" section
is where that spread gets described.

Re-calibrate whenever you add or remove samples:

```text
Re-calibrate the my-voice writing voice.
```

## Multiple voices

Each subdirectory of `data/voice/` is an independent voice, so you can keep a personal voice
and a company voice side by side:

```text
data/voice/
  my-voice/
    samples/
    STYLE.md
    drafts/
  company-blog/
    samples/
    STYLE.md
```

Name the voice in your prompt. If you have exactly one voice and do not name it, the agent
uses it and tells you which it picked; with several, it asks.

## Optional: run the numbers yourself

Calibration runs this for you, so nothing here is required. It is handy for sanity checking a
corpus before you calibrate, or for seeing whether adding samples actually shifted anything.

```bash
python3 compose/hermes/skills/write-in-voice/scripts/corpus-stats.py data/voice/my-voice/samples
```

It uses the standard library only, so it runs on the host with nothing to install. Abbreviated
output:

```text
Files: 7    Words: 2,229    Sentences: 216

--- Sentence length (words) ---
  mean 10.3   median 9
  p10 2   p25 4   p75 14   p90 20
  distribution:
      1-5 words     82 (38.0%)  ##############################
     6-10 words     50 (23.1%)  ##################
    11-15 words     41 (19.0%)  ###############

--- Punctuation (per 1000 words) ---
  em dash           16.60   (total 37)
  semicolon          4.93   (total 11)
  exclamation mark   0.00   (total 0)
```

That example is the repo's own documentation, and it demonstrates the Step 1 failure mode
nicely: 38% of "sentences" run five words or shorter, because headers and list fragments get
counted as sentences. Real prose produces a much flatter distribution. Frontmatter, code
blocks, and inline code are stripped before measuring; headers and lists are not.

Zero counts tell you as much as high ones. Not a single exclamation mark anywhere in a corpus
is exactly the kind of finding that belongs in the "Never does" list.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Agent says the voice is not calibrated | No `STYLE.md` yet. Run the calibrate prompt from Step 3. |
| Agent cannot find the voice | Check `docker compose exec hermes ls /opt/voice`. A voice needs a `samples/` subdirectory to be recognised. |
| Samples directory looks empty in the container | Files were added outside `data/voice/`, or the Hermes profile is not running. Verify on the host with `ls data/voice/my-voice/samples`. |
| Skill not available at all | Confirm discovery with `docker compose exec hermes hermes skills list \| grep write-in-voice`. See the [skills setup notes](hermes.md#writing-in-your-voice) if it is missing. |
| Measured sentences are far shorter than your real writing | The corpus is reference material, not prose. See Step 1. |
| Drafts are close but flat | Usually the drafting model, not the corpus. Style matching is hard for small local models, and a bigger one tends to beat more samples. |
| Drafts borrow topics from your samples | The exemplars are being leaned on too hard. Swap in ones further from the current subject. |

## Notes

- Samples are read at draft time, so adding files takes effect immediately. Only the skill
  itself is read at startup.
- `STYLE.md` is yours to edit. Nothing overwrites it unless you ask for re-calibration.
- Cleaning up text that already exists is a different job. Use the `humanizer` skill directly
  for that.
