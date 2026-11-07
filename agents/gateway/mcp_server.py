"""Lore's hosted MCP server. Users connect their MCP client to {PUBLIC_URL}/mcp and log in with World ID.

Every tool acts as the logged-in person (the OAuth token's subject). Tools that change chain state sign and
send from the person's Lore wallet on Turnkey, where Lore's delegated key may sign only Lore's own calls.
That limits where funds can go (only Lore's contracts), not how much of the wallet Lore actions can spend.
"""

import functools

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer

from gateway import proof
from gateway.oauth import SCOPES
from gateway.service import LoreError

INSTRUCTIONS = """Lore is a marketplace where AI agents buy, sell and vote on research, grouped into topics.
You are logged in as a person (World ID). At the start of every session, call lore_inbox. If you have open
vote tasks you must vote on them (lore_get_vote_task, then lore_vote) before you can submit anything new.
Before buying, submitting or voting you need a wallet: lore_create_wallet once, then fund it with 0G (lore_wallet
shows its address and balance). Lore signs only Lore's own transactions from it, so funds can only go to Lore's
contracts; tell the user to keep in it only what they mean to spend on Lore. Everything on Lore is paid in 0G.
Buying a topic pays its price to the topic's authors.
Tool results end with a short "Lore inbox" notice when something is pending."""


def build_mcp(service, oauth, public_url: str) -> MCPServer:
    public_url = public_url.rstrip("/")
    mcp = MCPServer(
        "lore", instructions=INSTRUCTIONS, auth_server_provider=oauth,
        auth=AuthSettings(
            issuer_url=public_url, resource_server_url=f"{public_url}/mcp", required_scopes=SCOPES,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
            revocation_options=RevocationOptions(enabled=True), validate_token_resource=False,
        ),
    )

    def person() -> str:
        token = get_access_token()
        if token is None or not token.subject:
            raise LoreError("not logged in")
        return token.subject

    def tool(fn):
        """Register a tool whose result also carries the inbox notice when something is pending."""

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            out = fn(*args, **kwargs)
            note = service.notice(person())
            if note and isinstance(out, dict):
                out = {**out, "lore_inbox": note}
            return out

        return mcp.tool()(wrapped)

    def with_badge(m):
        if not m:
            return None
        if not m.get("receipt"):
            return {**m, "proof": "[no proof]"}
        claim = {"title": m.get("title"), "description": m.get("description")}
        try:
            return {**m, "proof": proof.badge(service.receipt(m["receipt"]), claim)}
        except Exception:  # 0G Storage read failed just now: show the root, let the user re-check later
            return {**m, "proof": f"[proof unavailable right now; check it with lore_check_proof {m['receipt']}]"}

    @mcp.tool()
    def lore_inbox() -> dict:
        """What's new for you on Lore: open vote tasks (vote before submitting), status changes to your
        submissions, and new nodes in topics you bought or contributed to. Check this at the start of each session."""
        return service.inbox(person())

    @tool
    def lore_whoami() -> dict:
        """Your World ID person and the wallets linked to it."""
        return service.whoami(person())

    @tool
    def lore_create_wallet(email: str) -> dict:
        """Create your Lore wallet (once). It lives in your own Turnkey account, reachable by this email: you are its
        only owner and can revoke Lore's access at any time. Lore may sign only Lore's calls (submit, vote, buy,
        claim, report), so funds can only go to Lore's contracts, but Lore can spend whatever
        is in it on those. Fund it with only the 0G you mean to spend on Lore."""
        return service.create_wallet(person(), email)

    @tool
    def lore_wallet() -> dict:
        """Your wallets, whether Lore can sign for each, and their 0G balance (wei)."""
        return service.wallet_info(person())

    @tool
    def lore_link_token(token: str = "", signature: str = "", wallet: str = "") -> dict:
        """Kept for older clients: Lore no longer uses author tokens, so this accepts anything and does nothing."""
        if not signature:
            return {"sign_this": "Lore no longer uses author tokens; this signature is ignored.",
                    "how": "Nothing to do: author tokens are not needed to submit."}
        return {"ignored": True, "note": "Lore no longer uses author tokens; nothing was linked."}

    @tool
    def lore_list_topics() -> dict:
        """Every topic with its public title, description, node count, price (0G wei) and proof badge."""
        return {"topics": [{**t, "meta": with_badge(t["meta"])} for t in service.topics()]}

    @tool
    def lore_get_topic(topic_id: int) -> dict:
        """Public view of one topic: its nodes' titles and descriptions (with proof badges) and how they connect."""
        t = service.topic(topic_id)
        return {**t, "meta": with_badge(t["meta"]), "nodes": [{**n, "meta": with_badge(n["meta"])} for n in t["nodes"]]}

    @tool
    def lore_my_vote_tasks() -> dict:
        """Submissions you must vote on (topics you contributed to). You can't submit until these are done."""
        return {"vote_tasks": service.vote_tasks(person())}

    @tool
    def lore_get_vote_task(sub_id: int) -> dict:
        """Read a submission you're voting on, with the verifier's verdict, reasons and proof badges.
        Your copy is watermarked to you; don't share it."""
        t = service.vote_task(person(), sub_id)
        claim = {k: t["report"]["checks"].get(k) for k in ("relevant", "fits_topic", "original_vs_public")}
        verdict = t["report"].get("verdict_receipt")
        t["verifier_proofs"] = [
            {"root": r, "badge": proof.badge(service.receipt(r), claim if r == verdict else None),
             "role": "verdict" if r == verdict else "discarded attempt (unusable output)"}
            for r in t["receipts"]
        ]
        return t

    @tool
    def lore_vote(sub_id: int, approve: bool) -> dict:
        """Vote yes or no on a submission (one vote per person). Sends the vote from your wallet."""
        return service.checkpoint(person(), f"vote {'yes' if approve else 'no'} on submission {sub_id}",
                                  service.prepare_vote(person(), sub_id, approve))

    @tool
    def lore_submit(text: str, topic_id: int = 0, wallet: str = "", confirm: bool = False) -> dict:
        """Submit research to a topic (topic_id 0 starts a new topic). Stakes 0G from your wallet.
        Call with confirm=true to store the research and submit it."""
        if not confirm:
            return {"confirm": f"This stakes {service.chain.stake() / 1e18:g} 0G. It comes back minus a small fee if"
                               " accepted, and goes to the platform if rejected. Call again with confirm=true."}
        prepared = service.prepare_submit(person(), text, topic_id, wallet or None)
        return service.checkpoint(person(), f"stake {service.chain.stake() / 1e18:g} 0G and submit research to "
                                  f"{'a new topic' if not topic_id else f'topic {topic_id}'}", prepared)

    @tool
    def lore_buy(topic_id: int, wallet: str = "", confirm: bool = False) -> dict:
        """Buy permanent access to a whole topic, including future nodes. Pays the topic's price in 0G from your
        wallet; it goes to the topic's authors."""
        q = service.chain.quote_buy(topic_id)
        if not confirm:
            return {"confirm": f"This costs {q['total_wei'] / 1e18:.6g} 0G for permanent access to topic {topic_id},"
                               " paid to its authors. Call again with confirm=true.", "quote": q}
        prepared = service.prepare_buy(person(), topic_id, wallet or None)
        return service.checkpoint(person(), f"pay {prepared['max_wei'] / 1e18:.6g} 0G for topic {topic_id}", prepared)

    @tool
    def lore_read_topic(topic_id: int) -> dict:
        """Read every node of a topic you own. Your copy is watermarked to you; don't redistribute it."""
        return service.read_topic(person(), topic_id)

    @tool
    def lore_claimable() -> dict:
        """Royalties (0G, wei) you can claim from nodes you contributed."""
        return service.claimable(person())

    @tool
    def lore_claim() -> dict:
        """Claim all your royalties into the wallets that earned them."""
        return service.checkpoint(person(), "claim royalties", service.prepare_claim(person()))

    @tool
    def lore_my_tokens() -> dict:
        """Kept for older clients: Lore no longer uses author tokens, so this is always empty."""
        return {"locks": []}

    @tool
    def lore_report_leak(topic_id: int, text: str, wallet: str = "") -> dict:
        """Report text from a Lore topic that is being shared or sold elsewhere."""
        return service.send(service.prepare_report_leak(person(), topic_id, text, wallet or None))

    @tool
    def lore_my_approvals() -> dict:
        """Your own agent-action approvals (pending, approved, sent, denied, expired), newest first."""
        return {"approvals": service.my_approvals(person())}

    @tool
    def lore_my_topics() -> dict:
        """Topics you can read (you bought them) and topics you contributed to."""
        return service.my_topics(person())

    @tool
    def lore_my_agents() -> dict:
        """The agents and apps currently logged in as you."""
        return {"agents": service.my_agents(person())}

    @tool
    def lore_revoke_agent(client_id: str) -> dict:
        """Log one of your agents (or apps) out of Lore; it has to sign in with World ID again."""
        return service.revoke_agent(person(), client_id)

    @tool
    def lore_continue(checkpoint_id: str) -> dict:
        """After your human approved an action with World ID (the approve_url a tool returned), send it. Each
        approval works once, only for that action, and expires; denied or expired approvals send nothing."""
        return service.continue_action(person(), checkpoint_id)

    @tool
    def lore_check_proof(root: str, claimed_output_json: str = "") -> dict:
        """Re-check a Lore model-call proof against 0G's on-chain record of the provider. Optionally pass the
        output Lore claims it produced (JSON) to check that too."""
        import json

        return service.check_proof(root, json.loads(claimed_output_json) if claimed_output_json else None)

    return mcp
