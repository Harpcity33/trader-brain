# Command Deck and private remote access

## Changes
- Reuse the owner's original emerald/silver Trader Brain logo; no new paid media generation.
- Command-deck layout, dark panels, emerald highlights, reduced-motion support, and iPhone PNG icons.
- Explicit comparison-account labels; no changes to either trading strategy or ledger.
- Optional `--tailnet-origin https://DEVICE.TAILNET.ts.net` adds a second HTTP listener on **127.0.0.1:8766 only**.
- Existing LAN HTTPS listener, private CA, port, and application token are preserved.
- The optional listener uses the same engine, not a second execution worker.

## Private routing
Use the signed Tailscale macOS and iPhone clients in the same personal tailnet.
The owner must complete login and approve Serve/HTTPS in Tailscale's official consent screen.
Configure `tailscale serve --bg http://127.0.0.1:8766` for private HTTPS ingress.
Do not enable Funnel, publicly share the device, forward router ports, or purchase a plan.
The domain is obtained from Tailscale's actual status, not guessed. It is stored only in local launchd configuration.
The TLS certificate's device hostname is published in certificate transparency records; the service itself remains private.

## Boundaries
The private proxy requires an exact HTTPS .ts.net origin and literal loopback binding.
Host, Origin, CSRF, pairing-token authentication and Secure/HttpOnly/SameSite cookies remain enforced.
Forwarded headers confer no authority. Broker-write support has not been added.
The Mac must be awake, online, and running the service. Tailscale does not turn a sleeping Mac into a cloud server.
Successful local tests or an online iPhone do not prove cellular access; verify the private URL on the phone with Wi-Fi disabled.
No custom certificate installation is required when using Tailscale's normal publicly trusted HTTPS URL.
