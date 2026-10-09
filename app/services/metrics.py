from requests import Session, RequestException
import os
from app.core.redis_config import redis_client
from app.config import settings



async def obtain_guest_token() -> str:
    refresh_token = redis_client.get("superset:refresh-token").decode()
    if refresh_token:
        session, access_token = await refresh_access(refresh_token)
    else:
        session, access_token = await get_access_token()
    if access_token == '':
        return ""
    session, csrf_token = await get_csfr_token(session, access_token)

    return await get_guest_token(session, access_token, csrf_token)


async def get_access_token() -> tuple[Session, str]:
    print("[INFO][get_access_token] getting access token")
    try:
        login = {
            "username": settings.superset_username,
            "password": settings.superset_password,
            "provider": "db",
            "refresh": True,
        }

        session = Session()
        response = session.post(f'{settings.superset_url}/api/v1/security/login', json=login)

        if response.status_code != 200:
            print(f"[ERROR][get_access_token] Error obtaining access token: {response.status_code} - {response.text}")
            return Session(), ''
        
        data = response.json()
        access_token = data.get("access_token") 
        refresh_token = data.get("refresh_token")

        # save refresh token on redis
        if refresh_token:
            print("[INFO][get_access_token] saved refresh token")
            redis_client.set("superset:refresh-token", refresh_token)

        print("[INFO][get_access_token] obtained access token")
        response.raise_for_status()
        return session, access_token
    except RequestException as e:
        print(f"[ERROR][get_access_token] Error obtaining access token: {e}")
        return Session(), ''

async def get_csfr_token(session: Session, access_token: str) -> tuple[Session, str]:
    print("[INFO][get_csfr_token] getting CSRF token")
    try:
        headers = {
            "Authorization": f"Bearer {access_token}"
        }
        
        response = session.get(f'{settings.superset_url}/api/v1/security/csrf_token/', headers=headers)
        if response.status_code != 200:
            print(f"[ERROR][get_csfr_token] Error obtaining CSRF token: {response.status_code} - {response.text}")
            return Session(), ''
        data = response.json()
        csrf_token = data.get("result")
        return session,csrf_token
    except RequestException as e:
        print(f"[ERROR][get_csfr_token] Error obtaining CSRF token: {e}")
        return Session(), ''

async def get_guest_token(session: Session, access_token: str, csrf_token: str) -> str:
    print("[INFO][get_guest_token] getting guest token")
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-CSRFToken": csrf_token
        }

        body = {
            "resources": [
                {
                "id": settings.superset_main_dashboard_id,
                "type": "dashboard"
                }
            ],
            "rls": [],
            "user": {
                "first_name": settings.superset_guest_first_name,
                "last_name": settings.superset_guest_last_name,
                "username": settings.superset_guest_username
            }
        }
        response = session.post(f'{settings.superset_url}/api/v1/security/guest_token/', headers=headers, json=body)
        if response.status_code != 200:
            print(f"[ERROR][get_guest_token] Error obtaining guest token: {response.status_code} - {response.text}")
            return ''
        data = response.json()
        guest_token = data.get("token")
        print("[INFO][get_guest_token] obtained guest token")

        return guest_token
    except RequestException as e:
        print(f"[ERROR][get_guest_token] Error obtaining guest token: {e}")
        return ''


async def refresh_access(refresh_token: str) -> tuple[Session, str]:
    print("[INFO][refresh_access] refreshing access token")
    try:
        session = Session()
        headers = {"Authorization": "Bearer " + refresh_token}

        response = session.post(
            f"{settings.superset_url}/api/v1/security/refresh",
            headers=headers
        )
        if response.status_code != 200:
            print(f"[ERROR][refresh_access] Error refreshing access token: {response.status_code} - {response.text}")
            return Session(), ''
        
        data = response.json()
        access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token")

        if new_refresh_token:
            redis_client.set("superset:refresh-token", new_refresh_token)
            print("[INFO][refresh_access] saved new refresh token")

        print("[INFO][refresh_access] obtained new access token")
        response.raise_for_status()
        return session, access_token
    except RequestException as e:
        print(f"[ERROR][refresh_access] Error refreshing access token: {e}")
        return Session(), ''
        