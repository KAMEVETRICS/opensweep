import os
import json
import asyncio
import discord
from discord.ext import commands
import opengradient as og
from dotenv import load_dotenv
from eth_account import Account

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OG_PRIVATE_KEY = os.getenv("OG_PRIVATE_KEY")

# Derive wallet address for proof link
WALLET_ADDRESS = None

# Initialize OpenGradient Client
if OG_PRIVATE_KEY:
    try:
        og_client = og.Client(private_key=OG_PRIVATE_KEY)
        acct = Account.from_key(OG_PRIVATE_KEY)
        WALLET_ADDRESS = acct.address
        print(f"[OK] OpenGradient Client Initialized (wallet: {WALLET_ADDRESS})", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to initialize OpenGradient Client: {e}", flush=True)
        og_client = None
else:
    print("[WARN] OG_PRIVATE_KEY not found. Bot will not moderate.", flush=True)
    og_client = None

# One-time Permit2 approval at startup
if og_client:
    try:
        og_client.llm.ensure_opg_approval(opg_amount=5.0)
        print("[OK] Permit2 approval confirmed", flush=True)
    except Exception as e:
        print(f"[WARN] Permit2 approval issue: {e}", flush=True)

# Initialize Discord Client
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[OK] Logged in as {bot.user}", flush=True)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if not og_client:
        await bot.process_commands(message)
        return

    # --- OpenGradient TEE Moderation ---
    try:
        system_prompt = (
            "You are a Content Moderation AI. Classify the user's message. "
            "If it contains hate speech, excessive profanity, threats, or scams, classify as 'unsafe'. "
            "Otherwise classify as 'safe'. "
            'Reply strictly in JSON: {"decision": "safe" or "unsafe", "reason": "short reason"}.'
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message.content}
        ]

        # Single TEE inference call with SETTLE_METADATA for full on-chain proof
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: og_client.llm.chat(
            model=og.TEE_LLM.GPT_4O,
            messages=messages,
            max_tokens=150,
            x402_settlement_mode=og.x402SettlementMode.SETTLE_METADATA
        ))

        # Parse result
        content = result.chat_output['content']
        payment_hash = result.payment_hash
        print(f"[MOD] {message.author}: {content} | payment_hash={payment_hash}", flush=True)

        # Parse JSON decision
        decision_start = content.find('{')
        decision_end = content.rfind('}') + 1
        if decision_start != -1 and decision_end > 0:
            decision_json = json.loads(content[decision_start:decision_end])
            decision = decision_json.get("decision", "safe").lower()
            reason = decision_json.get("reason", "No reason provided")
        else:
            decision = "unsafe" if "unsafe" in content.lower() else "safe"
            reason = "Automated AI Decision"

        if decision == "unsafe":
            # Delete the message (safely)
            try:
                await message.delete()
            except discord.NotFound:
                print("[WARN] Message already deleted", flush=True)
            except Exception as e:
                print(f"[WARN] Could not delete message: {e}", flush=True)

            # Build proof link
            # x402 OPG payments settle on Base Sepolia
            if payment_hash and payment_hash != "external":
                proof_url = f"https://sepolia.basescan.org/tx/{payment_hash}"
                proof_text = f"[View Settlement Tx]({proof_url})"
            elif WALLET_ADDRESS:
                proof_url = f"https://sepolia.basescan.org/address/{WALLET_ADDRESS}#tokentxns"
                proof_text = f"[View Wallet Settlements]({proof_url})"
            else:
                proof_url = "https://docs.opengradient.ai/developers/sdk/llm.html#tee-verification"
                proof_text = f"[Learn about TEE Verification]({proof_url})"

            embed = discord.Embed(
                title="⚠️ Message Removed",
                description=f"**Reason:** {reason}",
                color=0xFF0000
            )
            embed.add_field(name="Author", value=message.author.mention, inline=True)
            embed.add_field(
                name="Proof of Moderation",
                value=(
                    f"{proof_text}\n"
                    "Moderation ran inside a TEE and payment settled on Base Sepolia via [x402](https://docs.opengradient.ai/developers/x402/). "
                    "See the attached JSON file for the raw cryptographic output."
                ),
                inline=False
            )
            embed.set_footer(text="Powered by OpenGradient TEE | Verifiable AI")

            # Create a temporary JSON file with the raw TEE output
            tee_data = {
                "transaction_hash": result.transaction_hash,
                "finish_reason": result.finish_reason,
                "chat_output": result.chat_output,
                "completion_output": result.completion_output,
                "payment_hash": result.payment_hash,
                "model": "og.TEE_LLM.GPT_4O",
                "x402_settlement_mode": "SETTLE_METADATA"
            }
            import io
            json_file = discord.File(
                fp=io.BytesIO(json.dumps(tee_data, indent=2).encode('utf-8')),
                filename=f"tee_proof_{message.id}.json"
            )

            await message.channel.send(embed=embed, file=json_file)
            return

    except Exception as e:
        print(f"[ERROR] Moderation Error: {e}", flush=True)

    await bot.process_commands(message)

# Run Bot
if DISCORD_TOKEN is None:
    print("[ERROR] DISCORD_TOKEN not found in .env", flush=True)
else:
    bot.run(DISCORD_TOKEN)
