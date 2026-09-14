# Local development services

`plane-selfhost/` contains the disposable official Plane Community Edition
Docker Compose installation used for integration testing. The entire directory,
including generated environment files, volumes metadata, and local test
credentials, is intentionally ignored by Git.

The local instance is available at <http://localhost:18080>. The generated
official setup wrapper does not quote installation paths, so it cannot be used
reliably from this repository's space-containing directory. Operate the
generated Compose project directly instead:

```bash
cd "dev/plane-selfhost"
docker compose \
  -f plane-app/docker-compose.yaml \
  --env-file plane-app/plane.env \
  up -d
```

Stop containers without deleting their persistent volumes:

```bash
docker compose \
  -f plane-app/docker-compose.yaml \
  --env-file plane-app/plane.env \
  stop
```

Local login details and the test workspace/project IDs are recorded in the
ignored `plane-selfhost/TEST_CREDENTIALS.md` file.
