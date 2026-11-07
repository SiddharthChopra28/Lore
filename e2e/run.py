"""BUILD_PLAN workstream E: four people's agents drive Lore through the hosted MCP server, then we assert the chain.

Each agent logs in (OAuth; the gateway's labelled demo login when World ID is not configured) and creates its
Turnkey wallet with lore_create_wallet, exactly like a user's agent would; Lore signs every transaction from it.
Only 0G funding happens outside MCP. The gateway needs Lore's Turnkey keys (BUILD_PLAN spike T1).

    python deploy/run_local.py                                   # in one terminal: gateway, verifier, curator
    uv run --project agents --python 3.12 python e2e/run.py      # in another

Env: LORE_GATEWAY_URL (default http://127.0.0.1:8080), LORE_CHAIN_ID (default 16602), E2E_TAG (default random;
"none" submits the corpus text as is, for a fresh showcase deployment), E2E_TIMEOUT (seconds per wait, default 900).
E2E_QUICK=1 skips topic 2, which repeats what topic 1 covers.
E2E_EMAIL_DOMAIN (default example.com) names each agent's Turnkey user (lore-e2e-<name>@<domain>); 0G and the test authors'
come from DEPLOYER_PRIVATE_KEY in contracts/.env.
    ... python e2e/run.py --check    # log in and list the MCP tools only; no chain writes
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlparse

from eth_account import Account

from lore_shared.chain import ChainReader
from lore_shared.watermark import FRAME, ONE, ZERO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus  # noqa: E402

TOOLS = {
    "lore_inbox", "lore_whoami", "lore_link_token", "lore_my_tokens", "lore_create_wallet", "lore_wallet", "lore_list_topics", "lore_get_topic", "lore_my_vote_tasks",
    "lore_get_vote_task", "lore_vote", "lore_submit", "lore_buy", "lore_read_topic", "lore_claimable", "lore_claim",
    "lore_report_leak", "lore_check_proof",
}
PENDING, REPORTED, ACCEPTED, REJECTED = range(4)
TIMEOUT = float(os.environ.get("E2E_TIMEOUT", 900))
QUICK = os.environ.get("E2E_QUICK") == "1"  # skip topic 2, which repeats what topic 1 covers
PEOPLE = ("ada", "ben", "chen", "dana")


def research(subject: str, tag: str) -> str:
    """Synthetic text above MIN_CHARS; never real research."""
    lines = [f"Finding {i} on {subject} from run {tag} measured a distinct effect in sample {i * 7}." for i in range(1, 12)]
    return " ".join(lines)


def visible(text: str) -> str:
    return text.translate(str.maketrans("", "", FRAME + ZERO + ONE))


def three_sentences(text: str) -> str:
    """A marked 3-sentence excerpt from the middle of a delivered copy."""
    ends = [match.end() for match in re.finditer(re.escape(FRAME) + f"[{ZERO}{ONE}]{{64}}" + re.escape(FRAME), text)]
    if len(ends) < 5:
        raise AssertionError("delivered copy has too few watermark marks")
    return text[ends[0]:ends[3]].strip()


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


# --- login: OAuth 2.1 + PKCE against the gateway, finished through its demo login page ---
def login(gateway: str, name: str) -> str:
    import httpx

    with httpx.Client(base_url=gateway, timeout=30, follow_redirects=False) as h:
        redirect_uri = "http://127.0.0.1:9/callback"
        client = h.post("/register", json={"client_name": f"lore-e2e-{name}", "redirect_uris": [redirect_uri],
                                           "grant_types": ["authorization_code", "refresh_token"],
                                           "response_types": ["code"], "token_endpoint_auth_method": "none"})
        client.raise_for_status()
        client_id = client.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        r = h.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                                        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                                        "scope": "lore", "resource": f"{gateway}/mcp"})
        if r.status_code not in (302, 307):
            raise RuntimeError(f"authorize: {r.status_code} {r.text[:200]}")
        pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]
        done = h.post("/login/demo", params={"pending": pending}, json={"name": name})
        if done.status_code != 200:
            raise RuntimeError(f"demo login refused ({done.status_code}): is World ID configured on this gateway?")
        q = parse_qs(urlparse(done.json()["redirect"]).query)
        assert q["state"][0] == state, "OAuth state mismatch"
        tok = h.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": client_id,
                                     "redirect_uri": redirect_uri, "code_verifier": verifier, "resource": f"{gateway}/mcp"})
        tok.raise_for_status()
        return tok.json()["access_token"]


class Agent:
    """One person's agent: an MCP session as that person, with a Lore (Turnkey) wallet that Lore signs from."""

    def __init__(self, name: str, session, reader: ChainReader):
        self.name, self.session, self.reader = name, session, reader
        self.address = None  # set by create_wallet

    async def tool(self, name: str, **arguments):
        result = await self.session.call_tool(name, arguments)
        text = "".join(getattr(item, "text", "") for item in result.content)
        if result.is_error:
            raise RuntimeError(f"{self.name} {name}: {text}")
        data = result.structured_content
        if data is not None:
            return data.get("result", data) if set(data) == {"result"} else data
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def act(self, tool: str, **arguments):
        """A tool that changes chain state: Lore signs and sends, and returns once every transaction is mined."""
        out = await self.tool(tool, **arguments)
        assert out.get("tx_hashes"), f"{self.name}: {tool} sent nothing"
        return out

    async def create_wallet(self) -> None:
        domain = os.environ.get("E2E_EMAIL_DOMAIN", "example.com")
        out = await self.tool("lore_create_wallet", email=f"lore-e2e-{self.name}@{domain}")
        self.address = self.reader.web3.to_checksum_address(out["address"])
        who = await self.tool("lore_whoami")
        assert out["address"].lower() in who["wallets"], f"{self.name}: wallet not linked"


async def wait_for(description: str, check, every: float = 5):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        value = await asyncio.to_thread(check)
        if value:
            return value
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out waiting for {description}")


def deployer_key() -> str:
    for line in (ROOT / "contracts" / ".env").read_text().splitlines():
        if line.startswith("DEPLOYER_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("DEPLOYER_PRIVATE_KEY missing from contracts/.env (it pays the test agents' gas)")


def fund(reader: ChainReader, addresses: list[str], amount: int) -> None:
    """Testnet only: 0G (stakes, purchases and gas) from the deployer key."""
    w3, payer = reader.web3, Account.from_key(deployer_key())

    def sendtx(tx):
        tx.update({"from": payer.address, "nonce": w3.eth.get_transaction_count(payer.address, "pending"),
                   "gasPrice": w3.eth.gas_price, "chainId": w3.eth.chain_id})
        tx.setdefault("gas", int(w3.eth.estimate_gas(tx) * 1.3))
        r = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(payer.sign_transaction(tx).raw_transaction), timeout=180)
        assert r.status == 1, "funding transaction reverted"

    for a in addresses:
        if w3.eth.get_balance(a) < amount // 2:
            sendtx({"to": a, "value": amount, "gas": 21000})


class Scenario:
    def __init__(self, reader: ChainReader, agents: dict[str, Agent], tag: str | None):
        self.reader, self.a, self.tag = reader, agents, tag
        self.results: dict = {}

    def sub(self, sub_id: int) -> dict:
        return self.reader.record("Submissions", "getSubmission", sub_id)

    def topic_of(self, node_id: int) -> int:
        """A new-topic submission keeps topicId 0 on-chain; the node knows its real topic."""
        return self.reader.record("TopicGraph", "getNode", node_id)["topicId"]

    def treasury(self) -> int:
        """The treasury's 0G: rejected stakes, stake fees and the platform cut land here."""
        return self.reader.web3.eth.get_balance(self.reader.deployment["roles"]["treasury"])

    async def submit(self, who: str, text: str, topic_id: int = 0) -> int:
        agent = self.a[who]
        before = self.reader.call("Submissions", "submissionCount")
        await agent.act("lore_submit", text=corpus.tagged(text, self.tag), topic_id=topic_id, confirm=True)
        sid = await wait_for("submission id", lambda: (n := self.reader.call("Submissions", "submissionCount")) > before and n)
        say(f"{who} submitted sub {sid} to {'a new topic' if topic_id == 0 else f'topic {topic_id}'}")
        return sid

    async def status(self, sid: int, *wanted: int) -> dict:
        return await wait_for(f"submission {sid} in {wanted}", lambda: (s := self.sub(sid))["status"] in wanted and s)

    async def vote(self, who: str, sid: int, approve: bool) -> None:
        agent = self.a[who]
        deadline = time.monotonic() + TIMEOUT
        while True:
            tasks = (await agent.tool("lore_my_vote_tasks"))["vote_tasks"]
            if any(t.get("sub_id") == sid for t in tasks):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{who} never got a vote task for sub {sid}")
            await asyncio.sleep(5)
        task = await agent.tool("lore_get_vote_task", sub_id=sid)
        assert FRAME in task["text"], "vote-task copy is not watermarked"
        await agent.act("lore_vote", sub_id=sid, approve=approve)
        say(f"{who} voted {'yes' if approve else 'no'} on sub {sid} (verifier said {'pass' if task['report'].get('passed') else 'fail'})")

    async def node_meta(self, topic_id: int, node_id: int) -> dict:
        async def titled():
            try:
                t = await self.a["ada"].tool("lore_get_topic", topic_id=topic_id)
            except RuntimeError as e:  # e.g. a 0G Storage read failed just now
                say(f"retrying lore_get_topic: {e}")
                return None
            n = next((n for n in t["nodes"] if n["node_id"] == node_id), None)
            return n if n and n.get("meta") and n["meta"].get("title") else None

        deadline = time.monotonic() + max(TIMEOUT, 2400)  # the Router rate-limits; the curator backs off
        while time.monotonic() < deadline:
            if (n := await titled()):
                return n
            await asyncio.sleep(10)
        raise TimeoutError(f"curator never titled node {node_id}")

    def progress_path(self) -> Path:
        return ROOT / "deploy" / "state" / f"e2e-progress-{self.reader.contracts['Submissions'].address.lower()}.json"

    async def run(self, resume: bool = False) -> dict:
        r = self.results
        saved = self.progress_path()
        if resume and saved.exists():
            p = json.loads(saved.read_text())
            t1, t2, n1, n2, n4, n5, s3 = (p[k] for k in ("t1", "t2", "n1", "n2", "n4", "n5", "s3"))
            say(f"resuming after the submission phase: topics {t1}, {t2}; nodes {n1}, {n2}, {n4}, {n5}")
        else:
            t1, t2, n1, n2, n4, n5, s3 = await self.submissions()
            saved.write_text(json.dumps(dict(t1=t1, t2=t2, n1=n1, n2=n2, n4=n4, n5=n5, s3=s3)))
        return await self.market(t1, t2, n1, n2, n4, n5, s3)

    async def submissions(self):
        stake, fee = self.reader.call("Submissions", "stake"), self.reader.call("Submissions", "stakeFee")

        # 1. Ada opens topic 1; with no voters yet, the verifier's verdict decides.
        s1 = await self.submit("ada", corpus.FINETUNE_ROOT)
        rec = await self.status(s1, ACCEPTED, REJECTED)
        assert rec["status"] == ACCEPTED, "the first research note was rejected"
        n1 = rec["nodeId"]
        t1 = self.topic_of(n1)
        say(f"sub {s1} accepted: topic {t1}, node {n1}")

        # 2. Ben builds on it. Ada (topic 1's only person) must vote; the gateway finalizes early.
        s2 = await self.submit("ben", corpus.FINETUNE_MIXING, t1)
        await self.status(s2, REPORTED)
        await self.vote("ada", s2, True)
        rec = await self.status(s2, ACCEPTED, REJECTED)
        assert rec["status"] == ACCEPTED, "Ben's follow-up was rejected"
        n2 = rec["nodeId"]
        assert n1 in self.reader.call("TopicGraph", "parentsOf", n2), "follow-up does not point at the root"

        # 3. Chen submits a near-copy of Ada's note: the verifier fails it and the topic's people vote it down.
        treasury0 = self.treasury()
        s3 = await self.submit("chen", corpus.FINETUNE_COPYCAT, t1)
        rec = await self.status(s3, REPORTED)
        assert rec["pass"] is False, "verifier passed a near-copy"
        await self.vote("ada", s3, False)
        await self.vote("ben", s3, False)
        await self.status(s3, REJECTED)
        assert self.treasury() - treasury0 == stake, "a rejected stake must go entirely to the treasury"
        say(f"sub {s3} rejected; Chen's {stake / 1e18:g} 0G stake went to the treasury")

        # 4. Chen's genuine follow-up passes both people.
        s4 = await self.submit("chen", corpus.FINETUNE_EVAL, t1)
        await self.status(s4, REPORTED)
        await self.vote("ada", s4, True)
        await self.vote("ben", s4, True)
        rec = await self.status(s4, ACCEPTED, REJECTED)
        assert rec["status"] == ACCEPTED, "Chen's evaluation note was rejected"
        n4 = rec["nodeId"]

        if QUICK:  # skip topic 2: it repeats paths topic 1 already covered
            return t1, None, n1, n2, n4, None, s3

        # 5. Ben opens topic 2; Ada builds on it and Ben votes.
        s5 = await self.submit("ben", corpus.RAG_ROOT)
        rec = await self.status(s5, ACCEPTED, REJECTED)
        assert rec["status"] == ACCEPTED, "RAG root was rejected"
        n5 = rec["nodeId"]
        t2 = self.topic_of(n5)
        treasury0 = self.treasury()
        s6 = await self.submit("ada", corpus.RAG_RERANK, t2)
        await self.status(s6, REPORTED)
        await self.vote("ben", s6, True)
        rec = await self.status(s6, ACCEPTED, REJECTED)
        assert rec["status"] == ACCEPTED, "Ada's reranking note was rejected"
        assert self.treasury() - treasury0 == fee, "an accepted stake comes back minus the fee, which the treasury keeps"
        return t1, t2, n1, n2, n4, n5, s3

    async def market(self, t1, t2, n1, n2, n4, n5, s3) -> dict:
        r = self.results

        # 6. The curator gives every node a public title and description, with a checkable proof.
        meta = await self.node_meta(t1, n4)
        check = await self.a["dana"].tool("lore_check_proof", root=meta["meta"]["receipt"])
        assert check["status"] == "pass", f"curator proof did not pass: {check}"
        say(f"node {n4} titled '{meta['meta']['title']}' — proof {check['status']} ({check.get('binding')})")

        # 7. Dana buys topic 1 and reads a copy watermarked to her wallet.
        topics = await self.a["dana"].tool("lore_list_topics")
        assert any(t["topic_id"] == t1 for t in topics["topics"])
        await self.a["dana"].act("lore_buy", topic_id=t1, confirm=True)
        assert self.reader.call("Access", "hasAccess", t1, self.a["dana"].address)
        say(f"Dana bought topic {t1}: its price went to its authors")
        read = await self.a["dana"].tool("lore_read_topic", topic_id=t1)
        copies = [n["text"] for n in read["nodes"]]
        assert len(copies) == 3 and all(FRAME in c for c in copies), "Dana's copy is not watermarked per node"

        # 8. Dana's copy leaks; Ben reports it and the gateway traces it to Dana's wallet on-chain.
        excerpt = three_sentences(copies[0])
        before = self.reader.call("Reports", "nextReportId")
        await self.a["ben"].act("lore_report_leak", topic_id=t1, text=excerpt)
        rid = before
        traced = await wait_for("leak trace", lambda: (rep := self.reader.record("Reports", "getReport", rid))["matched"] and rep)
        assert traced["matchedWallet"].lower() == self.a["dana"].address.lower(), "leak traced to the wrong wallet"
        say(f"leak report {rid} traced to Dana ({traced['scoreBps'] / 100:g}% of mark bits)")

        # 9. Contributors claim royalties from Dana's purchase.
        claim = await self.a["ada"].tool("lore_claimable")
        gained = claim["total"]
        assert gained > 0, f"Ada has nothing to claim: {claim}"
        await self.a["ada"].act("lore_claim")
        assert (await self.a["ada"].tool("lore_claimable"))["total"] == 0, "Ada's claim left royalties behind"
        say(f"Ada claimed {gained / 1e18:g} 0G in royalties")

        r.update(topics=[t1, t2], nodes=[n1, n2, n4, n5], rejected=[s3], leak_report=rid, ada_royalty=gained)
        return r


async def main_async(check_only: bool, resume: bool = False) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    gateway = os.environ.get("LORE_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
    reader = ChainReader(int(os.environ.get("LORE_CHAIN_ID", 16602)))
    tag_env = os.environ.get("E2E_TAG")
    tag = None if tag_env == "none" else (tag_env or secrets.token_hex(3))
    say(f"run tag {tag} (rerun with E2E_TAG={tag} to reuse these demo people and wallets)")

    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        agents = {}
        for name in PEOPLE:
            # One demo person per run and name: a person who already has a wallet linked (an earlier run on this
            # deployment) would get a second one, and tools would then ask which wallet to use.
            token = await asyncio.to_thread(login, gateway, f"{name}-{tag}" if tag else name)
            http = await stack.enter_async_context(httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                                                     timeout=httpx2.Timeout(300)))
            streams = await stack.enter_async_context(streamable_http_client(f"{gateway}/mcp", http_client=http))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            agents[name] = Agent(name, session, reader)
        names = {t.name for t in (await agents["ada"].session.list_tools()).tools}
        missing = TOOLS - names
        assert not missing, f"hosted MCP is missing tools: {sorted(missing)}"
        say(f"{len(agents)} agents logged in; {len(names)} MCP tools available")
        if check_only:
            return
        for agent in agents.values():
            await agent.create_wallet()  # the same wallet again on a rerun: one per person
        say("Turnkey wallets created and linked to their people on-chain (PersonRegistry)")
        say("funding test agents with 0G (testnet only)")
        await asyncio.to_thread(fund, reader, [a.address for a in agents.values()], 5 * 10**17)
        result = await Scenario(reader, agents, tag).run(resume)
        say("END-TO-END PASSED: " + json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="log in and compare the tool list only")
    parser.add_argument("--resume", action="store_true",
                        help="skip the submission phase if it already completed on this deployment")
    args = parser.parse_args()
    asyncio.run(main_async(args.check, args.resume))


if __name__ == "__main__":
    main()
