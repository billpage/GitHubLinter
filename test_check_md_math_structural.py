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
bad = 0
for name, (text, want_f, want_s) in CASES.items():
    got_f, got_s = fence(text), span(text)
    ok = got_f == want_f and got_s == want_s
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name, "" if ok else f"fence={got_f} span={got_s}")
print("failures:", bad)
raise SystemExit(1 if bad else 0)
