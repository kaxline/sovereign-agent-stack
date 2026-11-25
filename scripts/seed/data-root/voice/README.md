# Writing voices

Each subdirectory here is one writing voice. Hermes reads them at `/opt/voice`.
Nothing in this directory is tracked by git.

## Setup

1. Create a voice and drop in samples:

       mkdir -p ${DATA_ROOT}/voice/my-voice/samples
       cp ~/writing/*.md ${DATA_ROOT}/voice/my-voice/samples/

   Use finished writing you would be happy to publish again. Whatever lands
   here gets imitated, warts and all. Aim for 5 or more pieces of 500+ words.
   Plain text and markdown are read (.md, .txt, .rst).

2. Calibrate once per voice, and again whenever you add samples. In Hermes:

       Calibrate the my-voice writing voice.

   This writes `${DATA_ROOT}/voice/my-voice/STYLE.md`. Read it. It is a normal markdown
   file, and editing it by hand improves results faster than anything else.
   The "Never does" section deserves the most attention.

3. Write:

       Write a 600-word post about pricing in the my-voice voice.

## Layout

    voice/
      my-voice/
        samples/      # your writing (you add these)
        STYLE.md      # produced by calibration; hand-editable
        drafts/       # generated output

## Notes

- Requires the Hermes profile. For the full step-by-step guide, including how to
  pick a corpus and troubleshoot, see `docs/writing-voice.md`.
- This directory is for drafted artifacts ("write this post in my voice"). It
  does not change how Hermes talks in WebUI or dashboard chat. Chat tone is
  `SOUL.md` on the profile that serves that surface — for the WebUI,
  `data/hermes/profiles/browser/SOUL.md`. Distill samples into that file; do
  not paste this corpus into the prompt. Keep chat logs in a separate
  subdirectory from essay/blog samples. See `docs/hermes.md`.
- Style matching is one of the harder tasks for a small local model. When drafts
  feel flat, try a larger drafting model before adding samples.
- Relocate this tree with `make data-dir-migrate` — see `docs/data-dir.md`.
