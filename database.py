from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from config import MONGO_URI, DATABASE_NAME

client = AsyncIOMotorClient(MONGO_URI)
db = client[DATABASE_NAME]

tiers_col = db["tiers"]
categories_col = db["categories"]
accounts_col = db["accounts"]
users_col = db["users"]
payments_col = db["payments"]
counters_col = db["counters"]
supplier_col = db["supplier_config"]
gp_requests_col = db["gp_requests"]
gp_codes_col = db["gp_codes"]
deposit_requests_col = db["deposit_requests"]
otp_logs_col = db["otp_logs"]
joined_channels_col = db["joined_channels"]
admins_col = db["admins"]
settings_col = db["settings"]
ledger_col = db["ledger"]
audit_col = db["audit_log"]
deposit_proofs_col = db["deposit_proofs"]


async def get_next_sequence(name: str) -> int:
    counter = await counters_col.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["seq"]


async def init_db():
    try:
        await categories_col.drop_index("name_1")
    except Exception:
        pass
    await tiers_col.create_index("name", unique=True)
    await categories_col.create_index([("name", 1), ("tier_id", 1)], unique=True)
    await accounts_col.create_index("account_id", unique=True)
    await users_col.create_index("user_id", unique=True)
    await admins_col.create_index("user_id", unique=True)
    await ledger_col.create_index([("user_id", 1), ("ts", -1)])
    await audit_col.create_index([("ts", -1)])

    # Seed the default types if they don't exist yet (admin can add more later)
    default_tier_names = ["Fresh", "Old", "Spam"]
    tier_ids = {}
    for name in default_tier_names:
        existing = await tiers_col.find_one({"name": name})
        if existing:
            tier_ids[name] = existing["tier_id"]
        else:
            tid = await get_next_sequence("tier_id")
            await tiers_col.insert_one({"tier_id": tid, "name": name})
            tier_ids[name] = tid

    # Migrate any pre-existing categories into tier_id form:
    # - categories with the old string "tier" field (fresh/old/spam) -> matching tier_id
    # - categories with neither "tier" nor "tier_id" -> default to "Fresh"
    legacy_map = {"fresh": tier_ids["Fresh"], "old": tier_ids["Old"], "spam": tier_ids["Spam"]}
    async for cat in categories_col.find({"tier_id": {"$exists": False}}):
        new_tier_id = legacy_map.get(cat.get("tier"), tier_ids["Fresh"])
        await categories_col.update_one(
            {"_id": cat["_id"]},
            {"$set": {"tier_id": new_tier_id}, "$unset": {"tier": ""}}
        )


_USER_DEFAULTS = {
    "balance": 0.0,
    "is_verified": 0,
    "is_banned": 0,
    "referred_by": None,
    "referral_count": 0,
    "referral_rewarded": 0,
    "onboarded": 0,
}


async def get_user(user_id: int):
    try:
        user = await users_col.find_one_and_update(
            {"user_id": user_id},
            {"$setOnInsert": {**_USER_DEFAULTS, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        user = await users_col.find_one({"user_id": user_id})

    # Old/partial docs (created by /dd, /ss or deposit-approve upserts)
    # miss some fields -> fill them in so /start never hits a KeyError.
    missing = {k: v for k, v in _USER_DEFAULTS.items() if k not in user}
    if missing:
        await users_col.update_one(
            {"user_id": user_id, **{k: {"$exists": False} for k in missing}},
            {"$set": missing},
        )
        user.update(missing)
    return user


async def mark_onboarded(user_id: int):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"onboarded": 1}},
        upsert=True,
    )


async def update_verification(user_id: int, status: int):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_verified": status}},
        upsert=True,
    )


async def add_ledger(user_id: int, delta: float, reason: str, ref=None, by=None, balance_after=None):
    """One row per balance change: what, why, who did it, balance afterwards."""
    await ledger_col.insert_one({
        "user_id": user_id,
        "delta": delta,
        "reason": reason,
        "ref": ref,
        "by": by,
        "balance_after": balance_after,
        "ts": datetime.now(timezone.utc),
    })


async def update_balance(user_id: int, amount: float, reason: str = "manual", ref=None, by=None):
    doc = await users_col.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"balance": amount}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    try:
        await add_ledger(user_id, amount, reason, ref=ref, by=by, balance_after=(doc or {}).get("balance"))
    except Exception as e:
        print(f"Ledger write failed (balance already updated): {e}")


async def get_user_ledger(user_id: int, limit: int = 15):
    cursor = ledger_col.find({"user_id": user_id}).sort("ts", -1).limit(limit)
    return await cursor.to_list(length=limit)


# ---------------- Audit log (admin activity) ----------------
async def add_audit(admin_id: int, admin_name: str, admin_username, action: str, fields: list):
    await audit_col.insert_one({
        "admin_id": admin_id,
        "admin_name": admin_name,
        "admin_username": admin_username,
        "action": action,
        "fields": [[str(l), str(v)] for l, v in fields],
        "ts": datetime.now(timezone.utc),
    })


async def get_audit(limit: int = 15):
    cursor = audit_col.find({}).sort("ts", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def save_payment_record(user_id: int, amount: float, currency: str, status: str):
    await payments_col.insert_one({
        "user_id": user_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "timestamp": datetime.now(timezone.utc),
    })


async def save_star_payment(charge_id: str, user_id: int, stars: int, inr: float) -> bool:
    """Returns False if this charge_id was already processed (duplicate)."""
    try:
        await payments_col.insert_one({
            "_id": charge_id,
            "user_id": user_id,
            "amount": inr,
            "stars": stars,
            "currency": "STARS",
            "status": "TOPUP_SUCCESS",
            "timestamp": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


async def get_user_payments(user_id: int):
    cursor = payments_col.find({"user_id": user_id}).sort("timestamp", -1)
    return await cursor.to_list(length=50)


async def get_tiers():
    cursor = tiers_col.find({})
    return [(doc["tier_id"], doc["name"]) async for doc in cursor]


async def add_tier(name: str):
    """Returns the tier_id, creating it if it doesn't already exist."""
    existing = await tiers_col.find_one({"name": name})
    if existing:
        return existing["tier_id"]
    tid = await get_next_sequence("tier_id")
    try:
        await tiers_col.insert_one({"tier_id": tid, "name": name})
    except DuplicateKeyError:
        existing = await tiers_col.find_one({"name": name})
        return existing["tier_id"] if existing else None
    return tid


async def get_tier_by_id(tier_id: int):
    return await tiers_col.find_one({"tier_id": tier_id})


async def get_categories(tier_id: int = None):
    query = {"tier_id": tier_id} if tier_id is not None else {}
    cursor = categories_col.find(query)
    return [(doc["category_id"], doc["name"]) async for doc in cursor]


async def get_categories_with_counts(tier_id: int = None):
    query = {"tier_id": tier_id} if tier_id is not None else {}
    cursor = categories_col.find(query)
    categories = await cursor.to_list(length=None)
    if not categories:
        return []

    cat_ids = [cat["category_id"] for cat in categories]

    # one aggregation instead of one count_documents() call per category
    pipeline = [
        {"$match": {"category_id": {"$in": cat_ids}, "is_sold": 0, "session_dead": {"$ne": True}}},
        {"$group": {"_id": "$category_id", "count": {"$sum": 1}}},
    ]
    counts = {}
    async for row in accounts_col.aggregate(pipeline):
        counts[row["_id"]] = row["count"]

    return [(cat["category_id"], cat["name"], counts.get(cat["category_id"], 0)) for cat in categories]


async def get_category_by_id(cat_id: int):
    return await categories_col.find_one({"category_id": cat_id})


async def update_category_name(cat_id: int, new_name: str):
    await categories_col.update_one(
        {"category_id": cat_id},
        {"$set": {"name": new_name}}
    )


async def set_user_ban_status(user_id: int, status: int):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": status}},
        upsert=True
    )


async def get_detailed_stock_stats():
    available_accounts = await accounts_col.count_documents({"is_sold": 0, "session_dead": {"$ne": True}})
    sold_accounts = await accounts_col.count_documents({"is_sold": 1})
    total_users = await users_col.count_documents({})
    total_revenue = 0
    async for p in payments_col.find({"status": "SPEND_BUY_ACCOUNT"}):
        total_revenue += p.get("amount", 0)
    return {
        "available": available_accounts,
        "sold": sold_accounts,
        "users": total_users,
        "revenue": total_revenue
    }


async def get_all_sales_history(limit: int = 10):
    cursor = payments_col.find({"status": "SPEND_BUY_ACCOUNT"}).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_user_management_stats():
    total_users = await users_col.count_documents({})
    banned_users = await users_col.count_documents({"is_banned": 1})
    verified_users = await users_col.count_documents({"is_verified": 1})
    return {
        "total": total_users,
        "banned": banned_users,
        "verified": verified_users,
    }


async def get_banned_users(limit: int = 10, skip: int = 0):
    cursor = users_col.find({"is_banned": 1}, {"user_id": 1}).skip(skip).limit(limit)
    return [doc["user_id"] async for doc in cursor]


async def get_all_user_ids():
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(length=None)
    return [doc["user_id"] for doc in users]


async def set_supplier_config(api_url: str, api_key: str, is_active: bool):
    await supplier_col.update_one(
        {"_id": "config"},
        {"$set": {"api_url": api_url, "api_key": api_key, "is_active": is_active}},
        upsert=True
    )


async def get_supplier_config():
    config = await supplier_col.find_one({"_id": "config"})
    if not config:
        return {"api_url": "", "api_key": "", "is_active": False}
    return config


async def add_category(name: str, tier_id: int):
    """Returns the category_id, creating it if it doesn't already exist under this type."""
    existing = await categories_col.find_one({"name": name, "tier_id": tier_id})
    if existing:
        return existing["category_id"]

    cat_id = await get_next_sequence("category_id")
    try:
        await categories_col.insert_one({"category_id": cat_id, "name": name, "tier_id": tier_id})
    except DuplicateKeyError:
        existing = await categories_col.find_one({"name": name, "tier_id": tier_id})
        return existing["category_id"] if existing else None
    return cat_id


async def add_account(category_id: int, display_name: str, phone_number: str, session_string: str, price: float, two_step: str = "", telegram_user_id=None, two_step_enabled: bool = False, telegram_username=None, added_by=None, added_via=None):
    if price is None or price <= 0:
        raise ValueError("price must be greater than 0")

    acc_id = await get_next_sequence("account_id")
    await accounts_col.insert_one({
        "account_id": acc_id,
        "category_id": category_id,
        "display_name": display_name,
        "phone_number": phone_number,
        "session_string": session_string,
        "price": price,
        "two_step": two_step,
        "two_step_enabled": two_step_enabled,
        "telegram_user_id": telegram_user_id,
        "telegram_username": telegram_username,
        "added_by": added_by,
        "added_via": added_via,
        "added_at": datetime.now(timezone.utc),
        "is_sold": 0,
    })
    return acc_id


async def get_available_accounts(category_id: int):
    cursor = accounts_col.find({"category_id": category_id, "is_sold": 0, "session_dead": {"$ne": True}})
    return [(doc["account_id"], doc.get("display_name", doc["phone_number"]), doc["price"]) async for doc in cursor]


async def get_account_by_id(account_id: int):
    return await accounts_col.find_one({"account_id": account_id})


async def buy_account_safely(user_id: int, account_id: int):
    """Returns (status, claimed_account_dict_or_None).
    status is one of: "success", "low_balance", "not_found"."""
    await get_user(user_id)
    account = await accounts_col.find_one({"account_id": account_id, "is_sold": 0, "session_dead": {"$ne": True}})
    if not account:
        return "not_found", None

    price = account["price"]
    deducted = await users_col.find_one_and_update(
        {"user_id": user_id, "balance": {"$gte": price}},
        {"$inc": {"balance": -price}},
        return_document=ReturnDocument.AFTER,
    )
    if not deducted:
        return "low_balance", None

    claimed = await accounts_col.find_one_and_update(
        {"account_id": account_id, "is_sold": 0, "session_dead": {"$ne": True}},
        {"$set": {"is_sold": 1, "sold_to": user_id, "sold_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        refunded = await users_col.find_one_and_update(
            {"user_id": user_id}, {"$inc": {"balance": price}}, return_document=ReturnDocument.AFTER
        )
        try:
            await add_ledger(user_id, price, "purchase_refund", ref=account_id, balance_after=(refunded or {}).get("balance"))
        except Exception as e:
            print(f"Ledger write failed: {e}")
        return "not_found", None

    await save_payment_record(user_id, price, "INR", "SPEND_BUY_ACCOUNT")
    try:
        await add_ledger(user_id, -price, "purchase", ref=account_id, balance_after=deducted.get("balance"))
    except Exception as e:
        print(f"Ledger write failed: {e}")
    return "success", claimed


# ---------------- OTP delivery logs ----------------

async def log_otp_delivery(account_id: int, phone_number: str):
    """Records one OTP delivery and returns the delivery count so far (including this one)."""
    await otp_logs_col.insert_one({
        "account_id": account_id,
        "phone_number": phone_number,
        "timestamp": datetime.now(timezone.utc),
    })
    return await otp_logs_col.count_documents({"account_id": account_id})


async def get_otp_delivery_count(account_id: int):
    return await otp_logs_col.count_documents({"account_id": account_id})


async def get_accounts_by_category_full(category_id: int):
    cursor = accounts_col.find({"category_id": category_id}).sort("account_id", 1)
    return await cursor.to_list(length=None)


async def get_all_accounts_with_sessions():
    cursor = accounts_col.find(
        {"session_string": {"$nin": [None, ""]}},
        {"account_id": 1, "phone_number": 1, "session_string": 1}
    )
    return await cursor.to_list(length=None)


async def get_unjoined_accounts(target: str):
    """Accounts that have NOT already successfully joined this exact target."""
    joined_ids = set()
    async for doc in joined_channels_col.find({"target": target}, {"account_id": 1}):
        joined_ids.add(doc["account_id"])

    all_accounts = await get_all_accounts_with_sessions()
    return [a for a in all_accounts if a["account_id"] not in joined_ids]


async def mark_joined(account_id: int, target: str):
    await joined_channels_col.update_one(
        {"account_id": account_id, "target": target},
        {"$set": {"joined_at": datetime.now(timezone.utc)}},
        upsert=True
    )


async def clear_account_session(account_id: int):
    """Removes the session string after the bot's own session has been
    logged out, so the DB no longer holds a live credential for it."""
    await accounts_col.update_one(
        {"account_id": account_id},
        {"$set": {"session_string": "", "session_revoked": True, "session_revoked_at": datetime.now(timezone.utc)}}
    )


async def get_all_unsold_accounts():
    cursor = accounts_col.find({"is_sold": 0})
    return [
        (doc["account_id"], doc.get("display_name", doc["phone_number"]) + (" ☠️" if doc.get("session_dead") else ""), doc["price"])
        async for doc in cursor
    ]


async def delete_account(acc_id: int):
    await accounts_col.delete_one({"account_id": acc_id})



# ---------------- Google Play redeem code requests ----------------
GP_MAX_PENDING = 3


async def create_gp_request(user_id: int, amount: int, code: str, file_id: str):
    """Returns (request_id, 'ok') or (None, 'limit' / 'duplicate')."""
    pending = await gp_requests_col.count_documents({"user_id": user_id, "status": "pending"})
    if pending >= GP_MAX_PENDING:
        return None, "limit"
    try:
        # _id = code, so the same code can never be submitted twice
        await gp_codes_col.insert_one({"_id": code, "user_id": user_id})
    except DuplicateKeyError:
        return None, "duplicate"
    req_id = await get_next_sequence("gp_request_id")
    await gp_requests_col.insert_one({
        "request_id": req_id,
        "user_id": user_id,
        "amount": amount,
        "code": code,
        "file_id": file_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return req_id, "ok"


async def get_gp_request(req_id: int):
    return await gp_requests_col.find_one({"request_id": req_id})


async def set_gp_status(req_id: int, status: str):
    """Atomic: only a pending request can be approved/rejected, and only once."""
    return await gp_requests_col.find_one_and_update(
        {"request_id": req_id, "status": "pending"},
        {"$set": {"status": status, "reviewed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


async def release_gp_code(code: str):
    await gp_codes_col.delete_one({"_id": code})


# ---------------- Deposit (UPI / Crypto) requests ----------------

async def create_deposit_request(user_id: int, amount: int, method: str, file_id: str, utr=None, file_unique_id=None):
    """Only one pending deposit request per user at a time.
    Returns (request_id, 'ok') or (None, 'pending')."""
    existing = await deposit_requests_col.find_one({"user_id": user_id, "status": "pending"})
    if existing:
        return None, "pending"

    req_id = await get_next_sequence("deposit_request_id")
    await deposit_requests_col.insert_one({
        "request_id": req_id,
        "user_id": user_id,
        "amount": amount,
        "method": method,
        "file_id": file_id,
        "utr": utr,
        "file_unique_id": file_unique_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return req_id, "ok"


async def get_deposit_request(req_id: int):
    return await deposit_requests_col.find_one({"request_id": req_id})


async def set_deposit_status(req_id: int, status: str):
    """Atomic: only a pending request can be approved/rejected, and only once."""
    return await deposit_requests_col.find_one_and_update(
        {"request_id": req_id, "status": "pending"},
        {"$set": {"status": status, "reviewed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


# ---------------- Dynamic admins (/addadmin, /removeadmin, /checkadmins) ----------------
async def add_admin(user_id: int, added_by: int) -> bool:
    """Returns True if newly added, False if already an admin."""
    try:
        await admins_col.insert_one({
            "user_id": user_id,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


async def remove_admin(user_id: int) -> bool:
    result = await admins_col.delete_one({"user_id": user_id})
    return result.deleted_count > 0


async def get_admins() -> list:
    """List of admin docs, oldest first."""
    return [a async for a in admins_col.find({}).sort("added_at", 1)]


# ---------------- Simple key/value settings (payment QR, UPI id, ...) ----------------
async def get_setting(key: str, default=None):
    doc = await settings_col.find_one({"_id": key})
    return doc["value"] if doc and "value" in doc else default


async def set_setting(key: str, value) -> None:
    await settings_col.update_one(
        {"_id": key},
        {"$set": {"value": value, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def delete_setting(key: str) -> bool:
    result = await settings_col.delete_one({"_id": key})
    return result.deleted_count > 0


# ---------------- Dead-session checker ----------------
async def get_accounts_for_session_check():
    """Unsold accounts that still hold a session string."""
    cursor = accounts_col.find(
        {"is_sold": 0, "session_string": {"$nin": [None, ""]}, "session_revoked": {"$ne": True}},
        {"account_id": 1, "phone_number": 1, "display_name": 1, "session_string": 1},
    )
    return await cursor.to_list(length=None)


async def mark_session_dead(account_id: int, reason: str):
    await accounts_col.update_one(
        {"account_id": account_id},
        {"$set": {"session_dead": True, "dead_reason": reason, "last_checked_at": datetime.now(timezone.utc)}},
    )


async def mark_session_ok(account_id: int):
    await accounts_col.update_one(
        {"account_id": account_id},
        {"$set": {"last_checked_at": datetime.now(timezone.utc)}, "$unset": {"session_dead": "", "dead_reason": ""}},
    )


# ---------------- Account / user lookups for /acc and /user ----------------
async def find_account(query: str):
    """query = account id (short number) or phone number (digits, with/without +)."""
    digits = "".join(ch for ch in query if ch.isdigit())
    if not digits:
        return None
    if len(digits) >= 8:
        return await accounts_col.find_one({"phone_number": {"$in": [digits, "+" + digits]}})
    return await accounts_col.find_one({"account_id": int(digits)})


async def get_user_orders(user_id: int, limit: int = 5):
    cursor = accounts_col.find({"sold_to": user_id}).sort("sold_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_user_totals(user_id: int):
    """(orders, spent, deposits_count, deposited) for one user."""
    orders, spent = 0, 0.0
    async for p in payments_col.find({"user_id": user_id, "status": "SPEND_BUY_ACCOUNT"}):
        orders += 1
        spent += p.get("amount", 0)
    dep_count, deposited = 0, 0.0
    async for p in payments_col.find({"user_id": user_id, "status": "TOPUP_SUCCESS"}):
        dep_count += 1
        deposited += p.get("amount", 0)
    return orders, spent, dep_count, deposited


# ---------------- /stats ----------------
async def _sum_payments(status: str, since):
    pipeline = [
        {"$match": {"status": status, "timestamp": {"$gte": since}}},
        {"$group": {"_id": None, "count": {"$sum": 1}, "total": {"$sum": "$amount"}}},
    ]
    async for row in payments_col.aggregate(pipeline):
        return row["count"], row["total"]
    return 0, 0.0


async def get_dashboard(since_today, since_7d, since_30d):
    out = {}
    out["users_total"] = await users_col.count_documents({})
    out["users_banned"] = await users_col.count_documents({"is_banned": 1})
    out["users_today"] = await users_col.count_documents({"created_at": {"$gte": since_today}})
    out["users_7d"] = await users_col.count_documents({"created_at": {"$gte": since_7d}})
    out["stock"] = await accounts_col.count_documents({"is_sold": 0, "session_dead": {"$ne": True}})
    out["dead"] = await accounts_col.count_documents({"is_sold": 0, "session_dead": True})
    out["sold_total"] = await accounts_col.count_documents({"is_sold": 1})
    for label, since in (("today", since_today), ("7d", since_7d), ("30d", since_30d)):
        out[f"sales_{label}"] = await _sum_payments("SPEND_BUY_ACCOUNT", since)
        out[f"topup_{label}"] = await _sum_payments("TOPUP_SUCCESS", since)
    out["pending_deposits"] = await deposit_requests_col.count_documents({"status": "pending"})
    out["pending_gp"] = await gp_requests_col.count_documents({"status": "pending"})
    return out


# ---------------- Deposit proof (UTR / screenshot) duplicate guard ----------------
# One doc per used proof, _id = "utr:XXXX" / "shot:UNIQUEID" -> a proof can only
# be claimed once (same trick as gp_codes). Released again if the deposit is rejected.
async def deposit_proof_used(kind: str, value: str) -> bool:
    return await deposit_proofs_col.find_one({"_id": f"{kind}:{value}"}) is not None


async def claim_deposit_proofs(user_id: int, utr: str, shot_id: str):
    """Returns 'ok', 'utr' or 'shot' (which proof was already used)."""
    claimed = []
    for kind, value in (("utr", utr), ("shot", shot_id)):
        if not value:
            continue
        try:
            await deposit_proofs_col.insert_one({"_id": f"{kind}:{value}", "user_id": user_id, "ts": datetime.now(timezone.utc)})
            claimed.append(f"{kind}:{value}")
        except DuplicateKeyError:
            for cid in claimed:
                await deposit_proofs_col.delete_one({"_id": cid})
            return kind
    return "ok"


async def release_deposit_proofs(utr, shot_id):
    for kind, value in (("utr", utr), ("shot", shot_id)):
        if value:
            await deposit_proofs_col.delete_one({"_id": f"{kind}:{value}"})

