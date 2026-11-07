"""The hosted gateway: service rules as a World ID person, OAuth + World ID login, internal API, MCP tools."""

import asyncio
import json
import threading
import time

import pytest
from eth_account import Account
from eth_utils import keccak
from fastapi.testclient import TestClient
from lore_shared.constants import MAX_CHARS
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from gateway import tasks, worker
from gateway.app import create_app
from gateway.db import connect
from gateway.indexer import apply
from gateway.oauth import PENDING_TTL
from gateway.service import LoreError
from gateway.worldid import WorldIDError
from tests.fakes import FakeChain, FakeContent, FakeStorage

ALICE, BOB, CAROL = (Account.create() for _ in range(3))
B32 = lambda h: bytes.fromhex(h.removeprefix("0x"))  # noqa: E731
TOKEN = "service-secret"


def person(acct) -> str:
    """A stand-in World ID nullifier: one per test human."""
    return "0x" + keccak(text="person:" + acct.address.lower()).hex()


class FakeWorld:
    """Stands in for gateway.worldid.WorldID: a proof is {"person", "signal"} and must match the signal."""

    def request(self, signal):
        return {"action": "lore-person", "signal": signal}

    def verify(self, result, signal):
        if result.get("signal") != signal or "person" not in result:
            raise WorldIDError("proof was not made for this request")
        return result["person"]


@pytest.fixture
def env(tmp_path):
    # The clock starts at the real time: the SDK's bearer middleware checks token expiry against time.time().
    chain, storage, clock = FakeChain(), FakeStorage(), {"t": int(time.time())}
    path = str(tmp_path / "gw.db")
    app = create_app(path, chain, FakeContent(), storage, now=lambda: clock["t"], world=FakeWorld(),
                     service_token=TOKEN, public_url="https://lore.test", provider_signer=lambda p: "0xsigner")
    db, log = connect(path), {"block": 0}

    def ev(name, **a):
        log["block"] += 1
        with db:
            apply(db, name, a, log["block"], 0, clock["t"])

    for acct in (ALICE, BOB, CAROL):  # each human has one wallet linked to their World ID
        ev("AgentRegistered", agent=acct.address, person=B32(person(acct)))
    return type("Env", (), dict(chain=chain, storage=storage, clock=clock, app=app, svc=app.state.service,
                                db=db, ev=ev, path=path, oauth=app.state.oauth, mcp=app.state.mcp))


def submit(env, acct, text, topic_id):
    """The person ingests through the service, then 'their wallet' submits on-chain."""
    r = env.svc.prepare_submit(person(acct), text, topic_id)
    sid = 1 + env.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    env.ev("Submitted", subId=sid, topicId=topic_id, submitter=acct.address,
           contentHash=B32(r["content_hash"]), storageRoot=B32(r["storage_root"]))
    return sid, r


def seed(env):
    """Alice founds topic 1 (node 1); Bob then submits sub 2 to topic 1, which the verifier reports on."""
    submit(env, ALICE, "alice research", 0)
    env.ev("Reported", subId=1, **{"pass": True}, parents=[], reportHash=B32("0x" + "00" * 32), eligibleVoters=0)
    env.ev("NodeAdded", topicId=1, nodeId=1, contributor=ALICE.address, parents=[])
    env.ev("Finalized", subId=1, accepted=True, nodeId=1)
    env.chain.since[(1, ALICE.address.lower())] = env.clock["t"]
    env.clock["t"] += 10
    submit(env, BOB, "bob follow-up", 1)
    report = {"parents": [{"node_id": 1, "score": 0.8}], "receipts": ["0xreceipt"], "passed": True,
              "checks": {"relevant": True}}
    env.ev("Reported", subId=2, **{"pass": True}, parents=[1], reportHash=B32(env.storage.upload(json.dumps(report).encode())),
           eligibleVoters=1)


# --- vote-before-submit, per person ---

def test_contributor_must_vote_before_submitting(env):
    seed(env)
    assert [t["sub_id"] for t in env.svc.vote_tasks(person(ALICE))] == [2]
    with pytest.raises(LoreError, match="vote tasks"):
        env.svc.prepare_submit(person(ALICE), "alice again", 1)
    assert env.svc.vote_tasks(person(BOB)) == []  # never on your own submission
    env.ev("Voted", subId=2, voter=ALICE.address, person=B32(person(ALICE)), approve=True)
    assert env.svc.vote_tasks(person(ALICE)) == []
    tx = env.svc.prepare_submit(person(ALICE), "alice again", 1)["transactions"]
    assert [t["description"] for t in tx] == ["Submissions.submit"] and tx[0]["value"] == env.chain.stake()  # 0G stake


def test_one_vote_per_person_across_their_wallets(env):
    seed(env)
    second = Account.create()  # alice's second wallet
    env.ev("AgentRegistered", agent=second.address, person=B32(person(ALICE)))
    env.ev("Voted", subId=2, voter=second.address, person=B32(person(ALICE)), approve=True)
    assert env.svc.vote_tasks(person(ALICE)) == []


def test_vote_task_is_watermarked_logged_and_only_for_eligible_people(env):
    seed(env)
    t = env.svc.vote_task(person(ALICE), 2)
    assert t["text"] == f"bob follow-up[wm:{ALICE.address.lower()}:sub:2]" and t["receipts"] == ["0xreceipt"]
    assert [tuple(r) for r in env.db.execute("SELECT wallet, ref FROM deliveries")] == [(ALICE.address.lower(), "sub:2")]
    with pytest.raises(LoreError, match="not eligible"):
        env.svc.vote_task(person(CAROL), 2)
    assert env.svc.prepare_vote(person(ALICE), 2, True)["transactions"][0]["description"] == "Submissions.vote"


def test_vote_window_uses_the_onchain_window(env):
    seed(env)
    env.chain.window, env.chain.votable = 3600, set()
    env.clock["t"] += 3600
    assert env.svc.vote_tasks(person(ALICE)) == []


def test_copied_content_is_never_decrypted_for_the_copiers_accomplices(env):
    seed(env)
    r = env.svc.prepare_submit(person(CAROL), "CAROL SECRET", 1)  # her submit never lands; Bob copies it
    env.ev("Submitted", subId=3, topicId=1, submitter=BOB.address, contentHash=B32(r["content_hash"]),
           storageRoot=B32(r["storage_root"]))
    env.ev("Reported", subId=3, **{"pass": False}, parents=[], eligibleVoters=1,
           reportHash=B32(env.storage.upload(json.dumps({"parents": [], "receipts": []}).encode())))
    assert env.svc.vote_task(person(ALICE), 3)["text"] is None
    assert env.svc.internal_plaintext("sub", 3)["ingested"] is False
    assert 3 not in [p["sub_id"] for p in env.svc.internal_candidates(1)["pending"]]


def test_an_old_ingest_cannot_skip_votes_assigned_later(env):
    seed(env)
    r = env.svc.prepare_submit(person(CAROL), "prepared early", 1)
    env.clock["t"] += 3600
    env.ev("Submitted", subId=4, topicId=1, submitter=CAROL.address, contentHash=B32(r["content_hash"]),
           storageRoot=B32(r["storage_root"]))
    assert env.svc.internal_plaintext("sub", 4)["ingested"] is False


# --- submitting ---

def test_submit_refuses_without_a_wallet_while_lagging_too_long_or_marked(env):
    nobody = "0x" + "99" * 32
    with pytest.raises(LoreError, match="lore_create_wallet"):
        env.svc.prepare_submit(nobody, "x", 0)
    with pytest.raises(LoreError, match="longer than"):
        env.svc.prepare_submit(person(ALICE), "x" * (MAX_CHARS + 1), 0)
    with pytest.raises(LoreError, match="reserved"):
        env.svc.prepare_submit(person(ALICE), "copy⁣with a mark", 0)
    env.chain.head = 1_000
    with pytest.raises(LoreError, match="catching up"):
        env.svc.prepare_submit(person(ALICE), "x", 0)
    assert env.storage.blobs == {}


def test_submit_can_be_retried_until_it_lands_and_never_by_someone_else(env):
    first = env.svc.prepare_submit(person(ALICE), "draft", 0)
    again = env.svc.prepare_submit(person(ALICE), "draft", 0)
    assert again["storage_root"] == first["storage_root"] and len(env.storage.blobs) == 1
    with pytest.raises(LoreError, match="already submitted"):
        env.svc.prepare_submit(person(BOB), "draft", 0)


def test_with_several_wallets_the_person_must_choose(env):
    second = Account.create()
    env.ev("AgentRegistered", agent=second.address, person=B32(person(ALICE)))
    with pytest.raises(LoreError, match="several wallets"):
        env.svc.prepare_buy(person(ALICE), 1)
    with pytest.raises(LoreError, match="isn't linked"):
        env.svc.prepare_buy(person(ALICE), 1, wallet=BOB.address)
    tx = env.svc.prepare_buy(person(ALICE), 1, wallet=second.address)["transactions"]
    assert [t["description"] for t in tx] == ["Access.buy"] and tx[0]["from"] == second.address.lower()
    assert tx[0]["value"] == env.chain.price  # the price, exactly


# --- reading ---

def test_reading_needs_access_and_each_node_copy_is_watermarked_and_logged(env):
    seed(env)
    with pytest.raises(LoreError, match="buy this topic"):
        env.svc.read_topic(person(CAROL), 1)
    env.chain.access.add((1, CAROL.address.lower()))
    nodes = env.svc.read_topic(person(CAROL), 1)["nodes"]
    env.svc.read_topic(person(CAROL), 1)  # reading again logs nothing new
    assert nodes[0]["text"] == f"alice research[wm:{CAROL.address.lower()}:node:1]"
    assert [r[0] for r in env.db.execute("SELECT ref FROM deliveries WHERE wallet=?", (CAROL.address.lower(),))] == ["node:1"]


def test_a_failed_commit_is_an_error_not_a_silent_success(env, monkeypatch):
    import gateway.service as svc_mod

    class CommitFails:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            raise RuntimeError("disk full")

    seed(env)
    env.chain.access.add((1, CAROL.address.lower()))
    real = svc_mod.connect
    monkeypatch.setattr(svc_mod, "connect", lambda p: CommitFails(real(p)))
    with pytest.raises(RuntimeError, match="disk full"):  # the copy is never returned if its log didn't land
        env.svc.read_topic(person(CAROL), 1)


# --- royalties, leaks, proofs, inbox ---

def test_claim_prepares_one_transaction_per_contributing_wallet(env):
    seed(env)
    env.chain.claimable_by_node = {1: 4_500000}
    assert env.svc.claimable(person(ALICE)) == {"total": 4_500000, "by_node": {1: 4_500000}}
    tx = env.svc.prepare_claim(person(ALICE))["transactions"]
    assert len(tx) == 1 and tx[0]["from"] == ALICE.address.lower() and tx[0]["args"] == [[1]]


def test_leak_matching_traces_buyers_and_voters_and_accuses_nobody_without_an_exact_mark(env):
    seed(env)
    env.chain.access.add((1, CAROL.address.lower()))
    env.svc.read_topic(person(CAROL), 1)
    env.svc.vote_task(person(ALICE), 2)
    assert sorted(worker.candidates_for(env.db, 1)) == sorted([(CAROL.address.lower(), "node:1"), (ALICE.address.lower(), "sub:2")])
    for rid, match in ((7, (ALICE.address.lower(), "sub:2", 9000)), (8, (None, None, 0))):
        h = env.svc.prepare_report_leak(person(BOB), 1, f"leaked {rid}")["evidence_hash"]
        env.ev("LeakReported", reportId=rid, topicId=1, reporter=BOB.address, evidenceHash=B32(h))
        worker.match_leaks(env.db, env.chain, lambda text, cands, m=match: m, set())
    assert env.chain.sent == [("Reports", "recordMatch", 7, ALICE.address, 9000)]


def test_check_proof_uses_the_onchain_signer(env):
    root = env.storage.upload(json.dumps({"provider": "0xP", "signed_text": "x", "signature": "0x00",
                                          "signer": "0xsigner", "response_hash": "0x"}).encode())
    out = env.svc.check_proof(root)
    assert out["checks"]["signer_matches_onchain"] is False and out["status"] == "fail"  # bad signature


def test_inbox_reports_changes_once_and_keeps_vote_tasks(env):
    seed(env)
    assert [t["sub_id"] for t in env.svc.inbox(person(ALICE))["vote_tasks"]] == [2]  # first look: baseline
    env.ev("NodeAdded", topicId=1, nodeId=5, contributor=BOB.address, parents=[1])
    assert "1 new update" in env.svc.notice(person(ALICE))
    assert env.svc.inbox(person(ALICE))["updates"] == ["New node #5 in topic 1: (title pending)"]
    assert env.svc.inbox(person(ALICE))["updates"] == []


# --- internal API: service secret only ---

def test_internal_endpoints_need_the_service_token(env):
    seed(env)
    with TestClient(env.app) as c:
        assert c.get("/internal/plaintext/sub/2").status_code == 401
        assert c.get("/internal/plaintext/sub/2", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/internal/plaintext/sub/2", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert ok["ingested"] and ok["text"] == "bob follow-up"
        assert c.get("/internal/topics/1", headers={"Authorization": f"Bearer {TOKEN}"}).json()["topic_id"] == 1


# --- hosted MCP: OAuth with World ID ---

def test_mcp_refuses_requests_without_a_world_id_login(env):
    with TestClient(env.app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
        assert c.get("/.well-known/oauth-authorization-server").json()["issuer"].startswith("https://lore.test")


def test_world_id_login_issues_a_token_for_the_person(env):
    async def flow():
        client = OAuthClientInformationFull(client_id="claude", redirect_uris=["https://client.test/cb"],
                                            token_endpoint_auth_method="none")
        await env.oauth.register_client(client)
        url = await env.oauth.authorize(client, AuthorizationParams(
            state="s1", scopes=["lore"], code_challenge="c" * 43, redirect_uri="https://client.test/cb",
            redirect_uri_provided_explicitly=True))
        pending = url.split("pending=")[1]
        with TestClient(env.app) as c:
            assert c.get(f"/login?pending={pending}").status_code == 200
            assert c.get(f"/login/request?pending={pending}").json()["signal"] == pending
            bad = c.post(f"/login/complete?pending={pending}", json={"signal": "another-login", "person": "0x1"})
            assert bad.status_code == 400  # a proof made for another login is refused
            done = c.post(f"/login/complete?pending={pending}", json={"signal": pending, "person": person(ALICE)}).json()
            assert c.post(f"/login/complete?pending={pending}", json={"signal": pending, "person": "0x1"}).status_code == 404
        code = done["redirect"].split("code=")[1].split("&")[0]
        assert "state=s1" in done["redirect"]
        loaded = await env.oauth.load_authorization_code(client, code)
        token = await env.oauth.exchange_authorization_code(client, loaded)
        assert await env.oauth.load_authorization_code(client, code) is None  # single use
        access = await env.oauth.load_access_token(token.access_token)
        assert access.subject == person(ALICE)
        refreshed = await env.oauth.exchange_refresh_token(client, await env.oauth.load_refresh_token(client, token.refresh_token), [])
        assert await env.oauth.load_refresh_token(client, token.refresh_token) is None  # rotated
        await env.oauth.revoke_token(await env.oauth.load_access_token(refreshed.access_token))
        assert await env.oauth.load_access_token(refreshed.access_token) is None

    asyncio.run(flow())


def call_tool_as(env, who: str, name: str, args: dict):
    async def go():
        ctx = auth_context_var.set(AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=["lore"], subject=who)))
        try:
            return await env.mcp.call_tool(name, args)
        finally:
            auth_context_var.reset(ctx)

    return asyncio.run(go())


def test_tools_act_as_the_logged_in_person(env):
    seed(env)
    me = call_tool_as(env, person(ALICE), "lore_whoami", {})
    out = me.structured_content or json.loads(me.content[0].text)
    assert out["person"] == person(ALICE) and out["wallets"] == [ALICE.address.lower()]
    assert "1 open vote task" in out["lore_inbox"]
    with pytest.raises(LoreError.__mro__[1], match="lore_create_wallet"):  # MCP shows the message to the user
        call_tool_as(env, "0x" + "77" * 32, "lore_buy", {"topic_id": 1, "confirm": True})


# --- indexer, worker, databases ---

def test_indexer_is_idempotent(env):
    seed(env)
    with env.db:
        apply(env.db, "Voted", {"subId": 2, "voter": CAROL.address, "person": B32(person(CAROL)), "approve": True}, 1, 0, 0)
    assert env.db.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 0


def test_finalizer_only_sends_what_the_chain_says_can_finalize(env):
    seed(env)
    assert tasks.finalizable(env.db, env.chain) == []
    env.chain.finalizable_ids = {2}
    worker.finalize_due(env.db, env.chain)
    assert env.chain.sent == [("Submissions", "finalize", 2)]


def test_worker_indexes_on_its_own_thread_in_batches(tmp_path):
    chain = FakeChain()
    chain.events = [(5, 0, "TopicCreated", {"topicId": 1}, 0), (3000, 0, "TopicCreated", {"topicId": 2}, 0)]
    chain.head = 3000
    path = str(tmp_path / "w.db")
    connect(path).close()

    def index_twice():
        db = connect(path)
        assert worker.sync(db, chain, start_block=0) == 1999
        assert worker.sync(db, chain, start_block=0) == 3000

    t = threading.Thread(target=index_twice)
    t.start()
    t.join()
    assert [r[0] for r in connect(path).execute("SELECT id FROM topics ORDER BY id")] == [1, 2]


def test_a_connection_can_be_used_from_another_thread(tmp_path):
    conn, errors = connect(str(tmp_path / "t.db")), []

    def use():
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert errors == []


def test_fill_meta_loads_json_named_by_the_onchain_root(env):
    seed(env)
    root = env.storage.upload(json.dumps({"title": "T"}).encode())
    env.ev("NodeMetaSet", nodeId=1, metaHash=B32(root))
    worker.fill_meta(env.db, env.storage)
    assert json.loads(env.db.execute("SELECT meta_json FROM nodes WHERE id=1").fetchone()[0]) == {"title": "T"}


def test_max_lag_tolerates_normal_indexing_jitter():
    from gateway.service import MAX_LAG

    assert MAX_LAG * 0.49 >= 5 * worker.SYNC_EVERY


def test_a_database_from_another_deployment_or_an_older_version_is_refused(tmp_path):
    import sqlite3

    from dbguard import WrongDatabase
    from gateway.db import bind_deployment

    db = connect(str(tmp_path / "a.db"))
    bind_deployment(db, "16602:0xaaa:1")
    bind_deployment(db, "16602:0xaaa:1")
    with pytest.raises(WrongDatabase, match="belongs to"):
        bind_deployment(db, "16602:0xbbb:2")
    old = sqlite3.connect(str(tmp_path / "old.db"))
    old.executescript("CREATE TABLE seen (block INT, idx INT); INSERT INTO seen VALUES (1, 0);")
    old.commit()
    with pytest.raises(WrongDatabase, match="older version"):
        bind_deployment(connect(str(tmp_path / "old.db")), "16602:0xaaa:1")


def test_a_logged_in_client_calls_tools_over_http(env):
    async def login() -> str:
        client = OAuthClientInformationFull(client_id="claude", redirect_uris=["https://client.test/cb"],
                                            token_endpoint_auth_method="none")
        await env.oauth.register_client(client)
        url = await env.oauth.authorize(client, AuthorizationParams(
            state=None, scopes=["lore"], code_challenge="c" * 43, redirect_uri="https://client.test/cb",
            redirect_uri_provided_explicitly=True))
        redirect = env.oauth.complete_login(url.split("pending=")[1], person(ALICE))
        code = await env.oauth.load_authorization_code(client, redirect.split("code=")[1].split("&")[0])
        return (await env.oauth.exchange_authorization_code(client, code)).access_token

    token = asyncio.run(login())
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

    def rpc(c, body, session=None):
        r = c.post("/mcp", json=body, headers={**h, **({"mcp-session-id": session} if session else {})})
        data = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
        return r, json.loads(data[-1]) if data else r.json()

    with TestClient(env.app, base_url="https://lore.test") as c:
        r, init = rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert r.status_code == 200 and init["result"]["serverInfo"]["name"] == "lore"
        session = r.headers.get("mcp-session-id")
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={**h, **({"mcp-session-id": session} if session else {})})
        _, out = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "lore_whoami", "arguments": {}}}, session)
        result = out["result"]
        payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert payload["person"] == person(ALICE) and payload["wallets"] == [ALICE.address.lower()]


# --- OAuth hardening ---

def test_only_safe_redirects_can_be_registered():
    from gateway.oauth import safe_redirect

    for ok in ("https://claude.ai/cb", "http://localhost:3334/callback", "http://127.0.0.1:9/cb", "cursor://anysphere/cb"):
        assert safe_redirect(ok), ok
    for bad in ("javascript:alert(document.domain)//", "data:text/html,x", "http://evil.test/cb", "file:///etc/passwd",
                "vbscript:x"):
        assert not safe_redirect(bad), bad


def test_registering_a_javascript_redirect_is_refused_over_http(env):
    with TestClient(env.app) as c:
        r = c.post("/register", json={"redirect_uris": ["javascript:alert(document.domain)//"], "client_name": "x",
                                      "token_endpoint_auth_method": "none"})
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        ok = c.post("/register", json={"redirect_uris": ["http://localhost:3334/callback"], "client_name": "Claude Code",
                                       "token_endpoint_auth_method": "none"})
        assert ok.status_code == 201


async def start_login(env, client_id="claude", name="Claude Code", redirect="http://localhost:3334/cb"):
    client = OAuthClientInformationFull(client_id=client_id, client_name=name, redirect_uris=[redirect],
                                        token_endpoint_auth_method="none")
    await env.oauth.register_client(client)
    url = await env.oauth.authorize(client, AuthorizationParams(
        state="s", scopes=["lore"], code_challenge="c" * 43, redirect_uri=redirect, redirect_uri_provided_explicitly=True))
    return client, url.split("pending=")[1]


def test_consent_page_names_the_app_escapes_it_and_warns_about_remote_redirects(env):
    _, local = asyncio.run(start_login(env))
    _, remote = asyncio.run(start_login(env, "evil", '<script>alert(1)</script>Evil', "https://evil.test/cb"))
    with TestClient(env.app) as c:
        page = c.get(f"/login?pending={local}").text
        assert "Claude Code" in page and "localhost:3334" in page and "not on your own computer" not in page
        page = c.get(f"/login?pending={remote}").text
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
        assert "evil.test" in page and "not on your own computer" in page and "Approve" in page


def test_logins_codes_and_tokens_expire_and_are_bound_to_their_client(env):
    async def flow():
        client, pending = await start_login(env)
        other, _ = await start_login(env, "other")
        env.clock["t"] += PENDING_TTL + 1  # past the pending login's lifetime
        assert env.oauth.pending(pending) is None and env.oauth.complete_login(pending, person(ALICE)) is None

        client, pending = await start_login(env)
        code = env.oauth.complete_login(pending, person(ALICE)).split("code=")[1].split("&")[0]
        assert await env.oauth.load_authorization_code(other, code) is None  # another client can't redeem it
        token = await env.oauth.exchange_authorization_code(client, await env.oauth.load_authorization_code(client, code))
        assert await env.oauth.load_refresh_token(other, token.refresh_token) is None
        env.clock["t"] += 3601  # past the access token's hour
        assert await env.oauth.load_access_token(token.access_token) is None

        client, pending = await start_login(env)
        code = env.oauth.complete_login(pending, person(ALICE)).split("code=")[1].split("&")[0]
        env.clock["t"] += 301  # past the code's 5 minutes
        assert await env.oauth.load_authorization_code(client, code) is None

    asyncio.run(flow())


def test_a_reused_code_is_invalid_grant_not_a_crash(env):
    from mcp.server.auth.provider import TokenError

    async def flow():
        client, pending = await start_login(env)
        code = env.oauth.complete_login(pending, person(ALICE)).split("code=")[1].split("&")[0]
        loaded = await env.oauth.load_authorization_code(client, code)
        await env.oauth.exchange_authorization_code(client, loaded)
        with pytest.raises(TokenError) as e:
            await env.oauth.exchange_authorization_code(client, loaded)  # the racing second redemption
        assert e.value.error == "invalid_grant"

    asyncio.run(flow())


def test_expired_oauth_rows_are_cleaned_up(env):
    asyncio.run(start_login(env))
    env.clock["t"] += 31 * 24 * 3600
    asyncio.run(start_login(env))
    assert env.db.execute("SELECT COUNT(*) FROM oauth_pending").fetchone()[0] == 1


def test_paying_tools_send_their_transactions_through_the_signer(env):
    class RecordingSigner:
        def __init__(self):
            self.txs = []

        def send_all(self, txs):
            self.txs += txs
            return [f"0xhash{i}" for i in range(len(txs))]

    env.svc.signer = RecordingSigner()
    with env.svc.tx() as conn:  # a Lore-managed (Turnkey) wallet: Lore signs for it
        conn.execute("INSERT INTO turnkey_wallets (person, email, created_ms, address, sub_org, ready)"
                     " VALUES (?, 'a@example.com', 0, ?, 'sub-a', 1)", (person(ALICE), ALICE.address.lower()))
    out = env.svc.send(env.svc.prepare_buy(person(ALICE), 1))
    assert out["tx_hashes"] == ["0xhash0"] and "transactions" not in out
    assert [t["description"] for t in env.svc.signer.txs] == ["Access.buy"]


def test_a_banned_person_cannot_start_a_submission_or_purchase(env):
    env.chain.banned = {ALICE.address.lower()}
    try:
        with pytest.raises(LoreError, match="removed from Lore"):
            env.svc.prepare_submit(person(ALICE), "x" * 600, 0)
        with pytest.raises(LoreError, match="removed from Lore"):
            env.svc.prepare_buy(person(ALICE), 1)
    finally:
        env.chain.banned = set()
