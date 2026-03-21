from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import shutil
import os
import uuid
import logging
import requests
from datetime import datetime, timedelta
import razorpay
from supabase import create_client, Client
from .png_to_svg import png_to_svg

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SUPABASE_URL         = "https://pswlpjqonxynzxsdyjud.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

PAYLOAD_URL          = "http://localhost:3000/api"
PAYLOAD_SECRET       = "e7be7f67ce829de0fbe6a19c"
PAYLOAD_ADMIN_EMAIL  = "parull0410@gmail.com"
PAYLOAD_ADMIN_PASS   = "123456"

# Cache token to avoid logging in on every request
_payload_token_cache: dict = {"token": None, "expires": 0}

def get_payload_headers() -> dict:
    """Login to Payload and return headers with valid JWT token."""
    import time
    now = time.time()

    # Reuse cached token if still valid (cache for 1 hour)
    if _payload_token_cache["token"] and now < _payload_token_cache["expires"]:
        return {
            "Authorization": f"Bearer {_payload_token_cache['token']}",
            "Content-Type":  "application/json",
        }

    try:
        res = requests.post(
            f"{PAYLOAD_URL}/users/login",
            headers={"Content-Type": "application/json"},
            json={"email": PAYLOAD_ADMIN_EMAIL, "password": PAYLOAD_ADMIN_PASS},
            timeout=15,
        )
        if res.status_code == 200:
            token = res.json().get("token")
            _payload_token_cache["token"]   = token
            _payload_token_cache["expires"] = now + 3600
            return {
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            }
        logging.error(f"Payload login failed: {res.status_code} {res.text}")
    except Exception as e:
        logging.error(f"Payload login error: {e}")

    # Fallback to secret key
    return {
        "Authorization": f"Bearer {PAYLOAD_SECRET}",
        "Content-Type":  "application/json",
    }

PAYLOAD_HEADERS = get_payload_headers

RAZORPAY_KEY_ID      = "rzp_test_RmG7hznjlclBga"
RAZORPAY_KEY_SECRET  = "YvACC6VRdicCORe4qH10Q05l"

FREE_TRIAL_LIMIT     = 10

# ─────────────────────────────────────────
# TABLE NAMES
# ─────────────────────────────────────────
TBL_API_KEYS         = "api_keys"
TBL_USER_API_CREDITS = "api_credits"
TBL_USER_CREDIT_TX   = "credit_transactions"
TBL_PAYMENTS         = "payments"
TBL_SUBSCRIPTIONS    = "subscriptions"
TBL_CONVERSIONS      = "conversions"
TBL_PAYLOAD_MAP      = "user_payload_map"

# ─────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
razorpay_client  = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ─────────────────────────────────────────
# APP
# ─────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def get_supabase_user(authorization: str):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization token")
    token = authorization.split(" ")[1] if " " in authorization else authorization
    result = supabase.auth.get_user(token)
    if not result.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return result.user


def get_payload_user_id(supabase_user_id: str) -> int | None:
    """Look up Payload integer user ID from our mapping table."""
    try:
        result = supabase.table(TBL_PAYLOAD_MAP)\
            .select("payload_user_id")\
            .eq("supabase_user_id", supabase_user_id)\
            .execute()
        if result.data:
            return result.data[0]["payload_user_id"]
        return None
    except Exception as e:
        logging.error(f"Payload map lookup error: {e}")
        return None


def get_payload_user(payload_user_id: int) -> dict | None:
    """Fetch Payload user by integer ID."""
    try:
        res = requests.get(
            f"{PAYLOAD_URL}/users/{payload_user_id}",
            headers=get_payload_headers(),
            timeout=15,
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        logging.error(f"Payload user fetch error: {e}")
        return None


def create_payload_user(email: str, full_name: str) -> dict | None:
    """Create a new user in Payload CMS."""
    try:
        res = requests.post(
            f"{PAYLOAD_URL}/users",
            headers=get_payload_headers(),
            json={
                "email":              email,
                "fullName":           full_name,
                "password":           uuid.uuid4().hex,
                "subscriptionStatus": "trial",
                "usageCount":         0,
            },
            timeout=15,
        )
        return res.json().get("doc")
    except Exception as e:
        logging.error(f"Payload user create error: {e}")
        return None


def update_payload_user(payload_user_id: int, data: dict):
    """PATCH a Payload user by integer ID."""
    try:
        res = requests.patch(
            f"{PAYLOAD_URL}/users/{payload_user_id}",
            headers=get_payload_headers(),
            json=data,
            timeout=15,
        )
        if res.status_code not in (200, 201):
            logging.error(f"Payload update failed: {res.status_code} {res.text}")
    except Exception as e:
        logging.error(f"Payload user update error: {e}")


def log_to_payload(payload_user_id: int | None, event: str, details: str = ""):
    """Write a log entry to Payload Logs collection."""
    try:
        body = {
            "event":     event,
            "details":   details,
            "createdAt": datetime.utcnow().isoformat(),
        }
        if payload_user_id:
            body["user"] = payload_user_id
        requests.post(
            f"{PAYLOAD_URL}/logs",
            headers=get_payload_headers(),
            json=body,
            timeout=15,
        )
    except Exception as e:
        logging.error(f"Payload log error: {e}")


def get_plan_from_payload(plan_id: str) -> dict:
    """Fetch plan details from Payload CMS by integer ID."""
    try:
        res = requests.get(
            f"{PAYLOAD_URL}/plans/{plan_id}",
            headers=get_payload_headers(),
            timeout=15,
        )
        if res.status_code != 200:
            raise HTTPException(status_code=400, detail="Plan not found in Payload")
        return res.json()
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Payload plan fetch error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch plan from Payload")


def mirror_api_key_to_payload(payload_user_id: int, api_key: str, description: str, user_email: str = ""):
    """Save API key to Payload api-keys collection."""
    try:
        res = requests.post(
            f"{PAYLOAD_URL}/api-keys",
            headers=get_payload_headers(),
            json={
                "user_id":     str(payload_user_id),
                "api_key":     api_key,
                "description": description,
                "user_email":  user_email,
                "active":      True,
                "created_at":  datetime.utcnow().isoformat(),
            },
            timeout=15,
        )
        if res.status_code not in (200, 201):
            logging.error(f"Payload api-key mirror failed: {res.status_code} {res.text}")
    except Exception as e:
        logging.error(f"Payload api-key mirror error: {e}")


def mirror_api_credits_to_payload(payload_user_id: int, user_id: str, credits: int):
    """Upsert API credits in Payload api-credits collection."""
    try:
        # Check if record exists
        res = requests.get(
            f"{PAYLOAD_URL}/api-credits",
            headers=get_payload_headers(),
            params={"where[user_id][equals]": str(payload_user_id)},
            timeout=15,
        )
        data = res.json()
        docs = data.get("docs", [])

        if docs:
            # Update existing
            doc_id = docs[0]["id"]
            requests.patch(
                f"{PAYLOAD_URL}/api-credits/{doc_id}",
                headers=get_payload_headers(),
                json={"credits_remaining": credits},
                timeout=15,
            )
        else:
            # Create new
            requests.post(
                f"{PAYLOAD_URL}/api-credits",
                headers=get_payload_headers(),
                json={
                    "user_id":           str(payload_user_id),
                    "credits_remaining": credits,
                },
                timeout=15,
            )
    except Exception as e:
        logging.error(f"Payload api-credits mirror error: {e}")


def mirror_credit_transaction_to_payload(payload_user_id: int, credits_added: int, price, payment_id: str):
    """Save credit transaction to Payload credit-transactions collection."""
    try:
        requests.post(
            f"{PAYLOAD_URL}/credit-transactions",
            headers=get_payload_headers(),
            json={
                "user_id":      str(payload_user_id),
                "credits_added": credits_added,
                "price":        float(price) if price else 0,
                "date":         datetime.utcnow().isoformat(),
            },
            timeout=15,
        )
    except Exception as e:
        logging.error(f"Payload credit-transaction mirror error: {e}")


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.get("/")
def home():
    return {"status": "API running"}


# ── SYNC USER ─────────────────────────────────────────────────────────────────
@app.post("/sync-user")
async def sync_user(
    email:             str = Form(...),
    full_name:         str = Form(""),
    supabase_user_id:  str = Form(""),
):
    # Check if mapping already exists
    existing_map = supabase.table(TBL_PAYLOAD_MAP)\
        .select("payload_user_id")\
        .eq("supabase_user_id", supabase_user_id)\
        .execute()

    if existing_map.data:
        return {"message": "User already synced", "payload_id": existing_map.data[0]["payload_user_id"]}

    # Create user in Payload
    new_user = create_payload_user(email, full_name)
    if not new_user:
        logging.warning(f"Payload sync skipped for {email} — Payload may be down")
        return {"message": "User registered (Payload sync pending)", "payload_id": None}

    payload_user_id = new_user["id"]

    # Save mapping in Supabase
    supabase.table(TBL_PAYLOAD_MAP).insert({
        "supabase_user_id": supabase_user_id,
        "payload_user_id":  payload_user_id,
        "email":            email,
    }).execute()

    log_to_payload(payload_user_id, "user_signup", f"New signup: {email}")

    return {"message": "User synced to Payload", "payload_id": payload_user_id}


# ── TRIAL STATUS ──────────────────────────────────────────────────────────────
@app.get("/trial-status")
async def trial_status(authorization: str = Header(None)):
    user            = get_supabase_user(authorization)
    payload_user_id = get_payload_user_id(user.id)
    usage_count     = 0

    if payload_user_id:
        payload_user = get_payload_user(payload_user_id)
        usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

    sub = (
        supabase.table(TBL_SUBSCRIPTIONS)
        .select("id")
        .eq("user_id", user.id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    has_active_plan = len(sub.data) > 0

    return {
        "has_active_plan": has_active_plan,
        "usage_count":     usage_count,
        "free_limit":      FREE_TRIAL_LIMIT,
        "free_remaining":  max(0, FREE_TRIAL_LIMIT - usage_count) if not has_active_plan else None,
        "trial_exhausted": usage_count >= FREE_TRIAL_LIMIT and not has_active_plan,
    }


# ── WEB CONVERSION ────────────────────────────────────────────────────────────
@app.post("/convert")
async def convert(
    file:          UploadFile = File(...),
    authorization: str        = Header(None),
):
    user            = get_supabase_user(authorization)
    user_id         = user.id
    payload_user_id = get_payload_user_id(user_id)
    usage_count     = 0

    if payload_user_id:
        payload_user = get_payload_user(payload_user_id)
        usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

    # Check active web subscription
    sub_result = (
        supabase.table(TBL_SUBSCRIPTIONS)
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .eq("plan_category", "web")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    active_sub = sub_result.data[0] if sub_result.data else None

    # Check expiry
    if active_sub and active_sub.get("expires_at"):
        expiry = datetime.fromisoformat(active_sub["expires_at"].replace("Z", "+00:00"))
        if datetime.now(expiry.tzinfo) > expiry:
            supabase.table(TBL_SUBSCRIPTIONS).update({"status": "expired"}).eq("id", active_sub["id"]).execute()
            if payload_user_id:
                update_payload_user(payload_user_id, {"subscriptionStatus": "expired"})
            active_sub = None

    # Gate: no active sub → check trial limit
    if not active_sub:
        if usage_count >= FREE_TRIAL_LIMIT:
            raise HTTPException(
                status_code=403,
                detail=f"Free trial limit of {FREE_TRIAL_LIMIT} conversions reached. Please buy a plan to continue.",
            )

    # Perform conversion
    os.makedirs("temp", exist_ok=True)
    uid             = str(uuid.uuid4())
    temp_png_path   = Path(f"temp/{uid}.png")
    output_svg_path = Path(f"temp/{uid}.svg")

    with temp_png_path.open("wb") as buf:
        shutil.copyfileobj(file.file, buf)

    png_to_svg(temp_png_path, svg_path=output_svg_path)

    with output_svg_path.open("rb") as f:
        svg_result = f.read()

    # Save conversion record
    supabase.table(TBL_CONVERSIONS).insert({
        "user_id":         user_id,
        "type":            "web",
        "subscription_id": active_sub["id"] if active_sub else None,
        "created_at":      datetime.utcnow().isoformat(),
    }).execute()

    # Increment usageCount in Payload
    if payload_user_id:
        new_count = usage_count + 1
        update_payload_user(payload_user_id, {"usageCount": new_count})
        log_to_payload(payload_user_id, "web_conversion", f"Conversion #{new_count}")

    return Response(content=svg_result, media_type="image/svg+xml")


# ── API CONVERSION ────────────────────────────────────────────────────────────
@app.post("/api/convert")
async def api_convert(
    file:      UploadFile = File(...),
    x_api_key: str        = Header(None),
):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

    key_result = supabase.table(TBL_API_KEYS).select("*").eq("api_key", x_api_key).execute()
    if not key_result.data:
        raise HTTPException(status_code=401, detail="Invalid API key")

    key_doc = key_result.data[0]
    if not key_doc["active"]:
        raise HTTPException(status_code=403, detail="API key is inactive")

    api_user_id = key_doc["user_id"]

    credits_result = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", api_user_id).execute()
    if not credits_result.data or credits_result.data[0]["credits_remaining"] <= 0:
        raise HTTPException(status_code=403, detail="API credits exhausted. Please top up.")

    credits_remaining = credits_result.data[0]["credits_remaining"]

    os.makedirs("temp", exist_ok=True)
    uid             = str(uuid.uuid4())
    temp_png_path   = Path(f"temp/{uid}.png")
    output_svg_path = Path(f"temp/{uid}.svg")

    with temp_png_path.open("wb") as buf:
        shutil.copyfileobj(file.file, buf)

    png_to_svg(temp_png_path, svg_path=output_svg_path)

    with output_svg_path.open("rb") as f:
        svg_result = f.read()

    new_credits = credits_remaining - 1

    # Deduct credit in Supabase
    supabase.table(TBL_USER_API_CREDITS).update(
        {"credits_remaining": new_credits}
    ).eq("user_id", api_user_id).execute()

    supabase.table(TBL_CONVERSIONS).insert({
        "user_id":      api_user_id,
        "type":         "api",
        "api_key_used": x_api_key,
        "created_at":   datetime.utcnow().isoformat(),
    }).execute()

    # Mirror updated credits to Payload
    payload_user_id = get_payload_user_id(str(api_user_id))
    if payload_user_id:
        mirror_api_credits_to_payload(payload_user_id, str(api_user_id), new_credits)

    return Response(content=svg_result, media_type="image/svg+xml")


# ── CREATE RAZORPAY ORDER ─────────────────────────────────────────────────────
@app.post("/create-order")
async def create_order(
    plan_id:       str = Form(...),
    authorization: str = Header(None),
):
    get_supabase_user(authorization)

    plan   = get_plan_from_payload(plan_id)
    amount = plan.get("price")
    if not amount:
        raise HTTPException(status_code=400, detail="Plan price not set in Payload")

    order = razorpay_client.order.create({
        "amount":          int(float(amount) * 100),
        "currency":        "INR",
        "payment_capture": 1,
    })

    return {"order_id": order["id"], "amount": order["amount"], "key": RAZORPAY_KEY_ID}


# ── VERIFY PAYMENT ────────────────────────────────────────────────────────────
@app.post("/verify-payment")
async def verify_payment(
    razorpay_order_id:   str = Form(...),
    razorpay_payment_id: str = Form(...),
    razorpay_signature:  str = Form(...),
    plan_id:             str = Form(...),
    authorization:       str = Header(None),
):
    user            = get_supabase_user(authorization)
    user_id         = user.id
    payload_user_id = get_payload_user_id(user_id)

    # 1. Verify signature
    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id":   razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature":  razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    # 2. Fetch plan
    plan          = get_plan_from_payload(plan_id)
    plan_name     = plan.get("planName")
    price         = plan.get("price")
    plan_category = plan.get("planCategory")
    usage_limit   = plan.get("usageLimit")
    duration_days = plan.get("durationDays")

    if not plan_category:
        raise HTTPException(status_code=400, detail="Plan category missing in Payload")

    expires_at = None
    if duration_days:
        expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

    # 3. Save payment to Supabase
    supabase.table(TBL_PAYMENTS).insert({
        "user_id":             user_id,
        "plan_id":             plan_id,
        "plan_name":           plan_name,
        "plan_category":       plan_category,
        "amount":              price,
        "razorpay_order_id":   razorpay_order_id,
        "razorpay_payment_id": razorpay_payment_id,
        "razorpay_signature":  razorpay_signature,
        "status":              "success",
        "created_at":          datetime.utcnow().isoformat(),
    }).execute()

    # 4. Save subscription to Supabase
    supabase.table(TBL_SUBSCRIPTIONS).insert({
        "user_id":           user_id,
        "plan_id":           plan_id,
        "plan_name":         plan_name,
        "plan_category":     plan_category,
        "status":            "active",
        "started_at":        datetime.utcnow().isoformat(),
        "expires_at":        expires_at,
        "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
        "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
        "payment_id":        razorpay_payment_id,
    }).execute()

    # 5. API plan → top up credits in Supabase
    if plan_category == "api" and usage_limit:
        existing = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user_id).execute()
        if existing.data:
            current      = existing.data[0]["credits_remaining"]
            new_credits  = current + int(usage_limit)
            supabase.table(TBL_USER_API_CREDITS).update(
                {"credits_remaining": new_credits}
            ).eq("user_id", user_id).execute()
        else:
            new_credits = int(usage_limit)
            supabase.table(TBL_USER_API_CREDITS).insert({
                "user_id":           user_id,
                "credits_remaining": new_credits,
            }).execute()

        supabase.table(TBL_USER_CREDIT_TX).insert({
            "user_id":       user_id,
            "credits_added": int(usage_limit),
            "price":         price,
            "payment_id":    razorpay_payment_id,
            "date":          datetime.utcnow().isoformat(),
        }).execute()

    # 6. Mirror everything to Payload
    if payload_user_id:
        # Update Payload user subscription status
        payload_update: dict = {
            "subscriptionStatus": "active",
            # "subscriptionPlan":   int(plan_id),
            "subscriptionPlan": plan_id,
        }
        if expires_at:
            payload_update["subscriptionExpiry"] = expires_at
        if duration_days:
            payload_update["billingCycle"] = "monthly"
        update_payload_user(payload_user_id, payload_update)

        # Mirror payment to Payload payments collection
        try:
            requests.post(
                f"{PAYLOAD_URL}/payments",
                headers=get_payload_headers(),
                json={
                    "user":          payload_user_id,
                    "amount":        float(price) if price else 0,
                    "status":        "success",
                    "transactionId": razorpay_payment_id,
                },
                timeout=15,
            )
        except Exception as e:
            logging.error(f"Payload payment mirror error: {e}")

        # Mirror API credits + transaction to Payload
        if plan_category == "api" and usage_limit:
            mirror_api_credits_to_payload(payload_user_id, user_id, new_credits)
            mirror_credit_transaction_to_payload(
                payload_user_id, int(usage_limit), price, razorpay_payment_id
            )

        log_to_payload(
            payload_user_id,
            "payment_success",
            f"Plan: {plan_name} | Amount: {price} | TxID: {razorpay_payment_id}",
        )

    return {"message": "Payment verified and subscription activated"}


# ── GENERATE API KEY ──────────────────────────────────────────────────────────
@app.post("/generate-api-key")
async def generate_api_key(
    description:   str = Form(...),
    authorization: str = Header(None),
):
    user    = get_supabase_user(authorization)
    user_id = user.id
    email   = user.email

    api_key = "sk_" + uuid.uuid4().hex[:16]

    # Save to Supabase
    supabase.table(TBL_API_KEYS).insert({
        "api_key":     api_key,
        "user_id":     user_id,
        "user_email":  email,
        "description": description,
        "active":      True,
        "created_at":  datetime.utcnow().isoformat(),
    }).execute()

    # Mirror to Payload
    payload_user_id = get_payload_user_id(user_id)
    if payload_user_id:
        mirror_api_key_to_payload(payload_user_id, api_key, description, email)
        log_to_payload(payload_user_id, "api_key_generated", f"Key: {api_key[:10]}... | Desc: {description}")

    return {"api_key": api_key}


# ── MY API KEYS ───────────────────────────────────────────────────────────────
@app.get("/my-api-keys")
async def my_api_keys(authorization: str = Header(None)):
    user = get_supabase_user(authorization)
    keys = supabase.table(TBL_API_KEYS).select("*").eq("user_id", user.id).execute()
    return keys.data


# ── MY API CREDITS ────────────────────────────────────────────────────────────
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):
#     user    = get_supabase_user(authorization)
#     credits = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user.id).execute()
#     return credits.data[0] if credits.data else {"credits_remaining": 0}
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)

#     credits = supabase.table(TBL_USER_API_CREDITS)\
#         .select("*")\
#         .eq("user_id", user.id)\
#         .execute()

#     sub = supabase.table(TBL_SUBSCRIPTIONS)\
#         .select("credits_total")\
#         .eq("user_id", user.id)\
#         .eq("status", "active")\
#         .order("created_at", desc=True)\
#         .limit(1)\
#         .execute()

#     total = sub.data[0]["credits_total"] if sub.data and sub.data[0]["credits_total"] else 0
#     remaining = credits.data[0]["credits_remaining"] if credits.data else 0

#     return {
#         "remaining_credits": remaining,
#         "total_credits": total
#     }
@app.get("/my-api-credits")
async def my_api_credits(authorization: str = Header(None)):
    user = get_supabase_user(authorization)

    credits = supabase.table(TBL_USER_API_CREDITS)\
        .select("*")\
        .eq("user_id", user.id)\
        .execute()

    sub = supabase.table(TBL_SUBSCRIPTIONS)\
        .select("credits_total")\
        .eq("user_id", user.id)\
        .eq("status", "active")\
        .execute()

    total = sum(row["credits_total"] for row in sub.data if row.get("credits_total")) if sub.data else 0
    remaining = credits.data[0]["credits_remaining"] if credits.data else 0

    return {
        "remaining_credits": remaining,
        "total_credits": total
    }


# ── MY ACTIVE SUBSCRIPTION ────────────────────────────────────────────────────
@app.get("/my-subscription")
async def my_subscription(authorization: str = Header(None)):
    user = get_supabase_user(authorization)
    sub  = (
        supabase.table(TBL_SUBSCRIPTIONS)
        .select("*")
        .eq("user_id", user.id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return sub.data[0] if sub.data else {"status": "no_active_subscription"}


# ── MY CONVERSIONS ────────────────────────────────────────────────────────────
@app.get("/my-conversions")
async def my_conversions(authorization: str = Header(None)):
    user = get_supabase_user(authorization)
    data = (
        supabase.table(TBL_CONVERSIONS)
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    return data.data


# ── MY PAYMENTS ───────────────────────────────────────────────────────────────
@app.get("/my-payments")
async def my_payments(authorization: str = Header(None)):
    user = get_supabase_user(authorization)
    data = (
        supabase.table(TBL_PAYMENTS)
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    return data.data

@app.delete("/api-key/{key_id}")
async def revoke_api_key(key_id: str, authorization: str = Header(None)):
    user = get_supabase_user(authorization)

    result = supabase.table(TBL_API_KEYS)\
        .delete()\
        .eq("id", key_id)\
        .eq("user_id", user.id)\
        .execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Key not found")

    return {"message": "API key revoked"}



# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# import requests
# from datetime import datetime, timedelta
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg

# # ─────────────────────────────────────────
# # CONFIG
# # ─────────────────────────────────────────
# SUPABASE_URL         = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# PAYLOAD_URL          = "http://localhost:3000/api"
# PAYLOAD_SECRET       = "e7be7f67ce829de0fbe6a19c"
# PAYLOAD_ADMIN_EMAIL  = "parull0393@gmail.com"
# PAYLOAD_ADMIN_PASS   = "123456"

# # Cache token to avoid logging in on every request
# _payload_token_cache: dict = {"token": None, "expires": 0}

# def get_payload_headers() -> dict:
#     """Login to Payload and return headers with valid JWT token."""
#     import time
#     now = time.time()

#     # Reuse cached token if still valid (cache for 1 hour)
#     if _payload_token_cache["token"] and now < _payload_token_cache["expires"]:
#         return {
#             "Authorization": f"Bearer {_payload_token_cache['token']}",
#             "Content-Type":  "application/json",
#         }

#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/users/login",
#             headers={"Content-Type": "application/json"},
#             json={"email": PAYLOAD_ADMIN_EMAIL, "password": PAYLOAD_ADMIN_PASS},
#             timeout=15,
#         )
#         if res.status_code == 200:
#             token = res.json().get("token")
#             _payload_token_cache["token"]   = token
#             _payload_token_cache["expires"] = now + 3600
#             return {
#                 "Authorization": f"Bearer {token}",
#                 "Content-Type":  "application/json",
#             }
#         logging.error(f"Payload login failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload login error: {e}")

#     # Fallback to secret key
#     return {
#         "Authorization": f"Bearer {PAYLOAD_SECRET}",
#         "Content-Type":  "application/json",
#     }

# PAYLOAD_HEADERS = get_payload_headers

# RAZORPAY_KEY_ID      = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET  = "YvACC6VRdicCORe4qH10Q05l"

# FREE_TRIAL_LIMIT     = 10

# # ─────────────────────────────────────────
# # TABLE NAMES
# # ─────────────────────────────────────────
# TBL_API_KEYS         = "api_keys"
# TBL_USER_API_CREDITS = "api_credits"
# TBL_USER_CREDIT_TX   = "credit_transactions"
# TBL_PAYMENTS         = "payments"
# TBL_SUBSCRIPTIONS    = "subscriptions"
# TBL_CONVERSIONS      = "conversions"
# TBL_PAYLOAD_MAP      = "user_payload_map"

# # ─────────────────────────────────────────
# # CLIENTS
# # ─────────────────────────────────────────
# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
# razorpay_client  = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # ─────────────────────────────────────────
# # APP
# # ─────────────────────────────────────────
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# # ─────────────────────────────────────────
# # HELPERS
# # ─────────────────────────────────────────

# def get_supabase_user(authorization: str):
#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing authorization token")
#     token = authorization.split(" ")[1] if " " in authorization else authorization
#     result = supabase.auth.get_user(token)
#     if not result.user:
#         raise HTTPException(status_code=401, detail="Invalid or expired token")
#     return result.user


# def get_payload_user_id(supabase_user_id: str) -> int | None:
#     """Look up Payload integer user ID from our mapping table."""
#     try:
#         result = supabase.table(TBL_PAYLOAD_MAP)\
#             .select("payload_user_id")\
#             .eq("supabase_user_id", supabase_user_id)\
#             .execute()
#         if result.data:
#             return result.data[0]["payload_user_id"]
#         return None
#     except Exception as e:
#         logging.error(f"Payload map lookup error: {e}")
#         return None


# def get_payload_user(payload_user_id: int) -> dict | None:
#     """Fetch Payload user by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=get_payload_headers(),
#             timeout=15,
#         )
#         if res.status_code == 200:
#             return res.json()
#         return None
#     except Exception as e:
#         logging.error(f"Payload user fetch error: {e}")
#         return None


# def create_payload_user(email: str, full_name: str) -> dict | None:
#     """Create a new user in Payload CMS."""
#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/users",
#             headers=get_payload_headers(),
#             json={
#                 "email":              email,
#                 "fullName":           full_name,
#                 "password":           uuid.uuid4().hex,
#                 "subscriptionStatus": "trial",
#                 "usageCount":         0,
#             },
#             timeout=15,
#         )
#         return res.json().get("doc")
#     except Exception as e:
#         logging.error(f"Payload user create error: {e}")
#         return None


# def update_payload_user(payload_user_id: int, data: dict):
#     """PATCH a Payload user by integer ID."""
#     try:
#         res = requests.patch(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=get_payload_headers(),
#             json=data,
#             timeout=15,
#         )
#         if res.status_code not in (200, 201):
#             logging.error(f"Payload update failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload user update error: {e}")


# def log_to_payload(payload_user_id: int | None, event: str, details: str = ""):
#     """Write a log entry to Payload Logs collection."""
#     try:
#         body = {
#             "event":     event,
#             "details":   details,
#             "createdAt": datetime.utcnow().isoformat(),
#         }
#         if payload_user_id:
#             body["user"] = payload_user_id
#         requests.post(
#             f"{PAYLOAD_URL}/logs",
#             headers=get_payload_headers(),
#             json=body,
#             timeout=15,
#         )
#     except Exception as e:
#         logging.error(f"Payload log error: {e}")


# def get_plan_from_payload(plan_id: str) -> dict:
#     """Fetch plan details from Payload CMS by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/plans/{plan_id}",
#             headers=get_payload_headers(),
#             timeout=15,
#         )
#         if res.status_code != 200:
#             raise HTTPException(status_code=400, detail="Plan not found in Payload")
#         return res.json()
#     except HTTPException:
#         raise
#     except Exception as e:
#         logging.error(f"Payload plan fetch error: {e}")
#         raise HTTPException(status_code=500, detail="Failed to fetch plan from Payload")


# def mirror_api_key_to_payload(payload_user_id: int, api_key: str, description: str):
#     """Save API key to Payload api-keys collection."""
#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/api-keys",
#             headers=get_payload_headers(),
#             json={
#                 "user_id":     str(payload_user_id),
#                 "api_key":     api_key,
#                 "description": description,
#             },
#             timeout=15,
#         )
#         if res.status_code not in (200, 201):
#             logging.error(f"Payload api-key mirror failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload api-key mirror error: {e}")


# def mirror_api_credits_to_payload(payload_user_id: int, user_id: str, credits: int):
#     """Upsert API credits in Payload api-credits collection."""
#     try:
#         # Check if record exists
#         res = requests.get(
#             f"{PAYLOAD_URL}/api-credits",
#             headers=get_payload_headers(),
#             params={"where[user_id][equals]": str(payload_user_id)},
#             timeout=15,
#         )
#         data = res.json()
#         docs = data.get("docs", [])

#         if docs:
#             # Update existing
#             doc_id = docs[0]["id"]
#             requests.patch(
#                 f"{PAYLOAD_URL}/api-credits/{doc_id}",
#                 headers=get_payload_headers(),
#                 json={"credits_remaining": credits},
#                 timeout=15,
#             )
#         else:
#             # Create new
#             requests.post(
#                 f"{PAYLOAD_URL}/api-credits",
#                 headers=get_payload_headers(),
#                 json={
#                     "user_id":           str(payload_user_id),
#                     "credits_remaining": credits,
#                 },
#                 timeout=15,
#             )
#     except Exception as e:
#         logging.error(f"Payload api-credits mirror error: {e}")


# def mirror_credit_transaction_to_payload(payload_user_id: int, credits_added: int, price, payment_id: str):
#     """Save credit transaction to Payload credit-transactions collection."""
#     try:
#         requests.post(
#             f"{PAYLOAD_URL}/credit-transactions",
#             headers=get_payload_headers(),
#             json={
#                 "user_id":      str(payload_user_id),
#                 "credits_added": credits_added,
#                 "price":        float(price) if price else 0,
#                 "date":         datetime.utcnow().isoformat(),
#             },
#             timeout=15,
#         )
#     except Exception as e:
#         logging.error(f"Payload credit-transaction mirror error: {e}")


# # ─────────────────────────────────────────
# # ROUTES
# # ─────────────────────────────────────────

# @app.get("/")
# def home():
#     return {"status": "API running"}


# # ── SYNC USER ─────────────────────────────────────────────────────────────────
# @app.post("/sync-user")
# async def sync_user(
#     email:             str = Form(...),
#     full_name:         str = Form(""),
#     supabase_user_id:  str = Form(""),
# ):
#     # Check if mapping already exists
#     existing_map = supabase.table(TBL_PAYLOAD_MAP)\
#         .select("payload_user_id")\
#         .eq("supabase_user_id", supabase_user_id)\
#         .execute()

#     if existing_map.data:
#         return {"message": "User already synced", "payload_id": existing_map.data[0]["payload_user_id"]}

#     # Create user in Payload
#     new_user = create_payload_user(email, full_name)
#     if not new_user:
#         logging.warning(f"Payload sync skipped for {email} — Payload may be down")
#         return {"message": "User registered (Payload sync pending)", "payload_id": None}

#     payload_user_id = new_user["id"]

#     # Save mapping in Supabase
#     supabase.table(TBL_PAYLOAD_MAP).insert({
#         "supabase_user_id": supabase_user_id,
#         "payload_user_id":  payload_user_id,
#         "email":            email,
#     }).execute()

#     log_to_payload(payload_user_id, "user_signup", f"New signup: {email}")

#     return {"message": "User synced to Payload", "payload_id": payload_user_id}


# # ── TRIAL STATUS ──────────────────────────────────────────────────────────────
# @app.get("/trial-status")
# async def trial_status(authorization: str = Header(None)):
#     user            = get_supabase_user(authorization)
#     payload_user_id = get_payload_user_id(user.id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     sub = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("id")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .limit(1)
#         .execute()
#     )
#     has_active_plan = len(sub.data) > 0

#     return {
#         "has_active_plan": has_active_plan,
#         "usage_count":     usage_count,
#         "free_limit":      FREE_TRIAL_LIMIT,
#         "free_remaining":  max(0, FREE_TRIAL_LIMIT - usage_count) if not has_active_plan else None,
#         "trial_exhausted": usage_count >= FREE_TRIAL_LIMIT and not has_active_plan,
#     }


# # ── WEB CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/convert")
# async def convert(
#     file:          UploadFile = File(...),
#     authorization: str        = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     # Check active web subscription
#     sub_result = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user_id)
#         .eq("status", "active")
#         .eq("plan_category", "web")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     active_sub = sub_result.data[0] if sub_result.data else None

#     # Check expiry
#     if active_sub and active_sub.get("expires_at"):
#         expiry = datetime.fromisoformat(active_sub["expires_at"].replace("Z", "+00:00"))
#         if datetime.now(expiry.tzinfo) > expiry:
#             supabase.table(TBL_SUBSCRIPTIONS).update({"status": "expired"}).eq("id", active_sub["id"]).execute()
#             if payload_user_id:
#                 update_payload_user(payload_user_id, {"subscriptionStatus": "expired"})
#             active_sub = None

#     # Gate: no active sub → check trial limit
#     if not active_sub:
#         if usage_count >= FREE_TRIAL_LIMIT:
#             raise HTTPException(
#                 status_code=403,
#                 detail=f"Free trial limit of {FREE_TRIAL_LIMIT} conversions reached. Please buy a plan to continue.",
#             )

#     # Perform conversion
#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     # Save conversion record
#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":         user_id,
#         "type":            "web",
#         "subscription_id": active_sub["id"] if active_sub else None,
#         "created_at":      datetime.utcnow().isoformat(),
#     }).execute()

#     # Increment usageCount in Payload
#     if payload_user_id:
#         new_count = usage_count + 1
#         update_payload_user(payload_user_id, {"usageCount": new_count})
#         log_to_payload(payload_user_id, "web_conversion", f"Conversion #{new_count}")

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── API CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/api/convert")
# async def api_convert(
#     file:      UploadFile = File(...),
#     x_api_key: str        = Header(None),
# ):
#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

#     key_result = supabase.table(TBL_API_KEYS).select("*").eq("api_key", x_api_key).execute()
#     if not key_result.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key_result.data[0]
#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key is inactive")

#     api_user_id = key_doc["user_id"]

#     credits_result = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", api_user_id).execute()
#     if not credits_result.data or credits_result.data[0]["credits_remaining"] <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted. Please top up.")

#     credits_remaining = credits_result.data[0]["credits_remaining"]

#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     new_credits = credits_remaining - 1

#     # Deduct credit in Supabase
#     supabase.table(TBL_USER_API_CREDITS).update(
#         {"credits_remaining": new_credits}
#     ).eq("user_id", api_user_id).execute()

#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":      api_user_id,
#         "type":         "api",
#         "api_key_used": x_api_key,
#         "created_at":   datetime.utcnow().isoformat(),
#     }).execute()

#     # Mirror updated credits to Payload
#     payload_user_id = get_payload_user_id(str(api_user_id))
#     if payload_user_id:
#         mirror_api_credits_to_payload(payload_user_id, str(api_user_id), new_credits)

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── CREATE RAZORPAY ORDER ─────────────────────────────────────────────────────
# @app.post("/create-order")
# async def create_order(
#     plan_id:       str = Form(...),
#     authorization: str = Header(None),
# ):
#     get_supabase_user(authorization)

#     plan   = get_plan_from_payload(plan_id)
#     amount = plan.get("price")
#     if not amount:
#         raise HTTPException(status_code=400, detail="Plan price not set in Payload")

#     order = razorpay_client.order.create({
#         "amount":          int(float(amount) * 100),
#         "currency":        "INR",
#         "payment_capture": 1,
#     })

#     return {"order_id": order["id"], "amount": order["amount"], "key": RAZORPAY_KEY_ID}


# # ── VERIFY PAYMENT ────────────────────────────────────────────────────────────
# @app.post("/verify-payment")
# async def verify_payment(
#     razorpay_order_id:   str = Form(...),
#     razorpay_payment_id: str = Form(...),
#     razorpay_signature:  str = Form(...),
#     plan_id:             str = Form(...),
#     authorization:       str = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)

#     # 1. Verify signature
#     try:
#         razorpay_client.utility.verify_payment_signature({
#             "razorpay_order_id":   razorpay_order_id,
#             "razorpay_payment_id": razorpay_payment_id,
#             "razorpay_signature":  razorpay_signature,
#         })
#     except razorpay.errors.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Payment signature verification failed")

#     # 2. Fetch plan
#     plan          = get_plan_from_payload(plan_id)
#     plan_name     = plan.get("planName")
#     price         = plan.get("price")
#     plan_category = plan.get("planCategory")
#     usage_limit   = plan.get("usageLimit")
#     duration_days = plan.get("durationDays")

#     if not plan_category:
#         raise HTTPException(status_code=400, detail="Plan category missing in Payload")

#     expires_at = None
#     if duration_days:
#         expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

#     # 3. Save payment to Supabase
#     supabase.table(TBL_PAYMENTS).insert({
#         "user_id":             user_id,
#         "plan_id":             plan_id,
#         "plan_name":           plan_name,
#         "plan_category":       plan_category,
#         "amount":              price,
#         "razorpay_order_id":   razorpay_order_id,
#         "razorpay_payment_id": razorpay_payment_id,
#         "razorpay_signature":  razorpay_signature,
#         "status":              "success",
#         "created_at":          datetime.utcnow().isoformat(),
#     }).execute()

#     # 4. Save subscription to Supabase
#     supabase.table(TBL_SUBSCRIPTIONS).insert({
#         "user_id":           user_id,
#         "plan_id":           plan_id,
#         "plan_name":         plan_name,
#         "plan_category":     plan_category,
#         "status":            "active",
#         "started_at":        datetime.utcnow().isoformat(),
#         "expires_at":        expires_at,
#         "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "payment_id":        razorpay_payment_id,
#     }).execute()

#     # 5. API plan → top up credits in Supabase
#     if plan_category == "api" and usage_limit:
#         existing = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user_id).execute()
#         if existing.data:
#             current      = existing.data[0]["credits_remaining"]
#             new_credits  = current + int(usage_limit)
#             supabase.table(TBL_USER_API_CREDITS).update(
#                 {"credits_remaining": new_credits}
#             ).eq("user_id", user_id).execute()
#         else:
#             new_credits = int(usage_limit)
#             supabase.table(TBL_USER_API_CREDITS).insert({
#                 "user_id":           user_id,
#                 "credits_remaining": new_credits,
#             }).execute()

#         supabase.table(TBL_USER_CREDIT_TX).insert({
#             "user_id":       user_id,
#             "credits_added": int(usage_limit),
#             "price":         price,
#             "payment_id":    razorpay_payment_id,
#             "date":          datetime.utcnow().isoformat(),
#         }).execute()

#     # 6. Mirror everything to Payload
#     if payload_user_id:
#         # Update Payload user subscription status
#         payload_update: dict = {
#             "subscriptionStatus": "active",
#             "subscriptionPlan":   int(plan_id),
#         }
#         if expires_at:
#             payload_update["subscriptionExpiry"] = expires_at
#         if duration_days:
#             payload_update["billingCycle"] = "monthly"
#         update_payload_user(payload_user_id, payload_update)

#         # Mirror payment to Payload payments collection
#         try:
#             requests.post(
#                 f"{PAYLOAD_URL}/payments",
#                 headers=get_payload_headers(),
#                 json={
#                     "user":          payload_user_id,
#                     "amount":        float(price) if price else 0,
#                     "status":        "success",
#                     "transactionId": razorpay_payment_id,
#                 },
#                 timeout=15,
#             )
#         except Exception as e:
#             logging.error(f"Payload payment mirror error: {e}")

#         # Mirror API credits + transaction to Payload
#         if plan_category == "api" and usage_limit:
#             mirror_api_credits_to_payload(payload_user_id, user_id, new_credits)
#             mirror_credit_transaction_to_payload(
#                 payload_user_id, int(usage_limit), price, razorpay_payment_id
#             )

#         log_to_payload(
#             payload_user_id,
#             "payment_success",
#             f"Plan: {plan_name} | Amount: {price} | TxID: {razorpay_payment_id}",
#         )

#     return {"message": "Payment verified and subscription activated"}


# # ── GENERATE API KEY ──────────────────────────────────────────────────────────
# @app.post("/generate-api-key")
# async def generate_api_key(
#     description:   str = Form(...),
#     authorization: str = Header(None),
# ):
#     user    = get_supabase_user(authorization)
#     user_id = user.id
#     email   = user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     # Save to Supabase
#     supabase.table(TBL_API_KEYS).insert({
#         "api_key":     api_key,
#         "user_id":     user_id,
#         "user_email":  email,
#         "description": description,
#         "active":      True,
#         "created_at":  datetime.utcnow().isoformat(),
#     }).execute()

#     # Mirror to Payload
#     payload_user_id = get_payload_user_id(user_id)
#     if payload_user_id:
#         mirror_api_key_to_payload(payload_user_id, api_key, description)
#         log_to_payload(payload_user_id, "api_key_generated", f"Key: {api_key[:10]}... | Desc: {description}")

#     return {"api_key": api_key}


# # ── MY API KEYS ───────────────────────────────────────────────────────────────
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     keys = supabase.table(TBL_API_KEYS).select("*").eq("user_id", user.id).execute()
#     return keys.data


# # ── MY API CREDITS ────────────────────────────────────────────────────────────
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):
#     user    = get_supabase_user(authorization)
#     credits = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user.id).execute()
#     return credits.data[0] if credits.data else {"credits_remaining": 0}


# # ── MY ACTIVE SUBSCRIPTION ────────────────────────────────────────────────────
# @app.get("/my-subscription")
# async def my_subscription(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     sub  = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     return sub.data[0] if sub.data else {"status": "no_active_subscription"}


# # ── MY CONVERSIONS ────────────────────────────────────────────────────────────
# @app.get("/my-conversions")
# async def my_conversions(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_CONVERSIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data


# # ── MY PAYMENTS ───────────────────────────────────────────────────────────────
# @app.get("/my-payments")
# async def my_payments(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_PAYMENTS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data

# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# import requests
# from datetime import datetime, timedelta
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg

# # ─────────────────────────────────────────
# # CONFIG
# # ─────────────────────────────────────────
# SUPABASE_URL         = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# PAYLOAD_URL          = "http://localhost:3000/api"
# PAYLOAD_SECRET       = "e7be7f67ce829de0fbe6a19c"
# PAYLOAD_HEADERS      = {
#     "Authorization": f"Bearer {PAYLOAD_SECRET}",
#     "Content-Type":  "application/json",
# }

# RAZORPAY_KEY_ID      = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET  = "YvACC6VRdicCORe4qH10Q05l"

# FREE_TRIAL_LIMIT     = 10

# # ─────────────────────────────────────────
# # TABLE NAMES
# # ─────────────────────────────────────────
# TBL_API_KEYS         = "api_keys"
# TBL_USER_API_CREDITS = "api_credits"
# TBL_USER_CREDIT_TX   = "credit_transactions"
# TBL_PAYMENTS         = "payments"
# TBL_SUBSCRIPTIONS    = "subscriptions"
# TBL_CONVERSIONS      = "conversions"
# TBL_PAYLOAD_MAP      = "user_payload_map"

# # ─────────────────────────────────────────
# # CLIENTS
# # ─────────────────────────────────────────
# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
# razorpay_client  = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # ─────────────────────────────────────────
# # APP
# # ─────────────────────────────────────────
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# # ─────────────────────────────────────────
# # HELPERS
# # ─────────────────────────────────────────

# def get_supabase_user(authorization: str):
#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing authorization token")
#     token = authorization.split(" ")[1] if " " in authorization else authorization
#     result = supabase.auth.get_user(token)
#     if not result.user:
#         raise HTTPException(status_code=401, detail="Invalid or expired token")
#     return result.user


# def get_payload_user_id(supabase_user_id: str) -> int | None:
#     """Look up Payload integer user ID from our mapping table."""
#     try:
#         result = supabase.table(TBL_PAYLOAD_MAP)\
#             .select("payload_user_id")\
#             .eq("supabase_user_id", supabase_user_id)\
#             .execute()
#         if result.data:
#             return result.data[0]["payload_user_id"]
#         return None
#     except Exception as e:
#         logging.error(f"Payload map lookup error: {e}")
#         return None


# def get_payload_user(payload_user_id: int) -> dict | None:
#     """Fetch Payload user by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=PAYLOAD_HEADERS,
#             timeout=15,
#         )
#         if res.status_code == 200:
#             return res.json()
#         return None
#     except Exception as e:
#         logging.error(f"Payload user fetch error: {e}")
#         return None


# def create_payload_user(email: str, full_name: str) -> dict | None:
#     """Create a new user in Payload CMS."""
#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/users",
#             headers=PAYLOAD_HEADERS,
#             json={
#                 "email":              email,
#                 "fullName":           full_name,
#                 "password":           uuid.uuid4().hex,
#                 "subscriptionStatus": "trial",
#                 "usageCount":         0,
#             },
#             timeout=15,
#         )
#         return res.json().get("doc")
#     except Exception as e:
#         logging.error(f"Payload user create error: {e}")
#         return None


# def update_payload_user(payload_user_id: int, data: dict):
#     """PATCH a Payload user by integer ID."""
#     try:
#         res = requests.patch(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=PAYLOAD_HEADERS,
#             json=data,
#             timeout=15,
#         )
#         if res.status_code not in (200, 201):
#             logging.error(f"Payload update failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload user update error: {e}")


# def log_to_payload(payload_user_id: int | None, event: str, details: str = ""):
#     """Write a log entry to Payload Logs collection."""
#     try:
#         body = {
#             "event":     event,
#             "details":   details,
#             "createdAt": datetime.utcnow().isoformat(),
#         }
#         if payload_user_id:
#             body["user"] = payload_user_id
#         requests.post(
#             f"{PAYLOAD_URL}/logs",
#             headers=PAYLOAD_HEADERS,
#             json=body,
#             timeout=15,
#         )
#     except Exception as e:
#         logging.error(f"Payload log error: {e}")


# def get_plan_from_payload(plan_id: str) -> dict:
#     """Fetch plan details from Payload CMS by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/plans/{plan_id}",
#             headers=PAYLOAD_HEADERS,
#             timeout=15,
#         )
#         if res.status_code != 200:
#             raise HTTPException(status_code=400, detail="Plan not found in Payload")
#         return res.json()
#     except HTTPException:
#         raise
#     except Exception as e:
#         logging.error(f"Payload plan fetch error: {e}")
#         raise HTTPException(status_code=500, detail="Failed to fetch plan from Payload")


# def mirror_api_key_to_payload(payload_user_id: int, api_key: str, description: str):
#     """Save API key to Payload api-keys collection."""
#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/api-keys",
#             headers=PAYLOAD_HEADERS,
#             json={
#                 "user_id":     str(payload_user_id),
#                 "api_key":     api_key,
#                 "description": description,
#             },
#             timeout=15,
#         )
#         if res.status_code not in (200, 201):
#             logging.error(f"Payload api-key mirror failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload api-key mirror error: {e}")


# def mirror_api_credits_to_payload(payload_user_id: int, user_id: str, credits: int):
#     """Upsert API credits in Payload api-credits collection."""
#     try:
#         # Check if record exists
#         res = requests.get(
#             f"{PAYLOAD_URL}/api-credits",
#             headers=PAYLOAD_HEADERS,
#             params={"where[user_id][equals]": str(payload_user_id)},
#             timeout=15,
#         )
#         data = res.json()
#         docs = data.get("docs", [])

#         if docs:
#             # Update existing
#             doc_id = docs[0]["id"]
#             requests.patch(
#                 f"{PAYLOAD_URL}/api-credits/{doc_id}",
#                 headers=PAYLOAD_HEADERS,
#                 json={"credits_remaining": credits},
#                 timeout=15,
#             )
#         else:
#             # Create new
#             requests.post(
#                 f"{PAYLOAD_URL}/api-credits",
#                 headers=PAYLOAD_HEADERS,
#                 json={
#                     "user_id":           str(payload_user_id),
#                     "credits_remaining": credits,
#                 },
#                 timeout=15,
#             )
#     except Exception as e:
#         logging.error(f"Payload api-credits mirror error: {e}")


# def mirror_credit_transaction_to_payload(payload_user_id: int, credits_added: int, price, payment_id: str):
#     """Save credit transaction to Payload credit-transactions collection."""
#     try:
#         requests.post(
#             f"{PAYLOAD_URL}/credit-transactions",
#             headers=PAYLOAD_HEADERS,
#             json={
#                 "user_id":      str(payload_user_id),
#                 "credits_added": credits_added,
#                 "price":        float(price) if price else 0,
#                 "date":         datetime.utcnow().isoformat(),
#             },
#             timeout=15,
#         )
#     except Exception as e:
#         logging.error(f"Payload credit-transaction mirror error: {e}")


# # ─────────────────────────────────────────
# # ROUTES
# # ─────────────────────────────────────────

# @app.get("/")
# def home():
#     return {"status": "API running"}


# # ── SYNC USER ─────────────────────────────────────────────────────────────────
# @app.post("/sync-user")
# async def sync_user(
#     email:             str = Form(...),
#     full_name:         str = Form(""),
#     supabase_user_id:  str = Form(""),
# ):
#     # Check if mapping already exists
#     existing_map = supabase.table(TBL_PAYLOAD_MAP)\
#         .select("payload_user_id")\
#         .eq("supabase_user_id", supabase_user_id)\
#         .execute()

#     if existing_map.data:
#         return {"message": "User already synced", "payload_id": existing_map.data[0]["payload_user_id"]}

#     # Create user in Payload
#     new_user = create_payload_user(email, full_name)
#     if not new_user:
#         logging.warning(f"Payload sync skipped for {email} — Payload may be down")
#         return {"message": "User registered (Payload sync pending)", "payload_id": None}

#     payload_user_id = new_user["id"]

#     # Save mapping in Supabase
#     supabase.table(TBL_PAYLOAD_MAP).insert({
#         "supabase_user_id": supabase_user_id,
#         "payload_user_id":  payload_user_id,
#         "email":            email,
#     }).execute()

#     log_to_payload(payload_user_id, "user_signup", f"New signup: {email}")

#     return {"message": "User synced to Payload", "payload_id": payload_user_id}


# # ── TRIAL STATUS ──────────────────────────────────────────────────────────────
# @app.get("/trial-status")
# async def trial_status(authorization: str = Header(None)):
#     user            = get_supabase_user(authorization)
#     payload_user_id = get_payload_user_id(user.id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     sub = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("id")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .limit(1)
#         .execute()
#     )
#     has_active_plan = len(sub.data) > 0

#     return {
#         "has_active_plan": has_active_plan,
#         "usage_count":     usage_count,
#         "free_limit":      FREE_TRIAL_LIMIT,
#         "free_remaining":  max(0, FREE_TRIAL_LIMIT - usage_count) if not has_active_plan else None,
#         "trial_exhausted": usage_count >= FREE_TRIAL_LIMIT and not has_active_plan,
#     }


# # ── WEB CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/convert")
# async def convert(
#     file:          UploadFile = File(...),
#     authorization: str        = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     # Check active web subscription
#     sub_result = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user_id)
#         .eq("status", "active")
#         .eq("plan_category", "web")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     active_sub = sub_result.data[0] if sub_result.data else None

#     # Check expiry
#     if active_sub and active_sub.get("expires_at"):
#         expiry = datetime.fromisoformat(active_sub["expires_at"].replace("Z", "+00:00"))
#         if datetime.now(expiry.tzinfo) > expiry:
#             supabase.table(TBL_SUBSCRIPTIONS).update({"status": "expired"}).eq("id", active_sub["id"]).execute()
#             if payload_user_id:
#                 update_payload_user(payload_user_id, {"subscriptionStatus": "expired"})
#             active_sub = None

#     # Gate: no active sub → check trial limit
#     if not active_sub:
#         if usage_count >= FREE_TRIAL_LIMIT:
#             raise HTTPException(
#                 status_code=403,
#                 detail=f"Free trial limit of {FREE_TRIAL_LIMIT} conversions reached. Please buy a plan to continue.",
#             )

#     # Perform conversion
#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     # Save conversion record
#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":         user_id,
#         "type":            "web",
#         "subscription_id": active_sub["id"] if active_sub else None,
#         "created_at":      datetime.utcnow().isoformat(),
#     }).execute()

#     # Increment usageCount in Payload
#     if payload_user_id:
#         new_count = usage_count + 1
#         update_payload_user(payload_user_id, {"usageCount": new_count})
#         log_to_payload(payload_user_id, "web_conversion", f"Conversion #{new_count}")

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── API CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/api/convert")
# async def api_convert(
#     file:      UploadFile = File(...),
#     x_api_key: str        = Header(None),
# ):
#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

#     key_result = supabase.table(TBL_API_KEYS).select("*").eq("api_key", x_api_key).execute()
#     if not key_result.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key_result.data[0]
#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key is inactive")

#     api_user_id = key_doc["user_id"]

#     credits_result = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", api_user_id).execute()
#     if not credits_result.data or credits_result.data[0]["credits_remaining"] <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted. Please top up.")

#     credits_remaining = credits_result.data[0]["credits_remaining"]

#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     new_credits = credits_remaining - 1

#     # Deduct credit in Supabase
#     supabase.table(TBL_USER_API_CREDITS).update(
#         {"credits_remaining": new_credits}
#     ).eq("user_id", api_user_id).execute()

#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":      api_user_id,
#         "type":         "api",
#         "api_key_used": x_api_key,
#         "created_at":   datetime.utcnow().isoformat(),
#     }).execute()

#     # Mirror updated credits to Payload
#     payload_user_id = get_payload_user_id(str(api_user_id))
#     if payload_user_id:
#         mirror_api_credits_to_payload(payload_user_id, str(api_user_id), new_credits)

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── CREATE RAZORPAY ORDER ─────────────────────────────────────────────────────
# @app.post("/create-order")
# async def create_order(
#     plan_id:       str = Form(...),
#     authorization: str = Header(None),
# ):
#     get_supabase_user(authorization)

#     plan   = get_plan_from_payload(plan_id)
#     amount = plan.get("price")
#     if not amount:
#         raise HTTPException(status_code=400, detail="Plan price not set in Payload")

#     order = razorpay_client.order.create({
#         "amount":          int(float(amount) * 100),
#         "currency":        "INR",
#         "payment_capture": 1,
#     })

#     return {"order_id": order["id"], "amount": order["amount"], "key": RAZORPAY_KEY_ID}


# # ── VERIFY PAYMENT ────────────────────────────────────────────────────────────
# @app.post("/verify-payment")
# async def verify_payment(
#     razorpay_order_id:   str = Form(...),
#     razorpay_payment_id: str = Form(...),
#     razorpay_signature:  str = Form(...),
#     plan_id:             str = Form(...),
#     authorization:       str = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)

#     # 1. Verify signature
#     try:
#         razorpay_client.utility.verify_payment_signature({
#             "razorpay_order_id":   razorpay_order_id,
#             "razorpay_payment_id": razorpay_payment_id,
#             "razorpay_signature":  razorpay_signature,
#         })
#     except razorpay.errors.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Payment signature verification failed")

#     # 2. Fetch plan
#     plan          = get_plan_from_payload(plan_id)
#     plan_name     = plan.get("planName")
#     price         = plan.get("price")
#     plan_category = plan.get("planCategory")
#     usage_limit   = plan.get("usageLimit")
#     duration_days = plan.get("durationDays")

#     if not plan_category:
#         raise HTTPException(status_code=400, detail="Plan category missing in Payload")

#     expires_at = None
#     if duration_days:
#         expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

#     # 3. Save payment to Supabase
#     supabase.table(TBL_PAYMENTS).insert({
#         "user_id":             user_id,
#         "plan_id":             plan_id,
#         "plan_name":           plan_name,
#         "plan_category":       plan_category,
#         "amount":              price,
#         "razorpay_order_id":   razorpay_order_id,
#         "razorpay_payment_id": razorpay_payment_id,
#         "razorpay_signature":  razorpay_signature,
#         "status":              "success",
#         "created_at":          datetime.utcnow().isoformat(),
#     }).execute()

#     # 4. Save subscription to Supabase
#     supabase.table(TBL_SUBSCRIPTIONS).insert({
#         "user_id":           user_id,
#         "plan_id":           plan_id,
#         "plan_name":         plan_name,
#         "plan_category":     plan_category,
#         "status":            "active",
#         "started_at":        datetime.utcnow().isoformat(),
#         "expires_at":        expires_at,
#         "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "payment_id":        razorpay_payment_id,
#     }).execute()

#     # 5. API plan → top up credits in Supabase
#     if plan_category == "api" and usage_limit:
#         existing = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user_id).execute()
#         if existing.data:
#             current      = existing.data[0]["credits_remaining"]
#             new_credits  = current + int(usage_limit)
#             supabase.table(TBL_USER_API_CREDITS).update(
#                 {"credits_remaining": new_credits}
#             ).eq("user_id", user_id).execute()
#         else:
#             new_credits = int(usage_limit)
#             supabase.table(TBL_USER_API_CREDITS).insert({
#                 "user_id":           user_id,
#                 "credits_remaining": new_credits,
#             }).execute()

#         supabase.table(TBL_USER_CREDIT_TX).insert({
#             "user_id":       user_id,
#             "credits_added": int(usage_limit),
#             "price":         price,
#             "payment_id":    razorpay_payment_id,
#             "date":          datetime.utcnow().isoformat(),
#         }).execute()

#     # 6. Mirror everything to Payload
#     if payload_user_id:
#         # Update Payload user subscription status
#         payload_update: dict = {
#             "subscriptionStatus": "active",
#             "subscriptionPlan":   int(plan_id),
#         }
#         if expires_at:
#             payload_update["subscriptionExpiry"] = expires_at
#         if duration_days:
#             payload_update["billingCycle"] = "monthly"
#         update_payload_user(payload_user_id, payload_update)

#         # Mirror payment to Payload payments collection
#         try:
#             requests.post(
#                 f"{PAYLOAD_URL}/payments",
#                 headers=PAYLOAD_HEADERS,
#                 json={
#                     "user":          payload_user_id,
#                     "amount":        float(price) if price else 0,
#                     "status":        "success",
#                     "transactionId": razorpay_payment_id,
#                 },
#                 timeout=15,
#             )
#         except Exception as e:
#             logging.error(f"Payload payment mirror error: {e}")

#         # Mirror API credits + transaction to Payload
#         if plan_category == "api" and usage_limit:
#             mirror_api_credits_to_payload(payload_user_id, user_id, new_credits)
#             mirror_credit_transaction_to_payload(
#                 payload_user_id, int(usage_limit), price, razorpay_payment_id
#             )

#         log_to_payload(
#             payload_user_id,
#             "payment_success",
#             f"Plan: {plan_name} | Amount: {price} | TxID: {razorpay_payment_id}",
#         )

#     return {"message": "Payment verified and subscription activated"}


# # ── GENERATE API KEY ──────────────────────────────────────────────────────────
# @app.post("/generate-api-key")
# async def generate_api_key(
#     description:   str = Form(...),
#     authorization: str = Header(None),
# ):
#     user    = get_supabase_user(authorization)
#     user_id = user.id
#     email   = user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     # Save to Supabase
#     supabase.table(TBL_API_KEYS).insert({
#         "api_key":     api_key,
#         "user_id":     user_id,
#         "user_email":  email,
#         "description": description,
#         "active":      True,
#         "created_at":  datetime.utcnow().isoformat(),
#     }).execute()

#     # Mirror to Payload
#     payload_user_id = get_payload_user_id(user_id)
#     if payload_user_id:
#         mirror_api_key_to_payload(payload_user_id, api_key, description)
#         log_to_payload(payload_user_id, "api_key_generated", f"Key: {api_key[:10]}... | Desc: {description}")

#     return {"api_key": api_key}


# # ── MY API KEYS ───────────────────────────────────────────────────────────────
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     keys = supabase.table(TBL_API_KEYS).select("*").eq("user_id", user.id).execute()
#     return keys.data


# # ── MY API CREDITS ────────────────────────────────────────────────────────────
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):
#     user    = get_supabase_user(authorization)
#     credits = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user.id).execute()
#     return credits.data[0] if credits.data else {"credits_remaining": 0}


# # ── MY ACTIVE SUBSCRIPTION ────────────────────────────────────────────────────
# @app.get("/my-subscription")
# async def my_subscription(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     sub  = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     return sub.data[0] if sub.data else {"status": "no_active_subscription"}


# # ── MY CONVERSIONS ────────────────────────────────────────────────────────────
# @app.get("/my-conversions")
# async def my_conversions(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_CONVERSIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data


# # ── MY PAYMENTS ───────────────────────────────────────────────────────────────
# @app.get("/my-payments")
# async def my_payments(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_PAYMENTS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data



# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# import requests
# from datetime import datetime, timedelta
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg

# # ─────────────────────────────────────────
# # CONFIG
# # ─────────────────────────────────────────
# SUPABASE_URL         = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# PAYLOAD_URL          = "http://localhost:3000/api"
# PAYLOAD_SECRET       = "e7be7f67ce829de0fbe6a19c"
# PAYLOAD_HEADERS      = {
#     "Authorization": f"Bearer {PAYLOAD_SECRET}",
#     "Content-Type":  "application/json",
# }

# RAZORPAY_KEY_ID      = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET  = "YvACC6VRdicCORe4qH10Q05l"

# FREE_TRIAL_LIMIT     = 10

# # ─────────────────────────────────────────
# # TABLE NAMES
# # ─────────────────────────────────────────
# # TBL_API_KEYS         = "api_keys"
# # TBL_USER_API_CREDITS = "user_api_credits"
# # TBL_USER_CREDIT_TX   = "user_credit_transactions"
# # TBL_PAYMENTS         = "payments"
# # TBL_SUBSCRIPTIONS    = "subscriptions"
# # TBL_CONVERSIONS      = "conversions"
# # TBL_PAYLOAD_MAP      = "user_payload_map"
# # TBL_API_KEYS         = "api_keys"
# # TBL_USER_API_CREDITS = "app_user_api_credits"
# # TBL_USER_CREDIT_TX   = "app_user_credit_transactions"
# # TBL_PAYMENTS         = "app_payments"
# # TBL_SUBSCRIPTIONS    = "app_subscriptions"
# # TBL_CONVERSIONS      = "app_conversions"
# # TBL_PAYLOAD_MAP      = "app_user_payload_map"
# # ─────────────────────────────────────────
# # TABLE NAMES
# # ─────────────────────────────────────────
# TBL_API_KEYS         = "api_keys"
# TBL_USER_API_CREDITS = "api_credits"
# TBL_USER_CREDIT_TX   = "credit_transactions"
# TBL_PAYMENTS         = "payments"
# TBL_SUBSCRIPTIONS    = "subscriptions"
# TBL_CONVERSIONS      = "conversions"
# TBL_PAYLOAD_MAP      = "user_payload_map"

# # ─────────────────────────────────────────
# # CLIENTS
# # ─────────────────────────────────────────
# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
# razorpay_client  = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # ─────────────────────────────────────────
# # APP
# # ─────────────────────────────────────────
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# # ─────────────────────────────────────────
# # HELPERS
# # ─────────────────────────────────────────

# def get_supabase_user(authorization: str):
#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing authorization token")
#     token = authorization.split(" ")[1] if " " in authorization else authorization
#     result = supabase.auth.get_user(token)
#     if not result.user:
#         raise HTTPException(status_code=401, detail="Invalid or expired token")
#     return result.user


# def get_payload_user_id(supabase_user_id: str) -> int | None:
#     """Look up Payload integer user ID from our mapping table."""
#     try:
#         result = supabase.table(TBL_PAYLOAD_MAP)\
#             .select("payload_user_id")\
#             .eq("supabase_user_id", supabase_user_id)\
#             .execute()
#         if result.data:
#             return result.data[0]["payload_user_id"]
#         return None
#     except Exception as e:
#         logging.error(f"Payload map lookup error: {e}")
#         return None


# def get_payload_user(payload_user_id: int) -> dict | None:
#     """Fetch Payload user by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=PAYLOAD_HEADERS,
#             timeout=15,
#         )
#         if res.status_code == 200:
#             return res.json()
#         return None
#     except Exception as e:
#         logging.error(f"Payload user fetch error: {e}")
#         return None


# def create_payload_user(email: str, full_name: str) -> dict | None:
#     """Create a new user in Payload CMS."""
#     try:
#         res = requests.post(
#             f"{PAYLOAD_URL}/users",
#             headers=PAYLOAD_HEADERS,
#             json={
#                 "email":              email,
#                 "fullName":           full_name,
#                 "password":           uuid.uuid4().hex,
#                 "subscriptionStatus": "trial",
#                 "usageCount":         0,
#             },
#             timeout=15,
#         )
#         return res.json().get("doc")
#     except Exception as e:
#         logging.error(f"Payload user create error: {e}")
#         return None


# def update_payload_user(payload_user_id: int, data: dict):
#     """PATCH a Payload user by integer ID."""
#     try:
#         res = requests.patch(
#             f"{PAYLOAD_URL}/users/{payload_user_id}",
#             headers=PAYLOAD_HEADERS,
#             json=data,
#             timeout=15,
#         )
#         if res.status_code not in (200, 201):
#             logging.error(f"Payload update failed: {res.status_code} {res.text}")
#     except Exception as e:
#         logging.error(f"Payload user update error: {e}")


# def log_to_payload(payload_user_id: int | None, event: str, details: str = ""):
#     """Write a log entry to Payload Logs collection."""
#     try:
#         body = {
#             "event":     event,
#             "details":   details,
#             "createdAt": datetime.utcnow().isoformat(),
#         }
#         if payload_user_id:
#             body["user"] = payload_user_id
#         requests.post(
#             f"{PAYLOAD_URL}/logs",
#             headers=PAYLOAD_HEADERS,
#             json=body,
#             timeout=15,
#         )
#     except Exception as e:
#         logging.error(f"Payload log error: {e}")


# def get_plan_from_payload(plan_id: str) -> dict:
#     """Fetch plan details from Payload CMS by integer ID."""
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/plans/{plan_id}",
#             headers=PAYLOAD_HEADERS,
#             timeout=15,
#         )
#         if res.status_code != 200:
#             raise HTTPException(status_code=400, detail="Plan not found in Payload")
#         return res.json()
#     except HTTPException:
#         raise
#     except Exception as e:
#         logging.error(f"Payload plan fetch error: {e}")
#         raise HTTPException(status_code=500, detail="Failed to fetch plan from Payload")


# # ─────────────────────────────────────────
# # ROUTES
# # ─────────────────────────────────────────

# @app.get("/")
# def home():
#     return {"status": "API running"}


# # ── SYNC USER ─────────────────────────────────────────────────────────────────
# @app.post("/sync-user")
# async def sync_user(
#     email:             str = Form(...),
#     full_name:         str = Form(""),
#     supabase_user_id:  str = Form(""),
# ):
#     # Check if mapping already exists
#     existing_map = supabase.table(TBL_PAYLOAD_MAP)\
#         .select("payload_user_id")\
#         .eq("supabase_user_id", supabase_user_id)\
#         .execute()

#     if existing_map.data:
#         return {"message": "User already synced", "payload_id": existing_map.data[0]["payload_user_id"]}

#     # Create user in Payload
#     new_user = create_payload_user(email, full_name)
#     if not new_user:
#         # Don't fail — just return success without payload sync
#         logging.warning(f"Payload sync skipped for {email} — Payload may be down")
#         return {"message": "User registered (Payload sync pending)", "payload_id": None}

#     payload_user_id = new_user["id"]

#     # Save mapping in Supabase
#     supabase.table(TBL_PAYLOAD_MAP).insert({
#         "supabase_user_id": supabase_user_id,
#         "payload_user_id":  payload_user_id,
#         "email":            email,
#     }).execute()

#     return {"message": "User synced to Payload", "payload_id": payload_user_id}
# # @app.post("/sync-user")
# # async def sync_user(
# #     email:             str = Form(...),
# #     full_name:         str = Form(""),
# #     supabase_user_id:  str = Form(""),
# # ):
# #     """
# #     Called from frontend after Supabase signup.
# #     Creates the user in Payload and saves the ID mapping.
# #     """
# #     # Check if mapping already exists
# #     existing_map = supabase.table(TBL_PAYLOAD_MAP)\
# #         .select("payload_user_id")\
# #         .eq("supabase_user_id", supabase_user_id)\
# #         .execute()

# #     if existing_map.data:
# #         return {"message": "User already synced", "payload_id": existing_map.data[0]["payload_user_id"]}

# #     # Create user in Payload
# #     new_user = create_payload_user(email, full_name)
# #     if not new_user:
# #         raise HTTPException(status_code=500, detail="Failed to create user in Payload")

# #     payload_user_id = new_user["id"]

# #     # Save mapping in Supabase
# #     supabase.table(TBL_PAYLOAD_MAP).insert({
# #         "supabase_user_id": supabase_user_id,
# #         "payload_user_id":  payload_user_id,
# #         "email":            email,
# #     }).execute()

# #     log_to_payload(payload_user_id, "user_signup", f"New signup: {email}")

# #     return {"message": "User synced to Payload", "payload_id": payload_user_id}


# # ── TRIAL STATUS ──────────────────────────────────────────────────────────────
# @app.get("/trial-status")
# async def trial_status(authorization: str = Header(None)):
#     user            = get_supabase_user(authorization)
#     payload_user_id = get_payload_user_id(user.id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     sub = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("id")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .limit(1)
#         .execute()
#     )
#     has_active_plan = len(sub.data) > 0

#     return {
#         "has_active_plan": has_active_plan,
#         "usage_count":     usage_count,
#         "free_limit":      FREE_TRIAL_LIMIT,
#         "free_remaining":  max(0, FREE_TRIAL_LIMIT - usage_count) if not has_active_plan else None,
#         "trial_exhausted": usage_count >= FREE_TRIAL_LIMIT and not has_active_plan,
#     }


# # ── WEB CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/convert")
# async def convert(
#     file:          UploadFile = File(...),
#     authorization: str        = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)
#     usage_count     = 0

#     if payload_user_id:
#         payload_user = get_payload_user(payload_user_id)
#         usage_count  = payload_user.get("usageCount", 0) if payload_user else 0

#     # Check active web subscription
#     sub_result = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user_id)
#         .eq("status", "active")
#         .eq("plan_category", "web")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     active_sub = sub_result.data[0] if sub_result.data else None

#     # Check expiry
#     if active_sub and active_sub.get("expires_at"):
#         expiry = datetime.fromisoformat(active_sub["expires_at"].replace("Z", "+00:00"))
#         if datetime.now(expiry.tzinfo) > expiry:
#             supabase.table(TBL_SUBSCRIPTIONS).update({"status": "expired"}).eq("id", active_sub["id"]).execute()
#             if payload_user_id:
#                 update_payload_user(payload_user_id, {"subscriptionStatus": "expired"})
#             active_sub = None

#     # Gate: no active sub → check trial limit
#     if not active_sub:
#         if usage_count >= FREE_TRIAL_LIMIT:
#             raise HTTPException(
#                 status_code=403,
#                 detail=f"Free trial limit of {FREE_TRIAL_LIMIT} conversions reached. Please buy a plan to continue.",
#             )

#     # Perform conversion
#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     # Save conversion record
#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":         user_id,
#         "type":            "web",
#         "subscription_id": active_sub["id"] if active_sub else None,
#         "created_at":      datetime.utcnow().isoformat(),
#     }).execute()

#     # Increment usageCount in Payload
#     if payload_user_id:
#         new_count = usage_count + 1
#         update_payload_user(payload_user_id, {"usageCount": new_count})
#         log_to_payload(payload_user_id, "web_conversion", f"Conversion #{new_count}")

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── API CONVERSION ────────────────────────────────────────────────────────────
# @app.post("/api/convert")
# async def api_convert(
#     file:      UploadFile = File(...),
#     x_api_key: str        = Header(None),
# ):
#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

#     key_result = supabase.table(TBL_API_KEYS).select("*").eq("api_key", x_api_key).execute()
#     if not key_result.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key_result.data[0]
#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key is inactive")

#     api_user_id = key_doc["user_id"]

#     credits_result = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", api_user_id).execute()
#     if not credits_result.data or credits_result.data[0]["credits_remaining"] <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted. Please top up.")

#     credits_remaining = credits_result.data[0]["credits_remaining"]

#     os.makedirs("temp", exist_ok=True)
#     uid             = str(uuid.uuid4())
#     temp_png_path   = Path(f"temp/{uid}.png")
#     output_svg_path = Path(f"temp/{uid}.svg")

#     with temp_png_path.open("wb") as buf:
#         shutil.copyfileobj(file.file, buf)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     supabase.table(TBL_USER_API_CREDITS).update(
#         {"credits_remaining": credits_remaining - 1}
#     ).eq("user_id", api_user_id).execute()

#     supabase.table(TBL_CONVERSIONS).insert({
#         "user_id":      api_user_id,
#         "type":         "api",
#         "api_key_used": x_api_key,
#         "created_at":   datetime.utcnow().isoformat(),
#     }).execute()

#     return Response(content=svg_result, media_type="image/svg+xml")


# # ── CREATE RAZORPAY ORDER ─────────────────────────────────────────────────────
# @app.post("/create-order")
# async def create_order(
#     plan_id:       str = Form(...),
#     authorization: str = Header(None),
# ):
#     get_supabase_user(authorization)

#     plan   = get_plan_from_payload(plan_id)
#     amount = plan.get("price")
#     if not amount:
#         raise HTTPException(status_code=400, detail="Plan price not set in Payload")

#     order = razorpay_client.order.create({
#         "amount":          int(float(amount) * 100),
#         "currency":        "INR",
#         "payment_capture": 1,
#     })

#     return {"order_id": order["id"], "amount": order["amount"], "key": RAZORPAY_KEY_ID}


# # ── VERIFY PAYMENT ────────────────────────────────────────────────────────────
# @app.post("/verify-payment")
# async def verify_payment(
#     razorpay_order_id:   str = Form(...),
#     razorpay_payment_id: str = Form(...),
#     razorpay_signature:  str = Form(...),
#     plan_id:             str = Form(...),
#     authorization:       str = Header(None),
# ):
#     user            = get_supabase_user(authorization)
#     user_id         = user.id
#     payload_user_id = get_payload_user_id(user_id)

#     # 1. Verify signature
#     try:
#         razorpay_client.utility.verify_payment_signature({
#             "razorpay_order_id":   razorpay_order_id,
#             "razorpay_payment_id": razorpay_payment_id,
#             "razorpay_signature":  razorpay_signature,
#         })
#     except razorpay.errors.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Payment signature verification failed")

#     # 2. Fetch plan
#     plan          = get_plan_from_payload(plan_id)
#     plan_name     = plan.get("planName")
#     price         = plan.get("price")
#     plan_category = plan.get("planCategory")
#     usage_limit   = plan.get("usageLimit")
#     duration_days = plan.get("durationDays")

#     if not plan_category:
#         raise HTTPException(status_code=400, detail="Plan category missing in Payload")

#     expires_at = None
#     if duration_days:
#         expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

#     # 3. Save payment to Supabase
#     supabase.table(TBL_PAYMENTS).insert({
#         "user_id":             user_id,
#         "plan_id":             plan_id,
#         "plan_name":           plan_name,
#         "plan_category":       plan_category,
#         "amount":              price,
#         "razorpay_order_id":   razorpay_order_id,
#         "razorpay_payment_id": razorpay_payment_id,
#         "razorpay_signature":  razorpay_signature,
#         "status":              "success",
#         "created_at":          datetime.utcnow().isoformat(),
#     }).execute()

#     # 4. Save subscription
#     supabase.table(TBL_SUBSCRIPTIONS).insert({
#         "user_id":           user_id,
#         "plan_id":           plan_id,
#         "plan_name":         plan_name,
#         "plan_category":     plan_category,
#         "status":            "active",
#         "started_at":        datetime.utcnow().isoformat(),
#         "expires_at":        expires_at,
#         "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "payment_id":        razorpay_payment_id,
#     }).execute()

#     # 5. API plan → top up credits
#     if plan_category == "api" and usage_limit:
#         existing = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user_id).execute()
#         if existing.data:
#             current = existing.data[0]["credits_remaining"]
#             supabase.table(TBL_USER_API_CREDITS).update(
#                 {"credits_remaining": current + int(usage_limit)}
#             ).eq("user_id", user_id).execute()
#         else:
#             supabase.table(TBL_USER_API_CREDITS).insert({
#                 "user_id":           user_id,
#                 "credits_remaining": int(usage_limit),
#             }).execute()

#         supabase.table(TBL_USER_CREDIT_TX).insert({
#             "user_id":       user_id,
#             "credits_added": int(usage_limit),
#             "price":         price,
#             "payment_id":    razorpay_payment_id,
#             "date":          datetime.utcnow().isoformat(),
#         }).execute()

#     # 6. Update Payload user
#     if payload_user_id:
#         payload_update: dict = {
#             "subscriptionStatus": "active",
#             "subscriptionPlan":   int(plan_id),
#         }
#         if expires_at:
#             payload_update["subscriptionExpiry"] = expires_at
#         if duration_days:
#             payload_update["billingCycle"] = "monthly"

#         update_payload_user(payload_user_id, payload_update)

#         # Save payment in Payload Payments collection
#         try:
#             requests.post(
#                 f"{PAYLOAD_URL}/payments",
#                 headers=PAYLOAD_HEADERS,
#                 json={
#                     "user":          payload_user_id,
#                     "amount":        float(price) if price else 0,
#                     "status":        "success",
#                     "transactionId": razorpay_payment_id,
#                 },
#                 timeout=15,
#             )
#         except Exception as e:
#             logging.error(f"Payload payment record error: {e}")

#         log_to_payload(
#             payload_user_id,
#             "payment_success",
#             f"Plan: {plan_name} | Amount: {price} | TxID: {razorpay_payment_id}",
#         )

#     return {"message": "Payment verified and subscription activated"}


# # ── GENERATE API KEY ──────────────────────────────────────────────────────────
# @app.post("/generate-api-key")
# async def generate_api_key(
#     description:   str = Form(...),
#     authorization: str = Header(None),
# ):
#     user    = get_supabase_user(authorization)
#     user_id = user.id
#     email   = user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     supabase.table(TBL_API_KEYS).insert({
#         "api_key":     api_key,
#         "user_id":     user_id,
#         "user_email":  email,
#         "description": description,
#         "active":      True,
#         "created_at":  datetime.utcnow().isoformat(),
#     }).execute()

#     payload_user_id = get_payload_user_id(user_id)
#     log_to_payload(payload_user_id, "api_key_generated", f"Key: {api_key[:10]}... | Desc: {description}")

#     return {"api_key": api_key}


# # ── MY API KEYS ───────────────────────────────────────────────────────────────
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     keys = supabase.table(TBL_API_KEYS).select("*").eq("user_id", user.id).execute()
#     return keys.data


# # ── MY API CREDITS ────────────────────────────────────────────────────────────
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):
#     user    = get_supabase_user(authorization)
#     credits = supabase.table(TBL_USER_API_CREDITS).select("*").eq("user_id", user.id).execute()
#     return credits.data[0] if credits.data else {"credits_remaining": 0}


# # ── MY ACTIVE SUBSCRIPTION ────────────────────────────────────────────────────
# @app.get("/my-subscription")
# async def my_subscription(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     sub  = (
#         supabase.table(TBL_SUBSCRIPTIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .eq("status", "active")
#         .order("created_at", desc=True)
#         .limit(1)
#         .execute()
#     )
#     return sub.data[0] if sub.data else {"status": "no_active_subscription"}


# # ── MY CONVERSIONS ────────────────────────────────────────────────────────────
# @app.get("/my-conversions")
# async def my_conversions(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_CONVERSIONS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data


# # ── MY PAYMENTS ───────────────────────────────────────────────────────────────
# @app.get("/my-payments")
# async def my_payments(authorization: str = Header(None)):
#     user = get_supabase_user(authorization)
#     data = (
#         supabase.table(TBL_PAYMENTS)
#         .select("*")
#         .eq("user_id", user.id)
#         .order("created_at", desc=True)
#         .execute()
#     )
#     return data.data



# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# from datetime import datetime, timedelta
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg
# import requests

# # -----------------------------
# # Payload Config
# # -----------------------------
# PAYLOAD_URL = "http://localhost:3000/api"
# PAYLOAD_SECRET = "e7be7f67ce829de0fbe6a19c"
# HEADERS = {
#     "Authorization": f"Bearer {PAYLOAD_SECRET}",
#     "Content-Type": "application/json"
# }

# # -----------------------------
# # Supabase Config
# # -----------------------------
# SUPABASE_URL = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# # -----------------------------
# # Razorpay Config
# # -----------------------------
# RAZORPAY_KEY_ID = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET = "YvACC6VRdicCORe4qH10Q05l"

# razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # -----------------------------
# # FastAPI App
# # -----------------------------
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"]
# )


# # -----------------------------
# # Helper: Get Supabase user from token
# # -----------------------------
# def get_supabase_user(authorization: str):
#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")
#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)
#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")
#     return user.user


# # -----------------------------
# # Helper: Fetch plan from Payload
# # -----------------------------
# def get_plan_from_payload(plan_id: str):
#     try:
#         res = requests.get(
#             f"{PAYLOAD_URL}/plans/{plan_id}",
#             headers=HEADERS,
#             timeout=15
#         )
#         return res.json()
#     except Exception as e:
#         logging.error(f"Failed to fetch plan: {e}")
#         raise HTTPException(status_code=500, detail="Failed to fetch plan details")


# # -----------------------------
# # HOME
# # -----------------------------
# @app.get("/")
# def home():
#     return {"status": "API running"}


# # -----------------------------
# # WEB CONVERSION
# # Saves: conversions table
# # Checks: active web subscription, not expired
# # -----------------------------
# @app.post("/convert")
# async def convert(file: UploadFile = File(...), authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     user_id = user.id

#     # Check active web subscription
#     subscription = supabase.table("subscriptions") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .eq("status", "active") \
#         .eq("plan_category", "web") \
#         .order("created_at", desc=True) \
#         .limit(1) \
#         .execute()

#     if not subscription.data:
#         raise HTTPException(status_code=403, detail="No active web subscription found")

#     sub = subscription.data[0]

#     # Check expiry
#     if sub.get("expires_at"):
#         expiry = datetime.fromisoformat(sub["expires_at"].replace("Z", ""))
#         if datetime.utcnow() > expiry:
#             supabase.table("subscriptions") \
#                 .update({"status": "expired"}) \
#                 .eq("id", sub["id"]) \
#                 .execute()
#             raise HTTPException(status_code=403, detail="Subscription expired")

#     # Perform conversion
#     os.makedirs("temp", exist_ok=True)
#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     # Save conversion record
#     supabase.table("conversions").insert({
#         "user_id": user_id,
#         "type": "web",
#         "subscription_id": sub["id"],
#         "created_at": datetime.utcnow().isoformat()
#     }).execute()

#     return Response(content=svg_result, media_type="image/svg+xml")


# # -----------------------------
# # API CONVERSION (via API key)
# # Saves: conversions table, deducts api_credits
# # -----------------------------
# @app.post("/api/convert")
# async def api_convert(file: UploadFile = File(...), x_api_key: str = Header(None)):

#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing API key")

#     # Validate API key
#     key = supabase.table("api_keys") \
#         .select("*") \
#         .eq("api_key", x_api_key) \
#         .execute()

#     if not key.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key.data[0]

#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key inactive")

#     user_id = key_doc["user_id"]

#     # Check credits
#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if not credits.data:
#         raise HTTPException(status_code=403, detail="No API credits available")

#     credit_doc = credits.data[0]
#     credits_remaining = credit_doc["credits_remaining"]

#     if credits_remaining <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted")

#     # Perform conversion
#     os.makedirs("temp", exist_ok=True)
#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     # Deduct 1 credit
#     supabase.table("api_credits") \
#         .update({"credits_remaining": credits_remaining - 1}) \
#         .eq("user_id", user_id) \
#         .execute()

#     # Save conversion record
#     supabase.table("conversions").insert({
#         "user_id": user_id,
#         "type": "api",
#         "api_key_used": x_api_key,
#         "created_at": datetime.utcnow().isoformat()
#     }).execute()

#     return Response(content=svg_result, media_type="image/svg+xml")


# # -----------------------------
# # CREATE RAZORPAY ORDER
# # -----------------------------
# @app.post("/create-order")
# async def create_order(plan_id: str = Form(...), authorization: str = Header(None)):

#     user = get_supabase_user(authorization)

#     # Fetch plan from Payload to get price
#     plan = get_plan_from_payload(plan_id)
#     amount = plan.get("price")

#     if not amount:
#         raise HTTPException(status_code=400, detail="Invalid plan or price not set")

#     order = razorpay_client.order.create({
#         "amount": int(float(amount) * 100),  # paise
#         "currency": "INR",
#         "payment_capture": 1
#     })

#     return {
#         "order_id": order["id"],
#         "amount": order["amount"],
#         "key": RAZORPAY_KEY_ID
#     }


# # -----------------------------
# # VERIFY PAYMENT
# # Saves: payments, subscriptions, api_credits, credit_transactions
# # Updates: user subscription status
# # -----------------------------
# @app.post("/verify-payment")
# async def verify_payment(
#     razorpay_order_id: str = Form(...),
#     razorpay_payment_id: str = Form(...),
#     razorpay_signature: str = Form(...),
#     plan_id: str = Form(...),
#     authorization: str = Header(None)
# ):
#     user = get_supabase_user(authorization)
#     user_id = user.id

#     # Step 1: Verify Razorpay signature
#     try:
#         razorpay_client.utility.verify_payment_signature({
#             "razorpay_order_id": razorpay_order_id,
#             "razorpay_payment_id": razorpay_payment_id,
#             "razorpay_signature": razorpay_signature
#         })
#     except razorpay.errors.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Payment verification failed")

#     # Step 2: Fetch plan from Payload
#     plan = get_plan_from_payload(plan_id)
#     plan_name     = plan.get("planName")
#     price         = plan.get("price")
#     plan_category = plan.get("planCategory")  # 'web' or 'api'
#     usage_limit   = plan.get("usageLimit")
#     duration_days = plan.get("durationDays")

#     if not plan_category:
#         raise HTTPException(status_code=400, detail="Plan category not set in Payload")

#     # Calculate expiry date
#     expires_at = None
#     if duration_days:
#         expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

#     # Step 3: Save payment record
#     supabase.table("payments").insert({
#         "user_id":              user_id,
#         "plan_id":              plan_id,
#         "plan_name":            plan_name,
#         "amount":               price,
#         "razorpay_order_id":    razorpay_order_id,
#         "razorpay_payment_id":  razorpay_payment_id,
#         "razorpay_signature":   razorpay_signature,
#         "status":               "success",
#         "plan_category":        plan_category,
#         "created_at":           datetime.utcnow().isoformat()
#     }).execute()

#     # Step 4: Save subscription record
#     supabase.table("subscriptions").insert({
#         "user_id":           user_id,
#         "plan_id":           plan_id,
#         "plan_name":         plan_name,
#         "plan_category":     plan_category,
#         "status":            "active",
#         "started_at":        datetime.utcnow().isoformat(),
#         "expires_at":        expires_at,
#         "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
#         "payment_id":        razorpay_payment_id
#     }).execute()

#     # Step 5: If API plan → add/top up credits
#     if plan_category == "api" and usage_limit:
#         existing = supabase.table("api_credits") \
#             .select("*") \
#             .eq("user_id", user_id) \
#             .execute()

#         if existing.data:
#             current = existing.data[0]["credits_remaining"]
#             supabase.table("api_credits") \
#                 .update({"credits_remaining": current + int(usage_limit)}) \
#                 .eq("user_id", user_id) \
#                 .execute()
#         else:
#             supabase.table("api_credits").insert({
#                 "user_id":           user_id,
#                 "credits_remaining": int(usage_limit)
#             }).execute()

#         # Save credit transaction record
#         supabase.table("credit_transactions").insert({
#             "user_id":       user_id,
#             "credits_added": int(usage_limit),
#             "price":         price,
#             "payment_id":    razorpay_payment_id,
#             "date":          datetime.utcnow().isoformat()
#         }).execute()

#     return {"message": "Payment verified and subscription activated"}


# # -----------------------------
# # GENERATE API KEY
# # Saves: api_keys table
# # -----------------------------
# @app.post("/generate-api-key")
# async def generate_api_key(description: str = Form(...), authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     user_id = user.id
#     email = user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     supabase.table("api_keys").insert({
#         "api_key":     api_key,
#         "user_id":     user_id,
#         "user_email":  email,
#         "description": description,
#         "active":      True,
#         "created_at":  datetime.utcnow().isoformat()
#     }).execute()

#     return {"api_key": api_key}


# # -----------------------------
# # FETCH MY API KEYS
# # -----------------------------
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     keys = supabase.table("api_keys") \
#         .select("*") \
#         .eq("user_id", user.id) \
#         .execute()

#     return keys.data


# # -----------------------------
# # FETCH MY API CREDITS
# # -----------------------------
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user.id) \
#         .execute()

#     if credits.data:
#         return credits.data[0]

#     return {"credits_remaining": 0}


# # -----------------------------
# # FETCH MY ACTIVE SUBSCRIPTION
# # -----------------------------
# @app.get("/my-subscription")
# async def my_subscription(authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     subscription = supabase.table("subscriptions") \
#         .select("*") \
#         .eq("user_id", user.id) \
#         .eq("status", "active") \
#         .order("created_at", desc=True) \
#         .limit(1) \
#         .execute()

#     if subscription.data:
#         return subscription.data[0]

#     return {"status": "no active subscription"}


# # -----------------------------
# # FETCH MY CONVERSIONS
# # -----------------------------
# @app.get("/my-conversions")
# async def my_conversions(authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     conversions = supabase.table("conversions") \
#         .select("*") \
#         .eq("user_id", user.id) \
#         .order("created_at", desc=True) \
#         .execute()

#     return conversions.data


# # -----------------------------
# # FETCH MY PAYMENT HISTORY
# # -----------------------------
# @app.get("/my-payments")
# async def my_payments(authorization: str = Header(None)):

#     user = get_supabase_user(authorization)
#     payments = supabase.table("payments") \
#         .select("*") \
#         .eq("user_id", user.id) \
#         .order("created_at", desc=True) \
#         .execute()

#     return payments.data





# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# from datetime import datetime
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg
# import requests


# PAYLOAD_URL = "http://localhost:3000/api"
# PAYLOAD_SECRET = "e7be7f67ce829de0fbe6a19c"

# HEADERS = {
#     "Authorization": f"Bearer {PAYLOAD_SECRET}",
#     "Content-Type": "application/json"
# }

# # -----------------------------
# # Supabase Config
# # -----------------------------
# SUPABASE_URL = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# # -----------------------------
# # Razorpay Config
# # -----------------------------
# RAZORPAY_KEY_ID = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET = "YvACC6VRdicCORe4qH10Q05l"

# razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # -----------------------------
# # FastAPI App
# # -----------------------------
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"]
# )


# @app.get("/")
# def home():
#     return {"status": "API running"}


# # -----------------------------
# # SYNC USER TO PAYLOAD
# # Call this from frontend after every login/signup
# # -----------------------------
# @app.post("/sync-user")
# async def sync_user(authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     email = user.user.email
#     full_name = user.user.user_metadata.get("full_name", "") or ""

#     # Check if user already exists in Payload
#     try:
#         existing = requests.get(
#             f"{PAYLOAD_URL}/users",
#             params={"where[email][equals]": email},
#             headers=HEADERS,
#             timeout=15
#         ).json()

#         if existing.get("docs"):
#             return {"status": "already_exists"}

#         # Create user in Payload
#         requests.post(
#             f"{PAYLOAD_URL}/users",
#             json={
#                 "email": email,
#                 "fullName": full_name,
#                 "password": "supabase_" + uuid.uuid4().hex,
#                 "usageCount": 0,
#                 "subscriptionStatus": "trial"
#             },
#             headers=HEADERS,
#             timeout=15
#         )

#         return {"status": "synced"}

#     except Exception as e:
#         logging.error(f"Sync user error: {e}")
#         raise HTTPException(status_code=500, detail="Failed to sync user")


# # -----------------------------
# # WEBSITE CONVERSION
# # -----------------------------
# @app.post("/convert")
# async def convert(file: UploadFile = File(...), authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     os.makedirs("temp", exist_ok=True)

#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     return Response(content=svg_result, media_type="image/svg+xml")


# # -----------------------------
# # GENERATE API KEY
# # -----------------------------
# @app.post("/generate-api-key")
# async def generate_api_key(description: str = Form(...), authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id
#     email = user.user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     # Save in Supabase
#     supabase.table("api_keys").insert({
#         "api_key": api_key,
#         "user_id": user_id,
#         "user_email": email,
#         "description": description,
#         "active": True,
#         "created_at": datetime.utcnow().isoformat()
#     }).execute()

#     # Send copy to Payload admin (for visibility in admin panel)
#     try:
#         requests.post(
#             f"{PAYLOAD_URL}/api-keys",
#             json={
#                 "api_key": api_key,
#                 "user_email": email,
#                 "description": description,
#                 "active": True,
#             },
#             headers=HEADERS,
#             timeout=15
#         )
#     except Exception as e:
#         logging.warning(f"Failed to sync api key to Payload: {e}")

#     return {"api_key": api_key}


# # -----------------------------
# # FETCH USER API KEYS
# # -----------------------------
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     keys = supabase.table("api_keys") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     return keys.data


# # -----------------------------
# # FETCH API CREDITS
# # -----------------------------
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if credits.data:
#         return credits.data[0]

#     return {"credits_remaining": 0}


# # -----------------------------
# # API CONVERSION USING API KEY
# # -----------------------------
# @app.post("/api/convert")
# async def api_convert(file: UploadFile = File(...), x_api_key: str = Header(None)):

#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing API key")

#     key = supabase.table("api_keys") \
#         .select("*") \
#         .eq("api_key", x_api_key) \
#         .execute()

#     if not key.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key.data[0]

#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key inactive")

#     user_id = key_doc["user_id"]

#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if not credits.data:
#         raise HTTPException(status_code=403, detail="No API credits available")

#     credit_doc = credits.data[0]
#     credits_remaining = credit_doc["credits_remaining"]

#     if credits_remaining <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted")

#     os.makedirs("temp", exist_ok=True)

#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     supabase.table("api_credits") \
#         .update({"credits_remaining": credits_remaining - 1}) \
#         .eq("user_id", user_id) \
#         .execute()

#     return Response(content=svg_result, media_type="image/svg+xml")




# from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
# from fastapi.responses import Response
# from fastapi.middleware.cors import CORSMiddleware
# from pathlib import Path
# import shutil
# import os
# import uuid
# import logging
# from datetime import datetime
# import razorpay
# from supabase import create_client, Client
# from .png_to_svg import png_to_svg
# import requests


# PAYLOAD_URL = "http://localhost:3000/api"
# PAYLOAD_SECRET = "e7be7f67ce829de0fbe6a19c"

# HEADERS = {
#     "Authorization": f"Bearer {PAYLOAD_SECRET}",
#     "Content-Type": "application/json"
# }


# # -----------------------------
# # Supabase Config
# # -----------------------------
# SUPABASE_URL = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# # -----------------------------
# # Razorpay Config
# # -----------------------------
# RAZORPAY_KEY_ID = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET = "YvACC6VRdicCORe4qH10Q05l"

# razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# # requests.post(
# #     f"{PAYLOAD_URL}/api-keys",
# #     json={
# #         "api_key": api_key,
# #         "user_id": user_id,
# #         "user_email": email,
# #         "description": description,
# #         "active": True,
# #         "created_at": datetime.utcnow().isoformat()
# #     },
# #     headers=HEADERS
# # )


# # -----------------------------
# # FastAPI App
# # -----------------------------
# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"]
# )


# @app.get("/")
# def home():
#     return {"status": "API running"}


# # -----------------------------
# # WEBSITE CONVERSION
# # -----------------------------
# @app.post("/convert")
# async def convert(file: UploadFile = File(...), authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     os.makedirs("temp", exist_ok=True)

#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     return Response(content=svg_result, media_type="image/svg+xml")


# # -----------------------------
# # GENERATE API KEY
# # -----------------------------
# @app.post("/generate-api-key")
# async def generate_api_key(description: str = Form(...), authorization: str = Header(None)):

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id
#     email = user.user.email

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     # Save in Supabase
#     supabase.table("api_keys").insert({
#         "api_key": api_key,
#         "user_id": user_id,
#         "user_email": email,
#         "description": description,
#         "active": True,
#         "created_at": datetime.utcnow().isoformat()
#     }).execute()

#     # Send copy to Payload admin
#     requests.post(
#         f"{PAYLOAD_URL}/api-keys",
#         json={
#             "api_key": api_key,
#             "user_id": user_id,
#             "user_email": email,
#             "description": description,
#             "active": True,
#             "created_at": datetime.utcnow().isoformat()
#         },
#         headers=HEADERS
#     )
#     print("Sending data to payload...")

#     return {"api_key": api_key}

# # -----------------------------
# # FETCH USER API KEYS
# # -----------------------------
# @app.get("/my-api-keys")
# async def my_api_keys(authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     user_id = user.user.id

#     keys = supabase.table("api_keys") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     return keys.data


# # -----------------------------
# # FETCH API CREDITS
# # -----------------------------
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):

#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     token = authorization.split(" ")[1]

#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if credits.data:
#         return credits.data[0]

#     return {"credits_remaining": 0}

# # -----------------------------
# # API CONVERSION USING API KEY
# # -----------------------------
# @app.post("/api/convert")
# async def api_convert(file: UploadFile = File(...), x_api_key: str = Header(None)):

#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing API key")

#     key = supabase.table("api_keys") \
#         .select("*") \
#         .eq("api_key", x_api_key) \
#         .execute()

#     if not key.data:
#         raise HTTPException(status_code=401, detail="Invalid API key")

#     key_doc = key.data[0]

#     if not key_doc["active"]:
#         raise HTTPException(status_code=403, detail="API key inactive")

#     user_id = key_doc["user_id"]

#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if not credits.data:
#         raise HTTPException(status_code=403, detail="No API credits available")

#     credit_doc = credits.data[0]
#     credits_remaining = credit_doc["credits_remaining"]

#     if credits_remaining <= 0:
#         raise HTTPException(status_code=403, detail="API credits exhausted")

#     os.makedirs("temp", exist_ok=True)

#     unique_id = str(uuid.uuid4())
#     temp_png_path = Path(f"temp/{unique_id}.png")
#     output_svg_path = Path(f"temp/{unique_id}.svg")

#     with temp_png_path.open("wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)

#     png_to_svg(temp_png_path, svg_path=output_svg_path)

#     with output_svg_path.open("rb") as f:
#         svg_result = f.read()

#     supabase.table("api_credits") \
#     .update({"credits_remaining": credits_remaining - 1}) \
#     .eq("user_id", user_id) \
#     .execute()

#     return Response(content=svg_result, media_type="image/svg+xml")



# from fastapi import FastAPI, UploadFile, File, Form
# from pathlib import Path
# import logging
# import shutil
# from fastapi.responses import FileResponse
# from fastapi.responses import Response
# # import requests
# import os
# import uuid
# from .png_to_svg import png_to_svg
# from fastapi.middleware.cors import CORSMiddleware
# from supabase import create_client, Client
# from fastapi import Header, HTTPException
# from datetime import datetime, timedelta
# import razorpay


# SUPABASE_URL = "https://pswlpjqonxynzxsdyjud.supabase.co"
# SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

# supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# # # -----------------------------
# # # Payload Config
# # # -----------------------------

# # PAYLOAD_URL = "http://localhost:3000/api"
# # PAYLOAD_SECRET = "e7be7f67ce829de0fbe6a19c"

# # HEADERS = {
# #     "Authorization": f"Bearer {PAYLOAD_SECRET}",
# #     "Content-Type": "application/json"
# # }

# RAZORPAY_KEY_ID = "rzp_test_RmG7hznjlclBga"
# RAZORPAY_KEY_SECRET = "YvACC6VRdicCORe4qH10Q05l"

# razorpay_client = razorpay.Client(
#     auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
# )

# # # -----------------------------
# # # Helper Function
# # # -----------------------------

# # def get_user_by_email(email):
# #     response = requests.get(
# #         f"{PAYLOAD_URL}/users",
# #         params={"where[email][equals]": email},
# #         headers=HEADERS   # ✅ important
# #     )
# #     return response.json()


# # # -----------------------------
# # # FastAPI App
# # # -----------------------------

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# @app.get("/")
# def home():
#     return {"status": "API running"}


# # @app.post("/sync-user")
# # def sync_user(
# #     email: str = Form(...),
# #     full_name: str = Form("")
# # ):
# #     try:
# #         existing = requests.get(
# #             f"{PAYLOAD_URL}/users",
# #             params={"where[email][equals]": email},
# #             headers=HEADERS,
# #             timeout=15
# #         ).json()

# #         if existing.get("docs"):
# #             return {"status": "User already exists in Payload"}
        
# #         expiry_date = (datetime.utcnow() + timedelta(days=7)).isoformat()
# #         response = requests.post(
# #             f"{PAYLOAD_URL}/users",
# #             json={
# #                 "email": email,
# #                 "password": "temp123456",
# #                 "fullName": full_name,
# #                 "usageCount": 0,
# #                 "subscriptionStatus": "trial",
# #                 "subscriptionExpiry": expiry_date
                
# #             },
# #             headers=HEADERS,
# #             timeout=15
# #         )

# #         return response.json()

# #     except Exception as e:
# #         print("Sync error:", str(e))
# #         return {"error": str(e)}



# @app.post("/convert")
# async def convert(
#     file: UploadFile = File(...),
#     authorization: str = Header(None)
# ):
#     if not authorization:
#         raise HTTPException(status_code=401, detail="Missing token")

#     try:
#         # 🔐 1. Verify Supabase token
#         token = authorization.split(" ")[1]
#         user_response = supabase.auth.get_user(token)

#         if not user_response.user:
#             raise HTTPException(status_code=401, detail="Invalid user")

#         email = user_response.user.email
#         print("EMAIL FROM TOKEN:", email)

#         # 🔎 2. Fetch user from Payload
#         user_id = user_response.user.id
#         plan_id = user_doc.get("subscriptionPlan")

# # If trial user → limit = 10
#         if user_doc.get("subscriptionStatus") == "trial":
#             usage_limit = 10
#             duration_days = None
#             plan_category = "web"
        
#         else:
#             if not plan_id:
#                 raise HTTPException(status_code=403, detail="No subscription plan assigned.")
        
#             # plans_response = requests.get(
#             #     f"{PAYLOAD_URL}/plans/{plan_id}",
#             #     headers=HEADERS,
#             #     timeout=15
#             # ).json()

#             # usage_limit = plans_response.get("doc", {}).get("usageLimit")
#             # duration_days = plans_response.get("doc", {}).get("durationDays")
#             # plan_category = plans_response.get("doc", {}).get("planCategory")

#         usage_count = user_doc.get("usageCount", 0)

#         if plan_category != "web":
#            raise HTTPException(status_code=403, detail="Invalid plan for website conversion")

#         usage_count = user_doc.get("usageCount", 0)
#         subscription_status = user_doc.get("subscriptionStatus")
#         expiry = user_doc.get("subscriptionExpiry")

#         # 1️⃣ Must be active
#         if subscription_status not in ["active", "trial"]:
#           raise HTTPException(status_code=403, detail="Subscription inactive.")

#         # 2️⃣ If plan has duration (Web Monthly)
#         if duration_days:
#             if not expiry:
#                 raise HTTPException(status_code=403, detail="Subscription expiry not set.")

#             expiry_date = datetime.fromisoformat(expiry.replace("Z", ""))
#             if datetime.utcnow() > expiry_date:
#                 # requests.patch(
#                 #     f"{PAYLOAD_URL}/users/{user_id}",
#                 #     json={"subscriptionStatus": "expired"},
#                 #     headers=HEADERS,
#                 #     timeout=15
#                 # )
#                 raise HTTPException(status_code=403, detail="Subscription expired.")

#         # 3️⃣ If plan has usage limit (Trial / API)
#         if usage_limit is not None:
#             if usage_count >= usage_limit:
#                 raise HTTPException(status_code=403, detail="Usage limit reached.")

#         # 🖼 4. Perform conversion
#         os.makedirs("temp", exist_ok=True)

#         unique_id = str(uuid.uuid4())
#         temp_png_path = Path(f"temp/{unique_id}.png")
#         output_svg_path = Path(f"temp/{unique_id}.svg")

#         with temp_png_path.open("wb") as buffer:
#             shutil.copyfileobj(file.file, buffer)

#         png_to_svg(temp_png_path, svg_path=output_svg_path)

#         with output_svg_path.open("rb") as f:
#             svg_result = f.read()

#         # 🔢 5. Increment usageCount
#         # requests.patch(
#         #     f"{PAYLOAD_URL}/users/{user_id}",
#         #     json={"usageCount": usage_count + 1},
#         #     headers=HEADERS,
#         #     timeout=15
#         # )

#         return Response(content=svg_result, media_type="image/svg+xml")

#     except HTTPException:
#         raise

#     except Exception as e:
#         logging.error(e)
#         raise HTTPException(status_code=500, detail="Conversion failed")
    
# @app.post("/create-order")
# async def create_order(plan_id: str = Form(...)):
#     try:
#         # pricing_response = requests.get(
#         #     f"{PAYLOAD_URL}/plans/{plan_id}",
#         #     headers=HEADERS,
#         #     timeout=15
#         # ).json()

#         # print("Pricing response:", pricing_response)

#         # amount = pricing_response.get("price")

#         if not amount:
#             raise HTTPException(status_code=400, detail="Invalid plan")

#         order = razorpay_client.order.create({
#             "amount": int(float(amount) * 100),
#             "currency": "USD",
#             "payment_capture": 1
#         })

#         return {
#             "order_id": order["id"],
#             "amount": order["amount"],
#             "key": RAZORPAY_KEY_ID
#         }

#     except Exception as e:
#         print("FULL ORDER ERROR:", repr(e))
#         raise HTTPException(status_code=500, detail=str(e))

#     except Exception as e:
#         print("Order error:", str(e))
#         raise HTTPException(status_code=500, detail="Order creation failed")

# @app.post("/verify-payment")
# async def verify_payment(
#     razorpay_order_id: str = Form(...),
#     razorpay_payment_id: str = Form(...),
#     razorpay_signature: str = Form(...),
#     plan_id: str = Form(...),
#     authorization: str = Header(None)
# ):
#     try:
#         # 🔐 Verify Supabase user
#         if not authorization:
#             raise HTTPException(status_code=401, detail="Missing token")

#         token = authorization.split(" ")[1]
#         user_response = supabase.auth.get_user(token)

#         if not user_response.user:
#             raise HTTPException(status_code=401, detail="Invalid user")

#         email = user_response.user.email

#         # 🔎 Get user from Payload
#         # payload_user = requests.get(
#         #     f"{PAYLOAD_URL}/users",
#         #     params={"where[email][equals]": email},
#         #     headers=HEADERS,
#         #     timeout=15
#         # ).json()

#         # if not payload_user.get("docs"):
#         #     raise HTTPException(status_code=404, detail="User not found")

#         # user_doc = payload_user["docs"][0]
#         # user_id = user_doc["id"]

#         # 🔐 Razorpay Signature Verification
#         generated_signature = razorpay_client.utility.verify_payment_signature({
#             "razorpay_order_id": razorpay_order_id,
#             "razorpay_payment_id": razorpay_payment_id,
#             "razorpay_signature": razorpay_signature
#         })

#         # If no exception → payment is valid

#         # 🔎 Fetch pricing
#         # pricing_response = requests.get(
#         #     f"{PAYLOAD_URL}/plans/{plan_id}",
#         #     headers=HEADERS,
#         #     timeout=15
#         # ).json()

#         # # 🔎 Fetch plan data
#         # plan_doc = pricing_response.get("doc", {})

#         # duration_days = plan_doc.get("durationDays")
#         # usage_limit = plan_doc.get("usageLimit")
#         # plan_category = plan_doc.get("planCategory")

#         expiry_date = None
#         if duration_days:
#             expiry_date = (datetime.utcnow() + timedelta(days=duration_days)).isoformat()

#         # 🔥 Handle WEB subscription
#         if plan_category == "web":

#             # requests.patch(
#             #     f"{PAYLOAD_URL}/users/{user_id}",
#             #     json={
#             #         "subscriptionStatus": "active",
#             #         "subscriptionPlan": plan_id,
#             #         "subscriptionExpiry": expiry_date,
#             #         "usageCount": 0
#             #     },
#             #     headers=HEADERS,
#             #     timeout=15
#             # )

#         # 🔥 Handle API credit purchase
#         elif plan_category == "api":

#             # credits_response = requests.get(
#             #     f"{PAYLOAD_URL}/api-credits",
#             #     params={"where[user_id][equals]": user_id},
#             #     headers=HEADERS,
#             #     timeout=15
#             # ).json()

#             # if credits_response.get("docs"):

#             #     credit_doc = credits_response["docs"][0]

#             #     # requests.patch(
#             #     #     f"{PAYLOAD_URL}/api-credits/{credit_doc['id']}",
#             #     #     json={
#             #     #         "credits_remaining": credit_doc["credits_remaining"] + usage_limit
#             #     #     },
#             #     #     headers=HEADERS,
#             #     #     timeout=15
#             #     # )

#             # else:

#             #     requests.post(
#             #         f"{PAYLOAD_URL}/api-credits",
#             #         json={
#             #             "user_id": user_id,
#             #             "credits_remaining": usage_limit
#             #         },
#             #         headers=HEADERS,
#             #         timeout=15
#             #     )
#         supabase.table("credit_transactions").insert({
#              "user_id": user_id,
#              "credits_added": usage_limit,
#              "price": plan_doc.get("price"),
#              "date": datetime.utcnow().isoformat()
#          }).execute()

#         return {"message": "Payment verified and subscription activated"}

#     except razorpay.errors.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Payment verification failed")

#     except Exception as e:
#         logging.error(e)
#         raise HTTPException(status_code=500, detail="Payment processing failed")
    

# # to generate api keys
# @app.post("/generate-api-key")
# async def generate_api_key(
#     description: str = Form(...),
#     authorization: str = Header(None)
# ):

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     if not user.user:
#         raise HTTPException(status_code=401, detail="Invalid user")

#     user_id = user.user.id

#     api_key = "sk_" + uuid.uuid4().hex[:16]

#     supabase.table("api_keys").insert({
#         "api_key": api_key,
#         "user_id": user_id,
#         "description": description,
#         "active": True,
#         "created_at": datetime.utcnow().isoformat()
#     }).execute()

#     return {"api_key": api_key}


# # to fetch the api keys
# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     user_id = user.user.id

#     credits = supabase.table("api_credits") \
#         .select("*") \
#         .eq("user_id", user_id) \
#         .execute()

#     if credits.data:
#         return credits.data[0]

#     return {"credits_remaining": 0}
    
# @app.post("/api/convert")
# async def api_convert(
#     file: UploadFile = File(...),
#     x_api_key: str = Header(None)
# ):
#     try:

#         if not x_api_key:
#             raise HTTPException(status_code=401, detail="Missing API key")

#         # 1️⃣ Validate API key
#         key_response = supabase.table("api_keys") \
#             .select("*") \
#             .eq("api_key", x_api_key) \
#             .execute()

#         if not key_response.data:
#           raise HTTPException(status_code=401, detail="Invalid API key")

#         key_doc = key_response.data[0]

#         # if not key_response.get("docs"):
#         #     raise HTTPException(status_code=401, detail="Invalid API key")

#         # key_doc = key_response["docs"][0]

#         if not key_doc.get("active"):
#             raise HTTPException(status_code=403, detail="API key inactive")

#         user_id = key_doc.get("user_id")
#         if not user_id:
#            raise HTTPException(status_code=403, detail="API key not linked to user")

#         # 2️⃣ Check API credits
#         credits = supabase.table("api_credits") \
#             .select("*") \
#             .eq("user_id", user_id) \
#             .execute()

#         if not credits_response.get("docs"):
#             raise HTTPException(status_code=403, detail="No API credits available")

#         credit_doc = credits_response["docs"][0]
#         credits_remaining = credit_doc.get("credits_remaining", 0)

#         if credits_remaining <= 0:
#             raise HTTPException(status_code=403, detail="API credits exhausted")

#         # 3️⃣ Convert PNG → SVG
#         os.makedirs("temp", exist_ok=True)

#         unique_id = str(uuid.uuid4())
#         temp_png_path = Path(f"temp/{unique_id}.png")
#         output_svg_path = Path(f"temp/{unique_id}.svg")

#         with temp_png_path.open("wb") as buffer:
#             shutil.copyfileobj(file.file, buffer)

#         png_to_svg(temp_png_path, svg_path=output_svg_path)

#         with output_svg_path.open("rb") as f:
#             svg_result = f.read()

#         # 4️⃣ Deduct 1 credit
#         supabase.table("api_credits") \
#             .update({"credits_remaining": credits_remaining - 1}) \
#             .eq("id", credit_doc["id"]) \
#             .execute()

#         return Response(content=svg_result, media_type="image/svg+xml")

#     except HTTPException:
#         raise

#     except Exception as e:
#         print("API convert error:", str(e))
#         raise HTTPException(status_code=500, detail="API conversion failed")

# @app.get("/my-api-credits")
# async def my_api_credits(authorization: str = Header(None)):

#     token = authorization.split(" ")[1]
#     user = supabase.auth.get_user(token)

#     email = user.user.email

#     # payload_user = requests.get(
#     #     f"{PAYLOAD_URL}/users",
#     #     params={"where[email][equals]": email},
#     #     headers=HEADERS
#     # ).json()

#     # user_id = payload_user["docs"][0]["id"]

#     # credits = requests.get(
#     #     f"{PAYLOAD_URL}/api-credits",
#     #     params={"where[user_id][equals]": user_id},
#     #     headers=HEADERS
#     # ).json()

#     if credits.get("docs"):
#         return credits["docs"][0]

#     return {"credits_remaining": 0}