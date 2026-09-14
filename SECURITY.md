# Security policy

Enzo is an experimental V0 and is not production-ready. The current review UI
does not implement user authentication. In Plane mode, review mutations and
the HTTP worker-control endpoint are disabled by default.

Do not expose Enzo directly to the public internet. Run it on a trusted network
and put authenticated access control in front of it if remote access is needed.
Only enable `ENZO_REVIEW_UI_WRITE_ENABLED` or `ENZO_ADMIN_API_ENABLED` on a
trusted development deployment.

Agent processes and repository verification commands execute local code. Use
an OS/container boundary for untrusted repositories, and provide agents only
the minimum environment variables they require. Never commit `.env.local`,
Plane credentials, model-provider keys, repository credentials, runtime data,
or agent transcripts.

To report a vulnerability, use GitHub's private vulnerability reporting for
the repository once it is enabled. Do not include secrets or working exploit
details in a public issue.
