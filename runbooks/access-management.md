# Access management

Production access grants need an approved Service Desk Plus ticket before they run.

Two rules:

1. Access to an IT system is granted only after a request is submitted and approved in Service Desk Plus. This includes IT staff. If there is no approved Service Desk Plus ticket, do not grant access.
2. Shared privileged account passwords belong in CyberArk or Passwordstate. Do not put those passwords in a Service Desk Plus ticket, a chat, or a mail draft.

What the gate covers on a production org:

- Creating a user
- Creating a partner user
- Turning a user back on (IsActive true)
- Adding a permission set
- Profile or role changes that expand access
- Queue or group membership that grants access

A sandbox can still run the existing safer path. If a Service Desk Plus id is present, it is logged. It is not required there.

The check does not invent approval. With no Service Desk Plus lookup configured, production grants stay blocked. Set SDPLUS_API_BASE only when a real lookup is available. A ticket that cannot be confirmed approved is a block.

Existing GO and explicit production confirmation still apply. The ticket check is additional.

Ticket forms that count: SD+ 12345, SDP-12345, Service Desk Plus #12345, and ticket 12345 when the text also says Service Desk Plus or SD+.
