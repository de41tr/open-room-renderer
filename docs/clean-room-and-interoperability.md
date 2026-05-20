# Clean-Room And Interoperability Policy

Open Room Renderer is intended as an independent implementation of local network audio renderer behavior.

Acceptable contribution sources:

- Public documentation.
- Black-box observation of legitimately operated devices, apps, and services.
- Original implementation work.
- Synthetic tests and fixtures created specifically for this project.

Do not submit:

- Decompiled app code, firmware code, or copied source fragments.
- Proprietary constants copied from apps or firmware.
- Logos, icons, screenshots, UI assets, audio assets, or other proprietary media.
- Raw packet captures, raw protocol dumps, or unsanitized logs.
- Private keys, certificates, tokens, cookies, credentials, real device IDs, real MAC addresses, serials, or private network details.
- Code or instructions that bypass authentication, pairing, signatures, DRM, encryption, certificates, or other technical protection measures.

Protocol notes should be abstracted and sanitized. Describe behavior in terms of public request/response shape and state transitions, not by pasting raw captures or proprietary text.

Test fixtures must be synthetic. Use placeholder IDs such as `00000000-0000-4000-8000-000000000000`, documentation-only hostnames such as `example.test`, and synthetic MAC addresses such as `02:00:00:00:00:00`.

When in doubt, leave the material out of the public repository and ask maintainers for review.
