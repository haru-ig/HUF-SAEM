"""GPT-4o agent that distills compiler source snippets into constraint specifications."""

from __future__ import annotations

import re
from typing import Dict, List

from Fuzz4All.util.api_request import create_config, request_engine

# Maps each standard header to the function/type names that require it.
# Used to auto-detect which headers the constraint text implies.
_CPP_EXTRA_HEADERS: Dict[str, List[str]] = {
    "<cstring>": [
        "strcmp", "strlen", "strcpy", "strcat", "strncmp", "strncpy",
        "memcpy", "memmove", "memset", "memcmp", "strchr", "strstr", "strtok",
    ],
    "<cstdio>": [
        "printf", "scanf", "fprintf", "sprintf", "sscanf", "fopen", "fclose",
        "fread", "fwrite", "fgets", "fputs", "puts", "FILE",
    ],
    "<cstdlib>": [
        "malloc", "calloc", "realloc", "free", "rand", "srand",
        "atoi", "atof", "strtol", "strtod", "exit", "abort", "qsort",
    ],
    "<cmath>": [
        "sqrt", "pow", "sin", "cos", "tan", "log", "exp",
        "floor", "ceil", "fabs", "fmod", "atan",
    ],
    "<cassert>": ["assert"],
    "<cstdint>": [
        "int8_t", "int16_t", "int32_t", "int64_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    ],
}


def infer_extra_cpp_headers(text: str) -> List[str]:
    """Return sorted list of extra C++ standard headers implied by names in *text*."""
    needed = []
    for header, triggers in _CPP_EXTRA_HEADERS.items():
        pattern = r"\b(?:" + "|".join(re.escape(t) for t in triggers) + r")\b"
        if re.search(pattern, text):
            needed.append(header)
    return needed

_SYSTEM = """\
You are helping build test inputs for a compiler fuzzer. You will be shown a short \
excerpt from a compiler's OWN internal source code (its optimizer/IR layer) -- this \
is NOT example user code. The test inputs you describe will be ordinary {language} \
source files, compiled by a normal {language} compiler.

Your task: translate the excerpt's preconditions into a description of what the \
*input {language} source file* (plain code a programmer could write and hand to the \
compiler) should contain, so that the compiler's optimizer ends up exercising this \
code path while compiling it.

STRICT RULES:
1. Describe ONLY {language} source-level constructs that a programmer could type: \
expressions, statements, types, function/variable declarations, control flow, \
attributes.
2. NEVER use or mention compiler-internal class/type/API names from the excerpt \
(e.g. tree, gimple, rtx, basic_block, edge, tree_code, opt_pass, cgraph_node, \
gimple_assign, gimple_call, gimple_cond, ssa_name, TREE_TYPE, TREE_CODE, \
TREE_OPERAND, fold_build2, TYPE_OVERFLOW_UNDEFINED, or any GCC-internal macro/type).
3. NEVER suggest #include-ing, linking against, or calling the compiler's own \
headers/API.
4. If the excerpt has no plausible {language}-source-level analogue, respond with \
exactly: NO_TRANSLATION
5. The description will be used as guidance for code that CONTINUES inside an \
already-open `int main() {{ ... }}` function body (with `#include <iostream>` \
already present). Do NOT describe extra `#include` directives, new top-level \
function/class definitions, or another `main`. If the excerpt's precondition \
concerns a "function", describe its {language} analogue as a local lambda, local \
class/struct, or inline block of statements -- something that can be written \
*inside* `main`.

Example:
Excerpt precondition: "A gimple_assign with MINUS_EXPR where both operands are \
INTEGER_CST nodes and TYPE_OVERFLOW_UNDEFINED holds on TREE_TYPE of the result."
Good translation:
- Subtract two compile-time integer constants of a signed type, e.g. \
`constexpr int a = 5, b = 3; int r = a - b;` — the compiler can fold this \
at compile time because signed overflow is undefined.
Bad translation (do NOT do this):
- Create a gimple_assign with MINUS_EXPR where both TREE_OPERAND results are \
INTEGER_CST nodes and TYPE_OVERFLOW_UNDEFINED holds on TREE_TYPE.
"""

_DISTILL_TEMPLATE = """\
Excerpt from the compiler's {pass_category} code ({language}, compiler internals -- \
NOT example user code):

```{language}
{snippet}
```

List 2-4 bullet points describing {language} code to ADD INSIDE THE BODY of an \
already-open `int main() {{ ... }}` function (with `#include <iostream>` already \
present) so the above code path gets exercised while compiling it. Follow the \
rules and format from the system prompt -- in particular, do not introduce new \
top-level functions, another `main`, or extra `#include`s; use local \
lambdas/classes/blocks instead if needed. Start each bullet with '- '.
"""


# Built-in fallback constraints used when GPT-4o distillation yields too few
# unique results (e.g. most GCC snippets return NO_TRANSLATION).
_FALLBACK_CONSTRAINT_TEXTS: List[Dict] = [
    {
        "text": (
            "- Declare local variables of mixed arithmetic types (int, double, unsigned long) "
            "and perform arithmetic between them, exercising implicit conversions.\n"
            "- Iterate over a stack-allocated array of those values with a range-based for loop.\n"
            "- Compare results with both == and != to exercise integer promotion rules."
        ),
        "pass_category": "arithmetic type promotion",
    },
    {
        "text": (
            "- Declare a local struct or class with a constructor, destructor, and at least "
            "one copy or move operation.\n"
            "- Create stack instances and let them go out of scope, exercising RAII cleanup.\n"
            "- Pass one instance by value to a local lambda to trigger copy/move elision."
        ),
        "pass_category": "object lifetime / RAII",
    },
    {
        "text": (
            "- Write a constexpr function that computes a value at compile time via "
            "recursion or a loop.\n"
            "- Use the result as a template non-type argument or array size.\n"
            "- Verify the compile-time value with a static_assert."
        ),
        "pass_category": "constant folding / constexpr",
    },
    {
        "text": (
            "- Use std::vector or std::array with at least one erase, insert, or resize call.\n"
            "- Iterate with both index-based and iterator-based loops.\n"
            "- Sort the container and perform a binary search or lower_bound lookup."
        ),
        "pass_category": "STL container / iterator",
    },
    {
        "text": (
            "- Use a local lambda with a capture list (by value and by reference) and "
            "call it multiple times with different arguments.\n"
            "- Store the lambda in an auto variable and pass it to std::for_each.\n"
            "- Return a value from the lambda that depends on the captured state."
        ),
        "pass_category": "lambda / closure",
    },
    {
        "text": (
            "- Use bit-manipulation operators (&, |, ^, ~, <<, >>) on unsigned integers.\n"
            "- Pack several boolean flags into a single unsigned word and test individual bits.\n"
            "- Shift values by a runtime amount and verify with equality comparisons."
        ),
        "pass_category": "bitwise operations",
    },
    {
        "text": (
            "- Declare a pointer to a stack-allocated object, dereference it, and pass "
            "its address to a local lambda.\n"
            "- Perform pointer arithmetic on a stack array (offset, compare two pointers).\n"
            "- Include nullptr checks before dereferencing."
        ),
        "pass_category": "pointer arithmetic / null check",
    },
    {
        "text": (
            "- Use a try/catch block to handle an exception thrown with throw.\n"
            "- Catch the exception by const reference and inspect its what() or type.\n"
            "- Ensure a destructor (RAII guard) runs on both the normal and exceptional paths."
        ),
        "pass_category": "exception handling / unwind",
    },
    {
        "text": (
            "- Declare a template function or local class template parameterized on a "
            "type or non-type parameter.\n"
            "- Instantiate it with at least two different arguments.\n"
            "- Use if constexpr or std::is_same_v inside to select different code paths."
        ),
        "pass_category": "template instantiation / if constexpr",
    },
    {
        "text": (
            "- Declare a local function pointer or std::function variable and assign "
            "two different callables to it at runtime.\n"
            "- Call through the pointer in a loop, varying the argument each iteration.\n"
            "- Include a branch that selects the callable based on a runtime condition."
        ),
        "pass_category": "indirect call / function pointer",
    },
]


class DistillationAgent:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 512) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def distill_snippet(self, snippet: Dict, language: str = "cpp") -> str:
        messages = [
            {"role": "system", "content": _SYSTEM.format(language=language)},
            {
                "role": "user",
                "content": _DISTILL_TEMPLATE.format(
                    language=language,
                    pass_category=snippet.get("pass_category", "this optimization pass"),
                    snippet=snippet["snippet"][:1500],
                ),
            },
        ]
        config = create_config(
            prev={},
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0.2,
            model=self.model,
        )
        try:
            resp = request_engine(config)
            return resp.choices[0].message.content or ""
        except Exception:
            return ""

    def batch_distill(
        self,
        snippets: List[Dict],
        language: str = "cpp",
        min_specs: int = 5,
    ) -> List[Dict]:
        """Distill each snippet, dropping empty/untranslatable/duplicate results.

        Returns a list of {"text": <bullet points>, "pass_category": <str>} dicts,
        ordered the same as `snippets` (highest-complexity first).  If fewer than
        *min_specs* unique constraints survive, built-in fallback constraints are
        appended so the caller always has enough to rotate through.
        """
        results: List[Dict] = []
        seen: set = set()
        for snippet in snippets:
            text = self.distill_snippet(snippet, language).strip()
            if not text or text.upper().startswith("NO_TRANSLATION") or text in seen:
                continue
            seen.add(text)
            results.append({"text": text, "pass_category": snippet.get("pass_category", "")})

        # Supplement with built-in fallbacks when GPT-4o yields too few specs.
        if len(results) < min_specs:
            for fb in _FALLBACK_CONSTRAINT_TEXTS:
                if len(results) >= min_specs:
                    break
                if fb["text"] not in seen:
                    results.append(fb)
                    seen.add(fb["text"])

        return results

    def _build_single_spec(self, chosen: Dict, language: str) -> str:
        """Build a constraint spec string for one distilled constraint dict."""
        category_note = (
            f" (inspired by the compiler's {chosen['pass_category']} pass)"
            if chosen.get("pass_category")
            else ""
        )
        extra_headers: List[str] = []
        if language in ("cpp", "c"):
            extra_headers = infer_extra_cpp_headers(chosen["text"])
        all_includes = ["<iostream>"] + extra_headers
        includes_str = ", ".join(f"`#include {h}`" for h in all_includes)
        return (
            f"The {language} code below already starts with {includes_str} "
            f"and an open `int main() {{` -- continue it by adding statements to "
            f"the body of `main`{category_note}.\n"
            f"Use ONLY standard {language} headers/features already available -- "
            f"do NOT add `#include` directives, top-level function/class "
            f"definitions, another `main`, or any compiler-internal headers, "
            f"types, or APIs; use local lambdas, local classes, or inline blocks "
            f"instead if needed.\n"
            f"Try to incorporate the following characteristics into the code:\n"
            f"{chosen['text']}\n"
            f"IMPORTANT: {includes_str} and `int main() {{` are ALREADY "
            f"written immediately after this comment -- do NOT repeat them or "
            f"write them again in any form. Begin your output directly with the "
            f"first statement of `main`'s body (e.g. a variable declaration), "
            f"as if your text were pasted right after the `{{`."
        )

    def build_constraint_specs(self, constraints: List[Dict], language: str) -> List[str]:
        """Build one constraint spec string per distilled constraint.

        Returns specs in the same order as *constraints* (highest-complexity
        first).  Returns an empty list when *constraints* is empty.
        """
        return [self._build_single_spec(c, language) for c in constraints]

    def build_constraint_spec(self, constraints: List[Dict], language: str) -> str:
        """Return the first (best) constraint spec.  Kept for backward compat."""
        specs = self.build_constraint_specs(constraints, language)
        return specs[0] if specs else ""
