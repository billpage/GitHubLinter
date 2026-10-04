"""
Markdown LaTeX-math linter for GitHub-hosted repositories.

Catches the rendering pitfalls that cause LaTeX math to display incorrectly
on GitHub:

1. **Static** — patterns GitHub's MathJax config rejects even though vanilla
   MathJax/KaTeX would accept them (e.g. ``\\operatorname``, ``\\bm``,
   ``\\href``). These cause a visible "macro is not allowed" error in the
   rendered page. Also covers two cases where GitHub's *preprocessor* never
   recognises ``$...$`` as math at all, so nothing reaches MathJax and the
   dollar signs are left on the page verbatim:

   * an opening ``$`` glued to a hyphen or a quotation mark
     (``-$x$``, ``"$x$``);
   * math sitting inside a single-delimiter emphasis span
     (``*text $x$ text*``), because GitHub renders the markdown to HTML
     *before* scanning for ``$...$`` pairs, and math inside the resulting
     ``<em>`` is never picked up;
   * two ``$`...`$`` backtick-math spans glued directly together with no
     separating whitespace (``$`0`$-$`2`$``): the leftover closing `$` of
     the first span and the leftover opening `$` of the second get paired
     as a spurious plain-math expression whose content is whatever sits
     between them;
   * a closing ``$`` sandwiched between two round close-parentheses, the
     literal sequence ``)$)`` (``($\\Gamma_q(x)$)``): GitHub's parser does
     not recognise the ``$`` as a closing math delimiter in this position
     either.

2. **GFM** — corruption introduced by GitHub's markdown sanitiser before
   the math is handed to MathJax. Two distinct failure modes are covered:

   * CommonMark backslash-strip: ``\\X`` (where X is ASCII punctuation) is
     rewritten as literal X inside math. ``\\,`` becomes a literal comma,
     ``\\!`` a literal bang, ``\\bigl\\{`` becomes ``\\bigl{`` — the last
     produces a hard "Missing or unrecognized delimiter for \\bigl" error;
     the others corrupt spacing silently. ``\\%`` is the sharpest case: it
     becomes a literal ``%``, which opens a LaTeX comment running to end
     of line and silently truncates the rest of the expression, including
     the closing ``$`` — a hard "comment has no terminating newline" error.
   * The ``}_`` emphasis-trap: when `_` follows `}` inside an
     inline ``$...$`` region, CommonMark's emphasis rule fires
     (``}`` is punctuation so the ``_`` is unconditionally
     left-flanking). The underscore is eaten regardless of what
     follows it -- ``}_q``, ``}_0``, ``}_{``, ``}_\\vec`` are all
     broken. See community discussion 65772.

   Both failure modes are skipped for fenced ``\\`\\`\\`math`` blocks and
   for the alternative inline syntax ``$\\`...\\`$`` (backtick-dollar) —
   both forms are exempt from CommonMark inline processing on GitHub.
   The render passes likewise feed protected content to the engines
   verbatim, while dollar-delimited content is stripped first.

3. **Structural** — multi-line ``$$...$$`` blocks placed inside a list item.
   GitHub's markdown preprocessor silently fails to recognise these as math,
   then re-tokenises the indented ``+`` / ``-`` lines as nested bullet items.
   No error message — just garbled output.

   Two more structural rules (added 2026-10-04, from render probes on
   GitHub; see the README): a ``\`\`\`math`` fence inside a *list that
   already has inline math* renders as raw code
   (:func:`fence_after_inline_math_in_list_item`), whereas the same fence in a
   blockquote, at top level, or in a list without inline math typesets; and a
   wrapped ``$\`...\`$`` span whose continuation line starts with a block
   marker (``-``, ``+``, ``*``, ``1.``, ``#``) is cut by markdown, so the
   whole expression shows as code
   (:func:`math_span_split_by_block_marker`).

4. **Render (KaTeX, optional)** — every expression is fed to KaTeX in strict
   mode *after* applying GitHub's CommonMark backslash-strip transformation,
   so the engine sees what GitHub actually feeds the renderer rather than
   the raw source. Catches malformed LaTeX surviving the strip.

5. **Render (MathJax, optional)** — same expressions through MathJax 3 with
   only the ``base`` and ``ams`` packages loaded, matching GitHub's actual
   config. Catches undefined macros (``\\thickspace``, ``\\medspace``, ...)
   that MathJax with the AllPackages set would silently render as raw text.

The two render passes need ``node`` plus the ``katex`` and ``mathjax-full``
npm packages. When those aren't available the passes are skipped with a
warning; the static, GFM, and structural passes always run.

Run as a CLI from the repository root::

    python check_md_math.py docs/ README.md

Math expressions are extracted from ``$...$`` and ``$$...$$``
dollar-delimited math, ``\\`\\`\\`math``-fenced display math, and the
``$\\`...\\`$`` backtick-dollar inline form.

Exits 0 if clean, 1 if any issues found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# 1. Strip code regions from markdown so $-signs in code don't false-match.   #
#    All fenced blocks (including ```math) are blanked here; ```math is        #
#    re-found by :func:`extract_fenced_math` so its source can be tracked.    #
# --------------------------------------------------------------------------- #

_FENCED_BLOCK = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n"
    r"(?P<content>.*?)\n[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# Matches EITHER a `$`...`$` backtick-math span (preserved, not blanked --
# see the note above extract_math() for why this matters) OR a plain `...`
# code span (blanked, as before). The two are tried as alternatives at each
# position rather than as a lookaround exclusion on a single pattern: an
# exclusion approach (`(?<!\$)` / `(?!\$)` around the same greedy
# `[^`\n]+`) shifts the match start to the *second* backtick of a
# `$`...`$` span, whose greedy content-run then swallows every character up
# to the next backtick anywhere in the line -- including an entire
# unrelated second `$`...`$` span and all the prose between them. Matching
# `$`...`$` as its own atomic, non-greedy alternative first avoids that: it
# consumes exactly one span and lets the scan resume immediately after it,
# so two spans separated only by ordinary prose (the common case) are each
# still matched and preserved independently. It also means a documentation
# example like `` `$...$` `` (a single code span whose content happens to
# read like a math delimiter, used to describe this very syntax) is still
# correctly matched by the second alternative and blanked as ordinary code,
# since the first alternative requires an actual leading `$` before the
# backtick and there isn't one at that position.
_CODE_OR_BACKTICK_MATH = re.compile(
    r"\$`(?P<mathcontent>[^`\n]+?)`\$"
    r"|"
    r"`(?P<codecontent>[^`\n]+)`"
)
_INDENTED_CODE_LINE = re.compile(r"^(?: {4}|\t).*$", re.MULTILINE)


def _blank_keep_newlines(match: re.Match) -> str:
    s = match.group(0)
    return "".join("\n" if c == "\n" else " " for c in s)


def _blank_code_only(match: re.Match) -> str:
    if match.group("mathcontent") is not None:
        return match.group(0)  # `$`...`$` -- leave untouched
    return _blank_keep_newlines(match)  # plain `...` -- blank as code


def strip_code(text: str) -> str:
    """Replace code regions with whitespace, preserving line numbers.

    All fenced code blocks are blanked, including ``\\`\\`\\`math``; the
    fenced-math contents are re-found by :func:`extract_fenced_math` so
    that the linter can track which math expressions came from a fenced
    block (exempt from GitHub's CommonMark backslash-strip — verified
    empirically) versus from ``$...$`` / ``$$...$$`` (subject to it).
    Genuine ``$`...`$`` backtick-math spans are left untouched here; see
    the note above :func:`extract_math` for why.
    """
    text = _FENCED_BLOCK.sub(_blank_keep_newlines, text)
    text = _CODE_OR_BACKTICK_MATH.sub(_blank_code_only, text)
    text = _INDENTED_CODE_LINE.sub(_blank_keep_newlines, text)
    return text


# --------------------------------------------------------------------------- #
# 2. Extract math expressions from the (code-stripped) text.                  #
# --------------------------------------------------------------------------- #
#
# Discovered bug (August 2026): §1's inline-code stripper used to blank
# *every* single-backtick span, with no exception for one that is itself
# the content-carrier of a ``$`...`$`` math expression. That ran before
# this module ever looked for the backtick-dollar form, so ``$`K`$`` was
# silently reduced to ``$   $`` before extraction. Two consequences, one
# silent and repo-wide, one loud and specific to a glued pair of spans:
#
#   1. (Repo-wide, silent.) Every ``$`...`$`` expression -- the form these
#      docs recommend precisely because it is immune to the other traps
#      this module catches -- was excluded from the KaTeX/MathJax render
#      pass. `extract_math()` never received the backticks needed to find
#      it, so the render checks only ever validated the far rarer plain
#      ``$...$`` form. Confirmed by probe: ``$`\alpha_`$`` (invalid --
#      trailing `_` with no group) passed with 0 issues under the old
#      code and is correctly flagged once the fix below is in place.
#
#   2. (Loud, when it occurs.) Two ``$`...`$`` spans glued directly
#      together with no separating whitespace -- e.g. ``$`0`$-$`2`$`` --
#      left the residue ``$   $-$   $`` after blanking. The *plain*-math
#      regex below then ran on that residue and found a spurious pair:
#      the leftover closing `$` of the first span and the opening `$` of
#      the second, with the character between them read as the entire
#      expression. If that character is not valid bare LaTeX (an en dash,
#      say), KaTeX rejects it outright; if it happens to be alphanumeric
#      or ordinary punctuation, it silently renders as garbled inline
#      math instead. See §4g below for the corresponding static check --
#      the corrected extractor here cleanly separates the two spans, so
#      there is no longer a bogus expression for the render pass to catch,
#      and only a structural scan can catch this class of bug.
#
# Fix: §1's stripper now matches ``$`...`$`` as its own atomic, non-greedy
# alternative and leaves it untouched, so this function is the only place
# that ever consumes the backtick-dollar form.

_BLOCK_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(
    r"(?<![\\$])"    # opening $ not after \ or another $
    r"\$"
    r"(?![ \t\n$`])"  # not followed by whitespace, $, or backtick
    r"([^\n$]+?)"    # body: no newlines, no $
    r"(?<![ \t])"    # last char not whitespace
    r"\$"
    r"(?![0-9$])"    # not followed by a digit (e.g. $5) or another $
)
# GitHub-specific inline math syntax: $`...`$. The backticks protect the
# content from CommonMark's inline processing (emphasis markers, escapes),
# making this form robust to the `}_{` trap and other markdown-sanitiser
# corruption that plain $...$ is subject to. Documented at
# https://github.blog/changelog/2023-05-08-new-delimiter-syntax-for-inline-mathematical-expressions/
_INLINE_MATH_BACKTICK = re.compile(r"\$`([^`\n]+?)`\$")


@dataclass
class MathExpr:
    mode: str    # "inline" or "display"
    expr: str
    line: int
    start: int
    source: str = "dollar"
    # "dollar"   — $...$ or $$...$$ (subject to GitHub's markdown sanitiser)
    # "fenced"   — ```math (exempt — verified empirically)
    # "backtick" — $`...`$ (exempt — backticks protect content from markdown)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def extract_math(stripped: str) -> list[MathExpr]:
    """Return every $-delimited math expression in the (code-stripped) text.

    Three flavours are recognised:

    * ``$$...$$`` block math — tagged ``source="dollar"``, subject to
      GitHub's CommonMark backslash-strip.
    * ``$\\`...\\`$`` inline math — tagged ``source="backtick"``, exempt
      from CommonMark inline processing because the backticks make the
      content a code span as far as markdown is concerned.
    * ``$...$`` inline math — tagged ``source="dollar"``, subject to
      CommonMark backslash-strip and the ``}_{`` emphasis-trap.
    """
    out: list[MathExpr] = []
    masked = stripped

    for m in _BLOCK_MATH.finditer(stripped):
        out.append(MathExpr("display", m.group(1),
                             _line_of(stripped, m.start()), m.start(),
                             source="dollar"))
        s, e = m.span()
        masked = masked[:s] + _blank_keep_newlines(m) + masked[e:]

    # Backtick-dollar inline math — find these *before* the plain $...$
    # pass so the plain regex doesn't try to consume the $`...`$ form.
    for m in _INLINE_MATH_BACKTICK.finditer(masked):
        out.append(MathExpr("inline", m.group(1),
                             _line_of(masked, m.start()), m.start(),
                             source="backtick"))
        s, e = m.span()
        masked = masked[:s] + _blank_keep_newlines(m) + masked[e:]

    for m in _INLINE_MATH.finditer(masked):
        out.append(MathExpr("inline", m.group(1),
                             _line_of(masked, m.start()), m.start(),
                             source="dollar"))

    out.sort(key=lambda r: r.start)
    return out


# Re-find ```math blocks in the *raw* text so we can tag them as fenced.
_FENCED_MATH = re.compile(
    r"^[ \t]*(?P<fence>`{3,})(?P<info>math[^\n]*)\n"
    r"(?P<content>.*?)\n[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def extract_fenced_math(text: str) -> list[MathExpr]:
    """Return every ``\\`\\`\\`math`` fenced block as a display-math
    expression tagged ``source="fenced"``.

    Operates on the *raw* text (not the code-stripped form), since
    :func:`strip_code` blanks every fenced block and the contents would
    otherwise be lost. Fenced math is exempt from GitHub's CommonMark
    backslash-strip (verified empirically), so the linter applies a
    different set of checks to expressions tagged ``"fenced"``.
    """
    out: list[MathExpr] = []
    for m in _FENCED_MATH.finditer(text):
        # Tighten the info-string check: we only want exactly "math"
        # (possibly followed by whitespace) — not "mathematica" etc.
        info = m.group("info")
        if info != "math" and not info.startswith("math "):
            continue
        out.append(MathExpr(
            mode="display",
            expr=m.group("content"),
            line=_line_of(text, m.start()) + 1,  # +1 to land on the content
            start=m.start(),
            source="fenced",
        ))
    return out


# --------------------------------------------------------------------------- #
# 3. Static scan: GitHub-specific blocked macros.                              #
# --------------------------------------------------------------------------- #

_STATIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\\operatorname\b"),
     r"\operatorname{...} — GitHub's MathJax config rejects this. "
     r"Use \mathrm{...} (function names) or \text{...} (prose)."),
    (re.compile(r"\\DeclareMathOperator\b"),
     r"\DeclareMathOperator — not supported. Use \mathrm{...} inline."),
    (re.compile(r"\\newcommand\b|\\renewcommand\b|\\def\b"),
     r"\newcommand / \def — custom macros are not supported in GitHub math."),
    (re.compile(r"\\begin\{equation\*?\}"),
     r"\begin{equation} — wrap in $$...$$ instead; GitHub does not auto-number."),
    (re.compile(r"\\href\b"),
     r"\href{...} — disabled in GitHub's renderer."),
    (re.compile(r"\\verb\b"),
     r"\verb — not supported."),
    (re.compile(r"\\label\b|\\ref\b|\\eqref\b"),
     r"\label / \ref / \eqref — cross-references are not rendered."),
    (re.compile(r"\\intertext\b"),
     r"\intertext — not supported."),
    (re.compile(r"\\tag\b"),
     r"\tag — equation numbering is not supported."),
    (re.compile(r"\\mathds\b"),
     r"\mathds — dsfont package not available; use \mathbb."),
    (re.compile(r"\\bm\b"),
     r"\bm — bm package not loaded; use \pmb for bold Greek, \mathbf for Latin."),
    (re.compile(r"\\boldsymbol\b"),
     r"\boldsymbol — boldsymbol package not loaded (GitHub loads base and ams "
     r"only); use \pmb for bold Greek, \mathbf for Latin. \mathbf{\xi} renders "
     r"but is not bold."),
    (re.compile(r"\\colorbox\b|\\fcolorbox\b"),
     r"\colorbox / \fcolorbox — not supported."),
    (re.compile(r"\\definecolor\b"),
     r"\definecolor — not supported."),
]


def static_scan(expr: str) -> list[str]:
    return [msg for pat, msg in _STATIC_PATTERNS if pat.search(expr)]


# --------------------------------------------------------------------------- #
# 4. GFM scan: backslash-escapes that GitHub's markdown preprocessor strips.  #
# --------------------------------------------------------------------------- #
#
# GitHub's markdown preprocessor applies CommonMark backslash-escape rules
# *inside* math content, even though it shouldn't — any ``\X`` where X is an
# ASCII-punctuation character is rewritten to literal ``X`` before the math
# is handed to MathJax. This corrupts the most common TeX shortcuts.
#
# Two safe replacement strategies:
#   * **Letter-named macro** — works only when the macro is defined in
#     MathJax's base+ams package set, which is what GitHub uses. ``\,``,
#     ``\!``, ``\{``, ``\}`` have working letter-named alternatives;
#     ``\;`` (thick space) and ``\:`` (medium space) DO NOT — their
#     letter-named forms ``\thickspace`` and ``\medspace`` are not defined
#     in MathJax 3 base+ams and render as raw text on GitHub.
#   * **Doubled backslash** — universal: ``\\;`` is parsed by CommonMark
#     as escaped-backslash (``\\``) followed by literal ``;``, leaving
#     ``\;`` for MathJax. Works for every short form because the underlying
#     short forms are all in MathJax's base package.
#
# The recommendations below pick the simpler form when it's known to work,
# and fall back to doubled-backslash when there's no working letter-named
# alternative.

_GFM_TARGETS = [
    # (single-bs pattern, recommended replacement, fallback, description)
    ("\\,",  r"\thinspace",     r"\\,",  "thin space"),
    ("\\!",  r"\negthinspace",  r"\\!",  "negative thin space"),
    ("\\;",  r"\\;",            r"\\;",  "thick space (no working letter-named form)"),
    ("\\:",  r"\\:",            r"\\:",  "medium space (no working letter-named form)"),
    ("\\{",  r"\lbrace",        r"\\{",  "literal left brace (CRITICAL with \\bigl etc.)"),
    ("\\}",  r"\rbrace",        r"\\}",  "literal right brace (CRITICAL with \\bigr etc.)"),
    ("\\%",  r"\\%",            r"\\%",  "literal percent sign (CRITICAL: the stripped "
                                         "`%` opens a LaTeX comment running to end of "
                                         "line, silently eating the closing `$` and "
                                         "everything meant to follow it)"),
]

# Compile a regex per target with a negative lookbehind so we skip
# occurrences already preceded by a backslash (i.e. already doubled).
_GFM_RE = {
    pat: re.compile(r"(?<!\\)" + re.escape(pat))
    for pat, _, _, _ in _GFM_TARGETS
}


def gfm_escape_scan(expr: str) -> list[tuple[str, str, str, str, int]]:
    """Find TeX shortcuts that GitHub's CommonMark preprocessor will strip.

    Returns a list of ``(pattern, recommended, fallback, description, count)``
    per pattern that occurs in ``expr`` not already doubled.
    """
    out: list[tuple[str, str, str, str, int]] = []
    for pat, repl, fallback, desc in _GFM_TARGETS:
        n = len(_GFM_RE[pat].findall(expr))
        if n > 0:
            out.append((pat, repl, fallback, desc, n))
    return out


def commonmark_strip(s: str) -> str:
    """Apply GitHub's (mis-applied) CommonMark backslash-escape rule
    inside math content: ``\\X`` -> ``X`` for any ASCII punctuation X.

    Used to render expressions through KaTeX/MathJax the way GitHub's
    pipeline actually feeds them, so the render passes catch real failures
    rather than rejecting valid post-strip forms.
    """
    return re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])", r"\1", s)


# --------------------------------------------------------------------------- #
# 4b. Emphasis-trap scan: punctuation + `_` inside inline $...$ math.         #
# --------------------------------------------------------------------------- #
# Per CommonMark S6.1, an underscore opens emphasis when it is left-flanking.
# The rule permits emphasis to open when `_` is followed by non-whitespace AND
# preceded by any Unicode punctuation character.  A closing brace `}` is the
# most common trigger (subscript right after a group: `V^{(2)}_{\vec q}`), but
# any ASCII punctuation before `_` is equally broken:
#
#   `}_q`, `}_0`, `}_{`  -- subscript after closing brace (most common)
#   `'_i`, `'_m`, `'_{`  -- subscript after prime (x'_i, f'_{n})
#   `)_n`, `|_{a}`       -- subscript after closing delimiter
#
# The result is that the underscore is eaten and the whole $...$ region
# fails to render; later inline math on the same paragraph line often
# fails too (cascading).
#
# Note: `_` preceded by an ordinary letter or digit is NOT left-flanking
# by this rule, so `r_{ij}` and `\alpha_i` are safe.
#
# Fix: wrap the expression in backtick-dollar form ``$`...`$``.
# Any doubled-backslash spacing (``\\,`` ``\\;``) must be simplified to
# ``\,`` / ``\;`` inside the backtick-dollar region (CommonMark no longer
# strips them).
#
# Community discussion: https://github.com/orgs/community/discussions/65772

def emphasis_trap_scan(expr: str, mode: str) -> list[str]:
    """Return a list of messages for emphasis-trap hits in an inline
    ``$...$`` math expression.

    Only applies to inline math; ``$$...$$`` block math is processed by
    GitHub's parser as a different shape and is not affected by the
    same emphasis-marker rule.
    """
    if mode != "inline":
        return []
    if not re.search(r"(?<=[^\w\s])_\S", expr):
        return []
    m = re.search(r"(?<=[^\w\s])_\S", expr)
    trigger = expr[max(0, m.start()-1):m.end()] if m else "_"
    return [
        f"Inline math contains `{trigger}` -- "
        "GitHub's CommonMark preprocessor treats `_` preceded by "
        "punctuation as an italic opener (CommonMark S6.1). "
        "The underscore is eaten and the whole $...$ region fails to "
        "render; later inline math in the same paragraph may cascade. "
        "Common triggers: `}_{`, `}_q`, `}_0`, `'_i`, `'_{{`, `)_n`. "
        "Fix: switch to backtick-dollar form `$`...`$`. "
        r"Simplify any `\\,` / `\\;` spacing to `\,` / `\;` inside "
        "the backtick-dollar form (CommonMark no longer strips them). "
        "See https://github.com/orgs/community/discussions/65772 ."
    ]


# --------------------------------------------------------------------------- #
# 4c. (Removed) "Inverted-backtick" check -- tested and found unfounded.      #
# --------------------------------------------------------------------------- #
# A prior version of this linter flagged `` `$...$` `` (backtick OUTSIDE the
# dollar signs) as broken, on the theory that "GitHub's math pipeline is
# applied before (or in parallel with) inline-code processing in some
# contexts," so a code span wrapping dollar-shaped content would still be
# reinterpreted as math -- and, worse, that content containing `}_` (the
# emphasis-trap pattern) would fail visually even inside that code span.
#
# That theory does not hold. Tested directly against GitHub's own markdown
# rendering (POST https://api.github.com/markdown, mode=gfm) on the exact
# worked example the check's own docstring used, `` `$\mathbb{Z}_4$` ``, and
# separately on all 19 real instances this check was flagging in this
# repository's own README and bibliography: every single one rendered as a
# plain, correctly protected `<code>` element -- no `<math-renderer>` wrapper
# appeared, and the underscore stayed literal text rather than becoming
# emphasis. This matches CommonMark's specification: a code span has the
# highest inline-parsing precedence, so once backticks are recognised as a
# code span, nothing inside it -- not emphasis markers, not `$`, not
# anything else -- is reinterpreted, regardless of how many other rendering
# passes exist downstream.
#
# The check was traced to a commit (GitHubLinter 7e27d89) that introduced it
# by extrapolation from the (separately, and correctly, verified) emphasis-
# trap discovery, describing itself as "mirroring the wpmw linter update" --
# but wpmw's history contains no equivalent check and no record of this
# specific failure ever being observed on a live page. It appears to have
# been a plausible-sounding inference that was never actually tested.
#
# Net effect while it was active: it produced ONLY false positives here (19
# for 19), each one a legitimate use of a code span to *talk about* dollar-
# math syntax in prose -- exactly the kind of documentation this repository
# is full of. Removed rather than merely code-span-aware-ified, since there
# is no known real failure for it to guard against.
#
# If a genuine instance of this failure is ever observed on a live GitHub
# page, reinstate a check here -- but verify it the same way first.


# --------------------------------------------------------------------------- #
# 4e. Hyphen-dollar scan: `-$...$` in raw markdown text.                      #
# --------------------------------------------------------------------------- #
# GitHub's math pipeline does not recognise the opening `$` as a math
# delimiter when it is immediately preceded by a hyphen-minus (`-`).  This
# mirrors the common practice of excluding `-$` to avoid ambiguity with
# negative-value dollar signs such as `-$5`.  The result is that the `$`
# is treated as a literal character and the whole expression fails to render.
#
# The same failure occurs for other non-space characters glued to the
# opening `$`.  Straight and curly quotation marks are the ones observed in
# practice: `"$\mu$ returns to zero,"` leaves a literal `$\mu$` on the
# rendered page.
#
# Example: `Fourier-in-$s$` -- the `$s$` never renders.
# Example: `"$\mu$ returns to zero,"` -- likewise.
# Fix: use the backtick-dollar form ``$`s`$``, which GitHub's parser
# recognises as a distinct construct regardless of the preceding character.

_ADJACENT_DOLLAR_MATH = re.compile(
    r"(?P<lead>[-\"\u201c\u201d\'\u2018\u2019])"
    r"\$(?!`)"          # $ not already the start of the backtick-dollar form
    r"(?![\s$])"        # standard opening condition: not followed by space / $
    r"[^\n$]{1,120}"
    r"(?<![\s])\$"
    r"(?![0-9$`])"      # standard closing condition
)

# Backwards-compatible alias: the rule started life as a hyphen-only check.
_HYPHEN_DOLLAR_MATH = _ADJACENT_DOLLAR_MATH


def adjacent_dollar_scan(text: str) -> list[tuple[int, str]]:
    """Scan raw markdown text for inline ``$...$`` math where the opening
    ``$`` is glued to a hyphen or a quotation mark, returning
    ``(line_number, context)`` tuples.

    GitHub's parser does not recognise the opening ``$`` as a math
    delimiter in these positions.
    """
    results = []
    for m in _ADJACENT_DOLLAR_MATH.finditer(text):
        ln = text.count("\n", 0, m.start()) + 1
        results.append((ln, m.group()))
    return results


# Backwards-compatible alias (the rule began as a hyphen-only check).
hyphen_dollar_scan = adjacent_dollar_scan


# --------------------------------------------------------------------------- #
# 4h. Trailing-paren scan: `)$)` in raw markdown text.                        #
#     (Labelled 4h, not 4f, because this repo's own history already used 4f  #
#     for the emphasis-span check before this one was ported over. It is     #
#     4f in wpmw, where the two checks were added in the other order.)       #
# --------------------------------------------------------------------------- #
# GitHub's math-recognition step fails to treat a closing `$` as a math
# delimiter when the character immediately *before* it is a round
# close-parenthesis *and* the character immediately *after* it is also a
# round close-parenthesis -- the literal three-character sequence `)$)`.
# The dollar signs are then left literal on the rendered page.
#
# Confirmed empirically (August 2026) against the live rendered page for
# every `)$)` occurrence then present in wpmw (billpage/wpmw; 7 instances
# across 5 files): each one left `$...$` unrendered. Expressions ending in
# `)` but followed by anything else (`;`, `,`, `.`, a space, end of line)
# rendered correctly, as did `)$)`-shaped sequences where the character
# before the `$` was a different closing bracket (`]$)`, `}$)`) rather than
# a round parenthesis. The bug appears specific to round parentheses on
# both sides of the delimiter, not to closing brackets generally.
#
# Example (observed on a rendered page, August 2026):
#     (writing $\Gamma$ for $\Gamma_q(x)$) — is
# leaves the literal text `$\Gamma_q(x)$` on the rendered page; the same
# expression not immediately followed by `)` (e.g. `$\Gamma_q(x)$,`)
# renders fine.
#
# Ported from wpmw rather than independently re-verified against a live
# page here: this repo's own README.md and references/bibliography.md
# contain zero `)$)` occurrences at the time of porting (checked directly
# against the regex below before adding it), so there is nothing in this
# repo's own docs to confirm or refute against -- the mechanism is
# GitHub's math parser, not anything specific to wpmw's content, so the
# wpmw-verified behaviour should transfer, but treat this instance as
# ported-and-trusted rather than independently confirmed until a live
# `)$)` case is actually observed against this repo's own pages.
#
# Fix: use the backtick-dollar form ``$`...`$`` (see the emphasis-trap
# note above). Confirmed empirically to render correctly immediately
# followed by `)`, and with round parentheses inside the content.

_TRAILING_PAREN_DOLLAR = re.compile(
    r"(?<![\\$])\$(?![\s$`])"   # opening $ (standard opening condition)
    r"([^\n$]*\))"              # content, ending in a literal )
    r"\$"                       # closing $
    r"\)"                       # immediately followed by another )
)


def trailing_paren_scan(text: str) -> list[tuple[int, str]]:
    """Scan raw markdown text for inline ``$...$`` math whose content ends
    in ``)`` and whose closing ``$`` is immediately followed by another
    ``)`` -- the literal sequence ``)$)`` -- returning
    ``(line_number, context)`` tuples.

    GitHub's math-recognition step does not treat the ``$`` as a closing
    math delimiter in this position.
    """
    results = []
    for m in _TRAILING_PAREN_DOLLAR.finditer(text):
        ln = text.count("\n", 0, m.start()) + 1
        results.append((ln, m.group()))
    return results


# --------------------------------------------------------------------------- #
# 4f. Emphasis-span scan: math inside a single-delimiter emphasis span.       #
# --------------------------------------------------------------------------- #
# GitHub renders the markdown to HTML first and only then looks for `$...$`
# pairs to hand to MathJax.  Math that ends up inside an `<em>` produced by
# single-asterisk or single-underscore emphasis is not picked up, and the
# dollar signs are left on the page verbatim.
#
# Example (observed on a rendered GitHub page, July 2026):
#     *every pair sitting at the same place carries the same $\mu$.*
# renders the literal text `$\mu$` in italics, instead of the symbol.
#
# Fix: use the backtick-dollar form ``$`\mu`$``, whose code span is opaque
# to CommonMark inline processing, or move the math outside the emphasis.
# This bites hardest in figure captions and other italicised prose that
# happens to name a symbol.
#
# Note: doubled delimiters (`**strong**`, `__strong__`) are NOT flagged.
# We have direct evidence only for the single-delimiter case, and have seen
# long-standing `**...$x$...**` run-in headers that render correctly. If a
# counterexample turns up, widen `_EMPH_SPAN`.

_EMPH_SPAN = re.compile(
    r"(?<![\w*_])(?P<d>[*_])(?![\s*_])"
    r"(?P<body>(?:[^\n]|\n(?!\s*\n))+?)"
    r"(?<![\s*_])(?P=d)(?![\w*_])"
)

_PLAIN_INLINE_MATH = re.compile(r"(?<!\$)\$(?!`)(?![\s$])(?P<expr>[^\n$]+?)(?<!\s)\$(?![0-9$`])")


def _plain_math_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by plain ``$...$`` inline math."""
    return [(m.start(), m.end()) for m in _PLAIN_INLINE_MATH.finditer(text)]


# Emphasis cannot run across a block boundary, so neither may a candidate
# span: without this, the trailing `*lo*` of one table row would pair with
# an asterisk in the next one and swallow any math in between.
_BLOCK_BREAK = re.compile(
    r"^\s*(?:\||#{1,6}\s|[-+*]\s|\d+[.)]\s|>\s?|$)"
)


def _prose_blocks(text: str) -> list[tuple[int, str]]:
    """Split into inline-parsing units, as ``(char_offset, block_text)``."""
    blocks: list[tuple[int, str]] = []
    start = 0
    cur: list[str] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        if _BLOCK_BREAK.match(line):
            if cur:
                blocks.append((start, "".join(cur)))
                cur = []
            blocks.append((pos, line))
            start = pos + len(line)
        else:
            if not cur:
                start = pos
            cur.append(line)
        pos += len(line)
    if cur:
        blocks.append((start, "".join(cur)))
    return blocks


def emphasis_span_math_scan(text: str) -> list[tuple[int, str, str]]:
    """Find plain ``$...$`` math sitting inside a single-delimiter emphasis
    span, returning ``(line_number, span_preview, math_preview)`` tuples.

    Call with code-stripped text: inline code spans and fenced blocks must
    not contribute asterisks or dollars.

    Emphasis delimiters that are themselves inside a math expression -- the
    asterisks of ``$(x^{*}, t^{*})$``, for instance -- are ignored, so the
    pass does not fire on superscripted stars. The same holds inside a
    protected ``$`...`$`` span (a converted ``^*`` must not look like
    emphasis to this pass).
    """
    results: list[tuple[int, str, str]] = []
    for offset, block in _prose_blocks(text):
        math_spans = _plain_math_spans(block)
        if not math_spans:
            continue

        # asterisks inside a protected `$`...`$` span are not emphasis either
        protected = [(m.start(), m.end())
                     for m in re.finditer(r"\$`.*?`\$", block, flags=re.S)]

        def inside_math(pos: int, spans=math_spans + protected) -> bool:
            return any(a <= pos < b for a, b in spans)

        for m in _EMPH_SPAN.finditer(block):
            open_at, close_at = m.start(), m.end() - 1
            if inside_math(open_at) or inside_math(close_at):
                continue
            contained = [(a, b) for a, b in math_spans
                         if open_at < a and b <= close_at]
            if not contained:
                continue
            ln = text.count("\n", 0, offset + m.start()) + 1
            span_preview = " ".join(m.group(0).split())
            math_preview = ", ".join(block[a:b] for a, b in contained[:3])
            results.append((ln, span_preview, math_preview))
    return sorted(results)


# --------------------------------------------------------------------------- #
# 4g. Glued backtick-math scan: `$`...`$X$`...`$` with no separating space.   #
# --------------------------------------------------------------------------- #
# Two ``$`...`$`` spans placed directly against each other, with no
# whitespace between the closing `$` of one and the opening `$` of the
# next, leave a stray, unintended plain-math expression behind. The
# backtick-blanking pass (§1) reduces each span's *content* to blanks but
# leaves the four dollar signs in place; the plain ``$...$`` scanner then
# pairs the leftover closing `$` of the first span with the leftover
# opening `$` of the second, reading whatever sits between them -- the
# connecting text itself -- as its entire expression.
#
# Confirmed empirically (August 2026) against a live rendered page:
# ``Moments $`0`$-$`2`$ of the`` (with an en dash as the connector) --
# the dash between the two spans was parsed as its own bare math
# expression and rejected by KaTeX as an unrecognised Unicode character.
# The many ``$`X`$-word`` hyphenated-compound cases elsewhere are NOT this
# trap -- those have whitespace nowhere near the delimiters and render
# correctly. This is specifically the *zero-whitespace, span-touching-span*
# case, and it applies regardless of what the connecting text actually is:
# a connector that happens to be alphanumeric or ordinary punctuation
# won't produce a hard KaTeX error the way an unrecognised symbol does,
# but it still renders as a stray, garbled inline-math fragment instead of
# plain text.
#
# Fix: add a space on at least one side of the connector (only one is
# needed to break the adjacency), or fold both spans into one, e.g.
# ``$`0`$ - $`2`$`` or ``$`0\text{--}2`$``.

_GLUED_BACKTICK_MATH = re.compile(
    r"`\$"          # closing half of one $`...`$ span
    r"(?=\S)"       # connecting text does not open on whitespace
    r"[^\n$]+?"     # the connecting text itself (no $, no newline)
    r"(?<=\S)"      # connecting text does not close on whitespace
    r"\$`"          # opening half of the next $`...`$ span
)


def glued_backtick_math_scan(text: str) -> list[tuple[int, str]]:
    """Scan code-stripped markdown text for two ``$`...`$`` spans placed
    directly against each other with no separating whitespace, returning
    ``(line_number, context)`` tuples.

    GitHub's math parser reads the connecting text between the two spans
    as a stray plain-math expression of its own.
    """
    results = []
    for m in _GLUED_BACKTICK_MATH.finditer(text):
        ln = text.count("\n", 0, m.start()) + 1
        results.append((ln, m.group()))
    return results


# --------------------------------------------------------------------------- #
# 4j. Paragraph scan: `*` inside plain $...$ math, and `$...$` wrapped over   #
#     lines.                                                                  #
# --------------------------------------------------------------------------- #
# GitHub renders the markdown to HTML *before* it looks for ``$...$``, so any
# emphasis the CommonMark parser finds inside a plain dollar span puts
# ``<em>`` tags into the text and the span no longer matches.
#
# (a) ``*``. ``^*`` (complex conjugate) and ``^{*}`` are the usual cases. A
#     ``*`` after punctuation and before punctuation can both open and close
#     emphasis, so with another ``*`` anywhere in the paragraph -- often the
#     second ``^*`` in the same span -- the pair becomes ``<em>`` and eats the
#     math between them. 2026-10-04: ``$\delta = \Lambda_j^s(x^*, t^*)$`` in
#     ``phase_resonance_microdynamics.md`` showed as raw text; cmark-gfm found
#     four more spans of the same shape in three other notes.
# (b) A plain ``$...$`` span wrapped over two source lines is not seen by the
#     per-expression passes (they match one line at a time), so the ``}_{``
#     emphasis trap (4b) went unreported there
#     (``relational_pairing_and_carrier_lock.md``).
#
# Fix for both: backtick-dollar form ``$`...`$``, which GitHub protects.
# Verified against cmark-gfm: the rule reports exactly the six spans cmark
# cuts, and nothing else, on every note in the repository.

_PARA_PLAIN_MATH = re.compile(
    r"(?<![\\$])\$(?![ \t\n$`])"           # opening $
    r"(?P<expr>(?:[^$\n]|\n(?![ \t]*\n))+?)"  # may wrap, never over a blank line
    r"(?<![ \t\n])\$(?![0-9$])"            # closing $
)


def _paragraphs(text: str):
    """Yield ``(first_line_no, [lines])`` for each paragraph, outside fences,
    with blockquote markers removed."""
    para: list[str] = []
    start = 0
    fence: tuple[str, int] | None = None
    for no, raw in enumerate(text.split("\n"), 1):
        line = re.sub(r"^(?:[ \t]*>)+ ?", "", raw)
        st = line.strip()
        fm = re.match(r"^(`{3,}|~{3,})", st)
        if fence is not None:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= fence[1] \
                    and st == fm.group(1):
                fence = None
            continue
        if fm:
            if para:
                yield start, para
            para, fence = [], (fm.group(1)[0], len(fm.group(1)))
            continue
        if not st:
            if para:
                yield start, para
            para = []
            continue
        lm = re.match(r"^([ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+)(?=\S)", line)
        if lm:                       # a list item starts its own block
            if para:
                yield start, para
            para, start = [], no
            line = line[lm.end():]
        if not para:
            start = no
        para.append(line)
    if para:
        yield start, para


def _mask_protected(body: str) -> str:
    """Blank (with spaces, keeping newlines) everything GitHub protects from
    emphasis parsing: code spans, ``$`...`$`` spans and ``$$...$$`` blocks.
    Scans left to right so a backtick inside a math span is not mistaken for
    a code span and vice versa."""
    out = list(body)
    i, n = 0, len(body)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if body.startswith("$`", i):
            k = body.find("`$", i + 2)
            if k < 0:
                i += 2
                continue
            blank(i, k + 2)
            i = k + 2
        elif body.startswith("$$", i):
            k = body.find("$$", i + 2)
            if k < 0:
                i += 2
                continue
            blank(i, k + 2)
            i = k + 2
        elif body[i] == "`":
            j = i
            while j < n and body[j] == "`":
                j += 1
            run = body[i:j]
            k = body.find(run, j)
            while k >= 0 and k + len(run) < n and body[k + len(run)] == "`":
                k = body.find(run, k + 1)
            if k < 0:
                i = j
            else:
                blank(i, k + len(run))
                i = k + len(run)
        else:
            i += 1
    return "".join(out)


def _is_punct(c: str) -> bool:
    return unicodedata.category(c)[0] in "PS"


def _matched_asterisks(body: str) -> set[int]:
    """Positions of ``*`` characters that CommonMark turns into emphasis tags
    (CommonMark 0.30 §6.2, delimiter-run algorithm, ``*`` only)."""
    runs = []    # [start, length, orig_length, can_open, can_close]
    i, n = 0, len(body)
    while i < n:
        if body[i] == "*" and (i == 0 or body[i - 1] != "\\"):
            j = i
            while j < n and body[j] == "*":
                j += 1
            before = body[i - 1] if i > 0 else " "
            after = body[j] if j < n else " "
            ws_b, ws_a = before.isspace(), after.isspace()
            pu_b, pu_a = _is_punct(before), _is_punct(after)
            left = (not ws_a) and (not pu_a or ws_b or pu_b)
            right = (not ws_b) and (not pu_b or ws_a or pu_a)
            runs.append([i, j - i, j - i, left, right])
            i = j
        else:
            i += 1
    matched: set[int] = set()
    bottom: dict[int, int] = {}     # cmark's openers_bottom, keyed by the closer's original length % 3
    for ci, closer in enumerate(runs):
        if not closer[4]:
            continue
        while closer[1] > 0:
            key = closer[2] % 3
            oi = None
            for k in range(ci - 1, bottom.get(key, -1), -1):
                op = runs[k]
                if op[1] == 0 or not op[3]:
                    continue
                if (op[4] or closer[3]) and (op[2] + closer[2]) % 3 == 0 \
                        and not (op[2] % 3 == 0 and closer[2] % 3 == 0):
                    continue
                oi = k
                break
            if oi is None:
                bottom[key] = ci - 1
                break
            op = runs[oi]
            use = 2 if op[1] >= 2 and closer[1] >= 2 else 1
            for t in range(use):
                matched.add(op[0] + op[1] - 1 - t)
                matched.add(closer[0] + t)
            op[1] -= use
            closer[0] += use
            closer[1] -= use
            for k in range(oi + 1, ci):        # delimiters between are dropped
                runs[k][1] = 0
    return matched


def paragraph_math_emphasis_scan(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line, kind, span)`` for plain ``$...$`` spans that markdown
    will cut: ``kind`` is ``"asterisk"`` (an emphasis tag opens or closes
    inside the span) or ``"wrapped-underscore"``."""
    out: list[tuple[int, str, str]] = []
    for first, lines in _paragraphs(text):
        body = _mask_protected("\n".join(lines))
        hit = _matched_asterisks(body)
        for m in _PARA_PLAIN_MATH.finditer(body):
            line = first + body.count("\n", 0, m.start())
            if any(m.start() <= k < m.end() for k in hit):
                out.append((line, "asterisk", m.group()))
            elif "\n" in m.group("expr") and \
                    re.search(r"(?<=[^\w\s])_\S", m.group("expr")):
                out.append((line, "wrapped-underscore", m.group()))
    return out


# --------------------------------------------------------------------------- #
# Unclosed backtick-math scan: `$`...` ` with the closing `$` dropped.       #
# --------------------------------------------------------------------------- #
# The backtick-dollar form is written $`...`$ -- two delimiters, a `$`
# *and* a `` ` `` on each side. Dropping the closing `$` (typically because
# a run-in bold marker or sentence-final period was typed right after the
# closing backtick, e.g. `$`(x, p)`.**`) leaves `$`(x, p)`` -- to
# CommonMark this is nothing but a literal `$` followed by an ordinary
# `` `(x, p)` `` code span. Two consequences, both silent:
#
#   1. GitHub's math-recognition step, which looks for the literal
#      ``$`...`$`` pattern before CommonMark ever runs, does not find it
#      here (the trailing `$` is missing), so the span is never protected
#      from CommonMark's own code-span parsing.
#   2. CommonMark then treats the backticks as a plain code span in the
#      normal way, and code spans are opaque -- their content is not
#      further processed. The orphaned `$` immediately in front survives
#      as literal, visible text, exactly as if it had been typed as prose.
#
# `extract_math()` does not report anything for this case either: at the
# `$` position the backtick-dollar regex requires the whole
# ``$`...`$`` pattern to match starting there and fails; the plain
# ``$...$`` regex then finds no partner `$` on the same line for the lone
# leftover sign. The malformed span is therefore invisible to every check
# that operates on extracted math expressions -- this scan has to run on
# raw text instead, the same way ``list_item_block_math`` does.
#
# Confirmed empirically (August 2026, billpage/wpmw) against a live
# rendered page: ``**Not in $`(x, p)`.** Acting on $`W(x, p)`$, neither
# piece...`` -- the first, malformed span left the literal fragment
# ``Not in (x, p) . Actingon`` on the page (GitHub's own math-detection
# pass greedily paired the orphaned `$` with the *next* line's closing
# `` `$ `` and rendered everything between -- including the intervening
# prose -- as one garbled math expression, which is also why the words
# lost their spaces: LaTeX math mode treats bare whitespace as
# insignificant).
#
# Fix: add the missing trailing `$`.
#
# The scan has to stay code-span-aware to avoid flagging documentation
# that *talks about* this exact syntax inside its own code spans (e.g. a
# style-guide line showing `` `$...$` `` as an example) -- a naive
# ``\$`[^`]+`(?!\$)`` regex over raw text fires on those too, since a
# `$` sitting at the end of one code span's displayed text can be
# immediately followed by the backtick that opens the *next* code span.
# Multi-backtick spans (`` `` `x` `` ``, used to show literal backticks)
# are matched and skipped as a single atomic unit first, for the same
# reason.

_UNCLOSED_BACKTICK_MATH = re.compile(
    r"(?P<fence>`{2,})(?:(?!(?P=fence)).)*?(?P=fence)"  # multi-backtick code span -- skip
    r"|"
    r"\$`[^`\n]+?`\$"                        # well-formed $`...`$ -- skip
    r"|"
    r"\$`(?P<bad>[^`\n]+?)`(?!\$)"           # malformed: closing $ missing
    r"|"
    r"`[^`\n]+`"                             # any other single-backtick code span -- skip
)


def unclosed_backtick_math_scan(text: str) -> list[tuple[int, str]]:
    """Scan *raw* markdown text (not code-stripped -- this check needs to
    see the code spans strip_code() would blank) for a ``$`...``` span
    missing its closing ``$``, returning ``(line_number, context)`` tuples.

    Call on the raw file text, matching :func:`list_item_block_math`: this
    malformed pattern degrades into an ordinary code span under CommonMark,
    so :func:`extract_math` never captures it as a math expression to
    check in the first place.
    """
    results = []
    for m in _UNCLOSED_BACKTICK_MATH.finditer(text):
        if m.group("bad") is None:
            continue
        ln = text.count("\n", 0, m.start()) + 1
        results.append((ln, m.group()))
    return results


_LIST_ITEM_OPEN = re.compile(r"^(\s*)(?:[-+*]\s+|\d+[.)]\s+)")
_BLANK_LINE = re.compile(r"^\s*$")
_DOLLAR_DOLLAR_OPEN = re.compile(r"^(\s*)\$\$\s*$")
_DOLLAR_DOLLAR_INLINE = re.compile(r"^\s*\$\$.+\$\$\s*$")


def list_item_block_math(text: str) -> list[tuple[int, str, str]]:
    """Find multi-line ``$$...$$`` blocks placed inside a list item.

    Returns ``(line_no, message, snippet)`` for each occurrence.
    """
    lines = text.splitlines()
    issues: list[tuple[int, str, str]] = []
    in_list = False
    list_indent = -1
    in_fenced = False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fenced = not in_fenced
            continue
        if in_fenced:
            continue

        m = _LIST_ITEM_OPEN.match(line)
        if m:
            in_list = True
            list_indent = len(m.group(1))
            continue

        if _BLANK_LINE.match(line):
            continue

        # Inside a list item if the current line is indented past list_indent.
        if in_list and (len(line) - len(line.lstrip())) > list_indent:
            md = _DOLLAR_DOLLAR_OPEN.match(line)
            if md and not _DOLLAR_DOLLAR_INLINE.match(line):
                # Look ahead for the closing $$ on a later line.
                for j in range(i, min(i + 60, len(lines))):
                    if _DOLLAR_DOLLAR_OPEN.match(lines[j]):
                        snippet = "\n".join(lines[i - 1:j + 1])
                        if len(snippet) > 200:
                            snippet = snippet[:200] + "..."
                        issues.append((
                            i,
                            "Multi-line $$...$$ block inside a list item — "
                            "GitHub will not recognise it as math. Fix: "
                            "collapse to a single line, "
                            r"or use $$\begin{aligned}...\end{aligned}$$ on one line, "
                            "or write inline $`...`$ spans, "
                            "or move the block out of the list. (A ```math "
                            "fence also works, but only in a list with no "
                            "inline math: see "
                            "fence_after_inline_math_in_list_item.)",
                            snippet.replace("\n", " ↵ "),
                        ))
                        break
            continue

        in_list = False
        list_indent = -1

    return issues


_BQ_PREFIX = re.compile(r"^(?P<bq>(?:[ \t]*>)*)(?P<rest>.*)$")
_FENCE_LINE = re.compile(r"^(?P<f>`{3,}|~{3,})\s*(?P<info>[^`]*)$")
_LIST_MARKER = re.compile(r"^(?P<ind> *)(?P<mk>[-+*]|\d{1,9}[.)])(?P<sp> +)(?P<txt>\S.*)?$")
_ANY_INLINE_MATH = re.compile(
    r"\$`|(?<![$\\])\$(?!\$)[^$\n]+?(?<!\\)\$(?!\$)")
# a line that starts a new block when it follows a paragraph line
_BLOCK_START = re.compile(
    r"^\s*(?:[-+*]\s|\d{1,9}[.)]\s|#{1,6}(?:\s|$)|=+\s*$|-{2,}\s*$|```|~~~)")


def _scan_line(body: str, span_open: bool) -> tuple[bool, bool, str]:
    """Tokenise one line for backtick-math spans and ordinary code spans.

    Returns ``(span_open, saw_math, clean)``: whether a ``$`...`$`` math span
    is still open at end of line, whether any math opened or was open on the
    line, and the line with ordinary code spans blanked.
    """
    out: list[str] = []
    saw = span_open
    i, n = 0, len(body)
    while i < n:
        if span_open:
            k = body.find("`$", i)
            if k < 0:
                out.append(body[i:])
                i = n
            else:
                out.append(body[i:k + 2])
                i, span_open = k + 2, False
        elif body.startswith("$`", i):
            span_open, saw = True, True
            out.append("$`")
            i += 2
        elif body[i] == "`":
            j = i
            while j < n and body[j] == "`":
                j += 1
            run = body[i:j]
            k = body.find(run, j)
            while k >= 0 and k + len(run) < n and body[k + len(run)] == "`":
                k = body.find(run, k + 1)
            if k < 0:
                out.append(run)
                i = j
            else:
                out.append(" " * (k + len(run) - i))
                i = k + len(run)
        else:
            out.append(body[i])
            i += 1
    return span_open, saw, "".join(out)


def _split_bq(line: str) -> tuple[int, str]:
    """Return (blockquote depth, text after the ``>`` markers)."""
    m = _BQ_PREFIX.match(line)
    depth = m.group("bq").count(">")
    rest = m.group("rest")
    if depth and rest.startswith(" "):
        rest = rest[1:]
    return depth, rest


def fence_after_inline_math_in_list_item(text: str) -> list[tuple[int, str, str]]:
    """Find ``\`\`\`math`` fences nested in a list that already has inline math.

    Returns ``(line_no, message, snippet)``. On 2026-10-04 GitHub showed a
    fence as raw code exactly when it sat inside a *list* with inline math
    earlier in that list: render probe cases J and K, and §0 item 3 of
    ``poisson_kicks_and_pair_branching.md``, whose own item had no math but
    whose items 1 and 2 did. The same fence in a blockquote, at top level
    or in a list with no inline math typesets (probe cases A-G, L-N), and so
    does a ``$$`` block (H, I, O). Blockquote depth is tracked, so a list
    inside a quote is still a list.
    """
    out: list[tuple[int, str, str]] = []
    lists: list[list] = []   # [bq_depth, marker_indent, content_indent, has_math]
    open_fence: tuple[str, int] | None = None
    for no, line in enumerate(text.splitlines(), start=1):
        depth, body = _split_bq(line)
        stripped = body.strip()
        if open_fence is not None:
            ch, n = open_fence
            if stripped and set(stripped) == {ch} and len(stripped) >= n:
                open_fence = None
            continue
        if not stripped:
            continue
        indent = len(body) - len(body.lstrip(" "))
        lm = _LIST_MARKER.match(body)
        while lists and lists[-1][0] > depth:
            lists.pop()
        while (lists and lists[-1][0] == depth and indent < lists[-1][2]
               and not (lm and indent == lists[-1][1])):
            lists.pop()
        if lm:
            ci = indent + len(lm.group("mk")) + len(lm.group("sp"))
            if lists and lists[-1][0] == depth and lists[-1][1] == indent:
                lists[-1][2] = ci                      # next sibling item
            else:
                lists.append([depth, indent, ci, False])
            _, saw, clean = _scan_line(lm.group("txt") or "", False)
            if saw or _ANY_INLINE_MATH.search(clean):
                lists[-1][3] = True
            continue
        fm = _FENCE_LINE.match(stripped)
        if fm:
            open_fence = (fm.group("f")[0], len(fm.group("f")))
            info = fm.group("info").strip().split(" ")[0]
            if info == "math" and any(L[3] for L in lists):
                out.append((no,
                            "```math fence inside a list that already has "
                            "inline math: GitHub renders it as raw code "
                            "(render probe cases J, K, 2026-10-04). Fix: write "
                            "the equation as inline $`...`$ spans, or close the "
                            "list and put the display at the top level.",
                            stripped[:60]))
            continue
        if lists:
            _, saw, clean = _scan_line(body, False)
            if saw or _ANY_INLINE_MATH.search(clean):
                for L in lists:
                    L[3] = True
    return out


def math_span_split_by_block_marker(text: str) -> list[tuple[int, str, str]]:
    """Find backtick-math spans continued onto a line that opens a new block.

    A multi-line ``$`...`$`` span whose continuation line begins with
    ``- ``, ``+ ``, ``* ``, ``1. ``, ``#``, a setext underline or a fence is
    cut by markdown: the line becomes a list item, heading or fence, the
    span never closes, and the whole expression shows as code. A minus sign
    at the start of a wrapped line is the usual culprit (2026-10-04,
    Theorem C9 and Lemma T1). Fix: re-wrap so the line starts with another
    token, or put the span on one line.
    """
    out: list[tuple[int, str, str]] = []
    in_fence = False
    span_open = False
    for no, line in enumerate(text.splitlines(), start=1):
        _, body = _split_bq(line)
        if re.match(r"^\s*(```|~~~)", body):
            in_fence = not in_fence
            span_open = False
            continue
        if in_fence:
            continue
        if not body.strip():
            span_open = False
            continue
        if span_open and _BLOCK_START.match(body):
            out.append((no,
                        "A `$`...`$` math span continues onto this line, which "
                        "starts with a block marker; markdown ends the "
                        "paragraph here and the span renders as code. Re-wrap "
                        "the line so it does not start with `-`, `+`, `*`, "
                        "`N.` or `#`.",
                        body.strip()[:60]))
            span_open = False
        # walk the line's `$` ... `$ delimiters to update the open/closed state
        span_open, _, _ = _scan_line(body, span_open)
    return out


# --------------------------------------------------------------------------- #
# 4i. Table-pipe scan: an unescaped `|` inside math on a table row.          #
# --------------------------------------------------------------------------- #
# GitHub splits a table row into cells at every unescaped `|` *before* any
# inline parsing -- before code spans, and so before math, is recognised.
# The GFM spec is explicit that this applies inside inline spans too. An
# absolute value or norm written `$`|x|`$` on a table row therefore cuts
# the math span in two: the cell ends at the first `|`, the page shows the
# literal fragment `$`\Gamma = \sum_q` and nothing after it, and if the
# stray pipes land in the header row the column count changes and the whole
# block stops being a table at all.
#
# Confirmed empirically (September 2026) on the rendered ORIENTATION.md:
# ``| **(D)** | ... at rate $`\Gamma = \sum_q |K_{\mathrm{res}}(q)|`$ ... |``
# rendered as ``rate $`\Gamma = \sum_q`` with the rest of the cell gone;
# reproduced exactly by cmark-gfm, which also showed two tables in
# docs/analysis/species_sectors_and_annihilation.md whose header rows held
# such pipes rendering as plain text rather than as tables.
#
# Fix: escape the pipe as `\|`. GFM removes that backslash when it splits
# the row, so the math renderer receives an ordinary `|` -- the absolute
# value is drawn correctly. (This is why `\|` inside a table row does *not*
# produce the LaTeX double bar; outside a table it would.)
#
# Scope: raw text, rows whose first non-blank character is `|`, outside
# fenced blocks; both ``$`...`$`` and plain ``$...$`` spans. Double-
# backtick code spans are skipped first, so a style guide may show the
# broken form as an example; single-backtick code spans are blanked before
# the plain-dollar pass so their contents are not mistaken for math.

_TABLE_FENCE = re.compile(r"^\s*(```|~~~)")
_DOUBLE_TICK_SPAN = re.compile(r"(`{2,}).*?\1")
_BACKTICK_MATH_SPAN = re.compile(r"\$`(?P<expr>[^`\n]*?)`\$")
_SINGLE_TICK_SPAN = re.compile(r"`[^`\n]*`")
_PLAIN_MATH_SPAN = re.compile(r"(?<![\\$])\$(?P<expr>[^$\n]+?)\$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def table_pipe_math_scan(text: str) -> list[tuple[int, str]]:
    """Scan *raw* markdown text for math spans on table rows that contain an
    unescaped ``|``, returning ``(line_number, span)`` tuples."""
    results = []
    fence = False
    for ln, line in enumerate(text.split("\n"), 1):
        if _TABLE_FENCE.match(line):
            fence = not fence
            continue
        if fence or not line.lstrip().startswith("|"):
            continue
        row = _DOUBLE_TICK_SPAN.sub(lambda m: " " * len(m.group()), line)
        for m in _BACKTICK_MATH_SPAN.finditer(row):
            if _UNESCAPED_PIPE.search(m.group("expr")):
                results.append((ln, m.group()))
        row = _BACKTICK_MATH_SPAN.sub(lambda m: " " * len(m.group()), row)
        row = _SINGLE_TICK_SPAN.sub(lambda m: " " * len(m.group()), row)
        for m in _PLAIN_MATH_SPAN.finditer(row):
            if _UNESCAPED_PIPE.search(m.group("expr")):
                results.append((ln, m.group()))
    return results


# --------------------------------------------------------------------------- #
# 6. Optional render checks via node + katex / mathjax-full.                  #
# --------------------------------------------------------------------------- #

_KATEX_JS = r"""
const katex = require('katex');
const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let item;
  try { item = JSON.parse(line); }
  catch (e) { return; }
  const { id, expr, mode } = item;
  try {
    katex.renderToString(expr, {
      displayMode: (mode === "display"),
      throwOnError: true,
      strict: "error",
      trust: false,
    });
    process.stdout.write(JSON.stringify({id, ok: true}) + "\n");
  } catch (e) {
    process.stdout.write(JSON.stringify({
      id, ok: false, error: String(e.message || e),
    }) + "\n");
  }
});
"""

_MATHJAX_JS = r"""
const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
require('mathjax-full/js/input/tex/base/BaseConfiguration.js');
require('mathjax-full/js/input/tex/ams/AmsConfiguration.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);

// GitHub renders math via MathJax 3 with (effectively) only the base and
// ams TeX packages. Use that minimal set here so undefined commands like
// \thickspace and \medspace throw, instead of being silently rendered as
// raw <mtext> by the noundefined fallback that AllPackages would supply.
const tex = new TeX({ packages: ['base', 'ams'] });
const svg = new SVG({ fontCache: 'none' });
const html = mathjax.document('', { InputJax: tex, OutputJax: svg });

const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let item;
  try { item = JSON.parse(line); } catch (e) { return; }
  const { id, expr, mode } = item;
  try {
    const node = html.convert(expr, { display: (mode === 'display') });
    const out = adaptor.outerHTML(node);
    const errMatch = out.match(/data-mjx-error="([^"]+)"/);
    if (errMatch) {
      process.stdout.write(JSON.stringify({
        id, ok: false,
        error: errMatch[1].replace(/&quot;/g, '"'),
      }) + "\n");
    } else {
      process.stdout.write(JSON.stringify({id, ok: true}) + "\n");
    }
  } catch (e) {
    process.stdout.write(JSON.stringify({
      id, ok: false, error: String(e.message || e),
    }) + "\n");
  }
});
"""


def _node_available() -> bool:
    return shutil.which("node") is not None


def _run_engine(items: list[tuple[int, MathExpr]],
                engine_js: str,
                engine_name: str,
                node_cwd: Path) -> dict[int, dict]:
    """Run a list of expressions through a node-based engine.

    ``items`` is a list of ``(id, MathExpr)`` pairs. ``node_cwd`` is the
    directory whose ``node_modules/`` provides the engine (``katex`` or
    ``mathjax-full``).
    """
    if not items:
        return {}

    if not _node_available():
        print(f"  (skipping {engine_name}: 'node' not found in PATH)",
              file=sys.stderr)
        return {}

    if not (node_cwd / "node_modules").is_dir():
        print(f"  (skipping {engine_name}: no node_modules in {node_cwd})",
              file=sys.stderr)
        return {}

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     dir=str(node_cwd)) as f:
        f.write(engine_js)
        script_path = f.name

    try:
        payload = "\n".join(
            json.dumps({"id": iid, "expr": me.expr, "mode": me.mode})
            for iid, me in items
        )
        try:
            proc = subprocess.run(
                ["node", script_path],
                input=payload, capture_output=True, text=True,
                cwd=str(node_cwd), timeout=180,
            )
        except subprocess.TimeoutExpired:
            print(f"  ({engine_name} timed out)", file=sys.stderr)
            return {}

        results: dict[int, dict] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in r:
                results[r["id"]] = r
        return results
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 7. Top-level scan driver.                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class Issue:
    file: Path
    line: int
    severity: str   # STATIC, GFM, STRUCT, KATEX, MATHJX
    mode: str       # inline, display, ""
    expr: str
    message: str


def _walk_md(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".md":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(q for q in p.rglob("*.md")
                               if ".git" not in q.parts))
    return sorted(set(out))


def scan_paths(paths: Iterable[Path],
               *,
               run_katex: bool = True,
               run_mathjax: bool = True,
               node_cwd: Path | None = None) -> list[Issue]:
    """Scan markdown files for math-rendering issues.

    Returns a flat list of ``Issue`` records. An empty list means clean.
    """
    md_files = _walk_md(paths)
    by_id: dict[int, tuple[Path, MathExpr]] = {}
    next_id = 0
    issues: list[Issue] = []

    # Map id -> the post-CommonMark-strip expression we will actually
    # send to KaTeX and MathJax. Render the form GitHub will receive,
    # not the source-as-written.
    items_for_render: list[tuple[int, MathExpr]] = []

    for md in md_files:
        text = md.read_text(encoding="utf-8")

        # structural pass works on the raw text
        for line, msg, snippet in list_item_block_math(text):
            issues.append(Issue(md, line, "STRUCT", "display", snippet, msg))
        for line, msg, snippet in fence_after_inline_math_in_list_item(text):
            issues.append(Issue(md, line, "STRUCT", "display", snippet, msg))
        for line, msg, snippet in math_span_split_by_block_marker(text):
            issues.append(Issue(md, line, "STRUCT", "inline", snippet, msg))
        for line, expr in unclosed_backtick_math_scan(text):
            msg = (
                f"Backtick-math span `{expr}` is missing its closing `$`. "
                "GitHub's math-detection step looks for the literal "
                "`$`...`$` pattern before CommonMark runs; without the "
                "trailing `$` it doesn't match, so CommonMark parses the "
                "backticks as an ordinary code span instead, and the "
                "orphaned `$` in front is left as literal text -- or, "
                "worse, gets paired by GitHub's own scanner with the next "
                "properly-closed span's closing `` `$ `` on the line, "
                "turning everything in between into one garbled "
                "expression. Fix: add the missing trailing `$`."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr.strip(), msg))
        for line, expr in table_pipe_math_scan(text):
            fixed = re.sub(r"(?<!\\)\|", r"\\|", expr)
            msg = (
                f"Math `{expr}` on a table row contains an unescaped `|`. "
                "GitHub splits table cells at every unescaped pipe before "
                "it recognises math, so the span is cut at the first `|` "
                "and the rest of the cell is lost; in a header row the "
                "column count changes and the block stops being a table. "
                f"Fix: escape it as `\\|` -- `{fixed}`. GFM removes the "
                "backslash when splitting the row, so the math still "
                "receives a plain `|`."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr.strip(), msg))
        for line, kind, span in paragraph_math_emphasis_scan(text):
            if kind == "asterisk":
                msg = (
                    f"Inline math `{span}` contains a `*` that CommonMark "
                    "pairs with another `*` in the paragraph as emphasis "
                    "(`^*` can both open and close). The tags land inside "
                    "the `$...$` span and GitHub shows the raw source. "
                    "Fix: backtick-dollar form `$`...`$`, which GitHub "
                    "protects."
                )
            else:
                msg = (
                    f"Inline math `{span}` wraps over a line and contains "
                    "`_` after punctuation (`}_{`, `)_q`, `'_i`): CommonMark "
                    "treats it as an italic opener and the span fails to "
                    "render. Fix: backtick-dollar form `$`...`$`."
                )
            issues.append(Issue(md, line, "STATIC", "inline", span.strip(), msg))
        for line, expr in adjacent_dollar_scan(strip_code(text)):
            lead, inner = expr[0], expr[2:-1]
            what = "a hyphen" if lead == "-" else "a quotation mark"
            msg = (
                f"Inline math `{expr}` has `$` immediately preceded "
                f"by {what}. GitHub's math parser does not recognise the "
                "opening `$` as a math delimiter in this position, so the "
                "dollar signs are left on the rendered page verbatim. "
                f"Fix: use the backtick-dollar form: "
                f"`{lead}$`{inner}`$` -- the backtick-dollar construct "
                "is recognised regardless of the preceding character."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr.strip(), msg))
        for line, expr in trailing_paren_scan(strip_code(text)):
            inner = expr[1:-2]  # drop opening $ and trailing $)
            msg = (
                f"Inline math `{expr}` has its closing `$` sandwiched "
                "between two `)` characters (the sequence `)$)`). GitHub's "
                "math parser does not recognise the closing `$` as a math "
                "delimiter in this position, so the dollar signs are left "
                "on the rendered page verbatim. "
                f"Fix: use the backtick-dollar form: "
                f"$`{inner}`$) -- the backtick-dollar construct is "
                "recognised even immediately followed by `)`."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr.strip(), msg))
        for line, span, math in emphasis_span_math_scan(strip_code(text)):
            msg = (
                f"Inline math ({math}) sits inside a single-delimiter "
                "emphasis span. GitHub renders the markdown to HTML before "
                "looking for `$...$` pairs, and math inside the resulting "
                "`<em>` is not picked up -- the dollar signs are left on the "
                "rendered page verbatim. "
                "Fix: use the backtick-dollar form `$`...`$` inside the "
                "emphasis, or move the math outside it."
            )
            issues.append(Issue(md, line, "STATIC", "inline", span, msg))
        for line, expr in glued_backtick_math_scan(strip_code(text)):
            gap = expr[2:-2]  # drop the shared `$ ... $` delimiter halves
            msg = (
                f"Two backtick-math spans are glued together with no "
                f"separating space (`{expr}`). GitHub's math parser pairs "
                f"the leftover closing `$` of the first span with the "
                f"leftover opening `$` of the second and reads the "
                f"connecting text `{gap}` as a stray expression of its "
                "own, independent of both real spans. "
                "Fix: add a space on at least one side of the connector, "
                "or fold both spans into one."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr.strip(), msg))

        stripped = strip_code(text)

        # Gather every math expression: $-delimited and ```math fenced.
        # Each is tagged with `source` so the GFM and render passes can
        # treat them differently — fenced math is exempt from GitHub's
        # CommonMark backslash-strip.
        all_exprs = list(extract_math(stripped)) + list(extract_fenced_math(text))

        for me in all_exprs:
            iid = next_id
            next_id += 1
            by_id[iid] = (md, me)

            # For render: dollar-source content is fed through CommonMark
            # strip first (matching what GitHub feeds MathJax). Fenced and
            # backtick-protected content goes to the engines verbatim.
            if me.source == "dollar":
                render_expr = commonmark_strip(me.expr)
            else:
                render_expr = me.expr

            stripped_me = MathExpr(
                mode=me.mode,
                expr=render_expr,
                line=me.line,
                start=me.start,
                source=me.source,
            )
            items_for_render.append((iid, stripped_me))

            # Static pass applies regardless of source: GitHub's MathJax
            # config blocks the same set of macros either way.
            for hit in static_scan(me.expr):
                issues.append(Issue(md, me.line, "STATIC", me.mode, me.expr, hit))

            # GFM and emphasis-trap passes apply *only* to dollar-delimited
            # math. Both fenced ```math and $`...`$ forms are protected
            # from GitHub's CommonMark inline processing.
            if me.source == "dollar":
                for pat, repl, fallback, desc, n in gfm_escape_scan(me.expr):
                    qty = "" if n == 1 else f" (×{n})"
                    if repl == fallback:
                        suggestion = f"`{repl}`"
                    else:
                        suggestion = f"`{repl}` (or `{fallback}`)"
                    msg = (f"GitHub's CommonMark preprocessor strips the "
                           f"backslash from `{pat}` ({desc}) inside math, "
                           f"leaving a literal `{pat[1]}` for MathJax. "
                           f"Replace with {suggestion}{qty}.")
                    issues.append(Issue(md, me.line, "GFM ", me.mode, me.expr, msg))
                for hit in emphasis_trap_scan(me.expr, me.mode):
                    issues.append(Issue(md, me.line, "GFM ", me.mode, me.expr, hit))

    if (run_katex or run_mathjax) and node_cwd is None:
        node_cwd = Path.cwd()

    if run_katex:
        kr = _run_engine(items_for_render, _KATEX_JS, "katex", node_cwd)
        for iid, r in kr.items():
            if not r.get("ok"):
                md, me = by_id[iid]
                err = str(r.get("error", "unknown")).split("\n", 1)[0]
                issues.append(Issue(md, me.line, "KATEX ", me.mode, me.expr, err))

    if run_mathjax:
        mr = _run_engine(items_for_render, _MATHJAX_JS, "mathjax", node_cwd)
        for iid, r in mr.items():
            if not r.get("ok"):
                md, me = by_id[iid]
                err = str(r.get("error", "unknown")).split("\n", 1)[0]
                issues.append(Issue(md, me.line, "MATHJX", me.mode, me.expr, err))

    return issues


# --------------------------------------------------------------------------- #
# 8. CLI.                                                                      #
# --------------------------------------------------------------------------- #

def _format_report(issues: list[Issue], all_files: list[Path]) -> str:
    lines: list[str] = []
    by_file: dict[Path, list[Issue]] = {}
    for it in issues:
        by_file.setdefault(it.file, []).append(it)

    for md in all_files:
        rel = md
        try:
            rel = md.relative_to(Path.cwd())
        except ValueError:
            pass

        if md not in by_file:
            lines.append(f"OK   {rel}")
            continue

        lines.append(f"=== {rel} ({len(by_file[md])} issue(s)) ===")
        for it in sorted(by_file[md], key=lambda i: (i.line, i.severity)):
            preview = it.expr if len(it.expr) <= 90 else it.expr[:87] + "..."
            preview = preview.replace("\n", " ↵ ")
            lines.append(f"  L{it.line:4d} [{it.severity:6s}] [{it.mode:7s}] "
                          f"{it.message}")
            lines.append(f"         EXPR: {preview}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python check_md_math.py",
        description="Lint markdown files for GitHub LaTeX math-rendering issues.",
    )
    parser.add_argument("paths", nargs="*", default=["docs/", "README.md"],
                        help="Files or directories to scan "
                             "(default: docs/ and README.md).")
    parser.add_argument("--no-render", action="store_true",
                        help="Skip the optional KaTeX and MathJax render "
                             "passes (only run static + structural checks).")
    parser.add_argument("--node-cwd", type=Path, default=None,
                        help="Directory whose node_modules/ provides "
                             "katex and mathjax-full (default: cwd).")

    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.paths]

    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"error: paths not found: {', '.join(map(str, missing))}",
              file=sys.stderr)
        return 2

    issues = scan_paths(
        paths,
        run_katex=not args.no_render,
        run_mathjax=not args.no_render,
        node_cwd=args.node_cwd,
    )

    all_files = _walk_md(paths)
    report = _format_report(issues, all_files)
    if report:
        print(report)

    n_static = sum(1 for i in issues if i.severity == "STATIC")
    n_gfm    = sum(1 for i in issues if i.severity == "GFM ")
    n_struct  = sum(1 for i in issues if i.severity == "STRUCT")
    n_render  = sum(1 for i in issues if i.severity in ("KATEX ", "MATHJX"))

    print(f"Summary: {len(issues)} issue(s) "
          f"({n_static} static, {n_gfm} gfm, {n_struct} structural, "
          f"{n_render} render) across {len(all_files)} file(s).")

    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
