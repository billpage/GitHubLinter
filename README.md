# GitHubLinter — Markdown LaTeX Math Linter

A Python linter that catches LaTeX math rendering failures in GitHub-hosted
Markdown files **before** they reach readers. Designed to run as a GitHub
Actions CI check on every push that touches `.md` files.

Originally developed in the [WPMW project](https://github.com/billpage/wpmw)
by Claude Opus 4.7.

---

## What it checks

GitHub's math pipeline has several quirks that cause valid LaTeX to render
incorrectly or not at all. The linter catches five classes of problems:

| Pass | Severity | Description |
|------|----------|-------------|
| **Static** | Error | Macros GitHub's MathJax config blocks outright — `\operatorname`, `\bm`, `\href`, `\newcommand`, etc. Cause a visible "macro is not allowed" error. |
| **GFM** | Error/Warning | Corruption introduced by GitHub's CommonMark preprocessor before content reaches MathJax. Covers the backslash-strip (`\,` → literal comma, `\bigl\{` → delimiter error) *and* the punctuation-underscore emphasis-trap: `_` preceded by ANY punctuation (not just `}`) opens italic — `}_q`, `}_0`, `}_{`, `'_i`, `)_n` are all broken. Also adds a **Static** check for the inverted-backtick form `` `$...$` `` (backtick outside the dollars). Applies only to `$...$` / `$$...$$` — fenced ` ```math ` and `` $`...`$ `` (backtick-dollar) are both exempt. |
| **Structural** | Error | Multi-line `$$...$$` blocks inside list items. GitHub silently re-tokenises the indented content as nested bullet items — no error, just garbled output. |
| **KaTeX** | Error | Every expression rendered by KaTeX in strict mode *after* applying the CommonMark strip, so the engine sees exactly what GitHub feeds its renderer. |
| **MathJax** | Error | Same expressions through MathJax 3 with only `base` + `ams` packages — matching GitHub's actual config. Catches macros like `\thickspace` / `\medspace` that a full MathJax install would silently accept. |

The static, GFM, and structural passes are pure Python and always run.
The KaTeX and MathJax render passes require `node` and the respective npm
packages; they are skipped with a warning if unavailable.

---

## Quick start

### Local usage

```bash
# Static + GFM + structural passes only (no node required):
python check_md_math.py --no-render docs/ README.md

# All five passes (requires node + npm packages):
npm install --no-save katex mathjax-full
python check_md_math.py docs/ README.md
```

### As a GitHub Action

Copy `.github/workflows/check_md_math.yml` into your repository. The workflow
triggers on pushes and pull requests that touch any `.md` file, runs all five
passes, and fails the check if any issues are found.

```yaml
# .github/workflows/check_md_math.yml
name: check-md-math

on:
  push:
    branches: [main]
    paths: ["**.md", "check_md_math.py", ".github/workflows/check_md_math.yml"]
  pull_request:
    branches: [main]
    paths: ["**.md", "check_md_math.py", ".github/workflows/check_md_math.yml"]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Install npm render engines
        run: npm install --no-save katex mathjax-full
      - name: Run markdown math linter
        run: python check_md_math.py docs/ README.md
```

Adjust the paths in the final step to match the directories containing your
Markdown files.

---

## CLI reference

```
usage: python check_md_math.py [-h] [--no-render] [--node-cwd NODE_CWD] [paths ...]

positional arguments:
  paths              Files or directories to scan (default: docs/ and README.md).
                     Directories are scanned recursively for *.md files.

options:
  --no-render        Skip the KaTeX and MathJax render passes. Only the static,
                     GFM, and structural passes run. No node required.
  --node-cwd DIR     Directory whose node_modules/ provides katex and mathjax-full
                     (default: current working directory).
```

Exit codes: `0` = clean, `1` = issues found, `2` = bad arguments / path not found.

---

## Sample output

```
=== docs/algorithm.md (2 issue(s)) ===
  L  42 [STATIC] [display] \operatorname{...} — GitHub's MathJax config rejects this.
                             Use \mathrm{...} (function names) or \text{...} (prose).
         EXPR:  \operatorname{Tr}\!\left[\hat{\rho}\,\hat{A}\right]

  L  87 [GFM   ] [inline ] GitHub's CommonMark preprocessor strips the backslash
                             from `\,` (thin space) inside math, leaving a literal `,`
                             for MathJax. Replace with `\thinspace` (or `\\,`).
         EXPR:  a\,+\,b

OK   docs/supplement.md
Summary: 2 issue(s) (1 static, 1 gfm, 0 structural, 0 render) across 2 file(s).
```

---

## Style guide for math in GitHub Markdown

A cheat sheet for writing math that renders correctly on GitHub.

### GFM backslash-strip

GitHub's Markdown preprocessor incorrectly applies CommonMark backslash-escape
rules inside math regions. Any `\X` where X is an ASCII punctuation character
is rewritten to `X` before the expression reaches MathJax. Replace with one of
the safe forms:

| Don't write | Write instead | Notes |
|-------------|---------------|-------|
| `\,` | `\thinspace` (preferred) or `\\,` | thin space |
| `\!` | `\negthinspace` (preferred) or `\\!` | negative thin space |
| `\;` | `\\;` | thick space — **no working letter-named form** (see below) |
| `\:` | `\\:` | medium space — **no working letter-named form** (see below) |
| `\{` | `\lbrace` (preferred) or `\\{` | literal left brace — **CRITICAL** with `\bigl` etc. |
| `\}` | `\rbrace` (preferred) or `\\}` | literal right brace — **CRITICAL** with `\bigr` etc. |

> **Why not `\thickspace` and `\medspace`?** These look like the natural
> letter-named alternatives for `\;` and `\:`, but they are *not* defined in
> MathJax 3 with only `base` and `ams` packages — GitHub's actual config.
> On GitHub they render as raw text instead of math spacing. Use doubled
> backslash (`\\;` / `\\:`) for thick and medium spaces.

Fenced ` ```math ` blocks are **exempt** from this strip (verified
empirically). Inside a fenced math block you can write `\,`, `\;`, `\!`,
`\bigl\{`, `\bigr\}` directly — the backslashes reach MathJax intact.

The linter's **GFM pass** enforces this rule for `$...$` and `$$...$$`
expressions and skips it for fenced blocks.

### Blocked macros

GitHub's MathJax configuration loads only the `base` and `ams` packages and
disables several extensions. These macros are not supported:

| Macro | Replacement |
|-------|-------------|
| `\operatorname{...}` | `\mathrm{...}` for function names, `\text{...}` for prose |
| `\DeclareMathOperator` | Use `\mathrm{...}` inline |
| `\newcommand`, `\renewcommand`, `\def` | Not supported — write macros out in full |
| `\begin{equation}` / `\begin{equation*}` | Use `$$...$$` instead |
| `\href{...}` | Disabled |
| `\verb` | Not supported |
| `\label`, `\ref`, `\eqref` | Cross-references are not rendered |
| `\tag` | Equation numbering is not supported |
| `\intertext` | Not supported |
| `\mathds` | Use `\mathbb` instead |
| `\bm` | Use `\boldsymbol{...}` or `\mathbf{...}` |
| `\colorbox`, `\fcolorbox`, `\definecolor` | Not supported |

The linter's **static pass** catches all of these.

### Display math inside list items

A `$$...$$` block that spans multiple lines inside a Markdown list item is
silently misinterpreted — GitHub re-tokenises the indented content as nested
bullets. Fix options in order of preference:

1. **Use a ` ```math ` fenced block** — fenced blocks are recognised inside
   list items even when split over multiple lines (the preferred fix when the
   equation is complex).
2. **Collapse to a single line**: `` $$E = mc^2$$ ``
3. **Use `aligned` on one line**: `$$\begin{aligned} ... \\ ... \end{aligned}$$`
4. **Move the block out of the list** entirely.

The linter's **structural pass** detects this and suggests the fixes above.

### Fenced math blocks (` ```math `)

GitHub also accepts a fenced-code form for display math:

````
```math
\frac{\partial W}{\partial t} + \frac{p}{m}\frac{\partial W}{\partial x} = 0
```
````

This is equivalent to `$$...$$` for display math, and is **exempt from the
CommonMark backslash-strip pipeline** (verified empirically). Inside a fenced
math block you can write `\,`, `\;`, `\!`, `\bigl\{`, `\bigr\}` directly.

Two reasons to prefer the fenced form:

1. **Heavy use of backslash-escapes** — equations with lots of TeX spacing or
   sized-delimiter braces are clearer with `\,` `\;` `\bigl\{` than with
   `\thinspace` `\\;` `\bigl\lbrace`. Switch to a fenced block and write
   natural TeX.
2. **Awkward Markdown context** — fenced blocks survive list-item nesting,
   blockquote nesting, and `<details>` better than `$$...$$`. The structural
   pass already suggests this as one fix when a multi-line `$$` block is
   inside a list item.

Trade-offs: extra surrounding syntax, display-only (no inline use), and
visual diff churn if switching a long-established `$$...$$` block.

The linter applies the static pass (blocked macros) and both render passes to
fenced content, but **skips the GFM pass** — fenced math is exempt by design.

### Additional tips

- **Function names**: use `\mathrm{erf}`, `\mathrm{Tr}`, `\mathrm{sgn}`, etc.
  — same glyph and math-mode spacing as `\operatorname`, universally
  supported.
- **Prose in math**: use `\text{...}` for subscripts like
  `_{\text{short-range}}`, unit labels, etc.
- **Bold math**: use `\boldsymbol{x}` or `\mathbf{x}`, not `\bm{x}` (the
  `bm` package is not loaded on GitHub).
- **Inline math adjacent to digits**: `$x$5` can confuse GitHub's parser.
  A space — `$x$ 5` — avoids the problem entirely.
- **Inline math with `}_` or `'_` (subscript right after a brace or prime): wrap in
  backtick-dollar.** GitHub's markdown preprocessor treats any `_`
  preceded by punctuation as the start of an italic span — regardless of what
  follows the `_`. All of `}_q` (letter), `}_0` (digit), `}_{`
  (brace), `}_\vec` (command), and `'_i` (prime) trigger the trap.
  The underscore is eaten, the whole `$...$` fails to render, and
  other inline math later in the same paragraph often cascades and
  breaks too.
  Reference: community discussion
  [#65772](https://github.com/orgs/community/discussions/65772).

  The fix is GitHub's documented alternative inline-math syntax,
  `$`...`$` (backtick-dollar). The backticks make the content a
  code span as far as markdown is concerned, so the inline emphasis
  rule is skipped entirely.

  | Don't write | Write instead |
  | --- | --- |
  | `$V^{(2)}_{\vec q}$` | `` $`V^{(2)}_{\vec q}`$ `` |
  | `$\|\Gamma^{(2)}_q(r)\|$` | `` $`\|\Gamma^{(2)}_q(r)\|`$ `` |
  | `$W^{(2)}_0(x, p)$` | `` $`W^{(2)}_0(x, p)`$ `` |
  | `$(X_i, X'_i)$` | `` $`(X_i, X'_i)`$ `` |

  **Important:** any doubled-backslash spacing such as `\\,` or `\\;`
  inside the expression must be simplified to `\,` / `\;` inside the
  backtick-dollar form, because the backticks bypass CommonMark's
  processing — the extra backslash is no longer needed.

  Inline math without a punctuation-then-underscore pattern (e.g.
  `$\vec r_{ij}$`, `$V_2$`) is fine as plain `$...$`. Display
  math `$$...$$` is also not affected by this rule.

  The linter's GFM pass enforces this.

- **Inline math with two `^*` (complex conjugate) in the same paragraph.**
  The `*` after `^` is left-flanking per CommonMark and can open
  emphasis. If TWO `^*` expressions appear in the same paragraph,
  the first opens an italic span and the second closes it, eating
  both `$...$` regions between them. The fix is the same:
  `$`...`$` for any expression containing `^*` when another
  such expression is nearby. The linter does not yet detect this
  automatically — watch for it manually.

- **Inverted-backtick form `` `$...$` `` (backtick outside the dollars).**
  GitHub's math pipeline may still attempt to render the content
  even though it is inside a code span, so the code-span protection
  is not reliable. Use the correct form `$`...`$` instead.
  The linter's Static pass detects this.

- **Inline math with `$` immediately preceded by a hyphen** (e.g.
  `Fourier-in-$s$`, `-$N$`). GitHub's math parser excludes `$` as a
  math delimiter when the preceding character is `-`, treating `-$`
  as a negative-dollar sign rather than math. The `$...$` expression
  simply fails to render.
  Fix: use the backtick-dollar form `$`...`$` — it is recognised
  regardless of the preceding character:

  | Don't write | Write instead |
  | --- | --- |
  | `Fourier-in-$s$` | `Fourier-in-$`s`$` |
  | `-$N$` | `-$`N`$` |

  The linter's Static pass detects this.
- **Multi-line `$$...$$` outside lists**: fine and preferred for long
  derivations. The structural restriction applies only inside list items.

---

## Requirements

- Python 3.10+ (uses `|` union type hints from 3.10)
- For render passes: Node.js 18+ with `katex` and `mathjax-full` npm packages

No third-party Python packages are required.

---

## Repository layout

```
check_md_math.py              # The linter — single self-contained file
.github/
  workflows/
    check_md_math.yml         # GitHub Actions CI workflow
README.md                     # This file
```

---

## Origin

This linter was cherry-picked from the
[WPMW project](https://github.com/billpage/wpmw) where it lives at
`src/wpmwlib/check_md_math.py`. The WPMW version is invoked as
`PYTHONPATH=src python -m wpmwlib.check_md_math`; this standalone version
drops the package wrapper and is invoked directly as
`python check_md_math.py`.

The checks are derived from rendering failures actually encountered while
authoring LaTeX-heavy Markdown on GitHub, and have been validated against
GitHub's live renderer.

---

## License

TODO — license not yet selected.
