# OneBrain Web

This directory contains the optional OneBrain web client.

## Local development

```powershell
cd onebrain-web
npm install
npm run dev
```

The client reads one variable, `ONEBRAIN_API_BASE_URL`, which defaults to
`http://127.0.0.1:8000`. Put an override in `.env.local` when the API is
somewhere else. The browser-facing API URL must be HTTPS in a deployed
environment.

## Deployment

Deploy the web client only as part of an isolated Hetzner customer environment
or the dummy-data development gate. It must communicate only with the API in
that same environment.

Do not configure it with Mission Control URLs, fleet keys, broker credentials,
or another customer's service URL. Customer deployments do not expose fleet,
operator, provisioning, or rollout features through this client.

Use immutable image digests and the same release descriptor as the rest of the
customer suite. See the repository [deployment guide](../docs/deployment.md).
