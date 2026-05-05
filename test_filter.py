from fastapi import FastAPI, APIRouter
from app.api.routers import auth, email, dashboard, mobile

def filter_router(original_router: APIRouter, allowed_paths: set) -> APIRouter:
    new_router = APIRouter(tags=original_router.tags)
    for route in original_router.routes:
        if route.path in allowed_paths:
            new_router.routes.append(route)
    return new_router

app = FastAPI()

filtered_auth = filter_router(auth.router, {"/otp/send", "/otp/verify", "/refresh", "/me"})
app.include_router(filtered_auth, prefix="/api/v1/auth")

filtered_email = filter_router(email.router, {
    "/{company_id}/tickets",
    "/{company_id}/tickets/{ticket_id}",
    "/{company_id}/tickets/{ticket_id}/reply",
    "/{company_id}/tickets/{ticket_id}/seen"
})
app.include_router(filtered_email, prefix="/api/v1/email")

filtered_dashboard = filter_router(dashboard.router, {
    "/{company_id}/chats",
    "/{company_id}/chats/{chat_id}",
    "/{company_id}/chats/{chat_id}/seen"
})
app.include_router(filtered_dashboard, prefix="/api/v1/dashboard")

app.include_router(mobile.router, prefix="/api/v1/mobile")

for r in app.routes:
    if hasattr(r, 'methods'):
        print(f"{list(r.methods)[0]:7s} {r.path}")
