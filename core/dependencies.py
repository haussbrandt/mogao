from integrations.anki import Postprocessor
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
postprocessor = Postprocessor()
