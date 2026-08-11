#!/usr/bin/env python3
"""Measure the countable dimensions of a writing corpus.

Usage:  corpus-stats.py <samples-dir> [--top N]

Reports sentence and paragraph length distributions, punctuation rates,
contraction and first-person rates, and the most frequent content words.
Stdlib only. Intended to keep STYLE.md grounded in measurement rather than
in a model's guess at what the numbers probably are.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".text"}

# Abbreviations that end in a period without ending a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg",
    "ie", "cf", "al", "inc", "ltd", "co", "corp", "dept", "est", "fig",
    "approx", "min", "max", "no", "vol", "pp", "ed", "eds", "repr",
}

STOPWORDS = {
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are",
    "as", "at", "back", "be", "because", "been", "before", "being", "but",
    "by", "can", "could", "did", "do", "does", "doing", "don", "down",
    "each", "even", "for", "from", "get", "got", "had", "has", "have",
    "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if",
    "in", "into", "is", "it", "its", "just", "like", "me", "more", "most",
    "much", "my", "no", "not", "now", "of", "off", "on", "one", "only",
    "or", "other", "our", "out", "over", "own", "re", "s", "same", "she",
    "so", "some", "still", "such", "t", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through",
    "to", "too", "up", "us", "very", "was", "way", "we", "well", "were",
    "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "yours",
}

FIRST_PERSON = {"i", "me", "my", "mine", "myself", "we", "us", "our", "ours"}
SECOND_PERSON = {"you", "your", "yours", "yourself", "yourselves"}

PUNCTUATION = [
    ("em dash", r"—|(?<!-)--(?!-)"),
    ("en dash", r"–"),
    ("semicolon", r";"),
    ("colon", r":"),
    ("parentheses", r"\([^)]*\)"),
    ("question mark", r"\?"),
    ("exclamation mark", r"!"),
    ("ellipsis", r"\.\.\.|…"),
    ("curly quote", r"[“”‘’]"),
]

CONTRACTION_RE = re.compile(r"\b\w+['’](?:s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def strip_markup(text):
    """Remove structure that would skew prose measurements."""
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)  # frontmatter
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)  # fenced code
    text = re.sub(r"`[^`]*`", "", text)  # inline code
    text = re.sub(r"^\s{4,}\S.*$", "", text, flags=re.MULTILINE)  # indented code
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links/images
    text = re.sub(r"^\s*<!--.*?-->\s*$", "", text, flags=re.DOTALL | re.MULTILINE)
    return text


def split_sentences(text):
    """Approximate sentence segmentation; good enough for distributions."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])[\"'’”)\]]*\s+", text)
    sentences = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Re-join when the previous fragment ended on a known abbreviation.
        if sentences:
            tail = re.search(r"([A-Za-z]+)\.$", sentences[-1])
            if tail and tail.group(1).lower() in ABBREVIATIONS:
                sentences[-1] = sentences[-1] + " " + part
                continue
        sentences.append(part)
    return sentences


def percentile(values, pct):
    if not values:
        return 0
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[idx]


def median(values):
    return percentile(values, 50)


def histogram(lengths):
    buckets = [(0, 5), (6, 10), (11, 15), (16, 20), (21, 30), (31, 45), (46, 10**6)]
    labels = ["1-5", "6-10", "11-15", "16-20", "21-30", "31-45", "46+"]
    counts = []
    for low, high in buckets:
        counts.append(sum(1 for n in lengths if low <= n <= high))
    return list(zip(labels, counts))


def collect_files(root):
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in TEXT_SUFFIXES
        and not p.name.startswith(".")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples_dir", help="Directory of writing samples")
    parser.add_argument("--top", type=int, default=30, help="Content words to show")
    args = parser.parse_args()

    root = Path(args.samples_dir).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    files = collect_files(root)
    if not files:
        sys.exit(f"No text samples found in {root} (looked for {', '.join(sorted(TEXT_SUFFIXES))})")

    sentence_lengths = []
    paragraph_lengths = []
    words = []
    punct_counts = Counter()
    contractions = 0
    per_file = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
            continue

        text = strip_markup(raw)
        file_words = WORD_RE.findall(text)
        words.extend(w.lower() for w in file_words)

        file_sentences = 0
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block or re.match(r"^[#>|\-*+\d.]+\s*$", block):
                continue
            sentences = split_sentences(block)
            if not sentences:
                continue
            paragraph_lengths.append(len(sentences))
            file_sentences += len(sentences)
            for sentence in sentences:
                count = len(WORD_RE.findall(sentence))
                if count:
                    sentence_lengths.append(count)

        for label, pattern in PUNCTUATION:
            punct_counts[label] += len(re.findall(pattern, text))
        contractions += len(CONTRACTION_RE.findall(text))

        per_file.append((path.relative_to(root), len(file_words), file_sentences))

    total_words = len(words)
    if not total_words or not sentence_lengths:
        sys.exit("Samples contained no measurable prose.")

    def per_1k(n):
        return n * 1000.0 / total_words

    print("=" * 64)
    print(f"CORPUS: {root}")
    print("=" * 64)
    print(f"Files: {len(per_file)}    Words: {total_words:,}    Sentences: {len(sentence_lengths):,}")
    print()

    print("--- Files ---")
    width = max(len(str(name)) for name, _, _ in per_file)
    for name, wc, sc in per_file:
        print(f"  {str(name):<{width}}  {wc:>6,} words  {sc:>4} sentences")
    print()

    print("--- Sentence length (words) ---")
    mean_sentence = sum(sentence_lengths) / len(sentence_lengths)
    print(f"  mean {mean_sentence:.1f}   median {median(sentence_lengths)}")
    print(f"  p10 {percentile(sentence_lengths, 10)}   p25 {percentile(sentence_lengths, 25)}"
          f"   p75 {percentile(sentence_lengths, 75)}   p90 {percentile(sentence_lengths, 90)}")
    print(f"  min {min(sentence_lengths)}   max {max(sentence_lengths)}")
    print("  distribution:")
    peak = max((c for _, c in histogram(sentence_lengths)), default=1) or 1
    for label, count in histogram(sentence_lengths):
        share = 100.0 * count / len(sentence_lengths)
        bar = "#" * int(round(30.0 * count / peak))
        print(f"    {label:>5} words  {count:>5} ({share:>4.1f}%)  {bar}")
    print()

    print("--- Paragraph length (sentences) ---")
    mean_para = sum(paragraph_lengths) / len(paragraph_lengths)
    print(f"  mean {mean_para:.1f}   median {median(paragraph_lengths)}"
          f"   p90 {percentile(paragraph_lengths, 90)}   max {max(paragraph_lengths)}")
    print()

    print("--- Punctuation (per 1000 words) ---")
    for label, _ in PUNCTUATION:
        count = punct_counts[label]
        print(f"  {label:<16} {per_1k(count):>6.2f}   (total {count})")
    print()

    print("--- Habits ---")
    first = sum(1 for w in words if w in FIRST_PERSON)
    second = sum(1 for w in words if w in SECOND_PERSON)
    print(f"  contractions      {per_1k(contractions):>6.2f} per 1k   (total {contractions})")
    print(f"  first person      {per_1k(first):>6.2f} per 1k   (total {first})")
    print(f"  second person     {per_1k(second):>6.2f} per 1k   (total {second})")
    print()

    print(f"--- Top {args.top} content words ---")
    content = Counter(w for w in words if w not in STOPWORDS and len(w) > 2)
    for word, count in content.most_common(args.top):
        print(f"  {word:<20} {count:>5}   ({per_1k(count):.2f} per 1k)")
    print()
    print("Absences matter as much as these counts. Check the corpus for what is")
    print("missing (headers, lists, exclamation marks, second person) and record")
    print("it in the 'Never does' section of STYLE.md.")


if __name__ == "__main__":
    main()
