# Original IBKR SDK snapshot recovered

Recovered on 2026-09-19 from the original installation on the deployment host.
**Option 1 is available. The existing SDK pin does not need to change.**
This is a dependency recovery handoff for Kiro, not a new release, completed
installation, engineering approval of other changes, or trading activation.

## What is included

- `original-sdk-3869276715cc.tar.gz`: exact original snapshot and receipt under
  `snapshot/`, plus upstream Python/protobuf-message source and license notices
  under the separate `upstream/` directory.
- `ibkr-sdk-attestation.json`: original receipt, byte-for-byte unchanged.
- `recovery-evidence.json`: observed hashes, sizes, platform and check results.
- `archive-members.json`: size and SHA-256 of every regular archive member.
- `../../scripts/verify_recovered_ibkr_sdk.py` (at repository root): standalone
  offline verifier; it never imports the SDK, connects to a broker or installs.

The recovery archive contains more than 303 members because it also includes
the receipt and upstream sources/notices. Its **installed SDK tree** has exactly
303 files totaling 2,897,207 bytes. These match the September 14 deployment
report already in `validation/full-live/2026-09-14/DEPLOYMENT_REPORT_2026-09-14.md`.

| Evidence | SHA-256 |
| --- | --- |
| Original inventory | `3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc` |
| Original receipt | `996ce65c9f9ca192eae75609cfa1651745c0fc91e0372ec6d44a64bc0cfa45f3` |
| Original downloaded TWS archive | `673129e5cba58c4d77bc40647265f84ea42f605eccf88fa4c1221d62d12454f3` |

Version/platform: `ibapi 10.50.2`, `protobuf 5.29.5`, CPython 3.12; the protobuf
native module comes from `cp38-abi3-macosx_10_9_universal2`. This **is not a Linux
SDK snapshot**. Merely reinstalling the same version numbers will not recreate
its installed metadata, native binary and exact inventory hash.

## What was verified on the original host

1. The original source pin, receipt digest, ordered inventory digest, count,
   total byte count, every file's size/SHA-256, exact tree membership and modes.
2. The project's `validate_installed_sdk()` accepted the snapshot **and its
   binding to the existing local install state**, without importing the SDK.
3. All 235 installed `ibapi/` files matched the pristine downloaded upstream
   archive's Python client source, byte for byte.
4. Two packaging passes produced identical recovery archive bytes. This proves
   deterministic **repackaging of recovered bytes**, not a reproducible rebuild
   of the original dependencies from source.

Neither `expected_inventory_sha256` nor any installed runtime/control state was
changed. No broker connection, live order or activation was performed.

## Recover locally without installing a release

From a clone of this PR branch, use CPython 3.12 on macOS. Extract **outside the
checkout**, because the release builder rejects dirty/untracked build inputs.
First compare the archive SHA-256 with `recovery-evidence.json`:

```sh
shasum -a 256 recovery/ibkr-sdk-2026-09-14/original-sdk-3869276715cc.tar.gz
python3.12 -I -S -B -c 'import json; print(json.load(open("recovery/ibkr-sdk-2026-09-14/recovery-evidence.json"))["recovery_archive_sha256"])'
```

Only after those values match:

```sh
sdk_recovery_dir="$(mktemp -d "${TMPDIR:-/tmp}/titan-sdk-recovery.XXXXXX")"
tar -xzf recovery/ibkr-sdk-2026-09-14/original-sdk-3869276715cc.tar.gz -C "$sdk_recovery_dir"
python3.12 -I -S -B scripts/verify_recovered_ibkr_sdk.py \
  --snapshot-root "$sdk_recovery_dir/snapshot"
```

The verifier must pass. To supply the normal paused installer's `--ibkr-sdk-venv`
input, create a **new** SDK-only staging venv (do not pip-install over its files):

```sh
python3.12 -m venv --without-pip "$sdk_recovery_dir/sdk-venv"
cp -R "$sdk_recovery_dir/snapshot/dependencies/ibkr-sdk/3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc/site-packages/." \
  "$sdk_recovery_dir/sdk-venv/lib/python3.12/site-packages/"
```

Pass `$sdk_recovery_dir/sdk-venv` to `--ibkr-sdk-venv` in the **normal reviewed
paused installer**, using the same CPython 3.12 interpreter for installation.
Kiro's owner wrapper calls this argument `--sdk-venv`; that wrapper delegates
to the paused installer. Use the explicit existing IBKR install root, not a
generic default root. Do not run the wrapper until its candidate release and
the rest of the installation plan have been reviewed.

Do not copy `control/ibkr-sdk-attestation.json` into an existing installation by
hand. The normal installer regenerates the receipt and binds it into the new
install state. Do not transplant a production `control/` directory, invent an
install-state binding or edit the pinned digest to silence a mismatch. Keep
trading paused; dependency recovery alone does not establish launch readiness.

## Offline acceptance for this recovery PR

```sh
python3.12 -B -m unittest discover -s tests -p test_recovered_ibkr_sdk.py -v
python3.12 -I -S -B scripts/verify_recovered_ibkr_sdk.py --snapshot-root "$sdk_recovery_dir/snapshot"
git diff --check
```

Before a candidate deployment, additionally run the existing SDK-backed and
deployment test suites against the recovered bytes, including
`test_live_ibkr_sdk.py`, `test_live_ibkr_account_updates_sdk.py` and
`test_live_deployment.py`. SDK tests must actually run without SDK-absence skips;
set `TITAN_TEST_IBKR_SDK_ROOT` to the recovered `site-packages` for the wire test.
Do not contact a real Gateway merely to test dependency recovery.

## Option 2: reviewed re-pin only if needed later

If the old snapshot cannot be used (for example, a different deployment OS),
open a separate engineering PR for a fresh deterministic build. Record official
input archive/wheel hashes, Python/ABI, OS/architecture, build tool versions,
commands, environment and installation-path handling. Produce two independent
clean builds with identical inventories before proposing a new pin. Review the
actual file list/count and platform compatibility, run SDK and deployment
tests, and update `expected_inventory_sha256` only through the reviewed change.

**303 is the historical count, not a target to force onto a new build.**
Approval concerns the real complete inventory and its digest, not a cosmetic
count. Preserve all original evidence; do not relabel a new SDK as this snapshot.

## Distribution and privacy scope

Upstream IB API source headers and package metadata specify GPL-3.0-or-later.
The archive retains the upstream GPL text, NOTICE and third-party license texts,
the complete original Python-client and `.proto` source subtrees, build/support
files and `source/ProtoBuf_readme.txt`. These are separate from the immutable
installed snapshot. Protobuf's original BSD license remains in its dist-info
directory. Original notices are preserved; this does not change Titan's license.

The unchanged `ibapi-10.50.2.dist-info/direct_url.json` contains the original
local source-directory file URL. It is not a secret or download endpoint and
will not exist on another machine. It is retained because changing it changes
the inventory hash. The receipt includes the existing masked account/profile
namespace, not a full brokerage account identifier. SDK-file scans found no
credential or customer-account matches, but are not a proof about arbitrary
secrets elsewhere in the repository.

No Keychain data, Flex tokens, OAuth files, full account IDs, broker reports,
databases, production install-state, activation grants or signing material are
included. The recovery bytes do not authorize live trading.
