import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import (
    FRONTEND_URL,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(prefix="/api/stripe", tags=["stripe"])


class SubscriptionCheckoutRequest(BaseModel):
    user_id: str


class PayPerUseCheckoutRequest(BaseModel):
    user_id: str
    job_id: str  # 動画生成ジョブのID（決済後に処理を紐付けるため）


@router.post("/create-subscription-checkout")
async def create_subscription_checkout(body: SubscriptionCheckoutRequest):
    """
    サブスク用のStripe Checkoutセッションを作成して、決済URLを返す。
    フロントエンドはこのURLにリダイレクトする。
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[
                {
                    "price_data": {
                        "currency": "jpy",
                        "product_data": {
                            "name": "チキンクラウド プラン",
                            "description": "playout が無制限で使えるサブスクリプション",
                        },
                        "recurring": {"interval": "month"},
                        "unit_amount": 3000,  # 円（JPYは小数なし）
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{FRONTEND_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/payment/cancel",
            metadata={"user_id": body.user_id},
            # 日本語UIにする
            locale="ja",
        )
        return {"checkout_url": session.url}
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/create-pay-per-use-checkout")
async def create_pay_per_use_checkout(body: PayPerUseCheckoutRequest):
    """
    動画生成1回分（1,000円）のStripe Checkoutセッションを作成して決済URLを返す。
    決済完了後、job_idをもとに動画生成を開始する。
    """
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "jpy",
                        "product_data": {
                            "name": "flowout 動画生成",
                            "description": "AI動画を1本生成します",
                        },
                        "unit_amount": 1000,
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{FRONTEND_URL}/flowout/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/flowout/cancel",
            metadata={"user_id": body.user_id, "job_id": body.job_id},
            locale="ja",
        )
        return {"checkout_url": session.url}
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/subscription-status/{customer_id}")
async def get_subscription_status(customer_id: str):
    """指定顧客のサブスク状態を返す。"""
    try:
        subscriptions = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
        is_active = len(subscriptions.data) > 0
        return {"is_active": is_active}
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    """
    Stripeからのイベント通知を受け取る。
    署名を検証してから処理する。
    """
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        job_id = session.get("metadata", {}).get("job_id")
        customer_id = session.get("customer")
        mode = session.get("mode")

        if mode == "subscription":
            # TODO: DBにcustomer_idとuser_idを紐付けてサブスク有効化
            print(f"[Stripe] サブスク開始: user_id={user_id}, customer_id={customer_id}")
        elif mode == "payment" and job_id:
            # TODO: job_idの動画生成キューを開始する
            print(f"[Stripe] 動画生成決済完了: user_id={user_id}, job_id={job_id}")

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        # TODO: DBのサブスク状態をキャンセル済みに更新する
        print(f"[Stripe] サブスクキャンセル: customer_id={customer_id}")

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        # TODO: 支払い失敗の通知をユーザーに送る
        print(f"[Stripe] 支払い失敗: customer_id={customer_id}")

    return JSONResponse({"status": "ok"})
