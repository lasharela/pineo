"""Settings from the environment. Values are read, never printed."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]          # pineo/
BAND_NAME = "PINEO_BAND_NUMBER"
DEFAULT_HUB = "http://127.0.0.1:8787"
DEFAULT_MINIMAX_URL = "https://api.minimax.io/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3.1-Flash"
MOTION_RATES = (10, 25, 50, 100, 208, 416)
# The only names taken from a .env file.
NAMES = ("DEEPGRAM_API_KEY", "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_MODEL", BAND_NAME, "PINEO_KIT_DIR",
         "PINEO_HUB_URL", "PINEO_MOTION_RATE", "PINEO_DG_ENDPOINTING", "PINEO_LLM_EFFORT", "PINEO_DG_KEYTERMS", "PINEO_AI_CLOSE")


class ConfigError(Exception):
    """One sentence for the person at the keyboard."""


def read_env_file(path) -> dict:
    found = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                name, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                if name.strip() in NAMES and value:
                    found[name.strip()] = value
    except (OSError, ValueError):
        pass
    return found


def load_env(environ=None, folders=None) -> None:
    """Fill in the names above from .env (the folder the program is started in, then the
    Pineo folder). A variable that is already set wins."""
    environ = os.environ if environ is None else environ
    folders = [Path.cwd(), REPO_ROOT] if folders is None else folders
    for folder in folders:
        for name, value in read_env_file(Path(folder) / ".env").items():
            if not environ.get(name, "").strip():
                environ[name] = value


def band_number(environ=None) -> int:
    """The team's band number, from PINEO_BAND_NUMBER and nowhere else."""
    environ = os.environ if environ is None else environ
    text = environ.get(BAND_NAME, "").strip()
    if not text:
        raise ConfigError("Set %s to the number written on your band's bag (in the environment or in .env); "
                          "Pineo never guesses a band number." % BAND_NAME)
    if not (text.isascii() and text.isdigit() and len(text) <= 3 and 1 <= int(text) <= 999):
        raise ConfigError("%s must be a band number from 1 to 999." % BAND_NAME)
    return int(text)


def kit_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("PINEO_KIT_DIR", "").strip() or REPO_ROOT / "vexo-open-kit")


def hub_url(environ=None, given=None) -> str:
    environ = os.environ if environ is None else environ
    url = (given or environ.get("PINEO_HUB_URL", "").strip() or DEFAULT_HUB).rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("The hub address must start with http:// or https://.")
    return url


def motion_rate(environ=None, given=None) -> int:
    """Gyro samples a second while the microphone is on. The kit's docs (api.md, "Fast
    motion": "With audio on, stay at 208 with both sensors or 416 with one") give the
    ceiling over Bluetooth. The default is 50: the calibrated detector gives the same result on the
    real recording cut to 50 a second, and headroom on the radio link matters more than resolution."""
    environ = os.environ if environ is None else environ
    text = str(given if given is not None else environ.get("PINEO_MOTION_RATE", "").strip() or 50)
    if not text.isdigit() or int(text) not in MOTION_RATES:
        raise ConfigError("The motion rate must be one of %s (samples a second)." % ", ".join(map(str, MOTION_RATES)))
    return int(text)


def endpointing(environ=None) -> int:
    environ = os.environ if environ is None else environ
    # 100 ms, as in the kit: Deepgram hands over a piece at every short pause, so finals come fast; the
    # pieces of one sentence are put together on our side (voice.py), not by waiting inside Deepgram.
    text = environ.get("PINEO_DG_ENDPOINTING", "").strip() or "100"
    if not (text.isdigit() and 10 <= int(text) <= 5000):
        raise ConfigError("PINEO_DG_ENDPOINTING must be a number of milliseconds from 10 to 5000.")
    return int(text)


def minimax(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    key = environ.get("MINIMAX_API_KEY", "").strip()
    base = (environ.get("MINIMAX_BASE_URL", "").strip() or DEFAULT_MINIMAX_URL).rstrip("/")
    if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
        raise ConfigError("MINIMAX_BASE_URL must start with https://")
    # M3.1-Flash always thinks (it refuses thinking off); "low" keeps that short. PINEO_LLM_EFFORT=none sends nothing.
    effort = environ.get("PINEO_LLM_EFFORT", "").strip().lower() or "low"
    if effort not in ("low", "medium", "high", "none"):
        raise ConfigError("PINEO_LLM_EFFORT must be low, medium, high or none.")
    return {"key": key, "base_url": base, "model": environ.get("MINIMAX_MODEL", "").strip() or DEFAULT_MINIMAX_MODEL,
            "extra": {} if effort == "none" else {"reasoning_effort": effort}}
