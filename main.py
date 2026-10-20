import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import FastAPI, Depends, Header, HTTPException
from database import engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jwt import InvalidTokenError
import asyncio

from datetime import date
from pydantic import BaseModel
from zoneinfo import ZoneInfo


from services.euromillions_scraper import (
    scrape_latest_euromillions_draw,
    scrape_euromillions_period,
)


def _get_next_draw_date(now: Optional[datetime] = None) -> date:
    paris_tz = ZoneInfo("Europe/Paris")

    current = now or datetime.now(paris_tz)

    if current.tzinfo is None:
        current = current.replace(tzinfo=paris_tz)
    else:
        current = current.astimezone(paris_tz)

    weekday = current.weekday()
    minutes = current.hour * 60 + current.minute
    cutoff = 20 * 60

    # Mardi : mardi avant 20 h, sinon vendredi.
    if weekday == 1:
        if minutes < cutoff:
            return current.date()
        return (current + timedelta(days=3)).date()

    # Vendredi : vendredi avant 20 h, sinon mardi suivant.
    if weekday == 4:
        if minutes < cutoff:
            return current.date()
        return (current + timedelta(days=4)).date()

    # Lundi : mardi dans 1 jour.
    if weekday == 0:
        return (current + timedelta(days=1)).date()

    # Mercredi ou jeudi : vendredi.
    if weekday in (2, 3):
        days_until_friday = 4 - weekday
        return (current + timedelta(days=days_until_friday)).date()

    # Samedi ou dimanche : mardi suivant.
    days_until_tuesday = (1 - weekday + 7) % 7
    return (current + timedelta(days=days_until_tuesday)).date()


class CollectPeriodRequest(BaseModel):
    start_date: date
    end_date: date

class CreateGroupGridRequest(BaseModel):
    numbers: list[int]
    stars: list[int]
    draw_date: date

# Configure logging BEFORE creating logger
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# JWT secret (can be set in backend/.env as SECRET_KEY)
# Use a secret of at least 32 chars for HS256 (PyJWT recommendation)
SECRET_KEY = os.getenv('SECRET_KEY')
logger.info(f'Backend initialized with SECRET_KEY: {SECRET_KEY[:10]}...')

app = FastAPI()
counter = 0


@app.get("/")
def test():

    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT version();"))
            version = result.scalar()

        return {
            "status": "OK",
            "postgres": version
        }

    except Exception as e:
        return {
            "status": "ERROR",
            "message": str(e)
        }
    

@app.get("/tables")
def get_tables():

    query = text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name;
    """)

    with engine.connect() as connection:
        result = connection.execute(query)

        tables = [
            row[0]
            for row in result
        ]

    return {
        "tables": tables
    }


def get_all_from_table(table_name: str):

    query = text(
        f"""
        SELECT *
        FROM "{table_name}";
        """
    )

    with engine.connect() as connection:
        result = connection.execute(query)

        rows = result.mappings().all()

    return rows



def _get_user_id_from_token(
    authorization: Optional[str] = Header(None)
):
    if authorization is None:
        logger.warning("[AUTH] Missing Authorization header")
        raise HTTPException(status_code=401, detail="Missing authorization")

    try:
        logger.info(f"[AUTH] Header = {authorization}")

        scheme, token = authorization.split()

        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid auth scheme")

        logger.info(f"[AUTH] JWT = {token}")

        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=["HS256"]
        )

        logger.info(f"[AUTH] Payload = {payload}")

        return int(payload["sub"])

    except InvalidTokenError as e:
        logger.error(f"[AUTH] Invalid JWT : {e}")
        raise HTTPException(status_code=401, detail="Invalid token")

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("[AUTH] Unexpected error")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/me")
def me(user_id: int = Depends(_get_user_id_from_token)):
    # query = text("""
    #     SELECT id, pseudo
    #     FROM "Users"
    #     WHERE id = :id
    # """)
    query = text("""
        SELECT id, pseudo, credits, "isSuperAdmin"
        FROM "Users"
        WHERE id = :id
    """)

    with engine.connect() as conn:
        user = conn.execute(query, {"id": user_id}).mappings().first()

    return user

@app.post('/login')
def login(body: dict):
    pseudo = body.get('pseudo')
    password = body.get('password')
    if not pseudo or not password:
        raise HTTPException(status_code=400, detail='Missing credentials')

    query = text('''
        SELECT * FROM "Users" WHERE pseudo = :pseudo
    ''')

    with engine.connect() as connection:
        result = connection.execute(query, {'pseudo': pseudo}).mappings().first()

    if not result:
        raise HTTPException(status_code=401, detail='Invalid credentials')

    # NOTE: plaintext password comparison - replace with secure hash check if needed
    if result.get('password') != password:
        raise HTTPException(status_code=401, detail='Invalid credentials')

    payload = {
        'sub': str(result.get('id')),
        'pseudo': result.get('pseudo'),
        'exp': datetime.utcnow() + timedelta(days=7)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')

    return {'token': token, 'pseudo': result.get('pseudo'), 'id': result.get('id')}


# @app.get('/my_groups')
# def my_groups(user_id: int = Depends(_get_user_id_from_token)):
#     logger.info(f'[my_groups] requested for user_id={user_id}')

#     query_groups = text('''
#         SELECT g.*
#         FROM "Groups" g
#         JOIN "Members" m ON g.id = m."group"
#         WHERE m."user" = :uid
#         ORDER BY g.id
#     ''')

#     query_members = text('''
#         SELECT * FROM "Members" WHERE "user" = :uid ORDER BY id
#     ''')

#     with engine.connect() as connection:
#         res_g = connection.execute(query_groups, {'uid': user_id})
#         groups = res_g.mappings().all()

#         res_m = connection.execute(query_members, {'uid': user_id})
#         members = res_m.mappings().all()

#     logger.info(f'[my_groups] found groups_count={len(groups)} members_count={len(members)}')

#     return {'groups': groups, 'members': members}

@app.get("/my_groups")
def my_groups(
    user_id: int = Depends(_get_user_id_from_token),
):
    logger.info(
        f"[my_groups] requested for user_id={user_id}"
    )

    query_groups = text("""
        SELECT g.*
        FROM "Groups" g
        JOIN "Members" m
          ON g.id = m."group"
        WHERE m."user" = :uid
          AND m.request_status = 'accepted'
        ORDER BY g.id
    """)

    query_members = text("""
        SELECT *
        FROM "Members"
        WHERE "user" = :uid
        ORDER BY id
    """)

    with engine.connect() as connection:
        groups = connection.execute(
            query_groups,
            {"uid": user_id},
        ).mappings().all()

        members = connection.execute(
            query_members,
            {"uid": user_id},
        ).mappings().all()

    logger.info(
        f"[my_groups] found groups_count={len(groups)} "
        f"members_count={len(members)}"
    )

    return {
        "groups": [dict(group) for group in groups],
        "members": [dict(member) for member in members],
    }

@app.get("/groups")
def get_groups():

    return get_all_from_table("Groups")

# @app.get("/groups")
# def get_groups(
#     user_id: int = Depends(_get_user_id_from_token),
# ):
#     query = text("""
#         SELECT
#             g.*,
#             membership.request_status AS membership_status,
#             COALESCE(
#                 membership.request_status = 'accepted',
#                 FALSE
#             ) AS is_member
#         FROM "Groups" g
#         LEFT JOIN LATERAL (
#             SELECT m.request_status
#             FROM "Members" m
#             WHERE m."group" = g.id
#               AND m."user" = :user_id
#             ORDER BY m.created_at DESC, m.id DESC
#             LIMIT 1
#         ) membership ON TRUE
#         ORDER BY g.id
#     """)

#     with engine.connect() as connection:
#         groups = connection.execute(
#             query,
#             {"user_id": user_id},
#         ).mappings().all()

#     return [dict(group) for group in groups]


@app.get("/members")
def get_members():

    return get_all_from_table("Members")


@app.post("/groups/{group_id}/join-requests", status_code=201)
def request_to_join_group(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    """
    Crée ou renouvelle une demande pour rejoindre un groupe.

    - pending : erreur 409
    - accepted : erreur 409
    - rejected : réutilise la ligne existante
    - aucune demande existante : insère une nouvelle ligne
    """

    try:
        with engine.begin() as conn:

            # 1. Vérifier que le groupe existe
            group_exists = conn.execute(
                text("""
                    SELECT id
                    FROM "Groups"
                    WHERE id = :group_id
                """),
                {"group_id": group_id},
            ).scalar_one_or_none()

            if group_exists is None:
                raise HTTPException(
                    status_code=404,
                    detail="Ce groupe n'existe pas.",
                )

            # 2. Récupérer les lignes existantes pour cet utilisateur
            # et ce groupe, en verrouillant celles qui existent.
            existing_rows = conn.execute(
                text("""
                    SELECT id, request_status, created_at
                    FROM "Members"
                    WHERE "user" = :user_id
                      AND "group" = :group_id
                    ORDER BY created_at DESC, id DESC
                    FOR UPDATE
                """),
                {
                    "user_id": user_id,
                    "group_id": group_id,
                },
            ).mappings().all()

            # 3. Refuser si une demande est déjà en attente.
            if any(
                row["request_status"] == "pending"
                for row in existing_rows
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Une demande pour rejoindre ce groupe "
                        "est déjà en attente."
                    ),
                )

            # 4. Refuser si l'utilisateur est déjà membre.
            # On conserve la compatibilité avec les anciennes lignes
            already_member = any(
                row["request_status"] == "accepted"
                for row in existing_rows
            )

            if already_member:
                raise HTTPException(
                    status_code=409,
                    detail="Vous êtes déjà membre de ce groupe.",
                )

            # 5. Chercher une demande précédemment rejetée.
            rejected_row = next(
                (
                    row
                    for row in existing_rows
                    if row["request_status"] == "rejected"
                ),
                None,
            )

            if rejected_row is not None:
                # 6a. Réutiliser la ligne rejetée :
                # aucun nouvel INSERT.
                result = conn.execute(
                    text("""
                        UPDATE "Members"
                        SET request_status = 'pending',
                            created_at = NOW()
                        WHERE id = :member_id
                        RETURNING id, created_at, request_status
                    """),
                    {"member_id": rejected_row["id"]},
                ).mappings().one()

                action = "updated"

            else:
                # 6b. Aucune demande rejetée à réutiliser :
                # créer une nouvelle ligne.
                result = conn.execute(
                    text("""
                        INSERT INTO "Members" (
                            "user",
                            "group",
                            total_spent,
                            request_status
                        )
                        VALUES (
                            :user_id,
                            :group_id,
                            0,
                            'pending'
                        )
                        RETURNING id, created_at, request_status
                    """),
                    {
                        "user_id": user_id,
                        "group_id": group_id,
                    },
                ).mappings().one()

                action = "created"

        logger.info(
            "[request_to_join_group] "
            "user_id=%s group_id=%s request_id=%s action=%s",
            user_id,
            group_id,
            result["id"],
            action,
        )

        return {
            "success": True,
            "message": (
                "Votre demande pour rejoindre le groupe a été envoyée."
            ),
            "request": {
                "id": result["id"],
                "user_id": user_id,
                "group_id": group_id,
                "request_status": result["request_status"],
                "created_at": result["created_at"],
            },
        }

    except HTTPException:
        raise

    except Exception:
        logger.exception(
            "[request_to_join_group] Erreur pour user_id=%s, group_id=%s",
            user_id,
            group_id,
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Impossible d'envoyer la demande pour rejoindre le groupe."
            ),
        )


@app.delete("/groups/{group_id}/leave")
def leave_group(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    """
    Permet à l'utilisateur authentifié de quitter un groupe.

    Seule son appartenance dans la table Members est supprimée.
    Le groupe et ses grilles sont conservés.
    """

    delete_query = text("""
        DELETE FROM "Members"
        WHERE "group" = :group_id
          AND "user" = :user_id
        RETURNING id
    """)

    try:
        with engine.begin() as conn:
            membership_id = conn.execute(
                delete_query,
                {
                    "group_id": group_id,
                    "user_id": user_id,
                },
            ).scalar_one_or_none()

            if membership_id is None:
                raise HTTPException(
                    status_code=404,
                    detail="Vous n'êtes pas membre de ce groupe.",
                )

        logger.info(
            "[leave_group] user_id=%s left group_id=%s",
            user_id,
            group_id,
        )

        return {
            "success": True,
            "message": "Vous avez quitté le groupe.",
            "group_id": group_id,
        }

    except HTTPException:
        raise

    except Exception:
        logger.exception(
            "[leave_group] Erreur pour user_id=%s, group_id=%s",
            user_id,
            group_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Impossible de quitter le groupe.",
        )



@app.get("/groups/{group_id}/members")
def get_group_members(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    logger.info(
        "[group_members] requested for group_id=%s by user_id=%s",
        group_id,
        user_id,
    )

    query = text("""
        SELECT
            m.id AS member_id,
            m."group" AS group_id,
            m."user" AS user_id,
            m.total_spent AS total_spent,
            m.request_status AS request_status,
            u.pseudo
        FROM "Members" m
        JOIN "Users" u
            ON u.id = m."user"
        WHERE m."group" = :group_id
          AND EXISTS (
              SELECT 1
              FROM "Members" my_membership
              WHERE my_membership."group" = :group_id
                AND my_membership."user" = :user_id
                AND my_membership.request_status = 'accepted'
          )
        ORDER BY m.id
    """)

    with engine.connect() as connection:
        members = connection.execute(
            query,
            {
                "group_id": group_id,
                "user_id": user_id,
            },
        ).mappings().all()

    logger.info(
        "[group_members] found rows_count=%s",
        len(members),
    )

    return {
        "group_id": group_id,
        "members": [dict(member) for member in members],
    }

def _update_join_request_status(
    member_id: int,
    user_id: int,
    new_status: str,
):
    if new_status not in ("accepted", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="Statut de demande invalide.",
        )

    try:
        with engine.begin() as conn:
            # 1. Récupérer et verrouiller la demande.
            request_row = conn.execute(
                text("""
                    SELECT
                        m.id AS member_id,
                        m."user" AS user_id,
                        m."group" AS group_id,
                        m.request_status,
                        u.pseudo
                    FROM "Members" m
                    JOIN "Users" u ON u.id = m."user"
                    WHERE m.id = :member_id
                    FOR UPDATE OF m
                """),
                {"member_id": member_id},
            ).mappings().first()

            if request_row is None:
                raise HTTPException(
                    status_code=404,
                    detail="Demande d'adhésion introuvable.",
                )

            group_id = request_row["group_id"]
            requested_user_id = request_row["user_id"]

            # 2. Vérifier que la demande est toujours en attente.
            if request_row["request_status"] != "pending":
                raise HTTPException(
                    status_code=409,
                    detail="Cette demande n'est plus en attente.",
                )

            # 3. Empêcher l'utilisateur de traiter sa propre demande.
            if requested_user_id == user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Vous ne pouvez pas traiter votre propre demande.",
                )

            # 4. Vérifier que l'utilisateur connecté est membre accepté
            # du groupe auquel appartient la demande.
            is_accepted_member = conn.execute(
                text("""
                    SELECT 1
                    FROM "Members"
                    WHERE "group" = :group_id
                      AND "user" = :user_id
                      AND request_status = 'accepted'
                    LIMIT 1
                """),
                {
                    "group_id": group_id,
                    "user_id": user_id,
                },
            ).scalar_one_or_none()

            if is_accepted_member is None:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Seuls les membres acceptés de ce groupe "
                        "peuvent gérer les demandes."
                    ),
                )

            # 5. Mettre à jour le statut de la demande.
            updated = conn.execute(
                text("""
                    UPDATE "Members"
                    SET request_status = :new_status
                    WHERE id = :member_id
                      AND request_status = 'pending'
                    RETURNING
                        id AS member_id,
                        "user" AS user_id,
                        "group" AS group_id,
                        request_status
                """),
                {
                    "new_status": new_status,
                    "member_id": member_id,
                },
            ).mappings().first()

            if updated is None:
                raise HTTPException(
                    status_code=409,
                    detail="La demande a déjà été traitée.",
                )

        return {
            "success": True,
            "message": (
                "Demande acceptée."
                if new_status == "accepted"
                else "Demande refusée."
            ),
            "request": {
                **dict(updated),
                "pseudo": request_row["pseudo"],
            },
        }

    except HTTPException:
        raise

    except Exception:
        logger.exception(
            "[update_join_request] member_id=%s status=%s",
            member_id,
            new_status,
        )
        raise HTTPException(
            status_code=500,
            detail="Impossible de traiter la demande d'adhésion.",
        )


@app.post("/join-requests/{member_id}/accept")
def accept_join_request(
    member_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    return _update_join_request_status(
        member_id=member_id,
        user_id=user_id,
        new_status="accepted",
    )


@app.post("/join-requests/{member_id}/reject")
def reject_join_request(
    member_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    return _update_join_request_status(
        member_id=member_id,
        user_id=user_id,
        new_status="rejected",
    )


@app.post("/groups/{group_id}/grids")
def create_group_grid(
    group_id: int,
    body: CreateGroupGridRequest,
    user_id: int = Depends(_get_user_id_from_token),
):
    # --------------------------------------------------------
    # 1. Vérification des numéros et étoiles
    # --------------------------------------------------------

    if len(body.numbers) != 5:
        raise HTTPException(
            status_code=400,
            detail="Une grille doit contenir exactement 5 numéros.",
        )

    if len(set(body.numbers)) != 5:
        raise HTTPException(
            status_code=400,
            detail="Les 5 numéros doivent être distincts.",
        )

    if any(number < 1 or number > 50 for number in body.numbers):
        raise HTTPException(
            status_code=400,
            detail="Les numéros doivent être compris entre 1 et 50.",
        )

    if len(body.stars) != 2:
        raise HTTPException(
            status_code=400,
            detail="Une grille doit contenir exactement 2 étoiles.",
        )

    if len(set(body.stars)) != 2:
        raise HTTPException(
            status_code=400,
            detail="Les 2 étoiles doivent être distinctes.",
        )

    if any(star < 1 or star > 12 for star in body.stars):
        raise HTTPException(
            status_code=400,
            detail="Les étoiles doivent être comprises entre 1 et 12.",
        )

    numbers = sorted(body.numbers)
    stars = sorted(body.stars)

    # --------------------------------------------------------
    # 2. Vérification de l'appartenance au groupe
    # --------------------------------------------------------

    membership_query = text("""
        SELECT 1
        FROM "Members"
        WHERE "group" = :group_id
          AND "user" = :user_id
        LIMIT 1
    """)

    with engine.connect() as conn:
        membership = conn.execute(
            membership_query,
            {
                "group_id": group_id,
                "user_id": user_id,
            },
        ).first()

    if membership is None:
        raise HTTPException(
            status_code=403,
            detail="Vous n'êtes pas membre de ce groupe.",
        )

    # --------------------------------------------------------
    # 3. Insertion dans public."Group_grids"
    # --------------------------------------------------------

    insert_query = text("""
        INSERT INTO public."Group_grids" (
            author,
            n1, n2, n3, n4, n5,
            e1, e2,
            draw_date,
            status,
            "group"
        )
        VALUES (
            :author,
            :n1, :n2, :n3, :n4, :n5,
            :e1, :e2,
            :draw_date,
            :status,
            :group_id
        )
        RETURNING
            id,
            created_at,
            author,
            n1, n2, n3, n4, n5,
            e1, e2,
            draw_date,
            status,
            "group" AS group_id
    """)

    try:
        with engine.begin() as conn:
            grid = conn.execute(
                insert_query,
                {
                    "author": user_id,
                    "n1": numbers[0],
                    "n2": numbers[1],
                    "n3": numbers[2],
                    "n4": numbers[3],
                    "n5": numbers[4],
                    "e1": stars[0],
                    "e2": stars[1],
                    "draw_date": body.draw_date,
                    "status": "suggérée",
                    "group_id": group_id,
                },
            ).mappings().one()

        return {
            "success": True,
            "message": "Grille ajoutée au groupe.",
            "grid": dict(grid),
        }

    except Exception:
        logger.exception(
            "[create_group_grid] Erreur pendant l'insertion"
        )
        raise HTTPException(
            status_code=500,
            detail="Impossible d'enregistrer la grille.",
        )



@app.get("/groups/{group_id}/grids")
def get_group_grids(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    query = text("""
        SELECT
            gg.id,
            gg.created_at,
            gg.author,
            u.pseudo AS author_pseudo,
            gg.n1, gg.n2, gg.n3, gg.n4, gg.n5,
            gg.e1, gg.e2,
            gg.draw_date,
            COALESCE(gg.status, 'suggérée') AS status,
            gg."group" AS group_id
        FROM public."Group_grids" gg
        JOIN "Users" u
            ON u.id = gg.author
        WHERE gg."group" = :group_id
          AND EXISTS (
              SELECT 1
              FROM "Members" m
              WHERE m."group" = :group_id
                AND m."user" = :user_id
          )
        ORDER BY
            gg.draw_date DESC NULLS LAST,
            gg.created_at DESC
    """)

    with engine.connect() as conn:
        grids = conn.execute(
            query,
            {
                "group_id": group_id,
                "user_id": user_id,
            },
        ).mappings().all()

    return {
        "group_id": group_id,
        "grids": [dict(grid) for grid in grids],
    }


@app.get("/groups/{group_id}/grids/next-draw")
def get_group_grids_next_draw(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    draw_date = _get_next_draw_date()

    query = text("""
        SELECT
            gg.id,
            gg.created_at,
            gg.author,
            u.pseudo AS author_pseudo,
            gg.n1, gg.n2, gg.n3, gg.n4, gg.n5,
            gg.e1, gg.e2,
            gg.draw_date,
            COALESCE(gg.status, 'suggérée') AS status,
            gg."group" AS group_id
        FROM public."Group_grids" gg
        JOIN public."Users" u
            ON u.id = gg.author
        WHERE gg."group" = :group_id
          AND gg.draw_date = :draw_date
          AND EXISTS (
              SELECT 1
              FROM public."Members" m
              WHERE m."group" = :group_id
                AND m."user" = :user_id
          )
        ORDER BY
            gg.created_at DESC
    """)

    with engine.connect() as conn:
        grids = conn.execute(
            query,
            {
                "group_id": group_id,
                "user_id": user_id,
                "draw_date": draw_date,
            },
        ).mappings().all()

    return {
        "group_id": group_id,
        "draw_date": draw_date.isoformat(),
        "grids": [dict(grid) for grid in grids],
    }
    
@app.get("/split_rules")
def get_split_rules():

    return get_all_from_table("Split_rules")


@app.get("/users")
def get_users():

    return get_all_from_table("Users")

@app.get("/euromillions/draws")
def get_euromillions_draws():
    query = text("""
        SELECT
            id,
            draw_date,
            n1, n2, n3, n4, n5,
            e1, e2,
            winners,
            jackpot
        FROM "Draws"
        Order BY draw_date DESC
    """)

    with engine.connect() as conn:
        draws = conn.execute(query).mappings().all()

    return draws


@app.delete("/euromillions/draws/{draw_id}")
def delete_euromillions_draw(
    draw_id: int,
    user_id: int = Depends(_get_user_id_from_token),
):
    try:
        with engine.begin() as conn:
            # Vérification des droits côté serveur.
            # Ne jamais faire confiance au statut envoyé par Flutter.
            admin_query = text("""
                SELECT "isSuperAdmin"
                FROM "Users"
                WHERE id = :user_id
            """)

            is_super_admin = conn.execute(
                admin_query,
                {"user_id": user_id},
            ).scalar_one_or_none()

            if is_super_admin is not True:
                raise HTTPException(
                    status_code=403,
                    detail="Accès réservé aux super-administrateurs",
                )

            # Suppression uniquement si le tirage existe.
            delete_query = text("""
                DELETE FROM "Draws"
                WHERE id = :draw_id
                RETURNING id
            """)

            deleted_id = conn.execute(
                delete_query,
                {"draw_id": draw_id},
            ).scalar_one_or_none()

            if deleted_id is None:
                raise HTTPException(
                    status_code=404,
                    detail="Tirage introuvable",
                )

        return {
            "success": True,
            "message": "Tirage supprimé",
            "id": deleted_id,
        }

    except IntegrityError as exc:
        # Une table liée référence peut-être encore ce tirage.
        raise HTTPException(
            status_code=409,
            detail=(
                "Impossible de supprimer ce tirage : "
                "des données liées le référencent encore."
            ),
        ) from exc



@app.get("/euromillions/stats/current")
def get_current_euromillions_stats():

    query = text("""
        SELECT
            draw_date,
            n1,
            n2,
            n3,
            n4,
            n5
        FROM "Draws"
        ORDER BY draw_date DESC
    """)

    with engine.connect() as conn:
        all_draws = conn.execute(query).mappings().all()

    # ====================================================
    # HOT / COLD
    # Uniquement sur les 30 derniers tirages
    # ====================================================

    draws = all_draws[:30]

    counts = {
        number: 0
        for number in range(1, 51)
    }

    for draw in draws:
        for column in (
            "n1",
            "n2",
            "n3",
            "n4",
            "n5",
        ):
            number = draw[column]

            if number is not None:
                counts[number] += 1

    hot = sorted(
        counts.items(),
        key=lambda item: (
            -item[1],
            item[0],
        )
    )[:10]

    cold = sorted(
        counts.items(),
        key=lambda item: (
            item[1],
            item[0],
        )
    )[:10]

    # ====================================================
    # RETARD
    #
    # Sur TOUS les tirages disponibles.
    #
    # all_draws est du plus récent au plus ancien.
    #
    # Si le numéro est dans all_draws[0] :
    #     retard = 0
    #
    # Si le numéro est dans all_draws[1] :
    #     retard = 1
    #
    # etc.
    # ====================================================

    overdue_counts = {
        number: None
        for number in range(1, 51)
    }

    for number in range(1, 51):

        for index, draw in enumerate(all_draws):

            numbers = (
                draw["n1"],
                draw["n2"],
                draw["n3"],
                draw["n4"],
                draw["n5"],
            )

            if number in numbers:
                overdue_counts[number] = index
                break

    # Les numéros jamais sortis sont placés après
    # ceux qui ont un retard calculable.
    #
    # Parmi les numéros sortis, les plus gros retards
    # viennent en premier.
    overdue = sorted(
        overdue_counts.items(),
        key=lambda item: (
            item[1] is None,
            -(item[1] if item[1] is not None else 0),
            item[0],
        )
    )[:10]

    return {
        "hot": [
            {
                "number": number,
                "count": count,
            }
            for number, count in hot
        ],

        "cold": [
            {
                "number": number,
                "count": count,
            }
            for number, count in cold
        ],

        "overdue": [
            {
                "number": number,
                "count": (
                    count
                    if count is not None
                    else len(all_draws)
                ),
            }
            for number, count in overdue
        ],
    }



@app.get("/euromillions/stats")
def get_euromillions_stats():

    query = text("""
        SELECT
            id,
            draw_date,
            n1,
            n2,
            n3,
            n4,
            n5
        FROM "Draws"
        ORDER BY draw_date ASC
    """)

    with engine.connect() as conn:
        draws = conn.execute(query).mappings().all()

    results = {}

    for index, draw in enumerate(draws):

        draw_id = draw["id"]

        # ====================================================
        # HOT / COLD
        # ====================================================
        # Pour Hot / Cold, on regarde uniquement les
        # 30 tirages précédant le tirage actuel.

        previous_draws = draws[
            max(0, index - 30):index
        ]

        counts = {
            number: 0
            for number in range(1, 51)
        }

        for previous_draw in previous_draws:

            for column in (
                "n1",
                "n2",
                "n3",
                "n4",
                "n5",
            ):
                number = previous_draw[column]

                if number is not None:
                    counts[number] += 1

        hot = sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                item[0],
            )
        )[:10]

        cold = sorted(
            counts.items(),
            key=lambda item: (
                item[1],
                item[0],
            )
        )[:10]

        # ====================================================
        # RETARD
        # ====================================================
        # Pour le retard, on regarde TOUT l'historique
        # précédent, sans limite de 30 tirages.
        #
        # Exemple :
        #
        # - numéro sorti au tirage précédent -> retard = 0
        # - numéro sorti il y a 1 tirage      -> retard = 1
        # - numéro sorti il y a 2 tirages     -> retard = 2
        #
        # Le tirage actuel n'est évidemment pas pris en compte.

        all_previous_draws = draws[:index]

        overdue_counts = {}

        for number in range(1, 51):

            # Par défaut, si le numéro n'est jamais apparu
            # avant ce tirage, on lui attribue un retard
            # correspondant à tout l'historique précédent.
            delay = len(all_previous_draws)

            # On parcourt les tirages précédents du plus récent
            # au plus ancien.
            for i, previous_draw in enumerate(
                reversed(all_previous_draws)
            ):

                if number in (
                    previous_draw["n1"],
                    previous_draw["n2"],
                    previous_draw["n3"],
                    previous_draw["n4"],
                    previous_draw["n5"],
                ):
                    delay = i
                    break

            overdue_counts[number] = delay

        # Les plus gros retards en premier.
        # En cas d'égalité, le plus petit numéro en premier.
        overdue = sorted(
            overdue_counts.items(),
            key=lambda item: (
                -item[1],
                item[0],
            )
        )[:10]

        # ====================================================
        # RESULTAT
        # ====================================================

        results[draw_id] = {
            "hot": [
                {
                    "number": number,
                    "count": count,
                }
                for number, count in hot
            ],

            "cold": [
                {
                    "number": number,
                    "count": count,
                }
                for number, count in cold
            ],

            "overdue": [
                {
                    "number": number,
                    "count": count,
                }
                for number, count in overdue
            ],
        }

    return results

    

@app.post("/euromillions/collect")
async def collect_euromillions():
    global counter

    await asyncio.sleep(7)

    message = f"message {counter}"
    counter += 1

    return {"message": message}


@app.post("/collect/euromillions/latest")
def collect_latest_euromillions():
    try:
        draw = scrape_latest_euromillions_draw()

        query = text("""
            INSERT INTO "Draws" (
                draw_date,
                n1,n2,n3,n4,n5,
                e1,e2,
                jackpot,
                winners
            )
            VALUES (
                :draw_date,
                :n1,:n2,:n3,:n4,:n5,
                :e1,:e2,
                :jackpot,
                :winners
            )
            RETURNING id
        """)

        with engine.begin() as conn:
            draw_id = conn.execute(query, draw).scalar_one()

        return {
            "success": True,
            "inserted": True,
            "message": "Nouveau tirage ajouté.",
            "draw_id": draw_id,
            "draw": draw,
        }

    except IntegrityError:
        return {
            "success": True,
            "inserted": False,
            "message": "Ce tirage est déjà présent en base.",
            "draw": draw,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )
    


@app.get("/collect/euromillions/dates")
def get_collected_euromillions_dates():
    query = text("""
        SELECT draw_date
        FROM "Draws"
        ORDER BY draw_date
    """)

    with engine.connect() as conn:
        rows = conn.execute(query).scalars().all()

    return [
        draw_date.isoformat()
        for draw_date in rows
    ]


@app.post("/collect/euromillions/period")
def collect_euromillions_period(
    body: CollectPeriodRequest,
):
    if body.start_date > body.end_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "La date de début doit être antérieure "
                "ou égale à la date de fin."
            ),
        )

    try:
        draws = scrape_euromillions_period(
            body.start_date,
            body.end_date,
        )

        query = text("""
            INSERT INTO "Draws" (
                draw_date,
                n1,
                n2,
                n3,
                n4,
                n5,
                e1,
                e2,
                jackpot,
                winners
            )
            VALUES (
                :draw_date,
                :n1,
                :n2,
                :n3,
                :n4,
                :n5,
                :e1,
                :e2,
                :jackpot,
                :winners
            )
            ON CONFLICT (draw_date) DO NOTHING
        """)

        inserted = 0
        duplicates = 0

        with engine.begin() as conn:
            for draw in draws:
                result = conn.execute(
                    query,
                    draw,
                )

                if result.rowcount == 1:
                    inserted += 1
                else:
                    duplicates += 1

        return {
            "success": True,
            "inserted": inserted,
            "duplicates": duplicates,
            "total": len(draws),
            "message": (
                f"{inserted} nouveau(x) tirage(s) ajouté(s), "
                f"{duplicates} déjà présent(s)."
            ),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )