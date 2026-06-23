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
    # STL containers / algorithms — commonly mentioned in fallback specs
    "<vector>": ["vector"],
    "<array>": ["std::array"],
    "<string>": ["std::string"],
    "<algorithm>": [
        "std::sort", "std::find", "std::transform", "std::for_each",
        "std::count", "lower_bound", "upper_bound", "std::binary_search",
        "std::copy", "std::fill", "std::reverse", "std::unique",
    ],
    "<functional>": ["std::function", "std::bind"],
    "<numeric>": ["std::accumulate", "std::iota", "std::inner_product"],
    "<utility>": ["std::pair", "std::move", "std::swap", "std::make_pair"],
    "<type_traits>": ["std::is_same", "std::is_same_v", "std::enable_if"],
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
5. The description will be used as guidance for a COMPLETE, self-contained \
{language} program. You MAY describe top-level function/class/template \
definitions as well as code inside `main`. Still describe only standard \
{language} source constructs -- never the compiler's own internal headers, \
types, or APIs.

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

List 2-4 bullet points describing {language} code for a COMPLETE, self-contained \
{language} program so the above code path gets exercised while compiling it. The \
code may use top-level function/class/template definitions and/or statements in \
`main`. Follow the rules and format from the system prompt. Start each bullet \
with '- '.
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
            "- Declare a constexpr auto lambda that computes a value at compile time "
            "via a loop or simple arithmetic (lambdas can be constexpr in C++17+).\n"
            "- Use the result as an array size (e.g. int arr[lambda()]) or in a static_assert.\n"
            "- Verify the compile-time value with a static_assert inside main."
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
            "- Declare a local class or struct template (inside main) parameterized on "
            "a type or non-type parameter — local class templates are valid in C++.\n"
            "- Instantiate the local class template with at least two different arguments.\n"
            "- Add an if constexpr branch or std::is_same_v check inside a member method."
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
            f"The {language} code below starts with {includes_str} already "
            f"present. Write a COMPLETE, self-contained {language} program that "
            f"includes its own `int main()` and exercises the following "
            f"characteristics{category_note}.\n"
            f"You MAY define top-level functions, classes, and templates, and add "
            f"standard `#include` directives as needed. Use ONLY standard "
            f"{language} headers/features -- do NOT use any compiler-internal "
            f"headers, types, or APIs.\n"
            f"Try to incorporate the following characteristics into the code:\n"
            f"{chosen['text']}\n"
            f"IMPORTANT: {includes_str} is ALREADY written immediately after "
            f"this comment -- continue directly from there with the rest of the "
            f"program (you may add more `#include`s, then your definitions and "
            f"`int main()`)."
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
