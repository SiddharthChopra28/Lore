# Lore hosting runbook

Runs the gateway, verifier and curator on one Docker host. The gateway is also the hosted MCP server
users connect to (`$LORE_PUBLIC_URL/mcp`, World ID login). Status: the compose file and image are not yet run on a real host.

## Before the first deploy

These need people or credentials, so they are not automated:

- `agents/` merged, with P1's wiring from `e2e/DELIVERY_HANDOFF.md`.
- A host with Docker Compose and a `lore` user with uid 10001. On Azure, `azure.sh` makes one (below).
- A 0G Router API key created with `trust_mode: "private"`.

## Azure host

`azure.sh` prints what it will create and the cost, and asks before creating anything. It makes one
Ubuntu 24.04 VM (B2als_v2 in Malaysia West by default), a static IP with a free
`<label>.<region>.cloudapp.azure.com` name, a firewall (80 and 443 open, 22 only from your IP) and a storage
account for nightly sqlite backups. First boot installs Docker, Caddy and the backup cron, and adds 2 GB of swap.

```sh
az login
deploy/azure.sh                           # prints LORE_PUBLIC_URL at the end
ssh lore-admin@<label>.malaysiawest.cloudapp.azure.com
cloud-init status --wait                  # first boot takes a few minutes
git clone <repo> /opt/lore && cd /opt/lore/deploy
sudo cp Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Then follow "First deploy" from `/opt/lore/deploy`, with `LORE_PUBLIC_URL` set to the printed `https://` URL
(also the World ID app's and Turnkey's allowed origin). Backups land in the `backups` container under one
folder per night; the SAS in `/etc/lore-backup.env` can only write, and expires after a year. Delete
everything with `az group delete -n lore`.

## First deploy

```sh
cd deploy
python create_secrets.py                  # refuses to overwrite; prints addresses only
# Add ZG_API_KEY to secrets/verifier.env and secrets/curator.env.
# Add TURNKEY_API_PRIVATE_KEY and WORLD_RP_SIGNING_KEY to secrets/gateway.env.
# Fund every wallet in secrets/addresses.json with testnet 0G: https://faucet.0g.ai
# Ask P2 to grant roles: gateway=LEAK_DETECTOR and REGISTRAR, verifier=VERIFIER, curator=CURATOR.
python check_roles.py                     # exit 0 only when roles and gas are in place
mkdir -p state/gateway state/verifier state/curator
sudo chown -R 10001:10001 state
docker compose up -d --build              # reads deploy/.env (below); also builds the 0G Storage CLI
```

`docker compose` reads these from `deploy/.env` (none are secrets; the keys stay in `secrets/`):

```sh
LORE_PUBLIC_URL=https://<label>.malaysiawest.cloudapp.azure.com
ZG_MODEL=0gm-1.0-35b-a3b    # TeeML; glm-5.3 calls hung past the 5-minute limit on the Router (2026-11-07)
TURNKEY_ORGANIZATION_ID=...  TURNKEY_API_PUBLIC_KEY=...  TURNKEY_DELEGATED_PUBLIC_KEY=...  # one per line
WORLD_APP_ID=...  WORLD_RP_ID=...        # World ID login (spike W1)
# or, until World ID works: leave WORLD_APP_ID empty and set
LORE_DEMO_LOGIN=1
```

**Demo login is for testnet demos only.** Each name typed on the login page is one person, so anyone who types
`ada` acts as ada, including spending ada's Lore wallet. Use it only with small amounts of test 0G, and switch to
World ID (set `WORLD_APP_ID`, which turns demo login off) before anyone puts real value in.

The gateway binds to `127.0.0.1:8080`. Caddy (`Caddyfile`) serves it over TLS and keeps `/internal/*` private. The
explorer is at `/app/`.
Back up `secrets/` and `state/` off the host; `MASTER_KEY` loss loses all content.

### World ID configuration

Set `WORLD_APP_ID` and `WORLD_RP_ID` from the World Developer Portal in the
Compose environment (or `deploy/.env`), with `WORLD_ACTION=lore-person` unless
the configured action differs. Put `WORLD_RP_SIGNING_KEY` only in
`deploy/secrets/gateway.env`: the 32-byte hex key, optionally prefixed with `0x`,
must correspond to the signing key registered for that RP. The template leaves
it blank deliberately; startup refuses missing, blank or malformed keys.
Existing installations must add this entry before restarting the gateway.
The verifier and curator do not receive this key.

Compose defaults `WORLD_IDKIT_MODULE` to the spike's pinned ESM URL:
`https://cdn.jsdelivr.net/npm/@worldcoin/idkit-core@4.6.0/+esm`.
Override it with a tested self-hosted bundle URL if needed. The pin has offline
SDK evidence, but browser loading and real World verification remain untested.
RP request signing (`gateway/worldid.py`, a port of idkit-server 1.2.0 `signRequest`) and the login page's
request/result handling are implemented and unit-tested; a real World App login has not been completed yet.
Login through World ID Agent Pass (World IdP, `WORLD_IDP_*`) has run live; see
[docs/world-id-agent-pass.md](../docs/world-id-agent-pass.md).
**Run one set of Lore services per deployment.** If two gateways or verifiers with roles watch the same
contracts, whichever is faster acts first: another gateway's verifier fails submissions it never ingested.

**Turnkey's free plan caps signing.** Every user transaction is a Turnkey signature; past the quota Turnkey
answers 429 and every chain-changing tool fails ("Turnkey didn't sign …"). Use a paid plan for anything beyond a test.

## 0G mainnet (real 0G)

Run extensive tests on Galileo only; mainnet gets a minimal smoke run.

```sh
cd contracts
set -a; . ./.env.mainnet; set +a          # DEPLOYER_PRIVATE_KEY: becomes admin and treasury; fund it first
VERIFIER=<verifier wallet> CURATOR=<curator wallet> LEAK_DETECTOR=<gateway wallet> REGISTRAR=<gateway wallet> \
forge script script/Deploy.s.sol --rpc-url https://evmrpc.0g.ai --broadcast --slow
python script/export_abi.py
```

Budget, at 4 gwei: about 0.05 0G to deploy (11.6M gas on Galileo), a little gas for each service wallet and its
storage wallet, and for the smoke run a 0.1 0G stake (returned minus the fee when accepted) and one topic
purchase. Run the services with
`LORE_CHAIN_ID=16661`; Turnkey wallets are created per chain (their policies pin it).

## Redeploy

```sh
git pull
docker compose up -d --build
docker compose ps
```

Each service keeps its sqlite state in `state/<service>/`, so a redeploy resumes
from its last indexed block.

## Reading logs

```sh
docker compose logs -f --tail=100 gateway    # or verifier, curator
journalctl -u lore-attest.service -n 50
```

Logs must never hold plaintext or keys. If you see either, treat that key as
exposed and rotate it.

## Rotating secrets

| Secret | How | Warning |
|---|---|---|
| `ZG_API_KEY` | Create a new private-mode key, edit the env files, `docker compose up -d`. | Keep `trust_mode: "private"`. |
| Storage wallets | Put a new funded key in the service's env file and restart. | Safe at any time. |
| Role wallets | P2 grants the role to the new address, restart, then P2 revokes the old one. | Don't revoke first. |
| `WORLD_RP_SIGNING_KEY` | Register the replacement key for the World RP, update `secrets/gateway.env`, then recreate the gateway. | Keep the RP registration and deployed key in sync; test a real login. |
| `MASTER_KEY` | **Do not rotate in v1.** | Blobs have no key version; old content becomes unreadable. |
| `WM_SECRET` | **Do not rotate in v1.** | Marks have no key version; past deliveries become untraceable. |

If `MASTER_KEY` or `WM_SECRET` is exposed, stop the gateway and raise it with the
team. Recovery needs a migration that v1 does not have.

## Daily provider attestation

Install the CLI (`@0gfoundation/0g-compute-ts-sdk`; `@0glabs/0g-serving-broker` is
a deprecated shim), pin the version, and confirm `0g-compute-cli inference verify
--help` works. Providers are registered on mainnet, so check the CLI targets it.

```sh
sudo cp lore-attest.service lore-attest.timer /etc/systemd/system/
sudo systemctl enable --now lore-attest.timer
```

The job reads receipts through the gateway and publishes CLI output to 0G Storage
with its own wallet. Fund the `attestation` wallet. It has not run against a real
provider yet.

## End-to-end check

Run `e2e/run.py` against the deployment, twice from a fresh deploy (M4). See its
docstring for the wallets and settings it needs.
