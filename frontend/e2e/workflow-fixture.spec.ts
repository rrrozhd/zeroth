import { expect, test } from "@playwright/test";

import { workflowFixture } from "./support/live-evaluation";

test("workflow fixture exposes campaign-specific inspector labels", () => {
  const previous = {
    id: process.env.ZEROTH_EVALUATION_WORKFLOW2_ID,
    version: process.env.ZEROTH_EVALUATION_WORKFLOW2_GRAPH_VERSION,
    deployment: process.env.ZEROTH_EVALUATION_WORKFLOW2_DEPLOYMENT_REF,
    inspect: process.env.ZEROTH_EVALUATION_WORKFLOW2_INSPECT_NODE,
    child: process.env.ZEROTH_EVALUATION_WORKFLOW2_CHILD_NODE,
  };
  Object.assign(process.env, {
    ZEROTH_EVALUATION_WORKFLOW2_ID: "wf-2",
    ZEROTH_EVALUATION_WORKFLOW2_GRAPH_VERSION: "graph-2",
    ZEROTH_EVALUATION_WORKFLOW2_DEPLOYMENT_REF: "deployment-2",
    ZEROTH_EVALUATION_WORKFLOW2_INSPECT_NODE: "normalize",
    ZEROTH_EVALUATION_WORKFLOW2_CHILD_NODE: "investigate-child",
  });

  try {
    expect(workflowFixture(2)).toMatchObject({
      inspectNode: "normalize",
      childNode: "investigate-child",
    });
  } finally {
    const restore = (name: string, value: string | undefined) => {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    };
    restore("ZEROTH_EVALUATION_WORKFLOW2_ID", previous.id);
    restore("ZEROTH_EVALUATION_WORKFLOW2_GRAPH_VERSION", previous.version);
    restore("ZEROTH_EVALUATION_WORKFLOW2_DEPLOYMENT_REF", previous.deployment);
    restore("ZEROTH_EVALUATION_WORKFLOW2_INSPECT_NODE", previous.inspect);
    restore("ZEROTH_EVALUATION_WORKFLOW2_CHILD_NODE", previous.child);
  }
});
