from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
import asyncio
import random

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, Float, String, select

# -------------------- Database setup --------------------
DATABASE_URL = "sqlite+aiosqlite:///./aviator.db"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    user_id = Column(Integer, primary_key=True)
    balance = Column(Float, default=0.0)

class ProcessedTransaction(Base):
    __tablename__ = "processed_transactions"
    id = Column(Integer, primary_key=True, index=True)
    tx_hash = Column(String, unique=True, index=True)
    user_id = Column(Integer)

# -------------------- FastAPI setup --------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Models --------------------
class Bet(BaseModel):
    user_id: int
    amount: float

class CashoutRequest(BaseModel):
    user_id: int

class BalanceTopUp(BaseModel):
    user_id: int
    amount: float

# -------------------- Game state --------------------
bets: Dict[int, Dict[str, float]] = {}
connections: Dict[int, WebSocket] = {}
current_multiplier = 1.0
crash_multiplier = 2.0
round_active = False
accepting_bets = False  # ✅ Жаңа флаг

# -------------------- API Endpoints --------------------
@app.get("/balance")
async def get_balance(user_id: int):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        return {"balance": round(user.balance, 2) if user else 0.0}

@app.post("/topup_balance")
async def topup_balance(data: BalanceTopUp):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == data.user_id))
        user = result.scalar_one_or_none()
        if user:
            user.balance += data.amount
        else:
            user = User(user_id=data.user_id, balance=data.amount)
            session.add(user)
        await session.commit()
        return {"message": "Баланс толықтырылды"}

@app.post("/place_bet")
async def place_bet(bet: Bet):
    if not accepting_bets:
        return {"error": "Қазір ставка қабылданбайды"}

    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == bet.user_id))
        user = result.scalar_one_or_none()
        if not user or user.balance < bet.amount:
            return {"error": "Жеткілікті баланс жоқ"}
        user.balance -= bet.amount
        bets[bet.user_id] = {"amount": bet.amount, "auto_cashout": None}
        await session.commit()
        return {"message": "Ставка қабылданды"}

@app.post("/cashout")
async def cashout(data: CashoutRequest):
    if data.user_id in bets:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.user_id == data.user_id))
            user = result.scalar_one_or_none()
            if not user:
                return {"error": "Пайдаланушы табылмады"}
            win = bets[data.user_id]["amount"] * current_multiplier
            user.balance += win
            del bets[data.user_id]
            await session.commit()
            return {"message": f"Кэшаут сәтті! Ұтыс: {round(win, 2)}"}
    return {"error": "Ставка табылмады"}

@app.post("/check")
async def check_transactions():
    import httpx
    TONAPI_KEY = "TONAPI_КІЛТ"  # ❗ Өз .env не нақты API кілтіңді енгіз
    target_address = "UQANOGD2MmdOBjbi2Wy6jGFFAL33ciUwV0X3hXEfdw5OugRm"

    async with async_session() as session:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://tonapi.io/v2/blockchain/accounts/{target_address}/transactions",
                headers={"Authorization": f"Bearer {TONAPI_KEY}"}
            )
            txs = response.json().get("transactions", [])
            for tx in txs:
                tx_hash = tx["hash"]
                msg = tx.get("in_msg")
                if not msg or not msg.get("comment"):
                    continue

                comment = msg["comment"]
                if not comment.startswith("user_"):
                    continue

                try:
                    user_id = int(comment.replace("user_", ""))
                except:
                    continue

                amount = int(msg.get("value", 0)) / 1e9  # nanoTON → TON

                # Транзакция бұрын тіркелген бе?
                result = await session.execute(select(ProcessedTransaction).where(ProcessedTransaction.tx_hash == tx_hash))
                already_exists = result.scalar_one_or_none()
                if already_exists:
                    continue

                # Баланс қосу
                result = await session.execute(select(User).where(User.user_id == user_id))
                user = result.scalar_one_or_none()
                if user:
                    user.balance += amount
                else:
                    user = User(user_id=user_id, balance=amount)
                    session.add(user)

                # Транзакцияны базаға жазу
                session.add(ProcessedTransaction(tx_hash=tx_hash, user_id=user_id))
                await session.commit()

    return {"message": "Төлемдер тексерілді"}

# -------------------- WebSocket --------------------
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    connections[user_id] = websocket
    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        del connections[user_id]

# -------------------- Game Loop --------------------
async def round_loop():
    global current_multiplier, crash_multiplier, round_active, accepting_bets
    while True:
        # 1. Таймер уақыты: 3 секунд — ставка қабылдау уақыты
        accepting_bets = True
        await broadcast({"event": "waiting", "message": "Ставкалар қабылдануда"})
        await asyncio.sleep(3)
        accepting_bets = False

        # 2. Ұшу уақыты
        round_active = True
        current_multiplier = 1.0
        crash_multiplier = round(random.uniform(1.5, 3.0), 2)
        await broadcast({"event": "start", "crash_at": crash_multiplier})

        while current_multiplier < crash_multiplier:
            await asyncio.sleep(0.1)
            current_multiplier = round(current_multiplier + 0.01, 2)
            await broadcast({"event": "update", "multiplier": current_multiplier})

        # 3. Ұшақ құлады
        round_active = False
        await broadcast({"event": "crash", "at": crash_multiplier})
        bets.clear()
        await asyncio.sleep(2)

# -------------------- Broadcast --------------------
async def broadcast(message: dict):
    for ws in connections.values():
        try:
            await ws.send_json(message)
        except:
            pass

# -------------------- Startup --------------------
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(round_loop())
