"""Cross-stage consistency checks independent of Agents and LLM clients."""

from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput


class DesignConsistencyError(ValueError):
    """Safe consistency codes; do not include private input or output values."""


def validate_design_requirements(design: DesignOutput, requirements: RequirementsOutput) -> DesignOutput:
    """Check exact constraints, complete FR coverage and internal module references.

    These deterministic checks establish traceability, not semantic proof that
    architecture prose or technology choices actually comply with every constraint.
    """
    if design.preserved_constraints != requirements.constraints:
        raise DesignConsistencyError("preserved_constraints_must_match_input_exactly")
    expected_ids = {f"FR-{i}" for i in range(1, len(requirements.functional_requirements) + 1)}
    actual_ids = {key for module in design.modules for key in module.requirement_ids}
    if actual_ids - expected_ids:
        raise DesignConsistencyError("unknown_functional_requirement_id")
    if expected_ids - actual_ids:
        raise DesignConsistencyError("functional_requirements_not_fully_covered")
    names = [module.name for module in design.modules]
    if len(set(names)) != len(names):
        raise DesignConsistencyError("duplicate_module_names")
    for module in design.modules:
        if module.name in module.dependencies:
            raise DesignConsistencyError("module_cannot_depend_on_itself")
        if set(module.dependencies) - set(names):
            raise DesignConsistencyError("unknown_internal_module_dependency")
    return design

