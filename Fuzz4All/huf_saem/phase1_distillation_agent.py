"""GPT-4o agent that distills compiler source snippets into constraint specifications."""

from __future__ import annotations

from typing import Dict, List

from Fuzz4All.util.api_request import create_config, request_engine

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
(e.g. Value, Instruction, CallInst, SelectInst, BasicBlock, IRBuilder, LLVMContext, \
Module, TreeEntry, IRPosition, or anything in the `llvm::` namespace).
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
Excerpt precondition: "The SelectInst's true/false operands must both be float \
constants, and its condition must be an `icmp ult` comparing the true operand to \
the integer 2."
Good translation:
- Include a ternary expression `cond ? a : b` where `a` and `b` are floating-point \
literals, and `cond` is an unsigned `<` comparison between one of those literals \
(cast to an integer type) and the integer 2.
Bad translation (do NOT do this):
- Create a SelectInst with a TrueVal and FalseVal that are ConstantFP, and a CondVal \
that is an ICmpInst with predicate ICMP_ULT.
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

    def batch_distill(self, snippets: List[Dict], language: str = "cpp") -> List[Dict]:
        """Distill each snippet, dropping empty/untranslatable/duplicate results.

        Returns a list of {"text": <bullet points>, "pass_category": <str>} dicts,
        ordered the same as `snippets` (i.e. highest-complexity first, per
        SourceAnalyzer.top_k_snippets).
        """
        results: List[Dict] = []
        seen: set = set()
        for snippet in snippets:
            text = self.distill_snippet(snippet, language).strip()
            if not text or text.upper().startswith("NO_TRANSLATION") or text in seen:
                continue
            seen.add(text)
            results.append({"text": text, "pass_category": snippet.get("pass_category", "")})
        return results

    def build_constraint_spec(self, constraints: List[Dict], language: str) -> str:
        """Build a single, coherent constraint spec from the best distilled snippet.

        Only the first (highest-complexity) constraint set is used so the
        injected prompt stays focused on one theme, instead of concatenating
        many unrelated snippets into one incoherent block.
        """
        if not constraints:
            return ""
        chosen = constraints[0]
        category_note = (
            f" (inspired by the compiler's {chosen['pass_category']} pass)"
            if chosen.get("pass_category")
            else ""
        )
        return (
            f"The {language} code below already starts with `#include <iostream>` "
            f"and an open `int main() {{` -- continue it by adding statements to "
            f"the body of `main`{category_note}.\n"
            f"Use ONLY standard {language} headers/features already available -- "
            f"do NOT add `#include` directives, top-level function/class "
            f"definitions, another `main`, or any compiler-internal headers, "
            f"types, or APIs; use local lambdas, local classes, or inline blocks "
            f"instead if needed.\n"
            f"Try to incorporate the following characteristics into the code:\n"
            f"{chosen['text']}\n"
            f"IMPORTANT: `#include <iostream>` and `int main() {{` are ALREADY "
            f"written immediately after this comment -- do NOT repeat them or "
            f"write them again in any form. Begin your output directly with the "
            f"first statement of `main`'s body (e.g. a variable declaration), "
            f"as if your text were pasted right after the `{{`."
        )
