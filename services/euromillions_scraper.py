import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup


BASE_URL = (
    "https://www.tirage-euromillions.net/"
    "euromillions/annees/annee-{year}/"
)


def _scrape_euromillions_year(year: int) -> list[dict]:
    """
    Récupère tous les tirages EuroMillions complets
    disponibles pour une année donnée.
    """

    url = BASE_URL.format(year=year)

    response = requests.get(
        url,
        timeout=10,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    container = soup.find(
        "div",
        id="multi-draws-by-year",
    )

    if container is None:
        raise RuntimeError(
            f"Impossible de trouver le conteneur des tirages pour {year}."
        )

    table = container.find("table")

    if table is None:
        raise RuntimeError(
            f"Impossible de trouver le tableau des tirages pour {year}."
        )

    tbody = table.find("tbody")

    if tbody is None:
        raise RuntimeError(
            f"Impossible de trouver le tbody des tirages pour {year}."
        )

    rows = tbody.find_all("tr")

    draws = []

    for row in rows:
        cells = row.find_all("td")

        # Ignore les lignes de séparation :
        # "Septembre 2026", etc.
        if len(cells) != 4:
            continue

        # ---------------------------------------------------------
        # DATE
        # ---------------------------------------------------------

        date_text = cells[0].get_text(
            " ",
            strip=True,
        )

        # Exemple :
        # "Vendredi 18/09/2026"
        date_str = date_text.split()[-1]

        try:
            draw_date = datetime.strptime(
                date_str,
                "%d/%m/%Y",
            ).date()
        except ValueError:
            continue

        # ---------------------------------------------------------
        # NUMEROS + ETOILES
        # ---------------------------------------------------------

        draw_cell = cells[1]

        number_balls = draw_cell.find_all(
            "span",
            class_="ball_small",
        )

        star_balls = draw_cell.find_all(
            "span",
            class_="star_small",
        )

        # Le tirage n'est pas encore publié/complet.
        if len(number_balls) != 5:
            continue

        if len(star_balls) != 2:
            continue

        numbers = [
            int(ball.get_text(strip=True))
            for ball in number_balls
        ]

        stars = [
            int(ball.get_text(strip=True))
            for ball in star_balls
        ]

        # Sécurité supplémentaire
        if len(numbers) != 5 or len(stars) != 2:
            continue

        # ---------------------------------------------------------
        # GAGNANTS
        # ---------------------------------------------------------

        winners_text = cells[2].get_text(
            " ",
            strip=True,
        )

        winners = int(
            re.sub(r"\D", "", winners_text) or "0"
        )

        # ---------------------------------------------------------
        # JACKPOT
        # ---------------------------------------------------------

        jackpot_text = cells[3].get_text(
            " ",
            strip=True,
        )

        jackpot_digits = re.sub(
            r"\D",
            "",
            jackpot_text,
        )

        if not jackpot_digits:
            continue

        jackpot = int(jackpot_digits)

        # ---------------------------------------------------------
        # TIRAGE
        # ---------------------------------------------------------

        draws.append(
            {
                "draw_date": draw_date,
                "n1": numbers[0],
                "n2": numbers[1],
                "n3": numbers[2],
                "n4": numbers[3],
                "n5": numbers[4],
                "e1": stars[0],
                "e2": stars[1],
                "jackpot": jackpot,
                "winners": winners,
            }
        )

    return draws


def scrape_euromillions_period(
    start_date: date,
    end_date: date,
) -> list[dict]:
    """
    Récupère les tirages EuroMillions compris
    entre start_date et end_date inclus.
    """

    if start_date > end_date:
        raise ValueError(
            "La date de début doit être antérieure "
            "ou égale à la date de fin."
        )

    draws = []

    # Une année peut suffire ou la période peut traverser
    # plusieurs années.
    for year in range(
        start_date.year,
        end_date.year + 1,
    ):
        year_draws = _scrape_euromillions_year(year)

        for draw in year_draws:
            draw_date = draw["draw_date"]

            if start_date <= draw_date <= end_date:
                draws.append(draw)

    # Tri chronologique
    draws.sort(
        key=lambda draw: draw["draw_date"]
    )

    return draws


def scrape_latest_euromillions_draw():
    """
    Récupère le dernier tirage complet disponible.
    """

    draws = _scrape_euromillions_year(
        date.today().year
    )

    if not draws:
        raise RuntimeError(
            "Aucun tirage EuroMillions complet trouvé."
        )

    # Le site est normalement déjà trié du plus récent
    # au plus ancien, mais on ne dépend pas de cela.
    draws.sort(
        key=lambda draw: draw["draw_date"],
        reverse=True,
    )

    return draws[0]