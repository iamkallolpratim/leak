# Security

Leak handles bank statements, card numbers and email. If you find a way for
personal data to leave the machine unredacted, for the local server to be driven
by another website, or for an action to run without approval, please report it
privately via GitHub Security Advisories ("Report a vulnerability") rather than a
public issue.

Design notes for reviewers:

* PII is masked at ingestion (`leak/privacy/redact.py`) and again at egress by a
  fail-closed guard (`leak/privacy/guard.py`) that refuses to send a payload in
  which it still detects PII.
* The local API (`leak/api/app.py`) binds to 127.0.0.1, rejects non-localhost Host
  headers, sends no CORS headers, and requires a per-process token for every
  mutation and every unmask.
* The agent can only *enqueue* write actions; `leak/actions/execute.py` runs them
  after a human approves. There is no code that sends email.
