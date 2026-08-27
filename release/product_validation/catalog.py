"""Typed, versioned catalog for published product-surface acceptance."""

from __future__ import annotations

from fnmatch import fnmatchcase

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapabilityAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")
    routes: tuple[str, ...] = Field(min_length=1)
    control_patterns: tuple[str, ...] = Field(min_length=1)
    backend_operations: tuple[str, ...] = Field(min_length=1)
    checkpoints: tuple[str, ...] = Field(min_length=3)
    roles: tuple[str, ...] = Field(min_length=1)
    tenant_scoped: bool = True
    screenshot_required: bool = True
    runtime_evidence_required: bool = True


class FieldLevelContract(BaseModel):
    """Global equivalence classes every catalog-matched control must satisfy."""

    model_config = ConfigDict(extra="forbid")

    required_states: tuple[str, ...] = Field(min_length=9)
    select_options: str = Field(pattern=r"^every_enabled_option$")
    checkbox_states: tuple[bool, bool]
    conditional_fields: str = Field(pattern=r"^appearance_and_disappearance$")
    credential_capture: str = Field(pattern=r"^masked_only$")

    @model_validator(mode="after")
    def _complete_state_contract(self) -> FieldLevelContract:
        required = {
            "representative_valid",
            "required_empty",
            "type_or_syntax_invalid",
            "boundary_minimum",
            "boundary_maximum",
            "security_boundary",
            "save_refresh_reopen",
            "keyboard_and_focus",
            "role_denial_when_scoped",
        }
        if set(self.required_states) != required:
            raise ValueError("field-level equivalence contract is incomplete")
        if set(self.checkbox_states) != {False, True}:
            raise ValueError("field-level checkbox contract must include both states")
        return self


class OperationExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    reason: str = Field(min_length=1)


class OpenApiCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unmapped: tuple[str, ...]
    invalid_exclusions: tuple[str, ...]


class ProductValidationCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    catalog_id: str
    field_level_contract: FieldLevelContract
    capabilities: tuple[CapabilityAcceptance, ...]
    machine_only_exclusions: tuple[OperationExclusion, ...] = ()

    @model_validator(mode="after")
    def _unique_capabilities(self) -> ProductValidationCatalog:
        ids = [item.capability_id for item in self.capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("capability ids must be unique")
        return self

    @property
    def capability_ids(self) -> set[str]:
        return {item.capability_id for item in self.capabilities}

    @property
    def console_routes(self) -> set[str]:
        return {route for item in self.capabilities for route in item.routes}

    def compare_openapi(self, document: dict[str, object]) -> OpenApiCoverage:
        paths = document.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("OpenAPI document has no paths object")
        actual = {
            f"{method.upper()} {path}"
            for path, path_item in paths.items()
            if isinstance(path_item, dict)
            for method in path_item
            if method.lower() in {"get", "post", "put", "delete", "patch"}
        }
        exclusions = {item.operation for item in self.machine_only_exclusions}
        patterns = tuple(
            pattern for capability in self.capabilities for pattern in capability.backend_operations
        )
        unmapped = tuple(
            sorted(
                operation
                for operation in actual
                if operation not in exclusions
                and not any(fnmatchcase(operation, pattern) for pattern in patterns)
            )
        )
        invalid_exclusions = tuple(sorted(exclusions - actual))
        return OpenApiCoverage(
            unmapped=unmapped,
            invalid_exclusions=invalid_exclusions,
        )
