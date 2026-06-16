import re
import subprocess
import time
from typing import List, Union

import torch

from Fuzz4All.target.target import FResult, Target
from Fuzz4All.util.Logger import LEVEL
from Fuzz4All.util.util import comment_remover

main_code = """
int main(){
return 0;
}
"""


def _close_open_braces(code: str) -> str:
    """Append closing braces to balance any unclosed blocks from a truncated generation.

    Scans character-by-character, skipping string/char literals so that
    braces inside quotes are not counted.
    """
    depth = 0
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2  # skip escaped character
                elif code[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
            i += 1
        else:
            i += 1
    if depth > 0:
        code = code.rstrip() + "\n" + "}\n" * depth
    return code


class CPPTarget(Target):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.SYSTEM_MESSAGE = "You are a C++ Fuzzer"
        if kwargs["template"] == "fuzzing_with_config_file":
            config_dict = kwargs["config_dict"]
            self.prompt_used = self._create_prompt_from_config(config_dict)
            self.config_dict = config_dict
        else:
            raise NotImplementedError

    def write_back_file(self, code):
        try:
            with open(
                "/tmp/temp{}.cpp".format(self.CURRENT_TIME), "w", encoding="utf-8"
            ) as f:
                f.write(code)
        except:
            pass
        return "/tmp/temp{}.cpp".format(self.CURRENT_TIME)

    def wrap_prompt(self, prompt: str) -> str:
        return f"/* {prompt} */\n{self.prompt_used['separator']}\n{self.prompt_used['begin']}"

    def wrap_in_comment(self, prompt: str) -> str:
        return f"/* {prompt} */"

    def filter(self, code) -> bool:
        clean_code = code.replace(self.prompt_used["begin"], "").strip()
        target_api = self.prompt_used["target_api"]
        if target_api is not None and target_api not in clean_code:
            return False
        return True

    def clean(self, code: str) -> str:
        code = comment_remover(code)
        # generate() always prepends prompt_used["begin"] to the LLM output.
        # Ollama sometimes echoes the scaffold at the start of its response,
        # producing a double-header.  Three observed forms:
        #   1. Full echo:   LLM repeats begin verbatim.
        #   2. Extra-include echo: LLM adds extra #includes before int main().
        #   3. Partial echo: LLM emits only "int main() {" without the headers.
        # A single regex covers all three: optional #include lines followed by
        # int main() { at position 0 of the content after begin.
        begin = self.prompt_used["begin"]
        after_first = code[len(begin):]
        stripped = after_first.lstrip("\n")
        echo_pat = re.compile(
            r'(?:#include\s*<[^>]+>\s*)*'   # zero or more #include lines
            r'\s*'                            # optional blank lines
            r'int\s+main\s*\(\s*\)\s*\{'    # int main() {
        )
        m = echo_pat.match(stripped)
        if m:
            code = begin + "\n" + stripped[m.end():]
        code = _close_open_braces(code)
        return code

    # remove any comments, blank lines, or scaffold header lines
    def clean_code(self, code: str) -> str:
        code = comment_remover(code)
        # Build a set of individual lines from the scaffold "begin" block so
        # they are excluded from the cleaned code.  The previous comparison
        # tested each line against the full multiline begin string (always
        # False), leaving scaffold headers in the update-strategy example and
        # causing the LLM to echo them (double-header bug).
        begin_lines = {
            ln.strip()
            for ln in self.prompt_used["begin"].splitlines()
            if ln.strip()
        }
        code = "\n".join(
            ln for ln in code.split("\n")
            if ln.strip() and ln.strip() not in begin_lines
        )
        return code

    def validate_compiler(self, compiler, filename) -> (FResult, str):
        # check without -c option (+ linking)
        try:
            exit_code = subprocess.run(
                f"{compiler} -x c++ -std=c++23 {filename} -o /tmp/out{self.CURRENT_TIME}",
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=5,
                text=True,
            )
        except subprocess.TimeoutExpired as te:
            pname = f"'{filename}'"
            subprocess.run(
                ["ps -ef | grep " + pname + " | grep -v grep | awk '{print $2}'"],
                shell=True,
            )
            subprocess.run(
                [
                    "ps -ef | grep "
                    + pname
                    + " | grep -v grep | awk '{print $2}' | xargs -r kill -9"
                ],
                shell=True,
            )  # kill all tests thank you
            return FResult.TIMED_OUT, compiler

        if exit_code.returncode == 1:
            if "undefined reference to `main'" in exit_code.stderr:
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        code = f.read()
                except:
                    pass
                self.write_back_file(code + main_code)
                exit_code = subprocess.run(
                    f"{compiler} -std=c++23 -x c++ /tmp/temp{self.CURRENT_TIME}.cpp -o /tmp/out{self.CURRENT_TIME}",
                    shell=True,
                    capture_output=True,
                    encoding="utf-8",
                    text=True,
                )
                if exit_code.returncode == 0:
                    return FResult.SAFE, "its safe"
            return FResult.FAILURE, exit_code.stderr
        elif exit_code.returncode != 0:
            return FResult.ERROR, exit_code.stderr

        return FResult.SAFE, "its safe"

    def validate_individual(self, filename) -> (FResult, str):
        fresult, msg = self.validate_compiler(self.target_name, filename)
        if fresult == FResult.SAFE:
            return FResult.SAFE, "its safe"
        elif fresult == FResult.ERROR:
            return FResult.ERROR, f"{msg}"
        elif fresult == FResult.TIMED_OUT:
            return FResult.ERROR, "timed out"
        elif fresult == FResult.FAILURE:
            return FResult.FAILURE, f"{msg}"
        else:
            return (FResult.TIMED_OUT,)
