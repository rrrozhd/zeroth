from __future__ import annotations

from release.app_certification.dependency_sandbox import certification_resources


def write_generated_app(root, module: str = "generated_app") -> None:
    package = root / module.replace(".", "/")
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "contracts.py").write_text(
        "from pydantic import BaseModel\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://generated/payload': Payload}\n",
        encoding="utf-8",
    )
    (package / "graphs.py").write_text(
        "from zeroth.contracts.graph import EntrypointNode, Graph\n"
        "def build_graph():\n"
        "    node = EntrypointNode(node_id='start', graph_version_ref='generated@1', "
        "input_contract_ref='contract://generated/payload', "
        "output_contract_ref='contract://generated/payload', "
        "policy_bindings=['policy://generated/runtime'], "
        "capability_bindings=['process_spawn'])\n"
        "    return Graph(graph_id='generated', name='Generated', entry_step='start', "
        "nodes=[node], edges=[])\n",
        encoding="utf-8",
    )
    (package / "entrypoint.py").write_text(
        "from zeroth.governance.identity import ServiceRole\n"
        "from zeroth.governance.policy import (Capability, CapabilityRegistry, "
        "PolicyDefinition, PolicyGuard, PolicyRegistry)\n"
        "from zeroth.service.api.authentication import ServiceAuthConfig, "
        "StaticApiKeyCredential\n"
        "def build_auth_config():\n"
        "    return ServiceAuthConfig(api_keys=[StaticApiKeyCredential("
        "credential_id='generated', secret='generated-key', subject='generated', "
        "roles=[ServiceRole.ADMIN], tenant_id='tenant-generated')])\n"
        "def build_policy_guard():\n"
        "    capabilities = CapabilityRegistry()\n"
        "    capabilities.register('process_spawn', Capability.PROCESS_SPAWN)\n"
        "    policies = PolicyRegistry()\n"
        "    policies.register(PolicyDefinition(policy_id='policy://generated/runtime', "
        "allowed_capabilities=[Capability.PROCESS_SPAWN]))\n"
        "    return PolicyGuard(policy_registry=policies, capability_registry=capabilities)\n",
        encoding="utf-8",
    )


def successful_cleanup_document(run_id: str = "fixture-1") -> dict[str, object]:
    image_ids = {
        f"app-cert-candidate:{run_id}": "sha256:" + "a" * 64,
        f"app-cert-runtime:{run_id}": "sha256:" + "b" * 64,
    }
    return {
        "daemon_id": "fixture-daemon",
        "errors": [],
        "resources": [
            {
                "absent": True,
                "created_id": image_ids.get(name),
                "kind": kind,
                "name": name,
            }
            for kind, name in certification_resources(run_id)
        ],
        "run_id": run_id,
        "schema_version": 1,
        "status": "passed",
    }


def successful_workflow_stages() -> dict[str, str]:
    return {
        name: "success"
        for name in (
            "app_checkout",
            "certifier_checkout",
            "prepare",
            "image",
            "wheel",
            "sbom",
            "evidence",
            "containers",
            "health",
            "runtime",
            "certify",
            "cleanup",
        )
    }
