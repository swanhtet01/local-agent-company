# Security Policy

## Reporting a vulnerability

Report security problems privately by email to **swanhtet@supermega.dev**.

Please include:

- what the problem is, in one or two sentences;
- the exact steps to reproduce it;
- which files or commands are involved;
- what an attacker could do with it.

Please do **not** open a public issue, post a public write-up, or publish a
proof-of-concept for an unpatched problem. Give the maintainer a chance to fix
it first. If you do not get a reply, send a follow-up email rather than making
the report public.

This is a small project run by one person. There is no bug bounty and there is
no promised response time. Reports are read and taken seriously, but a fix may
take a while.

## Supported versions

This project is **pre-1.0**. There are no releases, no long-term support
branches, and no backported security fixes.

| Version | Supported |
| --- | --- |
| Latest commit on the default branch | Yes |
| Any older commit, tag, or copied bundle | No |

If you are running an older copy, update to the latest commit before reporting
a problem.

## Threat model

Read this section before you deploy anything. It describes what the project
does and does not protect against today. These are known, current properties,
not hypothetical risks.

### The dashboard has no authentication

The local dashboard and task-intake service (`local-company service start`) has
**no login, no user accounts, and no password**. Anyone who can reach the port
can use it.

The mutation token that authorises queue changes and local execution is
**embedded in the rendered HTML of the page**. This means that whenever a
service token is set, read access to the page is effectively write access. The
service does check the `Host` and `Origin` headers and rejects non-loopback
authorities and cross-site mutation origins, but that is a defence against
browser-based attacks, not authentication of a user.

**Never expose this port to a network.** Do not port-forward it, do not put a
reverse proxy in front of it and call it secured, and do not open it in a cloud
firewall. If you need to reach it from another machine, use an SSH tunnel or a
private overlay network such as Tailscale, and prefer the read-only `dashboard`
mode over the writable service.

### The dashboard binds loopback by hardcode

The listener address is `127.0.0.1` in code. There is no environment variable to
change it. This is deliberate: it is the last line of defence given the point
above. Do not patch it out to make remote access convenient.

### The build manifest is a drift detector, not a signature

The stamped build manifest and the SHA-256 seals on workflows, suite manifests,
reports, and evidence all answer one question: *have these bytes changed since
they were recorded?*

They do **not** answer: who wrote them, who approved them, or whether they came
from a trusted source. They are not digital signatures. There is no public-key
infrastructure in this project. An attacker who can write to your working
directory can also re-stamp the manifest and re-seal the file.

The same limitation applies to the optional Ollama executable SHA-256 pin: it
proves the binary equals the bytes you reviewed locally. It does not prove the
publisher's identity, and it says nothing about the DLLs, models, or
configuration that binary loads.

### Receipts and evidence can contain private data

Run receipts, browser screenshots, captured page text, accessibility snapshots,
desktop-automation evidence, and audit exports are written to disk under your
company home directory. They can contain:

- text that was visible on screen when a screenshot was taken, including field
  values, customer names, prices, and internal notes;
- page content and titles from any site you checked;
- excerpts from local files you registered as knowledge sources.

**Review this material with your own eyes before you share it with anyone.**
The `portable-summary.json` file produced by browser suites is the file intended
for sharing; the full receipt and the screenshots are not. Desktop teaching does
not record the characters you type — it stores `TEXT_1`-style placeholders — but
run-time evidence can still capture those values once they appear on screen. Use
`--no-evidence` when that trade-off is unacceptable, and leave
training screenshots off unless you know the visible content is safe to keep.

### It runs as you, with your privileges

There is no sandbox. The coordinator, the MCP server, and the desktop automation
all run as the operating-system user who started them, with that user's access
to files, applications, and the local network. A taught desktop workflow injects
real mouse and keyboard input into real applications. Preview resolves targets
and blocks known browser, messaging, and shell processes by default, and the
runner halts on window drift rather than guessing — but a workflow you taught
badly, against the wrong application, can still do real damage on your own
machine.

Keep the company home directory readable and writable only by your own user
account. On Windows the state directory inherits the ACL it is created with;
check that it has not been widened.

### Model output and imported text are untrusted input

Text imported through `knowledge add`, page content read during browser QA, and
anything a local model generates are all treated as **reference material, not
instructions**. Do not paste credentials, tokens, or API keys into objectives,
knowledge files, or suite manifests. URLs in a sealed manifest must not contain
passwords or signed query strings.

Reports are proposals. Approval records are decisions. Neither is proof that
work happened, and neither authorises anything to be sent, spent, deployed,
published, or deleted.

### What this project deliberately does not do

There is no external-action executor. Approving a request records a decision; it
does not send an email, move money, deploy code, publish content, or delete
data. Outbound traffic is limited to the local Ollama endpoint on loopback and,
during browser QA, to the URLs you explicitly asked it to read. If you find a
code path that performs an external action autonomously, that is a security bug
under this policy — please report it.

## Out of scope

The following are known and accepted, not vulnerabilities:

- The dashboard is unauthenticated. Exposing it to a network is an operator
  error, not a bug in the project.
- Local models produce low-quality or wrong output. That is a quality limit, not
  a security issue.
- A user with write access to the repository or the company home can defeat every
  integrity check in the project. That is expected; the seals detect accident and
  drift, not a local attacker.
