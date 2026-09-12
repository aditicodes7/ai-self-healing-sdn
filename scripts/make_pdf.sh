#!/bin/bash
# Render an artifact-style HTML page to PDF via headless Chrome.
#
# The page is written for screen: it themes to the viewer's dark mode and
# has a sticky section index. Neither belongs on paper, so a print
# stylesheet is appended here rather than carried in the page itself --
# the screen version stays clean, and paper gets what paper needs.
#
# Usage:  ./scripts/make_pdf.sh <input.html> <output.pdf> ["Title"]
set -euo pipefail

SRC="${1:?usage: make_pdf.sh <input.html> <output.pdf> [title]}"
OUT="${2:?usage: make_pdf.sh <input.html> <output.pdf> [title]}"
TITLE="${3:-Document}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || { echo "Google Chrome not found at $CHROME" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PRINTABLE="$WORK/print.html"

{
  echo '<!doctype html><html lang="en"><head><meta charset="utf-8">'
  echo "<title>$TITLE</title>"
  cat "$SRC"
  cat <<'CSS'
<style>
  /* Paper is always light: the screen page follows the viewer's theme,
     and a dark ground printed is unreadable and wasteful.
     The selector list must MATCH the page's own dark rule
     (:root:not([data-theme="light"]), specificity 0-2-0) or it loses to
     it -- a plain :root here rendered the first build as near-white body
     text on white paper. Equal specificity plus later source order wins. */
  :root,
  :root:not([data-theme="light"]),
  :root[data-theme="dark"] {
    --ground: #ffffff; --surface: #ffffff; --surface-sunk: #f2f4f7;
    --ink: #14181e; --muted: #4d5561; --accent: #0b6675;
    --accent-soft: #eaf4f6; --caution: #8a5209; --caution-soft: #f8f1e4;
    --rule: #d3d9e0; --rule-strong: #b6bfc9;
  }
  @page { size: A4; margin: 16mm 15mm 18mm; }
  body { font-size: 10.5pt; line-height: 1.5; background: #fff; }
  .page { max-width: none; padding: 0; }
  .masthead { padding-block: 0 18px; }
  h1 { font-size: 26pt; }
  .standfirst { font-size: 11.5pt; }

  /* The index is a navigation control, not content. On paper the section
     numbers in the headings do that job. */
  .toc { display: none; }
  .shell { display: block; padding-top: 22px; }
  .body-col { gap: 26px; }

  section { break-inside: auto; }
  h2 { font-size: 15pt; break-after: avoid; margin-top: 4px; }
  h2, h3, .mod-group > .head { break-after: avoid; }
  .table-wrap, .pipeline, .note, .mod-group, tr, li { break-inside: avoid; }
  .table-wrap { overflow: visible; }
  table { font-size: 9pt; }
  th, td { padding: 5px 9px; }
  .stage { flex: 1 1 118px; }
  .stage .what { font-size: 10pt; }
  .stage .who { font-size: 7.5pt; }
  .mod-group dd, .safety .rule-detail, p { max-width: none; }
  footer { margin-top: 28px; }
  a { text-decoration: none; }
</style>
CSS
  echo '</head><body></body></html>'
} > "$PRINTABLE"

# Chrome puts the content in <body> itself when the markup precedes it;
# move our nodes there so the page renders rather than sitting in <head>.
python3 - "$PRINTABLE" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
head_end = t.index("</head>")
body_open = t.index("<body>") + len("<body>")
# Everything after the <title>/<style> block that is real markup belongs in body.
m = re.search(r'(<div class="page">.*?</div>\s*)(?=<style>|</head>)', t, re.S)
if m:
    block = m.group(1)
    t = t.replace(block, "", 1)
    t = t.replace("<body>", "<body>\n" + block, 1)
open(p, "w", encoding="utf-8").write(t)
PY

"$CHROME" --headless --disable-gpu --no-sandbox \
  --virtual-time-budget=12000 --no-pdf-header-footer \
  --print-to-pdf="$OUT" "file://$PRINTABLE" 2>/dev/null

[ -s "$OUT" ] || { echo "PDF was not produced" >&2; exit 1; }
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
