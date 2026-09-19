# Publication record: Titan Trade Alerts information pages

Status: owner approved the pages and public support contact on September 14,
2026 (America/New_York). GitHub browser authentication succeeded. The public
source is committed; the managed GitHub Pages deployment succeeded. Both
public pages and their stylesheet were verified over HTTPS at
2026-09-15T02:19:35Z (September 14, 2026, 10:19 p.m. America/New_York).
No Google Cloud fields, credentials, email sends or trading actions changed.

The complete public payload is only `dist/index.html`, `dist/privacy.html`,
`dist/styles.css`, and `dist/.nojekyll`. There are no scripts, remote fonts,
forms, images, analytics, or live broker controls. GitHub manages the Pages
deployment job from the public repository's main branch root.
Do not publish this repository root or its trading records. Keep the private
OAuth download, Keychain material, install subtree and runtime databases out
of the website source and public artifact.

## Recorded approval and publication

The owner explicitly approved publishing both pages on free GitHub Pages,
including shian.harper@gmail.com as the public support contact. This approval
is separate from the earlier Google consent-screen contact approval.

The approved privacy commitments describe
send-only Gmail use, actual local retention, limited third-party sharing,
revocation and scoped deletion, and the absence of application-level
encryption for all operational records. They do not promise automatic
deletion, delivery, profits, Google verification, or active trading.

The target is the new public repository `Harpcity33/titan-trade-alerts`.
Only the four static assets were uploaded, without parent repository history.
Review banners were removed; the approved policy is effective September 14,
2026, and its host paragraph now identifies GitHub Pages.
The `noindex` metadata is not access control: publicly hosted pages and their
source may still be accessible. Do not change Google Cloud fields until the
actual HTTPS homepage and privacy URLs work. Add only an appropriate
owner-controlled authorized domain, never a domain owned by someone else.

Google's currently disabled Publish app tooltip requires the homepage and
privacy-policy URLs in addition to the existing app name and support email.
Publication may reveal further Google requirements; these files do not
guarantee acceptance or substitute for owner consent.

GitHub's authenticated connector reconfirmed `Harpcity33/trader-brain` is
private after the separate public repository was created. Its visibility was
not changed. No paid plan, trial, custom domain, billing, or service purchase
was selected.

## Publication evidence

- Public source: https://github.com/Harpcity33/titan-trade-alerts
- Public commit: `7b46e4486379ca47cc1b9097bff47e07d5834522`
- Exactly four blobs; recursive listing was not truncated.
- All four public raw files returned HTTP 200 and matched local bytes.
- Pages source: `main`, `/ (root)`; browser confirmed source saved.
- HTTPS is enforced and required for the default GitHub domain.
- Managed run: https://github.com/Harpcity33/titan-trade-alerts/actions/runs/34920672335
- Terminal run result: `completed`, `success`, updated `2026-09-15T02:18:59Z`.
- Homepage: https://harpcity33.github.io/titan-trade-alerts/
- Privacy: https://harpcity33.github.io/titan-trade-alerts/privacy.html
- Both pages and `styles.css` returned HTTP 200 over HTTPS and were byte-for-
  byte identical to the approved local source and the public commit.
- GitHub's Pages UI confirmed "Your site is live" with the same homepage URL.
- The existing preview tab was handed the deployed homepage URL.

SHA-256 of the uploaded payload:

| File | SHA-256 |
| --- | --- |
| index.html | 994313586e69f76b42dee3e53cc708757e0f24d85373e1307a7c50c5f775e793 |
| privacy.html | 9f0b97b03948d011e1d34d08a64b80f1043bbc8dd1209cb306ba93f62b9bd1ad |
| styles.css | b2b6f0287f1df75100eeab32a8410c1625ec8395b2c058977e0281e9cecfacea |
| .nojekyll | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |

## Google connection remains separate

Publication does not establish Google domain verification. Google's current
[domain guidance](https://support.google.com/cloud/answer/13804266?hl=en)
requires DNS-level Domain Property verification, not a URL-prefix/HTML check.
Do not claim ownership of github.io or fabricate verification. The
[personal-use exception](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification#exceptions)
may avoid formal review for this personal app, but must not be used to claim
that a blocked Google configuration step has succeeded. Do not purchase a
domain or service under the zero-additional-spending policy.

## Evidence behind the wording

- `src/titan_brain/live/gmail_oauth_setup.py`: exact send-only scope, system
  browser + loopback callback, fixed Google endpoints, PKCE/state, owner-only
  downloaded file, durable refresh validation, five create-only Keychain
  entries, no send or activation during enrollment.
- `src/titan_brain/live/provider_clients.py`: Keychain-backed refresh-aware
  Gmail authorizer and account-scoped provider selection.
- `src/titan_brain/live/notifications.py`: event body construction, redaction
  limits, MIME sender/recipient/headers, Gmail send endpoint, provider receipt,
  notification outbox and delivery handling.
- [Google scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Google User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
- [Google Workspace policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy)
- [Google connection management](https://support.google.com/accounts/answer/13533235)
- [GitHub Pages host data collection](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

The `.openai/hosting.json` static-output declaration is local build metadata.
No Sites project has been registered, saved or deployed. The explicitly
requested and approved host is GitHub Pages, not a different hosting service.

## Local checks

Both HTML entrypoints and local asset links were checked. The pages have
unique IDs, page titles, main content and navigation, and no scripts, frames,
forms, inputs, or remote stylesheet/image references. There is no JavaScript
to syntax-check. The homepage served HTTP 200 on a loopback-only preview.
No browser screenshot, DOM or interaction QA was performed because it was
not requested; the preview is for the owner's content review. No live runtime
code changed, so no trading-system rebuild, reinstall, service restart or
activation was performed. Gmail remains unconnected by this workflow.
