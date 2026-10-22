import os

import structlog
from requests import RequestException, Session

from app.config import settings
from app.core.redis_config import redis_client

logger = structlog.get_logger(__name__)



async def obtain_guest_token() -> str:
    try:
        refresh_token = redis_client.get("superset:refresh-token")
        if refresh_token:
            refresh_token = refresh_token.decode()
        if refresh_token:
            session, access_token = await refresh_access(refresh_token)
            if not access_token:
                # silent login if the refresh token is invalid
                session, access_token = await get_access_token()
        else:
            session, access_token = await get_access_token()
        if access_token == '':
            return ""
        session, csrf_token = await get_csfr_token(session, access_token)

        return await get_guest_token(session, access_token, csrf_token)
    except Exception:
        logger.exception("obtain_guest_token failed")
        raise


async def get_access_token() -> tuple[Session, str]:
    try:
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
            if data is None or not data:
                logger.info("get_access_token query succeeded with 0 results")
            access_token = data.get("access_token") 
            if access_token is None or not access_token:
                logger.info("get_access_token query succeeded with 0 results")
            refresh_token = data.get("refresh_token")
            if refresh_token is None or not refresh_token:
                logger.info("get_access_token query succeeded with 0 results")

            # save refresh token on redis
            if refresh_token:
                print("[INFO][get_access_token] saved refresh token")
                redis_client.set("superset:refresh-token", refresh_token, ex= 3600 * 24)

            print("[INFO][get_access_token] obtained access token")
            response.raise_for_status()
            return session, access_token
        except RequestException as e:
            logger.exception("get_access_token failed")
            print(f"[ERROR][get_access_token] Error obtaining access token: {e}")
            return Session(), ''
    except Exception:
        logger.exception("get_access_token failed")
        raise

async def get_csfr_token(session: Session, access_token: str) -> tuple[Session, str]:
    try:
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
            if data is None or not data:
                logger.info("get_csfr_token query succeeded with 0 results")
            csrf_token = data.get("result")
            if csrf_token is None or not csrf_token:
                logger.info("get_csfr_token query succeeded with 0 results")
            return session,csrf_token
        except RequestException as e:
            logger.exception("get_csfr_token failed")
            print(f"[ERROR][get_csfr_token] Error obtaining CSRF token: {e}")
            return Session(), ''
    except Exception:
        logger.exception("get_csfr_token failed")
        raise

async def get_guest_token(session: Session, access_token: str, csrf_token: str) -> str:
    try:
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
            if data is None or not data:
                logger.info("get_guest_token query succeeded with 0 results")
            guest_token = data.get("token")
            if guest_token is None or not guest_token:
                logger.info("get_guest_token query succeeded with 0 results")
            print("[INFO][get_guest_token] obtained guest token")

            return guest_token
        except RequestException as e:
            logger.exception("get_guest_token failed")
            print(f"[ERROR][get_guest_token] Error obtaining guest token: {e}")
            return ''
    except Exception:
        logger.exception("get_guest_token failed")
        raise


async def refresh_access(refresh_token: str) -> tuple[Session, str]:
    try:
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
            if data is None or not data:
                logger.info("refresh_access query succeeded with 0 results")
            access_token = data.get("access_token")
            if access_token is None or not access_token:
                logger.info("refresh_access query succeeded with 0 results")
            new_refresh_token = data.get("refresh_token")
            if new_refresh_token is None or not new_refresh_token:
                logger.info("refresh_access query succeeded with 0 results")

            if new_refresh_token:
                redis_client.set("superset:refresh-token", new_refresh_token, ex=3600 * 24)
                print("[INFO][refresh_access] saved new refresh token")

            print("[INFO][refresh_access] obtained new access token")
            response.raise_for_status()
            return session, access_token
        except RequestException as e:
            logger.exception("refresh_access failed")
            print(f"[ERROR][refresh_access] Error refreshing access token: {e}")
            return Session(), ''
            
    except Exception:
        logger.exception("refresh_access failed")
        raise