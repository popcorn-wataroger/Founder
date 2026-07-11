import os

from dotenv import load_dotenv

load_dotenv()

STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Stripe Checkoutの成功・キャンセル後のリダイレクト先
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")

# JWT
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "fallback-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Qdrant（ベクトルDB）の接続情報。値は .env から読み込む（コードに直書きしない）
QDRANT_URL = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
