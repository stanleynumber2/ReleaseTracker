import os
import re
import asyncio
import time
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import app_commands


print("MediaDB code version: 1.6.10")

# 1.6.8 is based on the known-good 1.6.3 command/data logic.
# The only intended feature change is local platform autocomplete.
# Autocomplete never contacts IGDB; IGDB is only contacted when a command runs.


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")

TWITCH_CLIENT_ID = (
    os.environ.get("TWITCH_CLIENT_ID")
    or os.environ.get("Twitch_client_id")
    or os.environ.get("Twitch_Client_ID")
)

TWITCH_CLIENT_SECRET = (
    os.environ.get("TWITCH_CLIENT_SECRET")
    or os.environ.get("Twitch_client_secret")
    or os.environ.get("Twitch_Client_Secret")
)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_WEB_URL = "https://www.themoviedb.org"
TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/w780"
TMDB_THUMBNAIL_URL = "https://image.tmdb.org/t/p/w342"

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_BASE_URL = "https://api.igdb.com/v4"


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

if not TMDB_API_KEY:
    raise RuntimeError("TMDB_API_KEY is missing.")

if not DISCORD_GUILD_ID:
    raise RuntimeError("DISCORD_GUILD_ID is missing.")


GUILD = discord.Object(id=int(DISCORD_GUILD_ID))


# =========================================================
# DISCORD CLIENT
# =========================================================

class MediaDBClient(discord.Client):

    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        print("MediaDB setup started.")

        self.tree.copy_global_to(guild=GUILD)

        synced = await self.tree.sync(guild=GUILD)

        print(f"Synced {len(synced)} guild command(s).")

        for command in synced:
            print(f"Synced: /{command.name}")

    async def on_ready(self):
        print(f"MediaDB online as {self.user}")


client = MediaDBClient()


# =========================================================
# COMMON CHOICES
# =========================================================

TYPE_CHOICES = [
    app_commands.Choice(name="Movie", value="movie"),
    app_commands.Choice(name="Game", value="game"),
    app_commands.Choice(name="Series", value="tv"),
]


# =========================================================
# TMDB
# =========================================================

async def fetch_tmdb(
    endpoint: str,
    params: dict | None = None
) -> dict:

    if params is None:
        params = {}

    params = {
        **params,
        "api_key": TMDB_API_KEY,
        "language": "en-US",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{TMDB_BASE_URL}/{endpoint}",
            params=params
        ) as response:

            if response.status != 200:
                body = await response.text()

                raise RuntimeError(
                    f"TMDb returned HTTP "
                    f"{response.status}: "
                    f"{body[:300]}"
                )

            return await response.json()


async def get_details(
    media_type: str,
    tmdb_id: int
) -> dict:

    return await fetch_tmdb(
        f"{media_type}/{tmdb_id}",
        {
            "append_to_response":
                "credits,watch/providers"
        }
    )


# =========================================================
# IGDB AUTH
# =========================================================

_igdb_access_token = None
_igdb_token_expires_at = 0



async def get_igdb_access_token(
    force_refresh: bool = False
) -> str:

    global _igdb_access_token
    global _igdb_token_expires_at

    if not TWITCH_CLIENT_ID:
        raise RuntimeError(
            "TWITCH_CLIENT_ID is missing."
        )

    if not TWITCH_CLIENT_SECRET:
        raise RuntimeError(
            "TWITCH_CLIENT_SECRET is missing."
        )

    now = time.time()

    if (
        not force_refresh
        and _igdb_access_token
        and now < _igdb_token_expires_at
    ):
        return _igdb_access_token

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            TWITCH_TOKEN_URL,
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            }
        ) as response:

            if response.status != 200:
                body = await response.text()

                raise RuntimeError(
                    f"Twitch authentication returned "
                    f"HTTP {response.status}: "
                    f"{body[:300]}"
                )

            data = await response.json()

    token = data.get("access_token")

    expires_in = int(
        data.get("expires_in")
        or 0
    )

    if not token:
        raise RuntimeError(
            "Twitch did not return an access token."
        )

    _igdb_access_token = token

    _igdb_token_expires_at = (
        time.time()
        + max(
            expires_in - 60,
            60
        )
    )

    return token


async def fetch_igdb(
    endpoint: str,
    query: str,
    retry: bool = True
) -> list[dict]:

    token = await get_igdb_access_token()

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{IGDB_BASE_URL}/{endpoint}",
            headers=headers,
            data=query
        ) as response:

            if (
                response.status == 401
                and retry
            ):
                await get_igdb_access_token(
                    force_refresh=True
                )

                return await fetch_igdb(
                    endpoint,
                    query,
                    retry=False
                )

            if response.status != 200:
                body = await response.text()

                raise RuntimeError(
                    f"IGDB returned HTTP "
                    f"{response.status}: "
                    f"{body[:500]}"
                )

            return await response.json()


# =========================================================
# LOCAL PLATFORM AUTOCOMPLETE
# =========================================================

# Friendly labels mapped to the exact platform names used by IGDB.
# This list is local on purpose: autocomplete never makes an API call.
PLATFORM_AUTOCOMPLETE = [
    ("PC", "PC (Microsoft Windows)"),

    ("PlayStation", "PlayStation"),
    ("PlayStation 2", "PlayStation 2"),
    ("PlayStation 3", "PlayStation 3"),
    ("PlayStation 4", "PlayStation 4"),
    ("PlayStation 5", "PlayStation 5"),
    ("PSP", "PlayStation Portable"),
    ("PlayStation Vita", "PlayStation Vita"),

    ("Xbox", "Xbox"),
    ("Xbox 360", "Xbox 360"),
    ("Xbox One", "Xbox One"),
    ("Xbox Series X|S", "Xbox Series X|S"),

    ("NES", "Nintendo Entertainment System"),
    ("SNES", "Super Nintendo Entertainment System"),
    ("Nintendo 64", "Nintendo 64"),
    ("GameCube", "Nintendo GameCube"),
    ("Wii", "Wii"),
    ("Wii U", "Wii U"),
    ("Nintendo Switch", "Nintendo Switch"),
    ("Nintendo Switch 2", "Nintendo Switch 2"),
    ("Game Boy", "Game Boy"),
    ("Game Boy Color", "Game Boy Color"),
    ("Game Boy Advance", "Game Boy Advance"),
    ("Nintendo DS", "Nintendo DS"),
    ("Nintendo 3DS", "Nintendo 3DS"),

    ("Sega Master System", "Sega Master System/Mark III"),
    ("Genesis / Mega Drive", "Sega Mega Drive/Genesis"),
    ("Sega CD", "Sega CD"),
    ("Sega 32X", "Sega 32X"),
    ("Sega Saturn", "Sega Saturn"),
    ("Dreamcast", "Dreamcast"),
    ("Game Gear", "Game Gear"),

    ("Atari 2600", "Atari 2600"),
    ("Atari 5200", "Atari 5200"),
    ("Atari 7800", "Atari 7800"),
    ("Atari Lynx", "Atari Lynx"),
    ("Atari Jaguar", "Atari Jaguar"),

    ("Neo Geo AES", "Neo Geo AES"),
    ("Neo Geo CD", "Neo Geo CD"),
    ("Neo Geo Pocket", "Neo Geo Pocket"),
    ("Neo Geo Pocket Color", "Neo Geo Pocket Color"),
    ("TurboGrafx-16 / PC Engine", "TurboGrafx-16/PC Engine"),
    ("PC Engine CD", "PC Engine CD"),
    ("3DO", "3DO Interactive Multiplayer"),
    ("WonderSwan", "WonderSwan"),
    ("WonderSwan Color", "WonderSwan Color"),
]


def normalize_platform_search(
    text: str
) -> str:

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        text.lower()
    ).strip()


async def platform_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:

    query = normalize_platform_search(
        current
    )

    aliases = {
        "ps1": "playstation",
        "ps2": "playstation 2",
        "ps3": "playstation 3",
        "ps4": "playstation 4",
        "ps5": "playstation 5",
        "psp": "playstation portable",
        "vita": "playstation vita",
        "nes": "nintendo entertainment system",
        "snes": "super nintendo entertainment system",
        "n64": "nintendo 64",
        "gc": "nintendo gamecube",
        "gamecube": "nintendo gamecube",
        "gb": "game boy",
        "gbc": "game boy color",
        "gba": "game boy advance",
        "ds": "nintendo ds",
        "3ds": "nintendo 3ds",
        "switch": "nintendo switch",
        "switch2": "nintendo switch 2",
        "switch 2": "nintendo switch 2",
        "xsx": "xbox series x s",
        "series x": "xbox series x s",
        "series s": "xbox series x s",
        "genesis": "sega mega drive genesis",
        "mega drive": "sega mega drive genesis",
        "saturn": "sega saturn",
        "dreamcast": "dreamcast",
        "tg16": "turbografx 16 pc engine",
        "pc engine": "turbografx 16 pc engine",
    }

    expanded = aliases.get(
        query,
        query
    )

    ranked = []

    for display_name, igdb_name in PLATFORM_AUTOCOMPLETE:

        display_norm = normalize_platform_search(
            display_name
        )

        value_norm = normalize_platform_search(
            igdb_name
        )

        searchable = (
            f"{display_norm} {value_norm}"
        )

        if not query:
            rank = 3

        elif (
            display_norm == query
            or value_norm == query
            or value_norm == expanded
        ):
            rank = 0

        elif (
            display_norm.startswith(query)
            or value_norm.startswith(query)
            or value_norm.startswith(expanded)
        ):
            rank = 1

        elif (
            query in searchable
            or expanded in searchable
        ):
            rank = 2

        else:
            continue

        ranked.append(
            (
                rank,
                display_name.lower(),
                display_name,
                igdb_name
            )
        )

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1]
        )
    )

    return [
        app_commands.Choice(
            name=display_name,
            value=igdb_name
        )
        for _, _, display_name, igdb_name
        in ranked[:25]
    ]


# =========================================================
# TMDB FORMATTERS
# =========================================================

def format_runtime(
    details: dict,
    media_type: str
) -> str:

    runtime = None

    if media_type == "movie":
        runtime = details.get("runtime")

    else:
        runtimes = (
            details.get("episode_run_time")
            or []
        )

        if runtimes:
            runtime = runtimes[0]

    if not runtime:
        return "Runtime unavailable"

    hours = runtime // 60
    minutes = runtime % 60

    if hours and minutes:
        return f"{hours}h {minutes}m"

    if hours:
        return f"{hours}h"

    return f"{minutes}m"


def format_genres(
    details: dict
) -> str:

    genres = [
        genre.get("name")
        for genre in details.get(
            "genres",
            []
        )
        if genre.get("name")
    ]

    if not genres:
        return "Genre unavailable"

    return " \U00002022 ".join(
        genres[:3]
    )


def format_cast(
    details: dict
) -> str:

    credits = (
        details.get("credits")
        or {}
    )

    cast = (
        credits.get("cast")
        or []
    )

    names = []

    for actor in cast:

        name = actor.get("name")

        if name:
            names.append(name)

        if len(names) == 3:
            break

    if not names:
        return "Cast unavailable"

    return " \U00002022 ".join(names)


def get_us_watch_data(
    details: dict
) -> dict:

    watch_data = (
        details.get("watch/providers")
        or {}
    )

    results = (
        watch_data.get("results")
        or {}
    )

    return (
        results.get("US")
        or {}
    )


def get_us_provider_names(
    details: dict
) -> list[str]:

    us_data = get_us_watch_data(
        details
    )

    names = []

    categories = [
        "flatrate",
        "free",
        "ads",
        "rent",
        "buy",
    ]

    for category in categories:

        providers = (
            us_data.get(category)
            or []
        )

        for provider in providers:

            name = provider.get(
                "provider_name"
            )

            if (
                name
                and name not in names
            ):
                names.append(name)

    return names


def format_tv_availability(
    details: dict
) -> str | None:

    names = []

    for network in (
        details.get("networks")
        or []
    ):

        name = network.get("name")

        if (
            name
            and name not in names
        ):
            names.append(name)

    for name in get_us_provider_names(
        details
    ):

        if name not in names:
            names.append(name)

    if not names:
        return None

    return " \U00002022 ".join(
        names[:5]
    )


def format_search_availability(
    details: dict
) -> str | None:

    names = get_us_provider_names(
        details
    )

    if not names:
        return None

    return " \U00002022 ".join(
        names[:6]
    )


# =========================================================
# COMMON DATE FORMATTERS
# =========================================================

def parse_tmdb_date(
    date_string: str
) -> datetime:

    return datetime.strptime(
        date_string,
        "%Y-%m-%d"
    ).replace(
        hour=12,
        tzinfo=timezone.utc
    )


def format_release_date(
    date_string: str
) -> str:

    if not date_string:
        return "Date unavailable"

    release_date = parse_tmdb_date(
        date_string
    )

    unix_time = int(
        release_date.timestamp()
    )

    return f"<t:{unix_time}:D>"


def format_unix_date(
    timestamp: int
) -> str:

    if not timestamp:
        return "Date unavailable"

    return f"<t:{int(timestamp)}:D>"


def unix_to_date_string(
    timestamp: int
) -> str:

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc
    ).strftime(
        "%Y-%m-%d"
    )


def format_game_release_date(
    timestamp: int
) -> str:

    if not timestamp:
        return "Date unavailable"

    release_date = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc
    )

    return release_date.strftime(
        "%B %d, %Y"
    ).replace(" 0", " ")


def format_game_date_countdown(
    timestamp: int
) -> str:

    if not timestamp:
        return "Date unavailable"

    release_date = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc
    ).date()

    today = datetime.now(
        timezone.utc
    ).date()

    days_remaining = (
        release_date - today
    ).days

    if days_remaining < 0:
        return "Released"

    if days_remaining == 0:
        return "Today"

    return f"{days_remaining}d"


def format_countdown(
    date_string: str
) -> str:

    release_date = parse_tmdb_date(
        date_string
    )

    now = datetime.now(
        timezone.utc
    )

    remaining = (
        release_date - now
    )

    total_seconds = int(
        remaining.total_seconds()
    )

    if total_seconds <= 0:
        return "Released"

    days, remainder = divmod(
        total_seconds,
        86400
    )

    hours, remainder = divmod(
        remainder,
        3600
    )

    minutes, _ = divmod(
        remainder,
        60
    )

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours or days:
        parts.append(f"{hours}h")

    parts.append(f"{minutes}m")

    return " ".join(parts)


def format_exact_countdown(
    date_string: str
) -> str:

    release_date = parse_tmdb_date(
        date_string
    )

    now = datetime.now(
        timezone.utc
    )

    remaining = (
        release_date - now
    )

    total_seconds = int(
        remaining.total_seconds()
    )

    if total_seconds <= 0:
        return "Released"

    days, remainder = divmod(
        total_seconds,
        86400
    )

    hours, remainder = divmod(
        remainder,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    return (
        f"{days}d "
        f"{hours}h "
        f"{minutes}m "
        f"{seconds}s"
    )


def format_game_exact_countdown(
    timestamp: int
) -> str:

    release_date = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc
    )

    now = datetime.now(
        timezone.utc
    )

    remaining = (
        release_date - now
    )

    total_seconds = int(
        remaining.total_seconds()
    )

    if total_seconds <= 0:
        return "Released"

    days, remainder = divmod(
        total_seconds,
        86400
    )

    hours, remainder = divmod(
        remainder,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    return (
        f"{days}d "
        f"{hours}h "
        f"{minutes}m "
        f"{seconds}s"
    )


# =========================================================
# RATINGS
# =========================================================

def score_meter(
    rating: float,
    vote_count: int
) -> str:

    if vote_count <= 0:
        return (
            "\U00002b50\U0000fe0f **Not Rated Yet**\n"
            "`\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1`"
        )

    rating = max(
        0,
        min(float(rating), 10)
    )

    filled = round(rating)
    empty = 10 - filled

    bar = (
        "\U000025b0" * filled
        + "\U000025b1" * empty
    )

    return (
        f"\U00002b50\U0000fe0f **{rating:.1f}/10**\n"
        f"`{bar}`"
    )


def game_score_meter(
    game: dict
) -> str:

    rating = (
        game.get("total_rating")
        or game.get("rating")
        or 0
    )

    count = (
        game.get("total_rating_count")
        or game.get("rating_count")
        or 0
    )

    if not rating or not count:
        return (
            "\U00002b50\U0000fe0f **Not Rated Yet**\n"
            "`\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1\U000025b1`"
        )

    ten_point_rating = (
        float(rating) / 10
    )

    ten_point_rating = max(
        0,
        min(
            ten_point_rating,
            10
        )
    )

    filled = round(
        ten_point_rating
    )

    empty = 10 - filled

    bar = (
        "\U000025b0" * filled
        + "\U000025b1" * empty
    )

    return (
        f"\U00002b50\U0000fe0f **{ten_point_rating:.1f}/10**\n"
        f"`{bar}`"
    )


# =========================================================
# TITLE NORMALIZATION
# =========================================================

ROMAN_NUMERALS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}

GAME_ABBREVIATIONS = {
    "ff": "final fantasy",
    "gta": "grand theft auto",
    "rdr": "red dead redemption",
    "re": "resident evil",
    "kh": "kingdom hearts",
    "dmc": "devil may cry",
    "mgs": "metal gear solid",
    "gow": "god of war",
    "ac": "assassins creed",
    "cod": "call of duty",
}

SEARCH_NOISE_WORDS = {
    "the", "a", "an", "and", "of", "for", "edition", "game",
}


def _split_compact_token(token: str) -> list[str]:
    match = re.fullmatch(r"([a-z]+)(\d+)", token)
    if match:
        return [match.group(1), match.group(2)]

    match = re.fullmatch(r"(\d+)([a-z]+)", token)
    if match:
        return [match.group(1), match.group(2)]

    return [token]


def normalize_title(
    text: str
) -> str:

    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    tokens = []

    for raw_token in text.split():
        for token in _split_compact_token(raw_token):
            if token in ROMAN_NUMERALS:
                token = str(ROMAN_NUMERALS[token])
            tokens.append(token)

    return " ".join(tokens).strip()


def expand_game_query(text: str) -> list[str]:
    normalized = normalize_title(text)
    tokens = normalized.split()

    variants = [text, normalized]

    if tokens:
        expanded_tokens = []
        changed = False

        for token in tokens:
            replacement = GAME_ABBREVIATIONS.get(token)
            if replacement:
                expanded_tokens.extend(replacement.split())
                changed = True
            else:
                expanded_tokens.append(token)

        if changed:
            variants.append(" ".join(expanded_tokens))

    seen = set()
    final = []
    for variant in variants:
        key = normalize_title(variant)
        if key and key not in seen:
            seen.add(key)
            final.append(variant)

    return final


def _title_tokens(text: str) -> list[str]:
    return [
        token for token in normalize_title(text).split()
        if token not in SEARCH_NOISE_WORDS
    ]


def _title_acronym_variants(text: str) -> set[str]:
    tokens = _title_tokens(text)
    if not tokens:
        return set()

    parts = []
    for token in tokens:
        if token.isdigit():
            parts.append(token)
        else:
            parts.append(token[0])

    return {"".join(parts[:i]) for i in range(2, len(parts) + 1)}


def game_title_match_score(query: str, candidate: str) -> float:
    query_norm = normalize_title(query)
    candidate_norm = normalize_title(candidate)

    if not query_norm or not candidate_norm:
        return 0.0

    if query_norm == candidate_norm:
        return 1000.0

    score = SequenceMatcher(None, query_norm, candidate_norm).ratio() * 100

    if candidate_norm.startswith(query_norm + " "):
        score += 160
    elif query_norm in candidate_norm:
        score += 100

    query_tokens = set(_title_tokens(query))
    candidate_tokens = set(_title_tokens(candidate))

    if query_tokens:
        overlap = len(query_tokens & candidate_tokens) / len(query_tokens)
        score += overlap * 180

    compact_query = "".join(_title_tokens(query))
    candidate_acronyms = _title_acronym_variants(candidate)

    if compact_query in candidate_acronyms:
        score += 300

    return score


def search_relevance(
    item: dict,
    query: str
) -> tuple:

    query_norm = normalize_title(
        query
    )

    title = (
        item.get("title")
        or item.get("name")
        or ""
    )

    original_title = (
        item.get("original_title")
        or item.get("original_name")
        or ""
    )

    title_norm = normalize_title(
        title
    )

    original_norm = normalize_title(
        original_title
    )

    titles = [
        title_norm,
        original_norm
    ]

    exact = any(
        candidate == query_norm
        for candidate in titles
    )

    extension = any(
        candidate.startswith(
            query_norm + " "
        )
        for candidate in titles
    )

    contains_phrase = any(
        query_norm in candidate
        for candidate in titles
    )

    vote_count = int(
        item.get("vote_count")
        or 0
    )

    popularity = float(
        item.get("popularity")
        or 0
    )

    if exact:
        match_rank = 0

    elif extension:
        match_rank = 1

    elif contains_phrase:
        match_rank = 2

    else:
        match_rank = 3

    return (
        match_rank,
        -vote_count,
        -popularity
    )


def game_search_relevance(
    game: dict,
    query: str
) -> tuple:

    title = game.get("name") or ""
    match_score = game_title_match_score(query, title)

    rating_count = int(
        game.get("total_rating_count")
        or game.get("rating_count")
        or 0
    )

    return (
        -match_score,
        -rating_count
    )


# =========================================================
# IGDB HELPERS
# =========================================================

def igdb_escape(
    text: str
) -> str:

    return (
        text
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def igdb_cover_url(
    game: dict,
    thumbnail: bool = False
) -> str | None:

    cover = (
        game.get("cover")
        or {}
    )

    url = cover.get(
        "url"
    )

    if not url:
        return None

    if url.startswith("//"):
        url = "https:" + url

    if thumbnail:
        url = url.replace(
            "t_thumb",
            "t_cover_big"
        )

    else:
        url = url.replace(
            "t_thumb",
            "t_1080p"
        )

    return url


async def resolve_igdb_platform(
    platform_name: str
) -> dict | None:

    safe_name = igdb_escape(
        platform_name
    )

    results = await fetch_igdb(
        "platforms",
        (
            f'search "{safe_name}"; '
            f"fields id,name; "
            f"limit 10;"
        )
    )

    target = normalize_title(
        platform_name
    )

    for platform in results:

        if normalize_title(
            platform.get("name")
            or ""
        ) == target:

            return platform

    if results:
        return results[0]

    return None


def get_game_platform_names(
    game: dict
) -> list[str]:

    names = []

    for platform in (
        game.get("platforms")
        or []
    ):

        name = platform.get(
            "name"
        )

        if (
            name
            and name not in names
        ):
            names.append(name)

    for release in (
        game.get("release_dates")
        or []
    ):

        platform = (
            release.get("platform")
            or {}
        )

        name = platform.get(
            "name"
        )

        if (
            name
            and name not in names
        ):
            names.append(name)

    return names


def game_matches_platform(
    game: dict,
    platform_name: str | None
) -> bool:

    if not platform_name:
        return True

    wanted = normalize_title(
        platform_name
    )

    return any(
        normalize_title(name)
        == wanted
        for name in get_game_platform_names(
            game
        )
    )


def format_game_platforms(
    game: dict,
    selected_platform: str | None = None
) -> str:

    if selected_platform:
        return selected_platform

    names = get_game_platform_names(
        game
    )

    if not names:
        return "Platform unavailable"

    return " \U00002022 ".join(
        names[:4]
    )


def format_game_genres(
    game: dict
) -> str:

    names = []

    for genre in (
        game.get("genres")
        or []
    ):

        name = genre.get(
            "name"
        )

        if (
            name
            and name not in names
        ):
            names.append(name)

    if not names:
        return "Genre unavailable"

    return " \U00002022 ".join(
        names[:3]
    )


def format_game_companies(
    game: dict
) -> str:

    developers = []
    publishers = []

    for relationship in (
        game.get("involved_companies")
        or []
    ):

        company = (
            relationship.get("company")
            or {}
        )

        name = company.get(
            "name"
        )

        if not name:
            continue

        if relationship.get(
            "developer"
        ):

            if name not in developers:
                developers.append(name)

        elif relationship.get(
            "publisher"
        ):

            if name not in publishers:
                publishers.append(name)

    if developers:
        return " \U00002022 ".join(
            developers[:2]
        )

    if publishers:
        return " \U00002022 ".join(
            publishers[:2]
        )

    return "Studio unavailable"


def get_game_release_timestamp(
    game: dict,
    platform_name: str | None = None,
    future_only: bool = False
) -> int | None:

    now = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    possible_dates = []

    if platform_name:

        wanted = normalize_title(
            platform_name
        )

        for release in (
            game.get("release_dates")
            or []
        ):

            timestamp = release.get(
                "date"
            )

            platform = (
                release.get("platform")
                or {}
            )

            release_platform = (
                platform.get("name")
                or ""
            )

            if not timestamp:
                continue

            if (
                normalize_title(
                    release_platform
                )
                != wanted
            ):
                continue

            if (
                future_only
                and int(timestamp) < now
            ):
                continue

            possible_dates.append(
                int(timestamp)
            )

    else:

        first_release = game.get(
            "first_release_date"
        )

        if first_release:

            if (
                not future_only
                or int(first_release) >= now
            ):

                possible_dates.append(
                    int(first_release)
                )

    if not possible_dates:
        return None

    return min(
        possible_dates
    )


# =========================================================
# IGDB SEARCH
# =========================================================

async def search_games(
    title: str,
    platform_name: str | None = None
) -> list[dict]:

    fields = (
        "id,"
        "name,"
        "summary,"
        "storyline,"
        "first_release_date,"
        "release_dates.date,"
        "release_dates.platform.name,"
        "platforms.name,"
        "genres.name,"
        "cover.url,"
        "rating,"
        "rating_count,"
        "total_rating,"
        "total_rating_count,"
        "involved_companies.company.name,"
        "involved_companies.developer,"
        "involved_companies.publisher,"
        "url"
    )

    merged = {}

    for query_variant in expand_game_query(title):
        safe_title = igdb_escape(query_variant)

        try:
            batch = await fetch_igdb(
                "games",
                (
                    f'search "{safe_title}"; '
                    f"fields {fields}; "
                    f"limit 25;"
                )
            )
        except Exception as error:
            print(f"IGDB variant search error ({query_variant}): {error}")
            continue

        for game in batch:
            game_id = game.get("id")
            if game_id is not None:
                merged[game_id] = game

    results = list(merged.values())

    if platform_name:
        results = [
            game
            for game in results
            if game_matches_platform(
                game,
                platform_name
            )
        ]

    results.sort(
        key=lambda game:
            game_search_relevance(
                game,
                title
            )
    )

    return results[:10]


# =========================================================
# IGDB UPCOMING
# =========================================================

async def get_upcoming_games(
    timeframe: str,
    platform_name: str | None = None
) -> list[dict]:

    today = datetime.now(
        timezone.utc
    ).date()

    days = (
        7
        if timeframe == "week"
        else 30
    )

    end_date = (
        today
        + timedelta(days=days)
    )

    start_timestamp = int(
        datetime(
            today.year,
            today.month,
            today.day,
            tzinfo=timezone.utc
        ).timestamp()
    )

    end_timestamp = int(
        datetime(
            end_date.year,
            end_date.month,
            end_date.day,
            23,
            59,
            59,
            tzinfo=timezone.utc
        ).timestamp()
    )

    platform_filter = ""

    if platform_name:

        platform = await resolve_igdb_platform(
            platform_name
        )

        if not platform:
            raise RuntimeError(
                f"IGDB could not find platform "
                f"{platform_name}."
            )

        platform_filter = (
            f" & platform = "
            f"{platform['id']}"
        )

    fields = (
        "date,"
        "platform.name,"
        "game.id,"
        "game.name,"
        "game.summary,"
        "game.first_release_date,"
        "game.release_dates.date,"
        "game.release_dates.platform.name,"
        "game.platforms.name,"
        "game.genres.name,"
        "game.cover.url,"
        "game.rating,"
        "game.rating_count,"
        "game.total_rating,"
        "game.total_rating_count,"
        "game.involved_companies.company.name,"
        "game.involved_companies.developer,"
        "game.involved_companies.publisher,"
        "game.url"
    )

    releases = await fetch_igdb(
        "release_dates",
        (
            f"fields {fields}; "
            f"where date >= {start_timestamp} "
            f"& date <= {end_timestamp}"
            f"{platform_filter}; "
            f"sort date asc; "
            f"limit 100;"
        )
    )

    games_by_id = {}

    for release in releases:

        game = (
            release.get("game")
            or {}
        )

        game_id = game.get(
            "id"
        )

        release_timestamp = release.get(
            "date"
        )

        if (
            not game_id
            or not release_timestamp
        ):
            continue

        existing = games_by_id.get(
            game_id
        )

        if (
            existing is None
            or int(release_timestamp)
            <
            int(
                existing.get(
                    "_game_release_date"
                )
                or release_timestamp
            )
        ):

            game["_game_release_date"] = int(
                release_timestamp
            )

            platform = (
                release.get("platform")
                or {}
            )

            game["_release_platform"] = (
                platform.get("name")
            )

            games_by_id[
                game_id
            ] = game

    results = list(
        games_by_id.values()
    )

    results.sort(
        key=lambda game:
            int(
                game.get(
                    "_game_release_date"
                )
                or 0
            )
    )

    return results


# =========================================================
# TMDB MOVIE COLLECTION
# =========================================================

async def get_movie_collection_parts(
    movie_item: dict
) -> list[dict]:

    tmdb_id = movie_item.get("id")

    if not tmdb_id:
        return []

    details = await fetch_tmdb(
        f"movie/{tmdb_id}"
    )

    collection = details.get(
        "belongs_to_collection"
    )

    if not collection:
        return []

    collection_id = collection.get(
        "id"
    )

    if not collection_id:
        return []

    data = await fetch_tmdb(
        f"collection/{collection_id}"
    )

    parts = []

    for item in data.get(
        "parts",
        []
    ):

        item["_media_type"] = "movie"
        item["_from_collection"] = True

        parts.append(item)

    parts.sort(
        key=lambda item:
            item.get("release_date")
            or "9999-12-31"
    )

    return parts


# =========================================================
# TMDB RELEASE DATE HELPERS
# =========================================================

async def get_us_movie_release_date(
    movie_id: int
) -> str | None:

    data = await fetch_tmdb(
        f"movie/{movie_id}/release_dates"
    )

    theatrical_dates = []

    for country in data.get(
        "results",
        []
    ):

        if (
            country.get("iso_3166_1")
            != "US"
        ):
            continue

        for release in country.get(
            "release_dates",
            []
        ):

            release_type = release.get(
                "type"
            )

            if release_type not in (
                3,
                2
            ):
                continue

            raw_date = release.get(
                "release_date"
            )

            if not raw_date:
                continue

            try:

                parsed = datetime.fromisoformat(
                    raw_date.replace(
                        "Z",
                        "+00:00"
                    )
                )

            except ValueError:
                continue

            theatrical_dates.append(
                (
                    release_type,
                    parsed
                )
            )

    if not theatrical_dates:
        return None

    today = datetime.now(
        timezone.utc
    ).date()

    future_dates = [
        entry
        for entry in theatrical_dates
        if entry[1].date() >= today
    ]

    if future_dates:

        future_dates.sort(
            key=lambda entry: (
                entry[1].date(),
                0
                if entry[0] == 3
                else 1
            )
        )

        return (
            future_dates[0][1]
            .date()
            .isoformat()
        )

    theatrical_dates.sort(
        key=lambda entry: (
            entry[1].date(),
            0
            if entry[0] == 3
            else 1
        )
    )

    return (
        theatrical_dates[0][1]
        .date()
        .isoformat()
    )


async def verify_us_movie_release(
    item: dict,
    start_date,
    end_date
) -> dict | None:

    tmdb_id = item.get("id")

    if not tmdb_id:
        return None

    data = await fetch_tmdb(
        f"movie/{tmdb_id}/release_dates"
    )

    us_entries = None

    for country in data.get(
        "results",
        []
    ):

        if (
            country.get(
                "iso_3166_1"
            )
            == "US"
        ):

            us_entries = country
            break

    if not us_entries:
        return None

    possible_dates = []

    for release in us_entries.get(
        "release_dates",
        []
    ):

        release_type = release.get(
            "type"
        )

        if release_type not in (
            3,
            2
        ):
            continue

        raw_date = release.get(
            "release_date"
        )

        if not raw_date:
            continue

        try:

            release_date = (
                datetime.fromisoformat(
                    raw_date.replace(
                        "Z",
                        "+00:00"
                    )
                ).date()
            )

        except ValueError:
            continue

        if (
            start_date
            <= release_date
            <= end_date
        ):

            possible_dates.append(
                (
                    release_type,
                    release_date
                )
            )

    if not possible_dates:
        return None

    possible_dates.sort(
        key=lambda entry: (
            0
            if entry[0] == 3
            else 1,
            entry[1]
        )
    )

    chosen_date = (
        possible_dates[0][1]
    )

    verified = dict(item)

    verified["release_date"] = (
        chosen_date.isoformat()
    )

    return verified


async def verify_us_tv_relevance(
    item: dict
) -> dict | None:

    tmdb_id = item.get("id")

    if not tmdb_id:
        return None

    details = await get_details(
        "tv",
        tmdb_id
    )

    origin_countries = (
        details.get("origin_country")
        or item.get("origin_country")
        or []
    )

    is_us_origin = (
        "US" in origin_countries
    )

    us_providers = (
        get_us_provider_names(
            details
        )
    )

    if (
        not is_us_origin
        and not us_providers
    ):
        return None

    verified = dict(item)

    verified["_details"] = details

    return verified


# =========================================================
# TMDB UPCOMING
# =========================================================

async def get_upcoming(
    media_type: str,
    timeframe: str
) -> list[dict]:

    today = datetime.now(
        timezone.utc
    ).date()

    days = (
        7
        if timeframe == "week"
        else 30
    )

    end_date = (
        today
        + timedelta(days=days)
    )

    if media_type == "movie":

        endpoint = "discover/movie"
        date_field = "release_date"

        params = {
            "region": "US",

            "with_release_type":
                "3|2",

            "release_date.gte":
                today.isoformat(),

            "release_date.lte":
                end_date.isoformat(),

            "sort_by":
                "release_date.asc",

            "include_adult":
                "false",
        }

    else:

        endpoint = "discover/tv"
        date_field = "first_air_date"

        params = {
            "first_air_date.gte":
                today.isoformat(),

            "first_air_date.lte":
                end_date.isoformat(),

            "sort_by":
                "first_air_date.asc",

            "include_null_first_air_dates":
                "false",

            "include_adult":
                "false",
        }

    data = await fetch_tmdb(
        endpoint,
        params
    )

    candidates = []

    for item in data.get(
        "results",
        []
    ):

        date_string = item.get(
            date_field
        )

        if not date_string:
            continue

        try:

            item_date = datetime.strptime(
                date_string,
                "%Y-%m-%d"
            ).date()

        except ValueError:
            continue

        if (
            today
            <= item_date
            <= end_date
        ):

            candidates.append(item)

    if media_type == "movie":

        checks = [
            verify_us_movie_release(
                item,
                today,
                end_date
            )
            for item in candidates
        ]

    else:

        checks = [
            verify_us_tv_relevance(
                item
            )
            for item in candidates
        ]

    checked_results = await asyncio.gather(
        *checks,
        return_exceptions=True
    )

    results = []

    for result in checked_results:

        if isinstance(
            result,
            Exception
        ):

            print(
                f"Filtering error: {result}"
            )

            continue

        if result is not None:
            results.append(result)

    results.sort(
        key=lambda item: (
            item.get(
                date_field,
                ""
            ),
            -float(
                item.get(
                    "popularity"
                )
                or 0
            )
        )
    )

    return results


# =========================================================
# TMDB SEARCH
# =========================================================

async def search_titles(
    title: str,
    media_type: str | None
) -> list[dict]:

    results = []

    if media_type == "movie":

        data = await fetch_tmdb(
            "search/movie",
            {
                "query": title,
                "include_adult": "false",
            }
        )

        for item in data.get(
            "results",
            []
        ):

            item["_media_type"] = "movie"
            results.append(item)

    elif media_type == "tv":

        data = await fetch_tmdb(
            "search/tv",
            {
                "query": title,
                "include_adult": "false",
            }
        )

        for item in data.get(
            "results",
            []
        ):

            item["_media_type"] = "tv"
            results.append(item)

    else:

        movie_data, tv_data = await asyncio.gather(

            fetch_tmdb(
                "search/movie",
                {
                    "query": title,
                    "include_adult": "false",
                }
            ),

            fetch_tmdb(
                "search/tv",
                {
                    "query": title,
                    "include_adult": "false",
                }
            )
        )

        for item in movie_data.get(
            "results",
            []
        ):

            item["_media_type"] = "movie"
            results.append(item)

        for item in tv_data.get(
            "results",
            []
        ):

            item["_media_type"] = "tv"
            results.append(item)

    query_norm = normalize_title(
        title
    )

    relevant = []

    for item in results:

        item_title = (
            item.get("title")
            or item.get("name")
            or ""
        )

        original_title = (
            item.get("original_title")
            or item.get("original_name")
            or ""
        )

        title_norm = normalize_title(
            item_title
        )

        original_norm = normalize_title(
            original_title
        )

        if (
            title_norm == query_norm
            or original_norm == query_norm
            or title_norm.startswith(
                query_norm + " "
            )
            or original_norm.startswith(
                query_norm + " "
            )
            or query_norm in title_norm
            or query_norm in original_norm
        ):

            relevant.append(item)

    relevant.sort(
        key=lambda item:
            search_relevance(
                item,
                title
            )
    )

    collection_results = []

    strongest_movie = next(
        (
            item
            for item in relevant
            if item.get(
                "_media_type"
            ) == "movie"
        ),
        None
    )

    if strongest_movie:

        try:

            collection_results = (
                await get_movie_collection_parts(
                    strongest_movie
                )
            )

        except Exception as error:

            print(
                f"Collection lookup error: {error}"
            )

    final_results = []

    seen = set()

    def add_unique(
        item: dict
    ):

        media = item.get(
            "_media_type"
        )

        item_id = item.get(
            "id"
        )

        key = (
            media,
            item_id
        )

        if (
            item_id is not None
            and key not in seen
        ):

            seen.add(key)

            final_results.append(
                item
            )

    if relevant:
        add_unique(
            relevant[0]
        )

    for item in collection_results:
        add_unique(item)

    for item in relevant:
        add_unique(item)

    return final_results[:10]


# =========================================================
# UPCOMING EMBEDS
# =========================================================

async def build_upcoming_embed(
    item: dict,
    media_type: str
) -> discord.Embed:

    tmdb_id = item.get("id")

    details = item.get(
        "_details"
    )

    if not details:

        details = await get_details(
            media_type,
            tmdb_id
        )

    if media_type == "movie":

        title = (
            details.get("title")
            or item.get("title")
            or "Untitled"
        )

        date_string = item.get(
            "release_date"
        )

        media_label = "MOVIE"

    else:

        title = (
            details.get("name")
            or item.get("name")
            or "Untitled"
        )

        date_string = item.get(
            "first_air_date"
        )

        media_label = "SERIES"

    page_url = (
        f"{TMDB_WEB_URL}/"
        f"{media_type}/"
        f"{tmdb_id}"
    )

    genre_text = format_genres(
        details
    )

    cast_text = format_cast(
        details
    )

    overview = (
        details.get("overview")
        or item.get("overview")
        or "No synopsis is currently available."
    ).strip()

    if len(overview) > 650:

        overview = (
            overview[:647].rstrip()
            + "..."
        )

    rating = float(
        details.get("vote_average")
        or item.get("vote_average")
        or 0
    )

    vote_count = int(
        details.get("vote_count")
        or item.get("vote_count")
        or 0
    )

    if media_type == "movie":

        runtime_text = format_runtime(
            details,
            "movie"
        )

        metadata_lines = [
            f"\U0001f3f7\U0000fe0f *{genre_text}*",
            f"\U0001f3ad **{cast_text}**",
            f"\U0001f552 **{runtime_text}**",
        ]

    else:

        metadata_lines = [
            f"\U0001f3f7\U0000fe0f *{genre_text}*",
            f"\U0001f3ad **{cast_text}**",
        ]

        availability = (
            format_tv_availability(
                details
            )
        )

        if availability:

            metadata_lines.append(
                f"\U0001f4fa **{availability}**"
            )

    metadata = "\n".join(
        metadata_lines
    )

    description = (
        f"{metadata}\n\n"
        f"{overview}\n\n"
        f"\U0001f4c5 **{format_release_date(date_string)}**\n"
        f"\U000023f3 **{format_countdown(date_string)}**\n"
        f"{score_meter(rating, vote_count)}"
    )

    embed = discord.Embed(
        title=title,
        url=page_url,
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name=(
            f"MEDIADB  \U00002022  "
            f"{media_label}"
        )
    )

    poster_path = (
        details.get("poster_path")
        or item.get("poster_path")
    )

    if poster_path:

        embed.set_image(
            url=(
                f"{TMDB_IMAGE_URL}"
                f"{poster_path}"
            )
        )

    if (
        media_type == "tv"
        and get_us_provider_names(
            details
        )
    ):

        embed.set_footer(
            text=(
                "Data provided by TMDb "
                "\U00002022 Availability powered by JustWatch"
            )
        )

    else:

        embed.set_footer(
            text="Data provided by TMDb"
        )

    return embed


async def build_game_upcoming_embed(
    game: dict,
    selected_platform: str | None
) -> discord.Embed:

    title = (
        game.get("name")
        or "Untitled Game"
    )

    release_timestamp = (
        game.get(
            "_game_release_date"
        )
    )

    summary = (
        game.get("summary")
        or game.get("storyline")
        or "No synopsis is currently available."
    ).strip()

    if len(summary) > 650:

        summary = (
            summary[:647].rstrip()
            + "..."
        )

    genre_text = format_game_genres(
        game
    )

    company_text = format_game_companies(
        game
    )

    platform_text = format_game_platforms(
        game,
        selected_platform
        or game.get(
            "_release_platform"
        )
    )

    metadata = "\n".join(
        [
            f"\U0001f3f7\U0000fe0f *{genre_text}*",
            f"\U0001f3e2 **{company_text}**",
            f"\U0001f3ae **{platform_text}**",
        ]
    )

    description = (
        f"{metadata}\n\n"
        f"{summary}\n\n"
        f"\U0001f4c5 **{format_game_release_date(release_timestamp)}**\n"
        f"\U000023f3 **{format_game_date_countdown(release_timestamp)}**\n"
        f"{game_score_meter(game)}"
    )

    embed = discord.Embed(
        title=title,
        url=game.get("url"),
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name="MEDIADB  \U00002022  GAME"
    )

    cover_url = igdb_cover_url(
        game
    )

    if cover_url:

        embed.set_image(
            url=cover_url
        )

    embed.set_footer(
        text="Game data provided by IGDB"
    )

    return embed


# =========================================================
# SEARCH EMBEDS
# =========================================================

async def build_search_embed(
    item: dict
) -> discord.Embed:

    media_type = item.get(
        "_media_type"
    )

    tmdb_id = item.get(
        "id"
    )

    details = await get_details(
        media_type,
        tmdb_id
    )

    if media_type == "movie":

        title = (
            details.get("title")
            or item.get("title")
            or "Untitled"
        )

        date_string = (
            details.get("release_date")
            or item.get("release_date")
        )

        media_label = "MOVIE"

    else:

        title = (
            details.get("name")
            or item.get("name")
            or "Untitled"
        )

        date_string = (
            details.get("first_air_date")
            or item.get("first_air_date")
        )

        media_label = "SERIES"

    year = ""

    if date_string:
        year = date_string[:4]

    display_title = (
        f"{title} ({year})"
        if year
        else title
    )

    page_url = (
        f"{TMDB_WEB_URL}/"
        f"{media_type}/"
        f"{tmdb_id}"
    )

    genre_text = format_genres(
        details
    )

    cast_text = format_cast(
        details
    )

    runtime_text = format_runtime(
        details,
        media_type
    )

    availability = (
        format_search_availability(
            details
        )
    )

    overview = (
        details.get("overview")
        or item.get("overview")
        or "No synopsis is currently available."
    ).strip()

    if len(overview) > 650:

        overview = (
            overview[:647].rstrip()
            + "..."
        )

    rating = float(
        details.get("vote_average")
        or item.get("vote_average")
        or 0
    )

    vote_count = int(
        details.get("vote_count")
        or item.get("vote_count")
        or 0
    )

    metadata_lines = [
        f"\U0001f3f7\U0000fe0f *{genre_text}*",
        f"\U0001f3ad **{cast_text}**",
        f"\U0001f552 **{runtime_text}**",
    ]

    if availability:

        metadata_lines.append(
            f"\U0001f4fa **{availability}**"
        )

    metadata = "\n".join(
        metadata_lines
    )

    description = (
        f"{metadata}\n\n"
        f"{overview}\n\n"
        f"\U0001f4c5 **{format_release_date(date_string)}**\n"
        f"{score_meter(rating, vote_count)}"
    )

    embed = discord.Embed(
        title=display_title,
        url=page_url,
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name=(
            f"MEDIADB  \U00002022  "
            f"{media_label}"
        )
    )

    poster_path = (
        details.get("poster_path")
        or item.get("poster_path")
    )

    if poster_path:

        embed.set_image(
            url=(
                f"{TMDB_IMAGE_URL}"
                f"{poster_path}"
            )
        )

    if availability:

        embed.set_footer(
            text=(
                "Data provided by TMDb "
                "\U00002022 Availability powered by JustWatch"
            )
        )

    else:

        embed.set_footer(
            text="Data provided by TMDb"
        )

    return embed


async def build_game_search_embed(
    game: dict,
    selected_platform: str | None
) -> discord.Embed:

    title = (
        game.get("name")
        or "Untitled Game"
    )

    release_timestamp = (
        get_game_release_timestamp(
            game,
            selected_platform
        )
    )

    if not release_timestamp:

        release_timestamp = (
            game.get(
                "first_release_date"
            )
        )

    year = ""

    if release_timestamp:

        year = datetime.fromtimestamp(
            int(release_timestamp),
            tz=timezone.utc
        ).strftime(
            "%Y"
        )

    display_title = (
        f"{title} ({year})"
        if year
        else title
    )

    genre_text = format_game_genres(
        game
    )

    company_text = format_game_companies(
        game
    )

    platform_text = format_game_platforms(
        game,
        selected_platform
    )

    summary = (
        game.get("summary")
        or game.get("storyline")
        or "No synopsis is currently available."
    ).strip()

    if len(summary) > 650:

        summary = (
            summary[:647].rstrip()
            + "..."
        )

    metadata = "\n".join(
        [
            f"\U0001f3f7\U0000fe0f *{genre_text}*",
            f"\U0001f3e2 **{company_text}**",
            f"\U0001f3ae **{platform_text}**",
        ]
    )

    description = (
        f"{metadata}\n\n"
        f"{summary}"
    )

    if release_timestamp:

        description += (
            f"\n\n"
            f"\U0001f4c5 **{format_unix_date(release_timestamp)}**"
        )

    description += (
        f"\n"
        f"{game_score_meter(game)}"
    )

    embed = discord.Embed(
        title=display_title,
        url=game.get("url"),
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name="MEDIADB  \U00002022  GAME"
    )

    cover_url = igdb_cover_url(
        game
    )

    if cover_url:

        embed.set_image(
            url=cover_url
        )

    embed.set_footer(
        text="Game data provided by IGDB"
    )

    return embed


# =========================================================
# COUNTDOWN HELPERS + EMBEDS
# =========================================================

async def get_future_countdown_item(
    results: list[dict]
) -> tuple[dict, str] | None:

    today = datetime.now(
        timezone.utc
    ).date()

    for item in results:

        media_type = item.get(
            "_media_type"
        )

        tmdb_id = item.get(
            "id"
        )

        if not tmdb_id:
            continue

        try:

            details = await get_details(
                media_type,
                tmdb_id
            )

            if media_type == "movie":

                original_date_string = (
                    details.get(
                        "release_date"
                    )
                    or item.get(
                        "release_date"
                    )
                )

                if not original_date_string:
                    continue

                original_release_date = (
                    datetime.strptime(
                        original_date_string,
                        "%Y-%m-%d"
                    ).date()
                )

                if (
                    original_release_date
                    < today
                ):
                    continue

                date_string = (
                    await get_us_movie_release_date(
                        tmdb_id
                    )
                )

                if not date_string:
                    date_string = (
                        original_date_string
                    )

            else:

                date_string = (
                    details.get(
                        "first_air_date"
                    )
                    or item.get(
                        "first_air_date"
                    )
                )

                if not date_string:
                    continue

                first_air_date = (
                    datetime.strptime(
                        date_string,
                        "%Y-%m-%d"
                    ).date()
                )

                if first_air_date < today:
                    continue

            countdown_date = (
                datetime.strptime(
                    date_string,
                    "%Y-%m-%d"
                ).date()
            )

            if countdown_date < today:
                continue

            return (
                item,
                date_string
            )

        except Exception as error:

            print(
                f"Countdown filter error: {error}"
            )

            continue

    return None


def get_future_game_countdown_item(
    results: list[dict],
    platform_name: str | None
) -> tuple[dict, int] | None:

    for game in results:

        timestamp = (
            get_game_release_timestamp(
                game,
                platform_name,
                future_only=True
            )
        )

        if not timestamp:
            continue

        return (
            game,
            timestamp
        )

    return None


async def build_countdown_embed(
    item: dict,
    date_string: str
) -> discord.Embed:

    media_type = item.get(
        "_media_type"
    )

    tmdb_id = item.get(
        "id"
    )

    details = await get_details(
        media_type,
        tmdb_id
    )

    if media_type == "movie":

        title = (
            details.get("title")
            or item.get("title")
            or "Untitled"
        )

        media_label = "MOVIE"

    else:

        title = (
            details.get("name")
            or item.get("name")
            or "Untitled"
        )

        media_label = "SERIES"

    page_url = (
        f"{TMDB_WEB_URL}/"
        f"{media_type}/"
        f"{tmdb_id}"
    )

    description = (
        f"\U0001f4c5 **{format_release_date(date_string)}**\n"
        f"\U000023f3 **{format_exact_countdown(date_string)}**"
    )

    embed = discord.Embed(
        title=title,
        url=page_url,
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name=(
            f"MEDIADB  \U00002022  "
            f"{media_label} COUNTDOWN"
        )
    )

    poster_path = (
        details.get("poster_path")
        or item.get("poster_path")
    )

    if poster_path:

        embed.set_thumbnail(
            url=(
                f"{TMDB_THUMBNAIL_URL}"
                f"{poster_path}"
            )
        )

    embed.set_footer(
        text="Data provided by TMDb"
    )

    return embed


async def build_game_countdown_embed(
    game: dict,
    timestamp: int,
    selected_platform: str | None
) -> discord.Embed:

    title = (
        game.get("name")
        or "Untitled Game"
    )

    platform_text = (
        selected_platform
        or format_game_platforms(
            game
        )
    )

    description = (
        f"\U0001f3ae **{platform_text}**\n"
        f"\U0001f4c5 **{format_unix_date(timestamp)}**\n"
        f"\U000023f3 **{format_game_exact_countdown(timestamp)}**"
    )

    embed = discord.Embed(
        title=title,
        url=game.get("url"),
        description=description,
        color=discord.Color.from_rgb(
            40,
            105,
            150
        )
    )

    embed.set_author(
        name=(
            "MEDIADB  \U00002022  "
            "GAME COUNTDOWN"
        )
    )

    cover_url = igdb_cover_url(
        game,
        thumbnail=True
    )

    if cover_url:

        embed.set_thumbnail(
            url=cover_url
        )

    embed.set_footer(
        text="Game data provided by IGDB"
    )

    return embed


# =========================================================
# EXISTING TMDB BROWSERS
# =========================================================

class ReleaseBrowser(
    discord.ui.View
):

    def __init__(
        self,
        results: list[dict],
        media_type: str,
        requester_id: int
    ):

        super().__init__(
            timeout=300
        )

        self.results = results
        self.media_type = media_type
        self.requester_id = requester_id

        self.page = 0

        self.total_pages = len(
            results
        )

        self.update_buttons()

    def update_buttons(
        self
    ):

        has_multiple_pages = (
            self.total_pages > 1
        )

        self.previous_button.disabled = (
            not has_multiple_pages
        )

        self.next_button.disabled = (
            not has_multiple_pages
        )

        self.page_button.label = (
            f"{self.page + 1} "
            f"/ {self.total_pages}"
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:

        if (
            interaction.user.id
            != self.requester_id
        ):

            await interaction.response.send_message(
                "Run `/upcoming` to open "
                "your own MediaDB browser.",
                ephemeral=True
            )

            return False

        return True

    async def get_current_embed(
        self
    ) -> discord.Embed:

        item = self.results[
            self.page
        ]

        return await build_upcoming_embed(
            item,
            self.media_type
        )

    @discord.ui.button(
        label="Previous",
        emoji="\U000025c0\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page - 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

    @discord.ui.button(
        label="1 / 1",
        style=discord.ButtonStyle.secondary,
        disabled=True
    )
    async def page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        pass

    @discord.ui.button(
        label="Next",
        emoji="\U000025b6\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page + 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )


class SearchBrowser(
    discord.ui.View
):

    def __init__(
        self,
        results: list[dict],
        requester_id: int
    ):

        super().__init__(
            timeout=300
        )

        self.results = results
        self.requester_id = requester_id

        self.page = 0

        self.total_pages = len(
            results
        )

        self.update_buttons()

    def update_buttons(
        self
    ):

        has_multiple_pages = (
            self.total_pages > 1
        )

        self.previous_button.disabled = (
            not has_multiple_pages
        )

        self.next_button.disabled = (
            not has_multiple_pages
        )

        self.page_button.label = (
            f"{self.page + 1} "
            f"/ {self.total_pages}"
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:

        if (
            interaction.user.id
            != self.requester_id
        ):

            await interaction.response.send_message(
                "Run `/search` to open "
                "your own MediaDB search.",
                ephemeral=True
            )

            return False

        return True

    async def get_current_embed(
        self
    ) -> discord.Embed:

        item = self.results[
            self.page
        ]

        return await build_search_embed(
            item
        )

    @discord.ui.button(
        label="Previous",
        emoji="\U000025c0\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page - 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

    @discord.ui.button(
        label="1 / 1",
        style=discord.ButtonStyle.secondary,
        disabled=True
    )
    async def page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        pass

    @discord.ui.button(
        label="Next",
        emoji="\U000025b6\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page + 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )


# =========================================================
# GAME BROWSERS
# =========================================================

class GameReleaseBrowser(
    discord.ui.View
):

    def __init__(
        self,
        results: list[dict],
        requester_id: int,
        platform_name: str | None
    ):

        super().__init__(
            timeout=300
        )

        self.results = results
        self.requester_id = requester_id
        self.platform_name = platform_name

        self.page = 0

        self.total_pages = len(
            results
        )

        self.update_buttons()

    def update_buttons(
        self
    ):

        multiple = (
            self.total_pages > 1
        )

        self.previous_button.disabled = (
            not multiple
        )

        self.next_button.disabled = (
            not multiple
        )

        self.page_button.label = (
            f"{self.page + 1} "
            f"/ {self.total_pages}"
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:

        if (
            interaction.user.id
            != self.requester_id
        ):

            await interaction.response.send_message(
                "Run `/upcoming` to open "
                "your own MediaDB browser.",
                ephemeral=True
            )

            return False

        return True

    async def get_current_embed(
        self
    ) -> discord.Embed:

        game = self.results[
            self.page
        ]

        return await build_game_upcoming_embed(
            game,
            self.platform_name
        )

    @discord.ui.button(
        label="Previous",
        emoji="\U000025c0\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page - 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

    @discord.ui.button(
        label="1 / 1",
        style=discord.ButtonStyle.secondary,
        disabled=True
    )
    async def page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        pass

    @discord.ui.button(
        label="Next",
        emoji="\U000025b6\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page + 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )


class GameSearchBrowser(
    discord.ui.View
):

    def __init__(
        self,
        results: list[dict],
        requester_id: int,
        platform_name: str | None
    ):

        super().__init__(
            timeout=300
        )

        self.results = results
        self.requester_id = requester_id
        self.platform_name = platform_name

        self.page = 0

        self.total_pages = len(
            results
        )

        self.update_buttons()

    def update_buttons(
        self
    ):

        multiple = (
            self.total_pages > 1
        )

        self.previous_button.disabled = (
            not multiple
        )

        self.next_button.disabled = (
            not multiple
        )

        self.page_button.label = (
            f"{self.page + 1} "
            f"/ {self.total_pages}"
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:

        if (
            interaction.user.id
            != self.requester_id
        ):

            await interaction.response.send_message(
                "Run `/search` to open "
                "your own MediaDB search.",
                ephemeral=True
            )

            return False

        return True

    async def get_current_embed(
        self
    ) -> discord.Embed:

        game = self.results[
            self.page
        ]

        return await build_game_search_embed(
            game,
            self.platform_name
        )

    @discord.ui.button(
        label="Previous",
        emoji="\U000025c0\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page - 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

    @discord.ui.button(
        label="1 / 1",
        style=discord.ButtonStyle.secondary,
        disabled=True
    )
    async def page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        pass

    @discord.ui.button(
        label="Next",
        emoji="\U000025b6\U0000fe0f",
        style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page = (
            self.page + 1
        ) % self.total_pages

        self.update_buttons()

        embed = await self.get_current_embed()

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )


# =========================================================
# /UPCOMING
# =========================================================

@client.tree.command(
    name="upcoming",
    description="Browse upcoming movie, game, or series releases."
)
@app_commands.describe(
    media_type="Choose Movie, Game, or Series.",
    timeframe="Choose week or month.",
    platform="Optional game platform. Start typing to search platforms."
)
@app_commands.rename(
    media_type="type",
    timeframe="time"
)
@app_commands.choices(
    media_type=TYPE_CHOICES,
    timeframe=[
        app_commands.Choice(
            name="Week",
            value="week"
        ),
        app_commands.Choice(
            name="Month",
            value="month"
        ),
    ]
)
@app_commands.autocomplete(
    platform=platform_autocomplete
)
async def upcoming(
    interaction: discord.Interaction,
    media_type: app_commands.Choice[str],
    timeframe: app_commands.Choice[str],
    platform: str | None = None
):

    if (
        platform
        and media_type.value != "game"
    ):

        await interaction.response.send_message(
            "`platform` only applies when "
            "`type` is set to **Game**.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    platform_name = (
        platform
        if platform
        else None
    )

    if media_type.value == "game":

        try:

            results = await get_upcoming_games(
                timeframe.value,
                platform_name
            )

        except Exception as error:

            print(
                f"IGDB upcoming error: {error}"
            )

            await interaction.followup.send(
                "MediaDB couldn't retrieve "
                "game release information right now."
            )

            return

        if not results:

            period = (
                "the next 7 days"
                if timeframe.value == "week"
                else "the next 30 days"
            )

            platform_text = (
                f" for **{platform_name}**"
                if platform_name
                else ""
            )

            await interaction.followup.send(
                f"No game releases were found "
                f"in {period}{platform_text}."
            )

            return

        view = GameReleaseBrowser(
            results=results,
            requester_id=interaction.user.id,
            platform_name=platform_name
        )

        try:

            embed = await view.get_current_embed()

        except Exception as error:

            print(
                f"IGDB game embed error: {error}"
            )

            await interaction.followup.send(
                "MediaDB found game releases, "
                "but couldn't load their details."
            )

            return

        await interaction.followup.send(
            embed=embed,
            view=view
        )

        return

    try:

        results = await get_upcoming(
            media_type.value,
            timeframe.value
        )

    except Exception as error:

        print(
            f"TMDb error: {error}"
        )

        await interaction.followup.send(
            "MediaDB couldn't retrieve "
            "release information right now."
        )

        return

    if not results:

        await interaction.followup.send(
            "No releases were found "
            "for that period."
        )

        return

    view = ReleaseBrowser(
        results=results,
        media_type=media_type.value,
        requester_id=interaction.user.id
    )

    try:

        embed = await view.get_current_embed()

    except Exception as error:

        print(
            f"TMDb detail error: {error}"
        )

        await interaction.followup.send(
            "MediaDB found releases, "
            "but couldn't load their details."
        )

        return

    await interaction.followup.send(
        embed=embed,
        view=view
    )


# =========================================================
# /SEARCH
# =========================================================

@client.tree.command(
    name="search",
    description="Search for a movie, game, or series."
)
@app_commands.describe(
    media_type="Choose Movie, Game, or Series.",
    title="Title to search for.",
    platform="Optional game platform. Start typing to search platforms."
)
@app_commands.rename(
    media_type="type"
)
@app_commands.choices(
    media_type=TYPE_CHOICES
)
@app_commands.autocomplete(
    platform=platform_autocomplete
)
async def search(
    interaction: discord.Interaction,
    media_type: app_commands.Choice[str],
    title: str,
    platform: str | None = None
):

    if (
        platform
        and media_type.value != "game"
    ):

        await interaction.response.send_message(
            "`platform` only applies when "
            "`type` is set to **Game**.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    platform_name = (
        platform
        if platform
        else None
    )

    if media_type.value == "game":

        try:

            results = await search_games(
                title,
                platform_name
            )

        except Exception as error:

            print(
                f"IGDB search error: {error}"
            )

            await interaction.followup.send(
                "MediaDB couldn't complete "
                "that game search right now."
            )

            return

        if not results:

            platform_text = (
                f" on **{platform_name}**"
                if platform_name
                else ""
            )

            await interaction.followup.send(
                f"No relevant game results "
                f"found for **{title}**"
                f"{platform_text}."
            )

            return

        view = GameSearchBrowser(
            results=results,
            requester_id=interaction.user.id,
            platform_name=platform_name
        )

        try:

            embed = await view.get_current_embed()

        except Exception as error:

            print(
                f"IGDB search embed error: {error}"
            )

            await interaction.followup.send(
                "MediaDB found a game, "
                "but couldn't load its details."
            )

            return

        await interaction.followup.send(
            embed=embed,
            view=view
        )

        return

    try:

        results = await search_titles(
            title,
            media_type.value
        )

    except Exception as error:

        print(
            f"Search error: {error}"
        )

        await interaction.followup.send(
            "MediaDB couldn't complete "
            "that search right now."
        )

        return

    if not results:

        await interaction.followup.send(
            f"No relevant results found for "
            f"**{title}**."
        )

        return

    view = SearchBrowser(
        results=results,
        requester_id=interaction.user.id
    )

    try:

        embed = await view.get_current_embed()

    except Exception as error:

        print(
            f"Search detail error: {error}"
        )

        await interaction.followup.send(
            "MediaDB found a result, "
            "but couldn't load its details."
        )

        return

    await interaction.followup.send(
        embed=embed,
        view=view
    )


# =========================================================
# /COUNTDOWN
# =========================================================

@client.tree.command(
    name="countdown",
    description="Countdown to an upcoming movie, game, or series release."
)
@app_commands.describe(
    media_type="Choose Movie, Game, or Series.",
    title="Upcoming title.",
    platform="Optional game platform. Start typing to search platforms."
)
@app_commands.rename(
    media_type="type"
)
@app_commands.choices(
    media_type=TYPE_CHOICES
)
@app_commands.autocomplete(
    platform=platform_autocomplete
)
async def countdown(
    interaction: discord.Interaction,
    media_type: app_commands.Choice[str],
    title: str,
    platform: str | None = None
):

    if (
        platform
        and media_type.value != "game"
    ):

        await interaction.response.send_message(
            "`platform` only applies when "
            "`type` is set to **Game**.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    platform_name = (
        platform
        if platform
        else None
    )

    if media_type.value == "game":

        try:

            results = await search_games(
                title,
                platform_name
            )

        except Exception as error:

            print(
                f"IGDB countdown search error: {error}"
            )

            await interaction.followup.send(
                "MediaDB couldn't search "
                "for that game right now."
            )

            return

        if not results:

            await interaction.followup.send(
                f"No relevant upcoming game "
                f"was found for **{title}**."
            )

            return

        future_match = (
            get_future_game_countdown_item(
                results,
                platform_name
            )
        )

        if not future_match:

            platform_text = (
                f" for **{platform_name}**"
                if platform_name
                else ""
            )

            await interaction.followup.send(
                f"No unreleased game matching "
                f"**{title}**{platform_text} "
                f"was found."
            )

            return

        game, timestamp = (
            future_match
        )

        try:

            embed = (
                await build_game_countdown_embed(
                    game,
                    timestamp,
                    platform_name
                )
            )

        except Exception as error:

            print(
                f"IGDB countdown embed error: {error}"
            )

            await interaction.followup.send(
                "MediaDB found the game, "
                "but couldn't load its countdown."
            )

            return

        await interaction.followup.send(
            embed=embed
        )

        return

    try:

        results = await search_titles(
            title,
            media_type.value
        )

    except Exception as error:

        print(
            f"Countdown search error: {error}"
        )

        await interaction.followup.send(
            "MediaDB couldn't search "
            "for that title right now."
        )

        return

    if not results:

        await interaction.followup.send(
            f"No relevant upcoming title "
            f"was found for **{title}**."
        )

        return

    future_match = (
        await get_future_countdown_item(
            results
        )
    )

    if not future_match:

        await interaction.followup.send(
            f"No unreleased movie or series "
            f"matching **{title}** was found."
        )

        return

    result, date_string = (
        future_match
    )

    try:

        embed = await build_countdown_embed(
            result,
            date_string
        )

    except Exception as error:

        print(
            f"Countdown detail error: {error}"
        )

        await interaction.followup.send(
            "MediaDB found an upcoming title, "
            "but couldn't load its countdown."
        )

        return

    await interaction.followup.send(
        embed=embed
    )


# =========================================================
# HOWLONGTOBEAT - DIRECT WEBSITE SEARCH
# =========================================================

HLTB_BASE_URL = "https://howlongtobeat.com"
HLTB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)

_hltb_search_endpoint = None
_hltb_search_user_id = None


def format_hltb_hours(value) -> str:
    if value is None:
        return "No data"

    try:
        hours = float(value)
    except (TypeError, ValueError):
        return "No data"

    if hours <= 0:
        return "No data"

    if hours.is_integer():
        return f"{int(hours)} hrs"

    return f"{hours:.1f} hrs"


def format_hltb_platforms(platforms) -> str:
    if not platforms:
        return "Platforms unavailable"

    if isinstance(platforms, str):
        platforms = [
            item.strip()
            for item in platforms.split(",")
            if item.strip()
        ]

    if isinstance(platforms, (list, tuple, set)):
        text = " \U00002022 ".join(
            str(platform)
            for platform in platforms
            if platform
        )
    else:
        text = str(platforms)

    return text or "Platforms unavailable"


def hltb_seconds_to_hours(value):
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return None

    if seconds <= 0:
        return None

    return seconds / 3600


def hltb_headers(auth: dict | None = None) -> dict:
    headers = {
        "User-Agent": HLTB_USER_AGENT,
        "Referer": HLTB_BASE_URL,
        "Origin": HLTB_BASE_URL,
        "Accept": "*/*",
    }

    if auth:
        headers["Content-Type"] = "application/json"

        if auth.get("token"):
            headers["x-auth-token"] = str(auth["token"])
        if auth.get("key"):
            headers["x-hp-key"] = str(auth["key"])
        if auth.get("value"):
            headers["x-hp-val"] = str(auth["value"])

    return headers


async def discover_hltb_search_info() -> tuple[str, str | None]:
    global _hltb_search_endpoint
    global _hltb_search_user_id

    if _hltb_search_endpoint:
        return _hltb_search_endpoint, _hltb_search_user_id

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(
                HLTB_BASE_URL,
                headers=hltb_headers()
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"HLTB homepage returned HTTP {response.status}"
                    )

                homepage = await response.text()

            script_paths = re.findall(
                r'<script[^>]+src=["\']([^"\']+)["\']',
                homepage,
                flags=re.IGNORECASE
            )

            for script_path in script_paths[:40]:
                script_url = (
                    script_path
                    if script_path.startswith("http")
                    else f"{HLTB_BASE_URL}{script_path}"
                )

                try:
                    async with session.get(
                        script_url,
                        headers=hltb_headers()
                    ) as script_response:
                        if script_response.status != 200:
                            continue

                        script = await script_response.text()
                except Exception:
                    continue

                endpoint_match = re.search(
                    r'fetch\s*\(\s*["\'](/api/[a-zA-Z0-9_/-]+)[^"\']*["\']\s*,\s*\{.*?method\s*:\s*["\']POST["\']',
                    script,
                    flags=re.IGNORECASE | re.DOTALL
                )

                if not endpoint_match:
                    continue

                endpoint = endpoint_match.group(1)
                parts = endpoint.strip("/").split("/")
                if len(parts) >= 2:
                    endpoint = f"/api/{parts[1]}"

                user_id_match = re.search(
                    r'users\s*:\s*\{\s*id\s*:\s*["\']([^"\']+)',
                    script
                )

                _hltb_search_endpoint = endpoint
                _hltb_search_user_id = (
                    user_id_match.group(1)
                    if user_id_match
                    else None
                )

                print(
                    f"HLTB search endpoint discovered: "
                    f"{_hltb_search_endpoint}"
                )

                return (
                    _hltb_search_endpoint,
                    _hltb_search_user_id
                )

        except Exception as error:
            print(f"HLTB endpoint discovery error: {error}")

    # Current known endpoint first, followed by recent historical names.
    _hltb_search_endpoint = "/api/bleed"
    _hltb_search_user_id = None

    return _hltb_search_endpoint, None


async def fetch_hltb_token(
    session: aiohttp.ClientSession,
    endpoint: str
) -> dict:

    clean_endpoint = endpoint.rstrip("/")

    async with session.get(
        f"{HLTB_BASE_URL}{clean_endpoint}/init",
        params={"t": int(time.time())},
        headers=hltb_headers()
    ) as response:
        body = await response.text()

        if response.status != 200:
            raise RuntimeError(
                f"HLTB token endpoint {clean_endpoint}/init "
                f"returned HTTP {response.status}: {body[:200]}"
            )

        try:
            data = await response.json(content_type=None)
        except Exception as error:
            raise RuntimeError(
                f"HLTB token response was not JSON: {body[:200]}"
            ) from error

    token = (
        data.get("token")
        or (data.get("data") or {}).get("token")
        or data.get("auth_token")
        or data.get("authToken")
    )

    auth_key = None
    auth_value = None

    for field_name, field_value in data.items():
        lower = str(field_name).lower()
        if "key" in lower:
            auth_key = field_value
        elif "val" in lower:
            auth_value = field_value

    if not token:
        raise RuntimeError("HLTB token response did not include a token.")

    return {
        "token": token,
        "key": auth_key,
        "value": auth_value,
    }


def build_hltb_payload(
    title: str,
    user_id: str | None,
    auth: dict
) -> dict:

    payload = {
        "searchType": "games",
        "searchTerms": title.split(),
        "searchPage": 1,
        "size": 20,
        "searchOptions": {
            "games": {
                "userId": 0,
                "platform": "",
                "sortCategory": "popular",
                "rangeCategory": "main",
                "rangeTime": {"min": 0, "max": 0},
                "gameplay": {
                    "perspective": "",
                    "flow": "",
                    "genre": "",
                    "difficulty": "",
                },
                "rangeYear": {"max": "", "min": ""},
                "modifier": "",
            },
            "users": {"sortCategory": "postcount"},
            "lists": {"sortCategory": "follows"},
            "filter": "",
            "sort": 0,
            "randomizer": 0,
        },
        "useCache": True,
    }

    if user_id:
        payload["searchOptions"]["users"]["id"] = user_id

    if auth.get("key") and auth.get("value"):
        payload[str(auth["key"])] = auth["value"]

    return payload


async def direct_hltb_search(title: str) -> list[dict]:
    endpoint, user_id = await discover_hltb_search_info()

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        auth = await fetch_hltb_token(
            session,
            endpoint
        )

        payload = build_hltb_payload(
            title,
            user_id,
            auth
        )

        async with session.post(
            f"{HLTB_BASE_URL}{endpoint}",
            headers=hltb_headers(auth),
            json=payload
        ) as response:
            body = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"HLTB search endpoint {endpoint} "
                    f"returned HTTP {response.status}: {body[:300]}"
                )

            try:
                data = await response.json(content_type=None)
            except Exception as error:
                raise RuntimeError(
                    f"HLTB search response was not JSON: {body[:300]}"
                ) from error

    results = []

    for item in data.get("data") or []:
        game_id = item.get("game_id")
        game_image = item.get("game_image")

        results.append({
            "game_id": game_id,
            "game_name": item.get("game_name") or "",
            "profile_platforms": item.get("profile_platform") or "",
            "main_story": hltb_seconds_to_hours(item.get("comp_main")),
            "main_extra": hltb_seconds_to_hours(item.get("comp_plus")),
            "completionist": hltb_seconds_to_hours(item.get("comp_100")),
            "game_image_url": (
                f"{HLTB_BASE_URL}/games/{game_image}"
                if game_image
                else None
            ),
            "game_web_link": (
                f"{HLTB_BASE_URL}/game/{game_id}"
                if game_id
                else None
            ),
        })

    return results


# =========================================================
# /HOWLONG
# =========================================================

@client.tree.command(
    name="howlong",
    description="Open a game's HowLongToBeat page."
)
@app_commands.describe(game="Game title to search for.")
async def howlong(interaction: discord.Interaction, game: str):
    await interaction.response.defer()

    try:
        results = await search_games(game)
    except Exception as error:
        print(f"IGDB /howlong title lookup error: {error}")
        await interaction.followup.send(
            "MediaDB couldn't resolve that game title right now."
        )
        return

    if not results:
        await interaction.followup.send(
            f"No relevant game result was found for **{game}**."
        )
        return

    resolved_game = results[0]
    resolved_title = resolved_game.get("name") or game

    hltb_url = (
        f"https://howlongtobeat.com/?q="
        f"{quote_plus(resolved_title)}"
    )

    platform_text = format_game_platforms(resolved_game)

    embed = discord.Embed(
        title=resolved_title,
        url=hltb_url,
        description=(
            f"\U0001f3ae **{platform_text}**\n\n"
            f"\U0001f517 **[View on HowLongToBeat]({hltb_url})**"
        ),
        color=discord.Color.from_rgb(40, 105, 150)
    )

    cover_url = igdb_cover_url(
        resolved_game,
        thumbnail=True
    )

    if cover_url:
        embed.set_thumbnail(url=cover_url)

    embed.set_footer(
        text="Game title provided by IGDB"
    )

    await interaction.followup.send(embed=embed)


client.run(DISCORD_TOKEN)
