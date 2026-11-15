# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub's **Security → Report a vulnerability** tab on this repository. If that option is not visible, open a public issue that says only "please contact me about a security report" and leave out the details.

Include:
- what you found and where (file, route or command)
- the steps to reproduce it, or a test that shows the problem
- the effect you expect an attacker to get

You can expect an acknowledgement within a week. Fixes are made on `main` and recorded in the commit message. Credit is given on request.

## Scope

In scope: the monitor server and its API, the key and role checks, the audit trail, transcript storage and masking, the container image, and the example deployment files in `deploy/`.

Out of scope: the storefront's cart and order actions, which are open by design in this demonstration (see the README, *Security*); reports about the fictional data; and issues that need a test environment you have changed yourself.

## Handling secrets

Never commit real keys. Keys come from environment variables; `.env` files are git-ignored. If a key is committed by mistake, revoke it first, then remove it from history.
