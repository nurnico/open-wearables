from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.config import settings
from app.database import DbSession
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import AuthorizationURLResponse
from app.schemas.model_crud.data_priority import (
    BulkProviderSettingsUpdate,
    ProviderSettingRead,
    ProviderSettingUpdate,
)
from app.services import DeveloperDep, user_connection_service
from app.services.provider_settings_service import ProviderSettingsService
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.factory import ProviderFactory
from app.services.providers.templates.base_oauth import BaseOAuthTemplate

router = APIRouter()
factory = ProviderFactory()
settings_service = ProviderSettingsService()


def get_oauth_strategy(provider: ProviderName) -> BaseProviderStrategy:
    """Helper to get provider strategy and ensure it supports OAuth."""
    strategy = factory.get_provider(provider.value)

    if not strategy.oauth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{provider.value}' does not support OAuth",
        )
    return strategy


@router.get(
    "/{provider}/authorize",
    summary="Get Provider Authorization URL",
    status_code=status.HTTP_200_OK,
    response_model=AuthorizationURLResponse,
    tags=["External: Providers"],
)
def authorize_provider(
    provider: ProviderName,
    user_id: Annotated[UUID, Query(description="User ID to connect")],
    redirect_uri: Annotated[str | None, Query(description="Optional redirect URI after authorization")] = None,
):
    """
    Initiate OAuth flow for a provider.

    Returns authorization URL where user should be redirected to log in.
    """
    strategy = get_oauth_strategy(provider)

    assert strategy.oauth
    auth_url, state = strategy.oauth.get_authorization_url(user_id, redirect_uri)
    return AuthorizationURLResponse(authorization_url=auth_url, state=state)


@router.get("/{provider}/callback", tags=["System: OAuth"])
def oauth_callback(
    provider: ProviderName,
    db: DbSession,
    code: Annotated[str | None, Query(description="Authorization code from provider")] = None,
    state: Annotated[str | None, Query(description="State parameter for CSRF protection")] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
):
    """
    OAuth callback endpoint.

    Provider redirects here after user authorizes. Exchanges code for tokens
    and parks the connection behind a one-time claim token; the connection is
    only persisted when the user's own client claims it (POST /oauth/claim).
    The claim token is appended to the redirect so it reaches only the
    browser that completed the consent.
    """
    if error:
        return RedirectResponse(
            url=f"/api/v1/oauth/error?message={error}:+{error_description or 'Unknown+error'}",
            status_code=303,
        )

    if not code or not state:
        return RedirectResponse(
            url="/api/v1/oauth/error?message=Missing+OAuth+parameters",
            status_code=303,
        )

    strategy = get_oauth_strategy(provider)

    assert strategy.oauth
    claim_token, _provider, redirect_uri = strategy.oauth.prepare_pending_connection(db, code, state)

    # Only redirect to the URI the authorize call pinned (frontend/app),
    # appending the one-time claim token for the completing browser.
    if redirect_uri:
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{separator}claim={claim_token}", status_code=303)

    return RedirectResponse(
        url=f"/api/v1/oauth/success?provider={provider.value}&claim={claim_token}",
        status_code=303,
    )


class OAuthClaimRequest(BaseModel):
    claim_token: str
    user_id: UUID


@router.post("/claim", tags=["System: OAuth"])
def oauth_claim(
    db: DbSession,
    body: OAuthClaimRequest,
):
    """
    Claim a pending OAuth connection for a user.

    Called by trusted first-party clients (the bittersprint backend with the
    user's JWT, the portal success page) with the one-time claim token that
    was delivered exclusively via the post-consent redirect. The connection
    is persisted for the claiming user — not for the party that minted the
    authorize URL (consent-binding fix).
    """
    provider_name = BaseOAuthTemplate.peek_claim_provider(body.claim_token)
    if not provider_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired claim token")

    strategy = get_oauth_strategy(ProviderName(provider_name))

    assert strategy.oauth
    claimed_provider = strategy.oauth.claim_connection(db, body.user_id, body.claim_token)

    # Stamp last_synced_at=now so the first periodic sync uses the connection
    # timestamp as its live-sync cursor and won't attempt to pull all history.
    user_connection_service.stamp_last_synced_at(db, body.user_id, claimed_provider)

    # Grace-period flag: automatically kick off a historical sync so integrators
    # who haven't yet adopted the explicit /sync/historical call still get backfill.
    # Controlled by HISTORICAL_SYNC_ON_CONNECT (default: true).
    if settings.historical_sync_on_connect:
        caps = strategy.capabilities
        if caps.webhook_callback:
            # this code is going to be removed later, so leave inner imports heres
            from app.integrations.celery.tasks import start_garmin_full_backfill

            start_garmin_full_backfill.delay(str(body.user_id))
        elif caps.rest_pull:
            from app.integrations.celery.tasks import sync_vendor_data

            now = datetime.now(timezone.utc)
            start_date = (now - timedelta(days=90)).isoformat()
            sync_vendor_data.delay(
                user_id=str(body.user_id),
                start_date=start_date,
                end_date=now.isoformat(),
                providers=[claimed_provider],
                is_historical=True,
            )

    return {"success": True, "provider": claimed_provider, "user_id": str(body.user_id)}


@router.get("/success", tags=["System: OAuth"])
def oauth_success(
    provider: Annotated[str, Query()],
    user_id: Annotated[str, Query()],
) -> dict:
    """Simple success page after OAuth completion."""
    return {
        "success": True,
        "message": f"Successfully connected to {provider}",
        "user_id": user_id,
        "provider": provider,
    }


@router.get("/error", tags=["System: OAuth"])
def oauth_error(
    message: Annotated[str, Query()] = "OAuth authentication failed",
) -> dict:
    """OAuth error page."""
    return {
        "success": False,
        "message": message,
    }


@router.get("/providers", response_model=list[ProviderSettingRead], tags=["External: Providers"])
def get_providers(
    db: DbSession,
    enabled_only: Annotated[bool, Query(description="Return only enabled providers")] = False,
    cloud_only: Annotated[bool, Query(description="Return only cloud (OAuth) providers")] = False,
):
    """
    Get providers with their configuration and metadata.

    Query params:
    - enabled_only: Filter to only enabled providers (default: False, returns all)
    - cloud_only: Filter to only providers with cloud OAuth API (default: False)

    Returns full provider details including name, icon_url, has_cloud_api, is_enabled.
    """
    all_providers = settings_service.get_all_providers(db)

    return [p for p in all_providers if (not enabled_only or p.is_enabled) and (not cloud_only or p.has_cloud_api)]


@router.put("/providers/{provider}", response_model=ProviderSettingRead, tags=["Internal: Providers"])
def update_provider_setting(
    provider: str,
    update: ProviderSettingUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """Update is_enabled and/or live_sync_mode for a single provider."""
    try:
        return settings_service.update_provider_setting(db, provider, update)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.put("/providers", response_model=list[ProviderSettingRead], tags=["Internal: Providers"])
def bulk_update_providers(
    updates: BulkProviderSettingsUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """
    Bulk update provider settings.

    Accepts a map of provider_id -> is_enabled and updates all providers at once.
    This is the primary endpoint for the admin UI to save checkbox states.
    """
    return settings_service.bulk_update_providers(db, updates.providers)
