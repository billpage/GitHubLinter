"""Regression tests for the two structural rules added after the 2026-10-04
render incident. Run:  python test_check_md_math_structural.py
Cases follow the render probes in README.md."""
from check_md_math import (fence_after_inline_math_in_list_item as F,
                           math_span_split_by_block_marker as S)

def fence(t): return [n for n, *_ in F(t)]
def span(t): return [n for n, *_ in S(t)]

CASES = {
 "A top level":                 ("```math\na=b\n```\n", [], []),
 "C list, no inline math":      ("1. item\n\n   ```math\n   e=f\n   ```\n", [], []),
 "D list, no blank, no inline": ("1. item\n   ```math\n   g=h\n   ```\n", [], []),
 "E blockquote":                ("> text\n>\n> ```math\n> i=j\n> ```\n", [], []),
 "G list in bq, no inline":     ("> 1. item\n>\n>    ```math\n>    m=n\n>    ```\n", [], []),
 "J list + inline, blank":      ("1. Let $`x = 1`$ here:\n\n   ```math\n   a=b\n   ```\n", [3], []),
 "K list + inline, no blank":   ("1. Let $`x = 1`$ here:\n   ```math\n   c=d\n   ```\n", [2], []),
 "L bq + inline":               ("> Let $`x = 1`$ here:\n>\n> ```math\n> e=f\n> ```\n", [], []),
 "list in bq + inline (X6)":    ("> 1. Let $`x = 1`$ here,\n>    ```math\n>    a=b\n>    ```\n", [2], []),
 "N top level + inline":        ("Let $`x = 1`$ here:\n\n```math\ng=h\n```\n", [], []),
 "O list + inline + $$":        ("1. Let $`x = 1`$ here:\n\n   $$i = j$$\n", [], []),
 "P inline only in prev item":  ("1. Let $`x = 1`$ here.\n2. Next:\n\n   ```math\n   k=l\n   ```\n", [4], []),
 "list ends, fence after":      ("1. Let $`x = 1`$ here.\n\nPlain paragraph.\n\n1. New list\n\n   ```math\n   a=b\n   ```\n", [], []),
 "nested sublist inherits":     ("- Let $`x`$.\n  - inner\n\n    ```math\n    a=b\n    ```\n", [4], []),
 "plain-dollar inline (algo)":  ("1. **Bin** onto the $M \\times M$ grid:\n\n   ```math\n   a=b\n   ```\n", [3], []),
 "code span with $` is not math": ("- a `` $`x`$ `` span\n\n  ```math\n  a=b\n  ```\n", [], []),
 "C9 wrapped minus":            ("> gives\n> $`a = b\n> - c`$, which\n", [], [3]),
 "wrapped, safe":               ("> gives\n> $`a = b\n> \; c`$, which\n", [], []),
 "closed span then bullet":     ("> $`a`$ then $`b`$.\n> - item\n", [], []),
}

# --- static rules ported from wpmw (table pipes, \boldsymbol) ---
import re
from check_md_math import table_pipe_math_scan as T, _STATIC_PATTERNS

def static_hit(text):
    return any(rx.search(text) for rx, _ in _STATIC_PATTERNS)

STATIC_CASES = {
 "table pipe, backtick math":   (lambda: [n for n, _ in T("| a | $`\\sum_q |K_q|`$ |\n")] == [1]),
 "table pipe, plain dollars":   (lambda: [n for n, _ in T("| a | $|x|$ |\n")] == [1]),
 "table pipe, escaped":         (lambda: T("| a | $`\\sum_q \\|K_q\\|`$ |\n") == []),
 "pipe outside a table":        (lambda: T("rate $`|x|`$ here\n") == []),
 "pipe in fenced block":        (lambda: T("```\n| a | $`|x|`$ |\n```\n") == []),
 "double-backtick example":     (lambda: T("| `` $`|x|`$ `` |\n") == []),
 "boldsymbol flagged":          (lambda: static_hit("$\\boldsymbol{x}$")),
 "bm flagged":                  (lambda: static_hit("$\\bm{x}$")),
 "pmb and mathbf fine":         (lambda: not static_hit("$\\pmb{\\xi}$ $\\mathbf{x}$")),
}
bad = 0
for name, ok in STATIC_CASES.items():
    bad += not ok()
    print(("ok   " if ok() else "FAIL ") + name)
for name, (text, want_f, want_s) in CASES.items():
    got_f, got_s = fence(text), span(text)
    ok = got_f == want_f and got_s == want_s
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name, "" if ok else f"fence={got_f} span={got_s}")
print("failures:", bad)

# --- paragraph pass: `*` in plain $...$ and wrapped $...$ (2026-10-04) ---
from check_md_math import paragraph_math_emphasis_scan as P

def kinds(t): return [(n, k) for n, k, _ in P(t)]

PARA_CASES = {
 "two ^* in one span":            (lambda: kinds("event $(x^*, t^*)$ here\n") == [(1, "asterisk")]),
 "two ^{*} in one span":          (lambda: kinds("at $(x^{*}, t^{*})$ is\n") == [(1, "asterisk")]),
 "one ^* in each of two lines":   (lambda: len(kinds("**Self.** H, $a = b^{*}$, ap\n$c = d^{*}$ x\n")) == 2),
 "lone ^* beside balanced bold":  (lambda: kinds("the **$M$** grid, $\\rho = \\psi\\psi^*$ with\n") == []),
 "lone ^* alone":                 (lambda: kinds("so $x^*$ here\n") == []),
 "backtick-dollar is protected":  (lambda: kinds("event $`(x^*, t^*)`$ here\n") == []),
 "code span with asterisks":      (lambda: kinds("use `*a*` and $x^*$ here\n") == []),
 "list items are separate":       (lambda: kinds("- $x^*$ a\n- $y^*$ b\n") == []),
 "fenced block ignored":          (lambda: kinds("```\nevent $(x^*, t^*)$\n```\n") == []),
 "converted ^* does not trip 4f":  (lambda: __import__("check_md_math", fromlist=["x"]).emphasis_span_math_scan(
                                        "**Self.** H, $`a^{*}`$, to $X = X'$ gives $`b^{*}`$: ok\n") == []),
 "wrapped span with }_{":         (lambda: kinds("$\\sum c_{a}\n\\mathrm{Re}(Z^{a}_{r})$ has\n") == [(1, "wrapped-underscore")]),
 "wrapped span without trap":     (lambda: kinds("$\\sum c_{a}\n\\mathrm{Re}(Z_r)$ has\n") == []),
 "one-line }_{ left to pass 4b":  (lambda: kinds("so $Z^{a}_{r}$ here\n") == []),
}
bad2 = 0
for name, ok in PARA_CASES.items():
    r = ok()
    bad2 += not r
    print(("ok   " if r else "FAIL ") + name)
print("paragraph-pass failures:", bad2)
raise SystemExit(1 if (bad or bad2) else 0)
