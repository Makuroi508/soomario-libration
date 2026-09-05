"""
One-time: authorize an AGENT WALLET on Bulk for your master account.
=====================================================================
Run this LOCALLY (never on Railway). It needs the MASTER wallet's private key
exactly once, to sign the `agentWalletCreation` action. After that the bot
trades with the agent key only (BULK_AGENT=1) and the master key stays offline.

Requires Python with bulk-keychain installed (Linux/mac any 3.9-3.13; on
Windows only Python 3.12 has a prebuilt wheel):  pip install bulk-keychain requests

Usage:
  set BULK_MASTER_PRIVATE_KEY=<base58 secret of the master wallet>   (env only)
  python bulk_authorize_agent.py --new-agent                 # generate + authorize
  python bulk_authorize_agent.py --agent-pubkey <PUBKEY>    # authorize an existing agent
  python bulk_authorize_agent.py --agent-pubkey <PUBKEY> --revoke
Options: --network mainnet|testnet (default mainnet), --rest-url <base>

It prints the exact Railway variables to set. If it generated a new agent it
prints the agent SECRET once - store it in Railway (BULK_PRIVATE_KEY) and in a
password manager; it is not saved anywhere by this script.
"""
import argparse
import json
import os
import sys

import requests

DEFAULT_REST = {
    "mainnet": "https://mainnet-api1.bulk.trade/api/v1",
    "testnet": "https://exchange-api.bulk.trade/api/v1",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="mainnet")
    ap.add_argument("--rest-url", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--new-agent", action="store_true", help="generate a fresh agent keypair")
    g.add_argument("--agent-pubkey", help="authorize/revoke this existing agent pubkey")
    ap.add_argument("--revoke", action="store_true")
    args = ap.parse_args()

    master_secret = (os.getenv("BULK_MASTER_PRIVATE_KEY") or "").strip()
    if not master_secret:
        sys.exit("BULK_MASTER_PRIVATE_KEY is not set (put it in the environment, never in a file)")

    try:
        from bulk_keychain import Keypair, Signer
    except ImportError:
        sys.exit("bulk-keychain not installed: pip install bulk-keychain")

    net = args.network.lower()
    rest = (args.rest_url or DEFAULT_REST.get(net, DEFAULT_REST["mainnet"])).rstrip("/")

    master = Keypair.from_base58(master_secret)
    master_pub = str(master.pubkey)
    signer = None
    for dom in (net, net.capitalize(), net.upper()):
        try:
            signer = Signer(master, dom); break
        except Exception:
            continue
    if signer is None:
        sys.exit(f"bulk-keychain rejected signature domain '{net}'")

    agent_secret = None
    if args.new_agent:
        agent = Keypair()
        agent_pub = str(agent.pubkey)
        agent_secret = agent.to_base58()
    else:
        agent_pub = args.agent_pubkey.strip()

    action = "REVOKE" if args.revoke else "AUTHORIZE"
    print(f"{action} agent {agent_pub}\n  for master {master_pub}\n  on {net} ({rest})")

    signed = signer.sign_agent_wallet(agent_pub, bool(args.revoke))
    body = {"actions": signed["actions"], "nonce": signed["nonce"],
            "account": signed.get("account") or master_pub,
            "signer": signed.get("signer") or master_pub,
            "signature": signed["signature"]}
    r = requests.post(f"{rest}/order", json=body, timeout=15)
    print(f"\nHTTP {r.status_code}: {r.text[:400]}")
    ok = r.status_code < 400
    try:
        j = r.json()
        if isinstance(j, dict) and (j.get("error") or str(j.get("status", "")).lower() in ("error", "rejected")):
            ok = False
    except ValueError:
        pass
    if not ok:
        sys.exit("\nagent wallet action NOT accepted - nothing changed.")

    # best-effort read-back
    try:
        acct = requests.post(f"{rest}/account", json={"type": "fullAccount", "user": master_pub},
                             timeout=15).json()
        fa = acct[0]["fullAccount"] if isinstance(acct, list) else acct.get("fullAccount", acct)
        print("\nauthorizedAgentWallets now:", fa.get("authorizedAgentWallets"))
    except Exception as e:
        print(f"\n(read-back skipped: {e})")

    if args.revoke:
        print("\nDone. Remove BULK_PRIVATE_KEY for this agent from Railway.")
        return

    print("\n=== Set these on Railway (Variables) ===")
    print(f"BULK_NETWORK={net}")
    print(f"BULK_AGENT=1")
    print(f"BULK_ACCOUNT_ADDRESS={master_pub}")
    if agent_secret:
        print(f"BULK_PRIVATE_KEY={agent_secret}")
        print("\n!! The agent SECRET above is shown ONCE. Store it in Railway + a password manager.")
    else:
        print("BULK_PRIVATE_KEY=<the secret of agent " + agent_pub + ">")
    print("\nThe master key is NOT needed by the bot - keep it offline.")


if __name__ == "__main__":
    main()
