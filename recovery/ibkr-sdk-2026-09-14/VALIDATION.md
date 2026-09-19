# Recovery validation — 2026-09-19

Base: Kiro working branch `kiro/step0-hermetic-tests`, commit
`7c39f0d44d0e9e2dd9d7369c182f4ca392603dda`.

The following are fresh, local offline results for this additive recovery
change; they are not a whole-system trading-readiness or release-build claim.

| Check | Result |
| --- | --- |
| Original installed SDK and install-state receipt binding | PASS |
| Original receipt/inventory hashes and all 303 files | PASS |
| 235 installed IB API files versus pristine upstream source | PASS, no differences |
| Two recovery archive packaging passes | Byte-identical |
| Verifier against a fresh extracted copy | PASS, 303 files / 2,897,207 bytes |
| Verifier positive/negative regression suite | 18 tests PASS, no skips |
| Existing SDK-backed suites (`test_live_ibkr*sdk.py`) using recovered bytes | 51 tests PASS, no skips |
| Existing paused-deployment regression suite (`test_live_deployment.py`) | 58 tests PASS, no skips |
| Repository validator | PASS: 217 Python / 37 JSON / 4 TOML files |
| Staged diff whitespace check | PASS |

Interpreter: CPython 3.12 on the original macOS host. Tests were run with
bytecode writing disabled. The SDK wire tests prohibit socket creation; no
real broker or network calls were used for these SDK tests. Deployment tests
use their isolated fixtures, not the existing production installation.

The dependency and production release pins remain unchanged. No installation,
live activation, production control-state edit or order operation was performed.
The complete broad test suite was not rerun as part of this recovery-only PR.

Independent bounded audit found no credential/customer-account pattern matches
in the 303 SDK files; original local `direct_url.json` path metadata is retained
intentionally. See README for public payload scope and license/source contents.
