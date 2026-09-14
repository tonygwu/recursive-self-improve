# Security reports

This is experimental software. Fixes target the current main branch; there is no
supported maintenance-release series yet.

Use GitHub private vulnerability reporting when the Security tab offers
**Report a vulnerability**. If unavailable, open an issue asking for a private
reporting channel. Include no exploit details, credentials, transcript excerpts
or private records in that public request. No response-time guarantee is offered.

A private report should name the affected revision, behavior, impact and a
reproduction with invented inputs. Do not initially send a personal state database.

The dashboard has no authentication and binds to loopback. Model calls may send
redacted session content to a provider; redaction is not a confidentiality guarantee.
A bounded model request does not itself disable provider tools. CLI permissions
and configured sandbox settings govern allowed operations. Prompt instructions
are not an enforced tool or filesystem boundary.

Dashboard decisions and instruction writes are separate. Inspect exact revisions
and recorded destinations, and run the worker deliberately. See
[workflow](docs/WORKFLOW.md) and [data boundary](docs/DATA_BOUNDARY.md).
