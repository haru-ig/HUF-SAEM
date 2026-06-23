"""Central orchestrator for the HUF-SAEM architecture.

Instantiated once per fuzzing run when a 'huf_saem:' block is present in the
YAML config. All phase methods are safe to call unconditionally from fuzz.py —
they return None / the original code when their phase is disabled.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from Fuzz4All.target.target import FResult, Target


class HUFSAEMCoordinator:
    # Per-batch style directives appended to the active constraint spec so the
    # LLM sees a fresh "theme" every batch even when only one spec was distilled.
    _VARIATION_HINTS: List[str] = [
        "STYLE HINT: Prefer unsigned integer types (unsigned int, unsigned long) as the primary data type.",
        "STYLE HINT: Prefer floating-point arithmetic (double or float) as the primary data type.",
        "STYLE HINT: Use std::string and string operations as the primary data type.",
        "STYLE HINT: Use nested loops (for, while, do-while) as the primary control structure.",
        "STYLE HINT: Express logic through local structs or classes with member variables.",
        "STYLE HINT: Use stack-allocated C-style arrays or std::array as the primary container.",
        "STYLE HINT: Use bit-manipulation operators (&, |, ^, <<, >>) prominently.",
        "STYLE HINT: Replace if-else with the ternary operator (?:) wherever possible.",
        "STYLE HINT: Use local lambdas that capture variables from the enclosing scope.",
        "STYLE HINT: Use raw pointer variables with address-of (&) and dereference (*) operations.",
    ]

    def __init__(self, config_dict: Dict[str, Any], target: "Target") -> None:
        self.config = config_dict.get("huf_saem", {})
        self.language = config_dict["target"]["language"]
        self.output_folder = config_dict["fuzzing"]["output_folder"]
        self.target = target

        # Phase 1
        self._p1_source_analyzer = None
        self._p1_distillation_agent = None
        self._p1_prompt_injector = None
        self._p1_constraint_spec: Optional[str] = None   # current spec (for compat)
        self._p1_constraint_specs: List[str] = []        # all specs, ordered by complexity
        self._p1_spec_index: int = 0                     # which spec is active
        self._p1_variation_index: int = 0                # which style hint is active
        self._p1_rotation_interval: int = 50             # files between constraint rotations
        self._p1_batch_size: int = config_dict.get("llm", {}).get("batch_size", 5)

        # Phase 2
        self._p2_mutator_registry = None
        self._p2_mutate_ratio: float = 0.0

        # Phase 3
        self._p3_seed_db = None
        self._p3_island_manager = None
        self._p3_cloze_controller = None

        # Phase 4
        self._p4_coverage_instrumentor = None
        self._p4_branch_tracker = None
        self._p4_cfg_analyzer = None
        self._p4_constraint_agent = None
        self._p4_solution_splicer = None
        self._p4_solver_interval: int = 100
        self._p4_last_generated_foss: List[str] = []

    # ------------------------------------------------------------------
    # One-time setup
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        self.target._huf_saem_coordinator = self  # Phase 1 hook in target.initialize()

        p1 = self.config.get("phase1", {})
        if p1.get("enabled", False):
            self._init_phase1(p1)

        p2 = self.config.get("phase2", {})
        if p2.get("enabled", False):
            self._init_phase2(p2)

        p3 = self.config.get("phase3", {})
        if p3.get("enabled", False):
            self._init_phase3(p3)

        p4 = self.config.get("phase4", {})
        if p4.get("enabled", False):
            self._init_phase4(p4)

    # Delimiter used to join/split multiple constraint specs in the cache file.
    _SPEC_DELIMITER = "\n\n===PHASE1_SPEC_BREAK===\n\n"

    def _init_phase1(self, cfg: Dict) -> None:
        from Fuzz4All.huf_saem.phase1_distillation_agent import (
            DistillationAgent,
            infer_extra_cpp_headers,
        )
        from Fuzz4All.huf_saem.phase1_prompt_injector import PromptInjector
        from Fuzz4All.huf_saem.phase1_source_analyzer import SourceAnalyzer

        source_dir = cfg.get("source_dir")
        if not source_dir:
            return

        source_language = cfg.get("source_language", "cpp")
        threshold = cfg.get("complexity_threshold", 4)
        top_k = cfg.get("top_k_snippets", 10)
        model = cfg.get("distillation_model", "gpt-4o")
        cache_path = cfg.get("constraint_cache")
        self._p1_rotation_interval = int(cfg.get("rotation_interval", 50))

        self._p1_prompt_injector = PromptInjector()

        # Load from cache when available.  Old single-spec caches (no
        # delimiter) are treated as a list of one.
        loaded_from_cache = False
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = f.read()
            if cached.strip():
                parts = [s for s in cached.split(self._SPEC_DELIMITER) if s.strip()]
                if parts:
                    self._p1_constraint_specs = parts
                    loaded_from_cache = True

        if not loaded_from_cache:
            analyzer = SourceAnalyzer(source_dir, source_language, threshold)
            # Request 4× top_k so batch_distill has a larger filtered pool.
            snippets = analyzer.top_k_snippets(top_k * 4)
            agent = DistillationAgent(model=model)
            constraints = agent.batch_distill(snippets, self.language)
            self._p1_constraint_specs = agent.build_constraint_specs(constraints, self.language)
            if cache_path and self._p1_constraint_specs:
                os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(self._SPEC_DELIMITER.join(self._p1_constraint_specs))

        if not self._p1_constraint_specs:
            return

        # Keep backward-compat attribute pointing at the current (first) spec.
        self._p1_spec_index = 0
        self._p1_constraint_spec = self._p1_constraint_specs[0]

        # Patch the scaffold once with the union of headers needed by ANY spec
        # so the scaffold is valid regardless of which spec is active.
        if self.language in ("cpp", "c"):
            all_extra: set = set()
            for spec in self._p1_constraint_specs:
                all_extra.update(infer_extra_cpp_headers(spec))
            if all_extra and self.target.prompt_used:
                begin = self.target.prompt_used["begin"]
                for h in sorted(all_extra):
                    include_line = f"#include {h}"
                    if include_line not in begin:
                        main_idx = begin.find("int main")
                        if main_idx >= 0:
                            # Scaffold pre-opens main: insert the include above it.
                            begin = begin[:main_idx] + include_line + "\n" + begin[main_idx:]
                        else:
                            # Header-only scaffold: append the include to the header.
                            begin = begin.rstrip() + "\n" + include_line
                self.target.prompt_used["begin"] = begin

    def _init_phase2(self, cfg: Dict) -> None:
        from Fuzz4All.huf_saem.phase2_bug_ingester import BugIngester
        from Fuzz4All.huf_saem.phase2_implementation_synthesis_agent import (
            ImplementationSynthesisAgent,
        )
        from Fuzz4All.huf_saem.phase2_mutator_invention_agent import MutatorInventionAgent
        from Fuzz4All.huf_saem.phase2_mutator_registry import MutatorRegistry

        mutator_dir = cfg.get("mutator_dir") or os.path.join(self.output_folder, "mutators")
        os.makedirs(mutator_dir, exist_ok=True)
        self._p2_mutate_ratio = float(cfg.get("mutate_ratio", 0.20))
        model = cfg.get("synthesis_model", "gpt-4o")

        ingester = BugIngester(
            github_repo=cfg.get("github_repo"),
            token=cfg.get("github_token") or os.environ.get("GITHUB_TOKEN"),
            csv_path=cfg.get("csv_path"),
        )
        bug_reports = ingester.merge_and_deduplicate()

        registry = MutatorRegistry(mutator_dir)
        registry.load_or_synthesize(
            bug_reports=bug_reports,
            language=self.language,
            invention_agent=MutatorInventionAgent(model=model),
            synthesis_agent=ImplementationSynthesisAgent(model=model),
        )
        self._p2_mutator_registry = registry

    def _init_phase3(self, cfg: Dict) -> None:
        from Fuzz4All.huf_saem.phase3_cloze_controller import ClozeController
        from Fuzz4All.huf_saem.phase3_cloze_masker import ClozeMasker
        from Fuzz4All.huf_saem.phase3_island_manager import IslandManager
        from Fuzz4All.huf_saem.phase3_seed_db import SeedDatabase

        db_path = cfg.get("seed_db_path") or os.path.join(self.output_folder, "seed_db")
        embedding_model = cfg.get("embedding_model", "all-MiniLM-L6-v2")
        window_size = int(cfg.get("window_size", 50))
        threshold = float(cfg.get("cloze_threshold", 0.30))
        mask_ratio = float(cfg.get("mask_ratio", 0.15))
        cloze_model = cfg.get("cloze_model", "gpt-4o")

        island_cfg = cfg.get("islands", {})
        num_islands = int(island_cfg.get("num_islands", 2))
        biases = island_cfg.get("biases", ["memory_allocation", "concurrency"])
        migration_interval = int(island_cfg.get("migration_interval", 50))
        migration_fraction = float(island_cfg.get("migration_fraction", 0.10))

        seed_db = SeedDatabase(db_path=db_path, embedding_model=embedding_model)
        masker = ClozeMasker(language=self.language, mask_ratio=mask_ratio)
        self._p3_island_manager = IslandManager(
            num_islands=num_islands,
            biases=biases,
            seed_db=seed_db,
            migration_interval=migration_interval,
            migration_fraction=migration_fraction,
        )
        self._p3_cloze_controller = ClozeController(
            threshold=threshold,
            window_size=window_size,
            seed_db=seed_db,
            masker=masker,
            model=cloze_model,
        )
        self._p3_seed_db = seed_db

    def _init_phase4(self, cfg: Dict) -> None:
        from Fuzz4All.huf_saem.phase4_branch_tracker import BranchTracker
        from Fuzz4All.huf_saem.phase4_cfg_analyzer import CFGAnalyzer
        from Fuzz4All.huf_saem.phase4_constraint_solving_agent import ConstraintSolvingAgent
        from Fuzz4All.huf_saem.phase4_coverage_instrumentor import CoverageInstrumentor
        from Fuzz4All.huf_saem.phase4_solution_splicer import SolutionSplicer

        block_threshold = int(cfg.get("block_threshold", 10))
        self._p4_solver_interval = int(cfg.get("solver_interval", 100))
        solver_model = cfg.get("solver_model", "gpt-4o")
        gcov_binary = cfg.get("gcov_binary", "gcov")
        min_downstream = int(cfg.get("min_downstream_size", 5))

        # detect compiler for this language
        compiler_map = {"cpp": "g++", "c": "gcc", "go": "go", "java": "javac"}
        compiler = compiler_map.get(self.language, "gcc")

        self._p4_coverage_instrumentor = CoverageInstrumentor(
            language=self.language,
            compiler=compiler,
            output_dir=self.output_folder,
            gcov_binary=gcov_binary,
        )
        self._p4_branch_tracker = BranchTracker(block_threshold=block_threshold)
        self._p4_cfg_analyzer = CFGAnalyzer(
            language=self.language, min_downstream=min_downstream
        )
        self._p4_constraint_agent = ConstraintSolvingAgent(model=solver_model)
        self._p4_solution_splicer = SolutionSplicer(language=self.language)

    # ------------------------------------------------------------------
    # Phase 1 — Source-Aware Autoprompting
    # ------------------------------------------------------------------

    def _augmented_spec(self, spec_idx: int, var_idx: int) -> str:
        """Return the spec at *spec_idx* with the variation hint at *var_idx* appended."""
        spec = self._p1_constraint_specs[spec_idx]
        hint = self._VARIATION_HINTS[var_idx % len(self._VARIATION_HINTS)]
        return spec + "\n" + hint

    def get_source_aware_prompt_augmentation(self) -> Optional[str]:
        """Return the initial constraint spec + variation hint (batch 0)."""
        if self._p1_constraint_specs and self._p1_prompt_injector:
            return self._augmented_spec(self._p1_spec_index, self._p1_variation_index)
        return None

    def maybe_rotate_phase1_constraint(self, iteration: int) -> None:
        """Update the active constraint spec and/or variation hint when due.

        - Spec rotates every *rotation_interval* files (across different GCC passes).
        - Variation hint rotates every *batch_size* files (every single batch),
          so the LLM sees a fresh style directive each generation even when only
          one spec was distilled.
        Called from the fuzz loop after each batch.
        """
        if not self._p1_constraint_specs:
            return
        n_specs = len(self._p1_constraint_specs)
        n_vars = len(self._VARIATION_HINTS)
        new_spec_idx = (iteration // self._p1_rotation_interval) % n_specs
        new_var_idx = (iteration // self._p1_batch_size) % n_vars
        if new_spec_idx != self._p1_spec_index or new_var_idx != self._p1_variation_index:
            self._p1_spec_index = new_spec_idx
            self._p1_variation_index = new_var_idx
            aug = self._augmented_spec(new_spec_idx, new_var_idx)
            self._p1_constraint_spec = aug
            self.target.apply_source_aware_prompt(aug)

    # ------------------------------------------------------------------
    # Phase 2 — Mutator application
    # ------------------------------------------------------------------

    def maybe_apply_mutator(self, code: str, iteration: int) -> str:
        if self._p2_mutator_registry is None:
            return code
        import random
        if random.random() < self._p2_mutate_ratio:
            return self._p2_mutator_registry.apply_random(code)
        return code

    # ------------------------------------------------------------------
    # Phase 3 — Seed DB + Cloze
    # ------------------------------------------------------------------

    def record_result_phase3(self, f_result: "FResult", code: str, iteration: int) -> None:
        from Fuzz4All.target.target import FResult
        if self._p3_cloze_controller is not None:
            self._p3_cloze_controller.record_result(f_result)
        if self._p3_island_manager is not None:
            self._p3_island_manager.maybe_migrate(iteration)
            if f_result == FResult.SAFE and self._p3_seed_db is not None:
                island = self._p3_island_manager.get_active_island(iteration)
                fitness = 1.0
                island.add_individual(code, fitness)
                self._p3_seed_db.add_seed(
                    code,
                    metadata={
                        "language": self.language,
                        "iteration": str(iteration),
                        "island_id": str(island.island_id),
                    },
                )

    def maybe_cloze_generate(self) -> Optional[str]:
        if self._p3_cloze_controller is None:
            return None
        return self._p3_cloze_controller.generate_cloze_input(self.language)

    def get_island_bias_prompt(self, iteration: int) -> str:
        if self._p3_island_manager is None:
            return ""
        island = self._p3_island_manager.get_active_island(iteration)
        return island.bias_prompt_fragment()

    # ------------------------------------------------------------------
    # Phase 4 — Coverage + Constraint Solving
    # ------------------------------------------------------------------

    def run_coverage_and_update(self, source_file: str) -> None:
        if self._p4_coverage_instrumentor is None or self._p4_branch_tracker is None:
            return
        if self.language in ("cpp", "c"):
            executable = self._p4_coverage_instrumentor.compile_with_coverage(source_file)
            if executable:
                cov_data = self._p4_coverage_instrumentor.run_and_collect(
                    executable, source_file
                )
                self._p4_branch_tracker.record_coverage(cov_data, source_file)
        else:
            cov_data = self._p4_coverage_instrumentor.stub_collect(source_file)
            self._p4_branch_tracker.record_coverage(cov_data, source_file)

    def maybe_run_constraint_solver(self, iteration: int) -> Optional[str]:
        if self._p4_branch_tracker is None or self._p4_constraint_agent is None:
            return None
        if iteration % self._p4_solver_interval != 0 or iteration == 0:
            return None

        blocked = self._p4_branch_tracker.blocked_branches()
        for branch_id in blocked:
            source_file = self._p4_branch_tracker.source_for_branch(branch_id)
            if source_file is None:
                continue
            if not self._p4_cfg_analyzer.is_worth_targeting(source_file, branch_id):
                continue
            context = self._p4_constraint_agent.extract_blocked_context(source_file, branch_id)
            solution = self._p4_constraint_agent.solve(
                source_file, branch_id, context, self.language
            )
            if solution:
                # Retrieve a recent base to splice into (last generated fuzz file)
                base_code = ""
                spliced = self._p4_solution_splicer.splice(base_code, solution)
                self._p4_branch_tracker.clear_branch(branch_id)
                return spliced
        return None
