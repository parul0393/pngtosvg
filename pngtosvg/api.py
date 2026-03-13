from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import shutil
import os
import uuid
import logging
from datetime import datetime, timedelta
import razorpay
from supabase import create_client, Client
from .png_to_svg import png_to_svg
import requests

# -----------------------------
# Payload Config
# -----------------------------
PAYLOAD_URL = "http://localhost:3000/api"
PAYLOAD_SECRET = "e7be7f67ce829de0fbe6a19c"
HEADERS = {
    "Authorization": f"Bearer {PAYLOAD_SECRET}",
    "Content-Type": "application/json"
}

# -----------------------------
# Supabase Config
# -----------------------------
SUPABASE_URL = "https://pswlpjqonxynzxsdyjud.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBzd2xwanFvbnh5bnp4c2R5anVkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAwNzQxMCwiZXhwIjoyMDg3NTgzNDEwfQ.nWsrDi03y4c_Tde4TPoZJ5nUq55zorQPEmivr3UqR5U"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# -----------------------------
# Razorpay Config
# -----------------------------
RAZORPAY_KEY_ID = "rzp_test_RmG7hznjlclBga"
RAZORPAY_KEY_SECRET = "YvACC6VRdicCORe4qH10Q05l"

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# -----------------------------
# FastAPI App
# -----------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# -----------------------------
# Helper: Get Supabase user from token
# -----------------------------
def get_supabase_user(authorization: str):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ")[1]
    user = supabase.auth.get_user(token)
    if not user.user:
        raise HTTPException(status_code=401, detail="Invalid user")
    return user.user


# -----------------------------
# Helper: Fetch plan from Payload
# -----------------------------
def get_plan_from_payload(plan_id: str):
    try:
        res = requests.get(
            f"{PAYLOAD_URL}/plans/{plan_id}",
            headers=HEADERS,
            timeout=5
        )
        return res.json()
    except Exception as e:
        logging.error(f"Failed to fetch plan: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch plan details")


# -----------------------------
# HOME
# -----------------------------
@app.get("/")
def home():
    return {"status": "API running"}


# -----------------------------
# WEB CONVERSION
# Saves: conversions table
# Checks: active web subscription, not expired
# -----------------------------
@app.post("/convert")
async def convert(file: UploadFile = File(...), authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    user_id = user.id

    # Check active web subscription
    subscription = supabase.table("subscriptions") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("status", "active") \
        .eq("plan_category", "web") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if not subscription.data:
        raise HTTPException(status_code=403, detail="No active web subscription found")

    sub = subscription.data[0]

    # Check expiry
    if sub.get("expires_at"):
        expiry = datetime.fromisoformat(sub["expires_at"].replace("Z", ""))
        if datetime.utcnow() > expiry:
            supabase.table("subscriptions") \
                .update({"status": "expired"}) \
                .eq("id", sub["id"]) \
                .execute()
            raise HTTPException(status_code=403, detail="Subscription expired")

    # Perform conversion
    os.makedirs("temp", exist_ok=True)
    unique_id = str(uuid.uuid4())
    temp_png_path = Path(f"temp/{unique_id}.png")
    output_svg_path = Path(f"temp/{unique_id}.svg")

    with temp_png_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    png_to_svg(temp_png_path, svg_path=output_svg_path)

    with output_svg_path.open("rb") as f:
        svg_result = f.read()

    # Save conversion record
    supabase.table("conversions").insert({
        "user_id": user_id,
        "type": "web",
        "subscription_id": sub["id"],
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    return Response(content=svg_result, media_type="image/svg+xml")


# -----------------------------
# API CONVERSION (via API key)
# Saves: conversions table, deducts api_credits
# -----------------------------
@app.post("/api/convert")
async def api_convert(file: UploadFile = File(...), x_api_key: str = Header(None)):

    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    # Validate API key
    key = supabase.table("api_keys") \
        .select("*") \
        .eq("api_key", x_api_key) \
        .execute()

    if not key.data:
        raise HTTPException(status_code=401, detail="Invalid API key")

    key_doc = key.data[0]

    if not key_doc["active"]:
        raise HTTPException(status_code=403, detail="API key inactive")

    user_id = key_doc["user_id"]

    # Check credits
    credits = supabase.table("api_credits") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()

    if not credits.data:
        raise HTTPException(status_code=403, detail="No API credits available")

    credit_doc = credits.data[0]
    credits_remaining = credit_doc["credits_remaining"]

    if credits_remaining <= 0:
        raise HTTPException(status_code=403, detail="API credits exhausted")

    # Perform conversion
    os.makedirs("temp", exist_ok=True)
    unique_id = str(uuid.uuid4())
    temp_png_path = Path(f"temp/{unique_id}.png")
    output_svg_path = Path(f"temp/{unique_id}.svg")

    with temp_png_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    png_to_svg(temp_png_path, svg_path=output_svg_path)

    with output_svg_path.open("rb") as f:
        svg_result = f.read()

    # Deduct 1 credit
    supabase.table("api_credits") \
        .update({"credits_remaining": credits_remaining - 1}) \
        .eq("user_id", user_id) \
        .execute()

    # Save conversion record
    supabase.table("conversions").insert({
        "user_id": user_id,
        "type": "api",
        "api_key_used": x_api_key,
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    return Response(content=svg_result, media_type="image/svg+xml")


# -----------------------------
# CREATE RAZORPAY ORDER
# -----------------------------
@app.post("/create-order")
async def create_order(plan_id: str = Form(...), authorization: str = Header(None)):

    user = get_supabase_user(authorization)

    # Fetch plan from Payload to get price
    plan = get_plan_from_payload(plan_id)
    amount = plan.get("price")

    if not amount:
        raise HTTPException(status_code=400, detail="Invalid plan or price not set")

    order = razorpay_client.order.create({
        "amount": int(float(amount) * 100),  # paise
        "currency": "INR",
        "payment_capture": 1
    })

    return {
        "order_id": order["id"],
        "amount": order["amount"],
        "key": RAZORPAY_KEY_ID
    }


# -----------------------------
# VERIFY PAYMENT
# Saves: payments, subscriptions, api_credits, credit_transactions
# Updates: user subscription status
# -----------------------------
@app.post("/verify-payment")
async def verify_payment(
    razorpay_order_id: str = Form(...),
    razorpay_payment_id: str = Form(...),
    razorpay_signature: str = Form(...),
    plan_id: str = Form(...),
    authorization: str = Header(None)
):
    user = get_supabase_user(authorization)
    user_id = user.id

    # Step 1: Verify Razorpay signature
    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature
        })
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    # Step 2: Fetch plan from Payload
    plan = get_plan_from_payload(plan_id)
    plan_name     = plan.get("planName")
    price         = plan.get("price")
    plan_category = plan.get("planCategory")  # 'web' or 'api'
    usage_limit   = plan.get("usageLimit")
    duration_days = plan.get("durationDays")

    if not plan_category:
        raise HTTPException(status_code=400, detail="Plan category not set in Payload")

    # Calculate expiry date
    expires_at = None
    if duration_days:
        expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()

    # Step 3: Save payment record
    supabase.table("payments").insert({
        "user_id":              user_id,
        "plan_id":              plan_id,
        "plan_name":            plan_name,
        "amount":               price,
        "razorpay_order_id":    razorpay_order_id,
        "razorpay_payment_id":  razorpay_payment_id,
        "razorpay_signature":   razorpay_signature,
        "status":               "success",
        "plan_category":        plan_category,
        "created_at":           datetime.utcnow().isoformat()
    }).execute()

    # Step 4: Save subscription record
    supabase.table("subscriptions").insert({
        "user_id":           user_id,
        "plan_id":           plan_id,
        "plan_name":         plan_name,
        "plan_category":     plan_category,
        "status":            "active",
        "started_at":        datetime.utcnow().isoformat(),
        "expires_at":        expires_at,
        "credits_total":     int(usage_limit) if plan_category == "api" and usage_limit else None,
        "credits_remaining": int(usage_limit) if plan_category == "api" and usage_limit else None,
        "payment_id":        razorpay_payment_id
    }).execute()

    # Step 5: If API plan → add/top up credits
    if plan_category == "api" and usage_limit:
        existing = supabase.table("api_credits") \
            .select("*") \
            .eq("user_id", user_id) \
            .execute()

        if existing.data:
            current = existing.data[0]["credits_remaining"]
            supabase.table("api_credits") \
                .update({"credits_remaining": current + int(usage_limit)}) \
                .eq("user_id", user_id) \
                .execute()
        else:
            supabase.table("api_credits").insert({
                "user_id":           user_id,
                "credits_remaining": int(usage_limit)
            }).execute()

        # Save credit transaction record
        supabase.table("credit_transactions").insert({
            "user_id":       user_id,
            "credits_added": int(usage_limit),
            "price":         price,
            # "payment_id":    razorpay_payment_id,
            "date":          datetime.utcnow().isoformat()
        }).execute()

    return {"message": "Payment verified and subscription activated"}


# -----------------------------
# GENERATE API KEY
# Saves: api_keys table
# -----------------------------
@app.post("/generate-api-key")
async def generate_api_key(description: str = Form(...), authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    user_id = user.id
    email = user.email

    api_key = "sk_" + uuid.uuid4().hex[:16]

    supabase.table("api_keys").insert({
        "api_key":     api_key,
        "user_id":     user_id,
        "user_email":  email,
        "description": description,
        "active":      True,
        "created_at":  datetime.utcnow().isoformat()
    }).execute()

    return {"api_key": api_key}


# -----------------------------
# FETCH MY API KEYS
# -----------------------------
@app.get("/my-api-keys")
async def my_api_keys(authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    keys = supabase.table("api_keys") \
        .select("*") \
        .eq("user_id", user.id) \
        .execute()

    return keys.data


# -----------------------------
# FETCH MY API CREDITS
# -----------------------------
@app.get("/my-api-credits")
async def my_api_credits(authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    credits = supabase.table("api_credits") \
        .select("*") \
        .eq("user_id", user.id) \
        .execute()

    if credits.data:
        return credits.data[0]

    return {"credits_remaining": 0}


# -----------------------------
# FETCH MY ACTIVE SUBSCRIPTION
# -----------------------------
@app.get("/my-subscription")
async def my_subscription(authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    subscription = supabase.table("subscriptions") \
        .select("*") \
        .eq("user_id", user.id) \
        .eq("status", "active") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if subscription.data:
        return subscription.data[0]

    return {"status": "no active subscription"}


# -----------------------------
# FETCH MY CONVERSIONS
# -----------------------------
@app.get("/my-conversions")
async def my_conversions(authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    conversions = supabase.table("conversions") \
        .select("*") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .execute()

    return conversions.data


# -----------------------------
# FETCH MY PAYMENT HISTORY
# -----------------------------
@app.get("/my-payments")
async def my_payments(authorization: str = Header(None)):

    user = get_supabase_user(authorization)
    payments = supabase.table("payments") \
        .select("*") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .execute()

    return payments.data





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
#             timeout=5
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
#             timeout=5
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
#             timeout=5
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
# #             timeout=5
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
# #             timeout=5
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
#             #     timeout=5
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
#                 #     timeout=5
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
#         #     timeout=5
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
#         #     timeout=5
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
#         #     timeout=5
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
#         #     timeout=5
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
#             #     timeout=5
#             # )

#         # 🔥 Handle API credit purchase
#         elif plan_category == "api":

#             # credits_response = requests.get(
#             #     f"{PAYLOAD_URL}/api-credits",
#             #     params={"where[user_id][equals]": user_id},
#             #     headers=HEADERS,
#             #     timeout=5
#             # ).json()

#             # if credits_response.get("docs"):

#             #     credit_doc = credits_response["docs"][0]

#             #     # requests.patch(
#             #     #     f"{PAYLOAD_URL}/api-credits/{credit_doc['id']}",
#             #     #     json={
#             #     #         "credits_remaining": credit_doc["credits_remaining"] + usage_limit
#             #     #     },
#             #     #     headers=HEADERS,
#             #     #     timeout=5
#             #     # )

#             # else:

#             #     requests.post(
#             #         f"{PAYLOAD_URL}/api-credits",
#             #         json={
#             #             "user_id": user_id,
#             #             "credits_remaining": usage_limit
#             #         },
#             #         headers=HEADERS,
#             #         timeout=5
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