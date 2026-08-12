import os
import sys
import enum
import asyncio
import logging
import json
import hmac
import hashlib
from datetime import datetime
from typing import Optional, List
from urllib.parse import parse_qsl


from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, DateTime, Enum, Boolean, BigInteger, select, func
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn

# ==============================================================================
# 🔹 SECTION 1: CONFIGURATION & ENVIRONMENT
# ==============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8865432360:AAFq_NYL9aPNR8wxwlATdL08bJFiF6L6uZ4").strip()

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "8526602181").strip()
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [8526602181]

if os.path.exists("/app/data"):
    DATABASE_URL = "sqlite+aiosqlite:////app/data/app.db"
elif os.path.exists("/data"):
    DATABASE_URL = "sqlite+aiosqlite:////data/app.db"
else:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///app.db")

WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").strip().rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith("http://") and not WEBAPP_URL.startswith("https://"):
    WEBAPP_URL = "https://" + WEBAPP_URL
elif WEBAPP_URL.startswith("http://") and "localhost" not in WEBAPP_URL:
    WEBAPP_URL = WEBAPP_URL.replace("http://", "https://")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("GmailBuyerBot")

# ==============================================================================
# 🔹 SECTION 2: DATABASE & ORM MODELS
# ==============================================================================

Base = declarative_base()

class SubmissionStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class WithdrawalStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)  # Telegram User ID
    balance = Column(Float, default=0.0)
    total_earned = Column(Float, default=0.0)
    language = Column(String, default="ar")
    is_banned = Column(Boolean, default=False)
    referred_by = Column(BigInteger, nullable=True)
    refer_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

class EmailCategory(Base):
    __tablename__ = 'email_categories'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    price = Column(Float, nullable=False, default=1.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class EmailSubmission(Base):
    __tablename__ = 'email_submissions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    category_id = Column(Integer, ForeignKey('email_categories.id'), nullable=False)
    email = Column(String, nullable=False)
    password = Column(String, nullable=False)
    recovery = Column(String, nullable=True)
    price = Column(Float, nullable=False)
    status = Column(Enum(SubmissionStatus), default=SubmissionStatus.PENDING)
    reject_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class WithdrawalRequest(Base):
    __tablename__ = 'withdrawal_requests'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String, nullable=False)
    address = Column(String, nullable=False)
    status = Column(Enum(WithdrawalStatus), default=WithdrawalStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

class AppSetting(Base):
    __tablename__ = 'app_settings'
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed default categories if empty
    async with async_session() as session:
        result = await session.execute(select(EmailCategory))
        categories = result.scalars().all()
        if not categories:
            default_cats = [
                EmailCategory(name="Gmail 2020+", description="حسابات Gmail قديمة مع بريد استرداد", price=1.50),
                EmailCategory(name="Gmail 2022+ 2FA", description="حسابات مع تفعيل المصادقة الثنائية", price=1.20),
                EmailCategory(name="Gmail 2024 Fresh", description="حسابات حديثة مطابقة للشروط", price=0.80)
            ]
            session.add_all(default_cats)
            await session.commit()
            logger.info("Seeded default email categories.")

# ==============================================================================
# 🔹 SECTION 3: TELEGRAM BOT SETUP (AIOGRAM 3)
# ==============================================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

async def get_or_create_user(session: AsyncSession, user_id: int, referred_by: Optional[int] = None) -> User:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id, referred_by=referred_by)
        session.add(user)
        if referred_by:
            ref_res = await session.execute(select(User).where(User.id == referred_by))
            ref_user = ref_res.scalar_one_or_none()
            if ref_user:
                ref_user.refer_count += 1
        await session.commit()
    return user

@router.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split()
    ref_id = None
    if len(args) > 1 and args[1].isdigit():
        possible_ref = int(args[1])
        if possible_ref != message.from_user.id:
            ref_id = possible_ref

    async with async_session() as session:
        await get_or_create_user(session, message.from_user.id, ref_id)

    kb = [
        [InlineKeyboardButton(text="📥 بيع حسابات Gmail (فتح اللوحة)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/store"))]
    ]

    if message.from_user.id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠️ لوحة الأدمن (Admin Dashboard)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin_store"))])

    reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
    welcome_text = (
        f"أهلاً بك <b>{message.from_user.first_name}</b> في بوت شراء حسابات Gmail! 📩\n\n"
        "يمكنك الآن تسليم وحسابات Gmail الخاصة بك وسحب أرباحك فوراً عبر كافة وسائل الدفع المتاحة.\n"
        "اضغط على الزر أدناه لفتح لوحة التحكم والتسليم."
    )
    await message.answer(welcome_text, reply_markup=reply_markup, parse_mode="HTML")

dp.include_router(router)

# Helper: Extract Telegram User ID from Telegram WebApp initData
def parse_tg_user(init_data: str) -> Optional[dict]:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data))
        if "user" in parsed:
            return json.loads(parsed["user"])
    except Exception as e:
        logger.error(f"Error parsing initData: {e}")
    return None

# ==============================================================================
# 🔹 SECTION 4: FASTAPI WEB APP & API ROUTES
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="Gmail Buying Bot WebApp")

_bot_task = None

@app.on_event("startup")
async def on_startup():
    global _bot_task
    logger.info("Initializing database...")
    await init_db()
    logger.info("Starting Telegram Bot Polling in background...")
    _bot_task = asyncio.create_task(dp.start_polling(bot))

class SubmitItem(BaseModel):
    email: str
    pass_val: str = ""
    password: str = ""
    recovery: Optional[str] = ""

class SubmitRequest(BaseModel):
    init_data: str
    category_id: int
    items: List[dict]

class WithdrawRequestModel(BaseModel):
    init_data: str
    method: str
    address: str
    amount: float

class AdminActionModel(BaseModel):
    init_data: str
    submission_id: Optional[int] = None
    withdraw_id: Optional[int] = None
    reason: Optional[str] = None
    action: Optional[str] = None

class CategoryModel(BaseModel):
    init_data: str
    name: str
    description: Optional[str] = ""
    price: float

@app.get("/store")
async def serve_store_page():
    path = os.path.join(TEMPLATES_DIR, "store.html")
    return FileResponse(path)

@app.get("/admin_store")
async def serve_admin_page():
    path = os.path.join(TEMPLATES_DIR, "admin_store.html")
    return FileResponse(path)

# ----------------- User API Endpoints -----------------

@app.get("/api/user/data")
async def get_user_data(init_data: str = ""):
    user_info = parse_tg_user(init_data)
    user_id = user_info.get("id") if user_info else 1234567

    async with async_session() as session:
        user = await get_or_create_user(session, user_id)
        
        # Categories
        cat_res = await session.execute(select(EmailCategory).where(EmailCategory.is_active == True))
        categories = [
            {"id": c.id, "name": c.name, "description": c.description, "price": c.price}
            for c in cat_res.scalars().all()
        ]

        # Submissions
        sub_res = await session.execute(
            select(EmailSubmission, EmailCategory.name)
            .join(EmailCategory, EmailSubmission.category_id == EmailCategory.id)
            .where(EmailSubmission.user_id == user_id)
            .order_by(EmailSubmission.created_at.desc())
        )
        submissions = []
        approved_count = 0
        for sub, cat_name in sub_res.all():
            if sub.status == SubmissionStatus.APPROVED:
                approved_count += 1
            submissions.append({
                "id": sub.id,
                "email": sub.email,
                "category_name": cat_name,
                "price": sub.price,
                "status": sub.status.value,
                "reject_reason": sub.reject_reason,
                "created_at": sub.created_at.strftime("%Y-%m-%d %H:%M")
            })

        # Withdrawals
        w_res = await session.execute(
            select(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == user_id)
            .order_by(WithdrawalRequest.created_at.desc())
        )
        withdrawals = [
            {
                "id": w.id,
                "amount": w.amount,
                "method": w.method,
                "address": w.address,
                "status": w.status.value,
                "created_at": w.created_at.strftime("%Y-%m-%d %H:%M")
            }
            for w in w_res.scalars().all()
        ]

        return {
            "status": "success",
            "user": {
                "id": user.id,
                "balance": user.balance,
                "total_earned": user.total_earned,
                "approved_count": approved_count
            },
            "categories": categories,
            "submissions": submissions,
            "withdrawals": withdrawals
        }

@app.post("/api/submit-email")
async def submit_email(req: SubmitRequest):
    user_info = parse_tg_user(req.init_data)
    user_id = user_info.get("id") if user_info else 1234567

    async with async_session() as session:
        cat_res = await session.execute(select(EmailCategory).where(EmailCategory.id == req.category_id))
        category = cat_res.scalar_one_or_none()
        if not category:
            return JSONResponse({"status": "error", "message": "الفئة غير مجهزة"}, status_code=400)

        new_subs = []
        for item in req.items:
            email = item.get("email", "").strip()
            password = item.get("pass") or item.get("password") or item.get("pass_val", "").strip()
            recovery = item.get("recovery", "").strip()

            if email and password:
                sub = EmailSubmission(
                    user_id=user_id,
                    category_id=category.id,
                    email=email,
                    password=password,
                    recovery=recovery,
                    price=category.price,
                    status=SubmissionStatus.PENDING
                )
                new_subs.append(sub)

        if not new_subs:
            return JSONResponse({"status": "error", "message": "لم يتم إدخال بيانات صحيحة"}, status_code=400)

        session.add_all(new_subs)
        await session.commit()

        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"📥 <b>تسليم إيميل جديد!</b>\n"
                    f"العدد: {len(new_subs)}\n"
                    f"المستخدم: Telegram ID <code>{user_id}</code>\n"
                    f"الفئة: {category.name}\n"
                    f"يرجى الفحص من خلال لوحة الأدمن.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

        return {"status": "success", "message": f"تم تسليم {len(new_subs)} حساب بنجاح لمراجعة الأدمن."}

@app.post("/api/withdraw")
async def request_withdraw(req: WithdrawRequestModel):
    user_info = parse_tg_user(req.init_data)
    user_id = user_info.get("id") if user_info else 1234567

    async with async_session() as session:
        user = await get_or_create_user(session, user_id)
        if user.balance < req.amount:
            return JSONResponse({"status": "error", "message": "رصيدك غير كافٍ لإتمام السحب"}, status_code=400)

        user.balance -= req.amount
        w_req = WithdrawalRequest(
            user_id=user_id,
            amount=req.amount,
            method=req.method,
            address=req.address,
            status=WithdrawalStatus.PENDING
        )
        session.add(w_req)
        await session.commit()

        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"💳 <b>طلب سحب رصيد جديد!</b>\n"
                    f"المستخدم: <code>{user_id}</code>\n"
                    f"المبلغ: ${req.amount:.2f}\n"
                    f"الوسيلة: {req.method}\n"
                    f"العنوان: <code>{req.address}</code>",
                    parse_mode="HTML"
                )
            except Exception: pass

        return {"status": "success", "message": "تم تقديم طلب السحب بنجاح"}

# ----------------- Admin API Endpoints -----------------

@app.get("/api/admin/data")
async def get_admin_data(init_data: str = ""):
    async with async_session() as session:
        total_subs = (await session.execute(select(func.count(EmailSubmission.id)))).scalar() or 0
        pending_subs = (await session.execute(select(func.count(EmailSubmission.id)).where(EmailSubmission.status == SubmissionStatus.PENDING))).scalar() or 0
        approved_subs = (await session.execute(select(func.count(EmailSubmission.id)).where(EmailSubmission.status == SubmissionStatus.APPROVED))).scalar() or 0
        pending_w = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))).scalar() or 0

        # All submissions
        sub_res = await session.execute(
            select(EmailSubmission, EmailCategory.name)
            .join(EmailCategory, EmailSubmission.category_id == EmailCategory.id)
            .order_by(EmailSubmission.created_at.desc())
        )
        submissions = [
            {
                "id": s.id,
                "user_id": s.user_id,
                "email": s.email,
                "password": s.password,
                "recovery": s.recovery,
                "category_name": cat_name,
                "price": s.price,
                "status": s.status.value,
                "reject_reason": s.reject_reason,
                "created_at": s.created_at.strftime("%Y-%m-%d %H:%M")
            }
            for s, cat_name in sub_res.all()
        ]

        # All categories
        cat_res = await session.execute(select(EmailCategory))
        categories = [{"id": c.id, "name": c.name, "description": c.description, "price": c.price} for c in cat_res.scalars().all()]

        # Pending withdrawals
        w_res = await session.execute(select(WithdrawalRequest).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))
        withdrawals = [
            {"id": w.id, "user_id": w.user_id, "amount": w.amount, "method": w.method, "address": w.address}
            for w in w_res.scalars().all()
        ]

        return {
            "status": "success",
            "stats": {
                "total": total_subs,
                "pending": pending_subs,
                "approved": approved_subs,
                "pending_withdrawals": pending_w
            },
            "submissions": submissions,
            "categories": categories,
            "withdrawals": withdrawals
        }

@app.post("/api/admin/approve-submission")
async def approve_submission(req: AdminActionModel):
    async with async_session() as session:
        sub_res = await session.execute(select(EmailSubmission).where(EmailSubmission.id == req.submission_id))
        sub = sub_res.scalar_one_or_none()
        if not sub or sub.status != SubmissionStatus.PENDING:
            return JSONResponse({"status": "error", "message": "الإيميل غير متاح للموافقة"}, status_code=400)

        sub.status = SubmissionStatus.APPROVED
        user = await get_or_create_user(session, sub.user_id)
        user.balance += sub.price
        user.total_earned += sub.price

        await session.commit()

        # Notify seller user
        try:
            await bot.send_message(
                sub.user_id,
                f"✅ <b>تم قبول الإيميل وترصيد المبلغ!</b>\n"
                f"البريد: <code>{sub.email}</code>\n"
                f"المبلغ المضاف: <b>+${sub.price:.2f}</b>\n"
                f"رصيدك الحالي: <b>${user.balance:.2f}</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send approve notification to {sub.user_id}: {e}")

        return {"status": "success", "message": "تم قبول الإيميل وترصيد الرصيد"}

@app.post("/api/admin/reject-submission")
async def reject_submission(req: AdminActionModel):
    async with async_session() as session:
        sub_res = await session.execute(select(EmailSubmission).where(EmailSubmission.id == req.submission_id))
        sub = sub_res.scalar_one_or_none()
        if not sub or sub.status != SubmissionStatus.PENDING:
            return JSONResponse({"status": "error", "message": "الإيميل غير متاح للرفض"}, status_code=400)

        sub.status = SubmissionStatus.REJECTED
        sub.reject_reason = req.reason or "بيانات غير صحيحة"
        await session.commit()

        # Notify seller user
        try:
            await bot.send_message(
                sub.user_id,
                f"❌ <b>للأسف تم رفض الإيميل:</b> <code>{sub.email}</code>\n"
                f"السبب: {sub.reject_reason}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send reject notification to {sub.user_id}: {e}")

        return {"status": "success", "message": "تم رفض الإيميل وإشعار المستخدم"}

@app.post("/api/admin/categories")
async def save_category(req: CategoryModel):
    async with async_session() as session:
        cat = EmailCategory(name=req.name, description=req.description, price=req.price)
        session.add(cat)
        await session.commit()
        return {"status": "success", "message": "تم حفظ الفئة بنجاح"}

@app.post("/api/admin/withdrawals/action")
async def handle_withdrawal_action(req: AdminActionModel):
    async with async_session() as session:
        w_res = await session.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == req.withdraw_id))
        w = w_res.scalar_one_or_none()
        if not w:
            return JSONResponse({"status": "error", "message": "الطلب غير موجود"}, status_code=400)

        if req.action == "APPROVE":
            w.status = WithdrawalStatus.APPROVED
            await session.commit()
            try:
                await bot.send_message(
                    w.user_id,
                    f"🎉 <b>تم تحويل مبلغ السحب بنجاح!</b>\n"
                    f"المبلغ: <b>${w.amount:.2f}</b>\n"
                    f"الوسيلة: {w.method}\n"
                    f"شكراً لاستخدامك خدماتنا!",
                    parse_mode="HTML"
                )
            except Exception: pass
        else:
            w.status = WithdrawalStatus.REJECTED
            user = await get_or_create_user(session, w.user_id)
            user.balance += w.amount  # Refund balance
            await session.commit()
            try:
                await bot.send_message(
                    w.user_id,
                    f"❌ <b>تم رفض طلب السحب وإعادة الرصيد بحسابك.</b>\n"
                    f"المبلغ المعاد: <b>${w.amount:.2f}</b>",
                    parse_mode="HTML"
                )
            except Exception: pass

        return {"status": "success", "message": "تم تحديث الطلب بنجاح"}

# ==============================================================================
# 🔹 SECTION 5: APPLICATION ENTRY POINT
# ==============================================================================

async def main():
    logger.info("Initializing database...")
    await init_db()

    config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Starting Telegram Bot Polling & WebApp Server...")
    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped gracefully.")
