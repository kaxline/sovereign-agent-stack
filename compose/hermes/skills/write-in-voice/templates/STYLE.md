---
voice: <voice-name>
calibrated: <YYYY-MM-DD>
sample_count: <n>
word_count: <n>
exemplars:
  - samples/<file>.md
  - samples/<file>.md
---

# Voice: <voice-name>

One paragraph describing this writer the way you would describe them to a ghostwriter.
What does it feel like to read them? Lead with what is distinctive, not what is generic.

## Sentence rhythm

Use the measured values from `corpus-stats.py`. Record the spread as well as the mean,
since much of the voice lives in the variance.

- Mean sentence length: <n> words (median <n>)
- Range: p10 <n>, p90 <n>
- Shape: <e.g. "mostly 12-20 words, punctuated by hard 4-word stops for emphasis">

## Paragraph shape

- Mean paragraph length: <n> sentences
- Typical structure: <e.g. "claim first, then two sentences of support, no summary line">

## Vocabulary

- Register: <formal / conversational / technical / mixed>
- Characteristic words and phrases: <list, from the frequency data and your reading>
- Words used where a plainer synonym was available: <list, or "none">

## Punctuation habits

Per 1000 words, from the stats output.

- Em dash: <n> | Semicolon: <n> | Colon: <n> | Parentheses: <n>
- Question marks: <n> | Exclamation marks: <n>
- Contraction rate: <n>%
- Notes: <e.g. "parentheses for asides, never for citations">

## Openings and closings

- How pieces open: <e.g. "cold open on a concrete detail; never a thesis statement">
- How pieces close: <e.g. "ends on the last point, no recap">

## Structure

- Headers: <never / only in long pieces / always>
- Lists: <never / sparingly / frequently>
- Other recurring moves: <e.g. "poses a question, then answers it two paragraphs later">

## Person and stance

- Person: <first / second / third; how often>
- Stance toward the reader: <peer / teacher / skeptic / other>
- Opinions: <states them plainly / hedges / lets the facts carry it>

## Never does

The section that earns its keep. List concrete, checkable prohibitions drawn from what is
absent in the corpus. Each item should be something you can verify in a finished draft.

- Never <...>
- Never <...>
- Never <...>

## Notes

Anything that did not fit above: inconsistencies across samples, formats that behave
differently (email versus long-form), or dimensions where the corpus gave too little
signal to say anything honest.
