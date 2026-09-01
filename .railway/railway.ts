import {
  defineRailway,
  github,
  postgres,
  preserve,
  project,
  service,
} from "railway/iac";

export default defineRailway(() => {
  const database = postgres("zeroth-postgres");

  const api = service("zeroth-api", {
    source: github("rrrozhd/zeroth", { branch: "main" }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "Dockerfile.cloud",
    },
    deploy: {
      startCommand: "uvicorn zeroth.econ.plane.main:app --host 0.0.0.0 --port 8000",
      preDeployCommand: ["zeroth-core migrate-econ"],
      healthcheckPath: "/health/ready",
      healthcheckTimeout: 300,
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 5,
      numReplicas: 1,
    },
    env: {
      ECP_DATABASE_URL: database.env.DATABASE_URL,
      ECP_CLOUD_ENTITLEMENTS_ENABLED: "true",
      ECP_CLOUD_SCHEDULER_ENABLED: "true",
      ECP_WORKOS_AUTHKIT_ENABLED: "true",
      ECP_PADDLE_BILLING_ENABLED: "true",
      ECP_PADDLE_SANDBOX: "false",
      ECP_JWT_SECRET: preserve(),
      ECP_WORKOS_CLIENT_ID: preserve(),
      ECP_WORKOS_API_KEY: preserve(),
      ECP_WORKOS_REDIRECT_URI: preserve(),
      ECP_WORKOS_COOKIE_PASSWORD: preserve(),
      ECP_CLOUD_BROWSER_ORIGIN: preserve(),
      ECP_PADDLE_API_KEY: preserve(),
      ECP_PADDLE_WEBHOOK_SECRET: preserve(),
      ECP_PADDLE_SOLO_PRICE_ID: preserve(),
    },
  });

  return project("zeroth-cloud", { resources: [database, api] });
});
