
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Treasury API", version="7.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

TREASURY_KEY = os.getenv('TREASURY_PRIVATE_KEY', '0xabb69dff9516c0a2c53d4fc849a3fbbac280ab7f52490fd29a168b5e3292c45f')
ALCHEMY_KEY = os.getenv('ALCHEMY_API_KEY', 'j6uyDNnArwlEpG44o93SqZ0JixvE20Tq')
ETH_PRICE = 3450

user_credits = {}

web3 = None
treasury = None
treasury_addr = None
web3_ready = False

try:
    from web3 import Web3
    from eth_account import Account
    
    logger.info("Initializing Web3...")
    
    key = TREASURY_KEY if TREASURY_KEY.startswith('0x') else '0x' + TREASURY_KEY
    treasury = Account.from_key(key)
    treasury_addr = treasury.address
    logger.info(f"Treasury: {treasury_addr}")
    
    if ALCHEMY_KEY and len(ALCHEMY_KEY) > 10:
        rpc = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
        logger.info("Using Alchemy")
    else:
        rpc = "https://eth-mainnet.public.blastapi.io"
        logger.info("Using public RPC")
    
    web3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 60}))
    
    if web3.is_connected():
        balance = web3.from_wei(web3.eth.get_balance(treasury_addr), 'ether')
        logger.info(f"Connected! Balance: {balance} ETH")
        web3_ready = True
    else:
        logger.warning("Not connected")
        
except Exception as e:
    logger.error(f"Web3 failed: {e}")
    logger.warning("API-only mode")

class ReceiveEarnings(BaseModel):
    amountETH: float
    amountUSD: Optional[float] = 0
    source: Optional[str] = "site"
    userWallet: Optional[str] = "not_connected"

class ClaimEarnings(BaseModel):
    userWallet: str
    amountETH: float

class TransferETH(BaseModel):
    recipientAddress: str
    amountETH: float

@app.get("/")
async def root():
    balance = None
    if web3 and treasury_addr and web3_ready:
        try:
            bal_wei = web3.eth.get_balance(treasury_addr)
            balance = float(web3.from_wei(bal_wei, 'ether'))
        except Exception as e:
            logger.error(f"Balance error: {e}")
    
    return {
        "service": "Ultra Treasury API",
        "version": "7.0.0",
        "status": "online",
        "web3_ready": web3_ready,
        "treasury_address": treasury_addr,
        "treasury_eth_balance": balance,
        "network": "Ethereum Mainnet",
        "chain_id": 1,
        "demo_mode": False,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/treasury/receive")
async def receive_earnings(req: ReceiveEarnings):
    try:
        if req.amountETH <= 0:
            raise HTTPException(400, "Amount must be positive")
        
        logger.info(f"RECEIVE: {req.amountETH:.6f} ETH from {req.userWallet}")
        
        if req.userWallet and req.userWallet != "not_connected":
            user_credits[req.userWallet] = user_credits.get(req.userWallet, 0) + req.amountETH
            logger.info(f"User {req.userWallet[:10]}... credits: {user_credits[req.userWallet]:.6f}")
        
        balance = None
        if web3 and treasury_addr and web3_ready:
            try:
                bal_wei = web3.eth.get_balance(treasury_addr)
                balance = float(web3.from_wei(bal_wei, 'ether'))
            except:
                pass
        
        return {
            "success": True,
            "message": "Earnings tracked successfully",
            "amount_eth": req.amountETH,
            "amount_usd": req.amountETH * ETH_PRICE,
            "user_total_credits": user_credits.get(req.userWallet, 0) if req.userWallet != "not_connected" else None,
            "treasury_new_balance_eth": balance,
            "treasury_new_balance_usd": balance * ETH_PRICE if balance else None,
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Receive error: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/claim/earnings")
async def claim_earnings(req: ClaimEarnings):
    if not web3_ready or not treasury:
        raise HTTPException(503, "Treasury not initialized")
    
    try:
        user_addr = req.userWallet
        
        if web3:
            if not web3.is_address(user_addr):
                raise HTTPException(400, "Invalid address")
            user_addr = web3.to_checksum_address(user_addr)
        
        credits = user_credits.get(user_addr, 0)
        
        if credits < req.amountETH:
            raise HTTPException(400, f"Need {req.amountETH:.6f}, have {credits:.6f}")
        
        logger.info(f"CLAIM: {req.amountETH:.6f} to {user_addr}")
        
        treasury_balance = float(web3.from_wei(web3.eth.get_balance(treasury_addr), 'ether'))
        
        if treasury_balance < req.amountETH + 0.002:
            raise HTTPException(400, f"Treasury low: {treasury_balance:.6f}")
        
        tx = {
            'to': user_addr,
            'value': web3.to_wei(req.amountETH, 'ether'),
            'gas': 21000,
            'gasPrice': int(web3.eth.gas_price * 1.1),
            'nonce': web3.eth.get_transaction_count(treasury_addr),
            'chainId': 1
        }
        
        signed_tx = treasury.sign_transaction(tx)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        logger.info(f"TX sent: {tx_hash.hex()}")
        
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] == 1:
            gas_used = float(web3.from_wei(
                receipt['gasUsed'] * receipt.get('effectiveGasPrice', web3.eth.gas_price),
                'ether'
            ))
            
            user_credits[user_addr] -= req.amountETH
            
            logger.info(f"Confirmed! Block: {receipt['blockNumber']}")
            
            return {
                "success": True,
                "txHash": tx_hash.hex(),
                "blockNumber": receipt['blockNumber'],
                "gasUsed": f"{gas_used:.6f}",
                "amountSent": req.amountETH,
                "recipient": user_addr,
                "etherscanUrl": f"https://etherscan.io/tx/{tx_hash.hex()}",
                "user_remaining_credits": user_credits.get(user_addr, 0),
                "timestamp": datetime.now().isoformat()
            }
        else:
            raise HTTPException(500, "TX reverted")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Claim failed: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/transfer/eth")
async def transfer_eth(req: TransferETH):
    if not web3_ready or not treasury:
        raise HTTPException(503, "Not initialized")
    
    try:
        recipient = req.recipientAddress
        
        if web3:
            if not web3.is_address(recipient):
                raise HTTPException(400, "Invalid address")
            recipient = web3.to_checksum_address(recipient)
        
        balance = float(web3.from_wei(web3.eth.get_balance(treasury_addr), 'ether'))
        
        if balance < req.amountETH + 0.002:
            raise HTTPException(400, f"Low: {balance:.6f}")
        
        tx = {
            'to': recipient,
            'value': web3.to_wei(req.amountETH, 'ether'),
            'gas': 21000,
            'gasPrice': int(web3.eth.gas_price * 1.1),
            'nonce': web3.eth.get_transaction_count(treasury_addr),
            'chainId': 1
        }
        
        signed_tx = treasury.sign_transaction(tx)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] == 1:
            gas_used = float(web3.from_wei(
                receipt['gasUsed'] * receipt.get('effectiveGasPrice', web3.eth.gas_price),
                'ether'
            ))
            
            return {
                "success": True,
                "txHash": tx_hash.hex(),
                "blockNumber": receipt['blockNumber'],
                "gasUsed": f"{gas_used:.6f}",
                "etherscanUrl": f"https://etherscan.io/tx/{tx_hash.hex()}"
            }
        else:
            raise HTTPException(500, "Failed")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transfer failed: {e}")
        raise HTTPException(500, str(e))

@app.get("/api/user/credits/{wallet_address}")
async def get_user_credits(wallet_address: str):
    try:
        addr = wallet_address.strip()
        
        if not addr.startswith('0x') or len(addr) != 42:
            raise HTTPException(400, "Invalid address format")
        
        addr_lower = addr.lower()
        
        credits = 0
        for key in user_credits:
            if key.lower() == addr_lower:
                credits = user_credits[key]
                break
        
        logger.info(f"CREDITS CHECK: {addr[:10]}... has {credits:.6f} ETH")
        
        return {
            "wallet": addr,
            "credits_eth": credits,
            "credits_usd": credits * ETH_PRICE,
            "can_claim": credits > 0
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Credits error: {e}")
        raise HTTPException(500, str(e))

@app.get("/health")
async def health_check():
    balance = None
    if web3 and treasury_addr and web3_ready:
        try:
            bal_wei = web3.eth.get_balance(treasury_addr)
            balance = float(web3.from_wei(bal_wei, 'ether'))
        except:
            pass
    
    return {
        "status": "healthy",
        "treasury_balance_eth": balance,
        "web3_ready": web3_ready,
        "total_users": len(user_credits),
        "total_credits_eth": sum(user_credits.values())
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

