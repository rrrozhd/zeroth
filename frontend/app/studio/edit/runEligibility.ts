// Pure eligibility rules for the Studio editor, extracted for testability.
//
// POST /v1/runs always executes the SERVED deployment's graph, so only the
// workflow whose id matches the serving deployment's graph_version_ref can be
// run from the canvas; deployment versions are created from published graphs
// only (drafts and archived graphs cannot be deployed).

export function servedGraphId(graphVersionRef: string): string {
  return graphVersionRef.split("@")[0];
}

export function canRunWorkflow(
  workflowId: string,
  servedGraphVersionRef: string | null | undefined,
): boolean {
  return servedGraphVersionRef != null && servedGraphId(servedGraphVersionRef) === workflowId;
}

export function canDeployWorkflow(status: string): boolean {
  return status === "published";
}
