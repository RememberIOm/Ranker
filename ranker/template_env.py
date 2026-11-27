from pathlib import Path

from fastapi.templating import Jinja2Templates

from ranker.schemas import CRITERION_COLORS

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
templates.env.globals["CRITERION_COLORS"] = tuple(CRITERION_COLORS)
