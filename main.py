import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import FastAPI, Depends, Header, HTTPException
from database import engine
from sqlalchemy import text

from jwt import InvalidTokenError
import asyncio

from datetime import date
from pydantic import BaseModel


from services.euromillions_scraper import (
    scrape_latest_euromillions_draw,
    scrape_euromillions_period,
)

class CollectPeriodRequest(BaseModel):
    start_date: date
    end_date: date

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
    query = text("""
        SELECT id, pseudo
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


@app.get('/my_groups')
def my_groups(user_id: int = Depends(_get_user_id_from_token)):
    logger.info(f'[my_groups] requested for user_id={user_id}')

    query_groups = text('''
        SELECT g.*
        FROM "Groups" g
        JOIN "Members" m ON g.id = m."group"
        WHERE m."user" = :uid
        ORDER BY g.id
    ''')

    query_members = text('''
        SELECT * FROM "Members" WHERE "user" = :uid ORDER BY id
    ''')

    with engine.connect() as connection:
        res_g = connection.execute(query_groups, {'uid': user_id})
        groups = res_g.mappings().all()

        res_m = connection.execute(query_members, {'uid': user_id})
        members = res_m.mappings().all()

    logger.info(f'[my_groups] found groups_count={len(groups)} members_count={len(members)}')

    return {'groups': groups, 'members': members}


@app.get("/groups")
def get_groups():

    return get_all_from_table("Groups")


@app.get("/members")
def get_members():

    return get_all_from_table("Members")


@app.get("/groups/{group_id}/members")
def get_group_members(
    group_id: int,
    user_id: int = Depends(_get_user_id_from_token)
):
    logger.info(
        f'[group_members] requested for group_id={group_id} by user_id={user_id}'
    )

    query = text("""
        SELECT
            m.id AS member_id,
            m."group" AS group_id,
            m."user" AS user_id,
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
          )
        ORDER BY m.id
    """)

    with engine.connect() as connection:
        members = connection.execute(
            query,
            {
                "group_id": group_id,
                "user_id": user_id,
            }
        ).mappings().all()

    logger.info(
        f'[group_members] found members_count={len(members)}'
    )

    return {
        "group_id": group_id,
        "members": members,
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

# @app.get("/euromillions/draws/{draw_id}/stats")
# def get_draw_stats(draw_id: int):

    with engine.connect() as conn:

        # Date du tirage concerné
        current_query = text("""
            SELECT draw_date
            FROM "Draws"
            WHERE id = :id
        """)

        current = conn.execute(
            current_query,
            {"id": draw_id}
        ).mappings().first()

        if current is None:
            raise HTTPException(
                status_code=404,
                detail="Draw not found"
            )

        draw_date = current["draw_date"]

        # Les 30 tirages précédant le tirage concerné
        previous_query = text("""
            SELECT
                draw_date,
                n1,
                n2,
                n3,
                n4,
                n5
            FROM "Draws"
            WHERE draw_date < :draw_date
            ORDER BY draw_date DESC
            LIMIT 30
        """)

        previous_draws = conn.execute(
            previous_query,
            {"draw_date": draw_date}
        ).mappings().all()

        # Tous les tirages précédant le tirage concerné
        history_query = text("""
            SELECT
                draw_date,
                n1,
                n2,
                n3,
                n4,
                n5
            FROM "Draws"
            WHERE draw_date < :draw_date
            ORDER BY draw_date DESC
        """)

        history = conn.execute(
            history_query,
            {"draw_date": draw_date}
        ).mappings().all()

    # ========================================================
    # HOT / COLD
    # ========================================================

    counts = {
        number: 0
        for number in range(1, 51)
    }

    for draw in previous_draws:
        for column in [
            "n1",
            "n2",
            "n3",
            "n4",
            "n5",
        ]:
            number = draw[column]

            if number is not None:
                counts[number] += 1

    hot = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0])
    )[:10]

    cold = sorted(
        counts.items(),
        key=lambda item: (item[1], item[0])
    )[:10]

    # ========================================================
    # OVERDUE
    # ========================================================

    last_seen = {
        number: None
        for number in range(1, 51)
    }

    for draw in history:

        for column in [
            "n1",
            "n2",
            "n3",
            "n4",
            "n5",
        ]:
            number = draw[column]

            if number is None:
                continue

            # L'historique est parcouru du plus récent
            # au plus ancien. La première occurrence
            # rencontrée est donc la dernière sortie.
            if last_seen[number] is None:
                last_seen[number] = draw["draw_date"]

    overdue = sorted(
        last_seen.items(),
        key=lambda item: (
            item[1] is None,
            item[1] if item[1] is not None else draw_date
        )
    )

    # Les dates les plus anciennes en premier
    overdue = overdue[:10]

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
                "last_seen": (
                    last_seen_date.isoformat()
                    if last_seen_date is not None
                    else None
                ),
            }
            for number, last_seen_date in overdue
        ],
    }

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
        LIMIT 30
    """)

    with engine.connect() as conn:
        draws = conn.execute(query).mappings().all()

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

    last_seen = {
        number: None
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

            if number is None:
                continue

            if last_seen[number] is None:
                last_seen[number] = draw["draw_date"]

    overdue = sorted(
        last_seen.items(),
        key=lambda item: (
            item[1] is not None,
            item[1]
            if item[1] is not None
            else date.min,
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
                "last_seen": (
                    last_seen_date.isoformat()
                    if last_seen_date is not None
                    else None
                ),
            }
            for number, last_seen_date in overdue
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

        # Les 30 tirages précédents
        previous_draws = draws[
            max(0, index - 30):index
        ]

        # ====================================================
        # HOT / COLD
        # ====================================================

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
        # OVERDUE
        # ====================================================

        # On part du principe qu'un numéro jamais sorti
        # avant ce tirage est plus "en retard" que tous
        # les numéros déjà sortis.
        last_seen = {
            number: None
            for number in range(1, 51)
        }

        # previous_draws est du plus ancien au plus récent.
        # On parcourt donc en sens inverse pour trouver
        # la dernière apparition de chaque numéro.
        for previous_draw in reversed(previous_draws):

            for column in (
                "n1",
                "n2",
                "n3",
                "n4",
                "n5",
            ):
                number = previous_draw[column]

                if number is None:
                    continue

                if last_seen[number] is None:
                    last_seen[number] = (
                        previous_draw["draw_date"]
                    )

        overdue = sorted(
            last_seen.items(),
            key=lambda item: (
                # Les numéros jamais sortis passent en premier
                item[1] is not None,

                # Puis les dates les plus anciennes
                item[1]
                if item[1] is not None
                else draw["draw_date"],
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
                    "last_seen": (
                        last_seen_date.isoformat()
                        if last_seen_date is not None
                        else None
                    ),
                }
                for number, last_seen_date in overdue
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

from sqlalchemy.exc import IntegrityError

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